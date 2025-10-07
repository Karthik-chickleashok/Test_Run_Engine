# =====================================================================
# TRE_online.py — Online Runner / Engine  •  Version 2.2.0u
# Test Run Engine (TRE)
#
# Highlights:
# - Robust Stop: non-blocking recv (short socket timeout) + shutdown()
# - Connect retries with backoff
# - Settle phase collects boot lines (no flush on first connect)
# - Ring buffer + prefeed (wait_capture) support
# - Sequential-by-default execution (one step at a time)
# - Per-step timeout: default 3s (Step#1 = 120s unless overridden)
# - Step types: find, not_find, sequence, action(tap/tap_pct/screenshot/wait/wait_capture)
# - Emits RAW matched line to UI (UI extracts payload via config)
# - Android via TRE_android (preferred); will gracefully report if unavailable
# =====================================================================

# === CHUNK 1/7 — Imports, Tunables, Utilities ========================

from __future__ import annotations

import os
import re
import socket
import time
import datetime
import traceback
import subprocess
import collections
from typing import Dict, Any, List, Optional, Callable, Tuple, Deque

# offline/helpers
import TRE_json as tre
try:
    import TRE_android as droid
    ANDROID_OK = True
except Exception:
    ANDROID_OK = False
WIRE_TAP_FILE = os.path.join(os.path.dirname(__file__), "..", "Logs", "wire_tap.log")
wire_fh = open(WIRE_TAP_FILE, "a", encoding="utf-8", errors="ignore")


# ---- Tunables ----
DEBUG_MATCH = False
CONNECT_TIMEOUT_SEC      = 5.0
RECV_BLOCK_BYTES         = 65536

RETRY_MAX_ATTEMPTS       = 5
RETRY_BACKOFF_BASE_S     = 0.5       # 0.5, 1, 2, 4, 8

SETTLE_DELAY_S           = 3.0       # collect boot lines before enabling matching
RING_BUFFER_MAX_LINES    = 10000

STEP1_TIMEOUT_S          = 120.0     # first step often needs larger boot window
DEFAULT_STEP_TIMEOUT_S   = 3.0       # all other steps default

IDLE_TICK_S              = 0.20      # engine loop cadence
WAIT_DRAIN_TICK_S        = 0.05      # drain cadence during waits (avoid backlog)

ROOT_DIR   = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
REPORTS_DIR= os.path.join(ROOT_DIR, "Reports")
os.makedirs(REPORTS_DIR, exist_ok=True)


def _now_s() -> float:
    return time.monotonic()


class LineRing:
    """Fixed-size ring buffer for text lines."""
    def __init__(self, max_lines: int = RING_BUFFER_MAX_LINES):
        self._dq: Deque[str] = collections.deque(maxlen=max_lines)

    def append(self, line: str) -> None:
        self._dq.append(line)

    def extend(self, lines: List[str]) -> None:
        self._dq.extend(lines)

    def drain(self) -> List[str]:
        out = list(self._dq)
        self._dq.clear()
        return out

    def __len__(self) -> int:
        return len(self._dq)


# === CHUNK 2/7 — Android compatibility (optional) ====================

class _AdbDirect:
    """
    Minimal direct ADB (adbb) fallback for actions IF TRE_android is missing
    or you prefer the tiny dependency. All subprocess calls are time-bounded.
    """
    def __init__(self, exe: str = "adbb"):
        self.exe = exe

    def tap(self, x: int, y: int) -> bool:
        # Try short 't x y' (your adbb shortcut); else fallback to shell input tap
        try:
            r = subprocess.run([self.exe, "t", str(x), str(y)],
                               timeout=1.5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return True
        except Exception:
            pass
        try:
            r = subprocess.run([self.exe, "shell", "input", "tap", str(x), str(y)],
                               timeout=1.5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return (r.returncode == 0)
        except Exception:
            return False

    def screencap_png_to(self, out_file: str) -> bool:
        try:
            with open(out_file, "wb") as f:
                r = subprocess.run([self.exe, "exec-out", "screencap", "-p"],
                                   timeout=3.0, stdout=f, stderr=subprocess.DEVNULL)
            return (r.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 0)
        except Exception:
            return False


# === CHUNK 3/7 — SessionManager: lifecycle, socket, drain =============

class SessionManager:
    """
    Online session engine:
      - Connect to DLT TCP stream with retries
      - Settle phase: collect boot lines (kept in ring buffer)
      - Prefeed support from wait_capture to the next step
      - Sequential step execution with per-step timeouts
      - Emits updates via callbacks to UI:

        on_status(msg: str)
        on_steps_init([{ idx, name, vc }])
        on_step_update(idx: int, name: str, vc: str, result: "PASS|FAIL|ERROR", line: Optional[str])

    Run model: call start() from a worker thread (it is blocking).
    Stop model: stop() is asynchronous — it flips flags and shutdowns the socket.
    """
    # ---- Construction ----
    def __init__(self,
                 host: str,
                 port: int,
                 tests: List[Dict[str, Any]],
                 out_dir: str,
                 snapshot_interval: int = 10,
                 on_status: Optional[Callable[[str], None]] = None,
                 on_steps_init: Optional[Callable[[List[Dict[str, Any]]], None]] = None,
                 on_step_update: Optional[Callable[[int, str, str, str, Optional[str]], None]] = None):
        self.host = host
        self.port = int(port)
        self.tests = [dict(t) for t in (tests or [])]
        self.out_dir = out_dir or REPORTS_DIR
        self.snapshot_interval = max(2, int(snapshot_interval or 10))

        self.on_status = on_status or (lambda m: None)
        self.on_steps_init = on_steps_init or (lambda lst: None)
        self.on_step_update = on_step_update or (lambda i,n,v,r,l: None)

        # connection + control
        self._sock: Optional[socket.socket] = None
        self._running = False
        self._paused = False

        # assemble + buffers
        self._buf = ""                      # partial line assembly
        self._ring = LineRing()             # pre-start boot lines
        self._matching_enabled = False

        # prefeed from wait_capture
        self._prefeed_lines: List[str] = []

        # step timing
        self._current_step_idx: Optional[int] = None
        self._current_step_start_s: float = 0.0

        # device cache for tap_pct
        self._dev_w = 0
        self._dev_h = 0

        # adb fallback
        self._adb = _AdbDirect("adbb")

    # ---- Lifecycle ----
    def pause(self):
        self._paused = True
        self.on_status("Paused.")

    def resume(self):
        self._paused = False
        self.on_status("Resumed.")

    def stop(self):
        """Immediate stop: flip flags, shutdown+close socket to break any recv()."""
        self._running = False
        self._paused  = False
        # nuke buffers so a new run won't instantly PASS on stale lines
        self._buf = ""
        try:
            self._ring.drain()
        except Exception:
            pass
        self._prefeed_lines.clear()
        try:
            if self._sock:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)  # interrupts recv on the engine thread
                except Exception:
                    pass
                try:
                    self._sock.close()
                except Exception:
                    pass
        finally:
            self._sock = None


    # === CHUNK 4/7 — Start: connect, settle, main loop =================

    def start(self):
        """Blocking; run in a worker thread. CUMULATIVE verify matching."""
        # reset per-run state
        for t in self.tests:
            t.pop("_done", None)
            t.pop("_seq_idx", None)
        self._buf = ""
        self._ring = LineRing()
        self._prefeed_lines = []
        self._matching_enabled = False
        self._current_step_idx = None
        self._current_step_start_s = 0.0

        # connect (with backoff)
        s = self._connect_with_retries()
        if not s:
            self.on_status("Failed to connect.")
            return
        self._sock = s
        self._running = True
        self.on_status("Connected.")

        # initialize UI step table
        # --- Patch A: stamp per-step start time for cumulative timeouts ---
        now = time.monotonic()
        for t in self.tests:
            t["_done"] = False
            t["_seq_idx"] = 0
            t["_t0"] = now   # used by cumulative timeout checker
        # --- end Patch A ---

        steps_info = []
        for idx, t in enumerate(self.tests, start=1):
            steps_info.append({"idx": idx, "name": t.get("name", f"Step {idx}"), "vc": self._describe_vc(t)})
        try:
            self.on_steps_init(steps_info)
        except Exception:
            pass

        # settle phase: capture boot lines into ring (no matching yet)
        settle_until = _now_s() + SETTLE_DELAY_S
        self.on_status(f"Settling for {SETTLE_DELAY_S:.1f}s to capture boot logs…")
        try:
            while self._running and _now_s() < settle_until:
                if self._paused:
                    self._drain_to_ring()
                    # --- Patch B1: enforce per-step timeouts + auto-finish ---
                    self._check_timeouts_cumulative()
                    # If everything finished, stop the loop gracefully
                    if all(t.get("_done") for t in self.tests):
                        self.on_status("All steps completed.")
                        break
                    # --- end Patch B1 ---

                    time.sleep(IDLE_TICK_S)
                    continue
                self._drain_to_ring()
                time.sleep(IDLE_TICK_S)
        except Exception as e:
            self.on_status(f"Settle error: {e}\n{traceback.format_exc()}")

        # CUMULATIVE MODE: do not start timeouts until the first line arrives.
        self._t0 = None  # set to _now_s() on first seen/pre-fed line

        # mark all verification steps as RUNNING immediately (visual)
        for i, t in enumerate(self.tests, start=1):
            if "action" not in t:
                name = t.get("name", f"Step {i}")
                vc   = self._describe_vc(t)
                self._emit(i, name, vc, "RUNNING", "")

        # execute any leading actions right away (sequential)
        self._action_ptr = 1
        self._run_pending_actions()  # executes actions at/after start until next non-action

        # feed pre-start lines to cumulative matcher before live matching
        pre_lines = self._ring.drain()
        self.on_status(f"Feeding {len(pre_lines)} buffered lines…")
        if pre_lines and self._t0 is None:
            self._t0 = _now_s()   # start the timeout clock when we actually have lines
        for ln in pre_lines:
            if not self._running:
                break
            self._process_line_cumulative(ln)

        # main live loop (matching enabled)
        self._matching_enabled = True
        self.on_status("Live matching started.")
        try:
            while self._running:
                # prefeed lines from wait_capture (rare)
                while self._prefeed_lines and self._running and not self._paused:
                    ln = self._prefeed_lines.pop(0)
                    self._process_line_cumulative(ln)

                # recv (short-timeout socket)
                text = self._recv_block()
                if text is None:
                    break
                if text:
                    self._buf += text
                    while True:
                        p = self._buf.find("\n")
                        if p < 0:
                            break
                        line = self._buf[:p]
                        self._buf = self._buf[p + 1:]
                        if self._paused:
                            continue
                        if not self._matching_enabled:
                            self._ring.append(line.rstrip("\r"))
                            continue
                        self._process_line_cumulative(line.rstrip("\r"))

                # timeouts for all unfinished verification steps
                self._check_timeouts_cumulative()

                # run next actions (sequential), if any
                self._run_pending_actions()

                # done?
                if self._all_done_cumulative():
                    self._running = False

                time.sleep(IDLE_TICK_S)

            if self._running:
                self.on_status("Stream closed.")
        except Exception as e:
            self.on_status(f"Runner error: {e}\n{traceback.format_exc()}")
        finally:
            # finalize pending steps before closing socket
            try:
                self._finalize_unfinished("stopped" if not self._running else "ended")
            except Exception:
                pass
            try:
                if self._sock:
                    try: self._sock.shutdown(socket.SHUT_RDWR)
                    except Exception: pass
            finally:
                try:
                    if self._sock: self._sock.close()
                except Exception:
                    pass
                self._sock = None
                self._running = False
                self.on_status("Stopped.")



# === CHUNK 5/7 — CUMULATIVE verify matching + sequential actions ======

    # ---- Identify unfinished items ---------------------------------
    def _unfinished_indices(self) -> List[int]:
        return [i for i,t in enumerate(self.tests, start=1) if not t.get("_done")]

    def _all_done_cumulative(self) -> bool:
        for t in self.tests:
            if not t.get("_done"):
                return False
        return True

    # ---- Timeout resolver (supports timeout_ms / timeout / no_timeout)
    def _current_timeout_s(self, idx: int, t: Dict[str, Any]) -> float:
        if t.get("no_timeout") is True:
            return 0.0
        if isinstance(t.get("timeout_ms"), (int, float)) and t["timeout_ms"] >= 0:
            return float(t["timeout_ms"]) / 1000.0
        if isinstance(t.get("timeout"), (int, float)) and t["timeout"] >= 0:
            return float(t["timeout"])
        return STEP1_TIMEOUT_S if idx == 1 else DEFAULT_STEP_TIMEOUT_S

    # ---- Actions run strictly in file order -------------------------
    def _run_pending_actions(self) -> None:
        # execute actions sequentially starting at _action_ptr
        while self._running and self._action_ptr <= len(self.tests):
            i = self._action_ptr
            t = self.tests[i - 1]
            if t.get("_done"):
                self._action_ptr += 1
                continue
            if "action" not in t:
                break  # stop at first verification step; actions only run in sequence
            name = t.get("name", f"Step {i}")
            vc   = self._describe_vc(t)
            ok, msg = self._perform_action(t["action"])
            t["_done"] = True
            self._emit(i, name, vc, ("PASS" if ok else "FAIL"), msg)
            self._action_ptr += 1
        # nothing to return

    # ---- Per-line cumulative processing for all verification steps ---
    def _process_line_cumulative(self, line: str) -> None:
        """
        Cumulative processing: check this incoming line against all unfinished steps.
        Stabilizers:
          - Always match WHOLE line (payload_only=False) to mirror offline behavior.
          - (Wire-tap, buffering handled elsewhere)
        """
        try:
            # 1) fast exit if everything is done
            if all(t.get("_done") for t in self.tests):
                return

            # 2) iterate through unfinished steps
            for idx0, t in enumerate(self.tests):
                if t.get("_done"):
                    continue

                idx = idx0 + 1
                name = t.get("name", f"Step {idx}")
                vc   = self._describe_vc(t)

                # ---- ACTION steps execute immediately on first opportunity ----
                if "action" in t:
                    ok, msg = self._perform_action(t["action"])
                    t["_done"] = True
                    self._emit(idx, name, vc, ("PASS" if ok else "FAIL"), msg)
                    continue

                # ---- FIND ----
                if "find" in t:
                    cfg = t["find"] or {}
                    cfg2 = dict(cfg); cfg2.setdefault("payload_only", False)  # force full-line
                    if tre.line_matches(line, cfg2):
                        t["_done"] = True
                        self._emit(idx, name, vc, "PASS", line)
                    continue

                # ---- NOT_FIND (fail immediately if forbidden appears) ----
                if "not_find" in t:
                    cfg = t["not_find"] or {}
                    cfg2 = dict(cfg); cfg2.setdefault("payload_only", False)  # force full-line
                    if tre.line_matches(line, cfg2):
                        t["_done"] = True
                        self._emit(idx, name, vc, "FAIL", line)
                    continue

                # ---- SEQUENCE: progress left-to-right ----
                if "sequence" in t:
                    seq = t.get("sequence", []) or []
                    if not seq:
                        t["_done"] = True
                        self._emit(idx, name, vc, "PASS", "")
                        continue

                    prog = t.setdefault("_seq_idx", 0)
                    node = seq[prog]
                    cfg  = node if isinstance(node, dict) else {"pattern": str(node), "literal": True}
                    cfg2 = dict(cfg); cfg2.setdefault("payload_only", False)  # force full-line

                    if tre.line_matches(line, cfg2):
                        t["_seq_idx"] = prog + 1
                        if t["_seq_idx"] >= len(seq):
                            t["_done"] = True
                            self._emit(idx, name, vc, "PASS", line)
                    continue

                # ---- Unknown rule type ----
                t["_done"] = True
                self._emit(idx, name, vc, "ERROR", "Unknown rule type")

        except Exception as e:
            # if anything blows up, mark the currently-evaluated step as error where possible
            try:
                self._emit(idx, name, vc, "ERROR", f"{type(e).__name__}: {e}")
            except Exception:
                pass

    def _check_timeouts_cumulative(self) -> None:
        """
        Cumulative mode timeouts:
          - Step 1: STEP1_TIMEOUT_S unless overridden by "timeout"
          - Others: DEFAULT_STEP_TIMEOUT_S unless overridden by "timeout"
          - FIND/SEQUENCE => FAIL on timeout
          - NOT_FIND      => PASS on timeout
          - ACTION        => FAIL on timeout
        """
        if not self._running:
            return

        now = _now_s()

        def _timeout_for(idx: int, t: dict) -> float:
            if isinstance(t.get("timeout"), (int, float)) and t["timeout"] >= 0:
                return float(t["timeout"])
            return STEP1_TIMEOUT_S if idx == 1 else DEFAULT_STEP_TIMEOUT_S

        for idx, t in enumerate(self.tests, start=1):
            if t.get("_done"):
                continue

            # ensure per-step timer exists
            t0 = t.get("_t0")
            if not t0:
                t0 = now
                t["_t0"] = t0

            tmo = _timeout_for(idx, t)
            if tmo <= 0 or (now - t0) < tmo:
                continue

            name = t.get("name", f"Step {idx}")
            vc   = self._describe_vc(t)

            if "not_find" in t:
                t["_done"] = True
                self._emit(idx, name, vc, "PASS", f"[timeout {tmo:.0f}s: pattern not seen]")
            elif "action" in t:
                t["_done"] = True
                self._emit(idx, name, vc, "FAIL", f"[timeout {tmo:.0f}s on action]")
            elif "find" in t or "sequence" in t:
                t["_done"] = True
                self._emit(idx, name, vc, "FAIL", f"[timeout {tmo:.0f}s]")
            else:
                t["_done"] = True
                self._emit(idx, name, vc, "ERROR", f"[timeout {tmo:.0f}s] unknown rule")


# === CHUNK 5d — finalize any still-pending steps ======================

    def _finalize_unfinished(self, reason: str = "stopped") -> None:
        """
        When the run ends (Stop pressed, stream closed), mark any steps that
        are still pending:
          - not_find  -> PASS (forbidden did not appear during the run)
          - find/seq  -> FAIL (never observed)
        """
        for i, t in enumerate(self.tests, start=1):
            if t.get("_done"):
                continue
            if "action" in t:
                # actions should not remain pending; mark as FAIL for visibility
                name = t.get("name", f"Step {i}")
                vc   = self._describe_vc(t)
                t["_done"] = True
                self._emit(i, name, vc, "FAIL", f"[{reason}: action not executed]")
                continue

            name = t.get("name", f"Step {i}")
            vc   = self._describe_vc(t)

            if "not_find" in t:
                t["_done"] = True
                self._emit(i, name, vc, "PASS", f"[{reason}: pattern not seen]")
            else:
                # find or sequence
                t["_done"] = True
                self._emit(i, name, vc, "FAIL", f"[{reason}: pattern never seen]")


# === CHUNK 5c — Output helpers =============================

    def _emit(self, idx: int, name: str, vc: str, result: str, line: Optional[str]):
        """Send step update to UI (index, name, VC, result, line)."""
        try:
            if self.on_step_update:
                self.on_step_update(idx, name, vc, result, line)
        except Exception:
            # never let UI error kill runner
            pass

# --- TRE_online.py : CHUNK 5b — VC descriptor (place INSIDE class SessionManager) ---

    def _describe_vc(self, t: Dict[str, Any]) -> str:
        """Human-friendly VC text for the UI table."""
        if "find" in t:
            return str(t["find"].get("pattern", ""))
        if "not_find" in t:
            return f"NOT {t['not_find'].get('pattern','')}"
        if "sequence" in t:
            seq = t.get("sequence", [])
            def pat(n):
                return n if isinstance(n, str) else str(n.get("pattern", ""))
            return " | ".join(pat(n) for n in seq)  # using ANY-of semantics now
        if "action" in t:
            a = t.get("action", {})
            at = str(a.get("type", "action")).lower()
            if at == "screenshot":
                fn = a.get("file")
                return f"[screenshot]{' → ' + fn if fn else ''}"
            if at in ("wait", "wait_capture"):
                ms = a.get("ms", "")
                return f"[{at} {ms}ms]"
            if at in ("tap", "tap_pct"):
                return f"[{at}]"
            return f"[{at}]"
        return "(unknown rule)"
# --- end CHUNK 5b ---



# === CHUNK 6/7 — Actions (wait, wait_capture, screenshot, taps) =======

    def _perform_action(self, action: Dict[str, Any]) -> Tuple[bool, str]:
        """
        Supported:
          {"type":"wait","ms":1000}
          {"type":"wait_capture","ms":2000}
          {"type":"screenshot","file":"name.png"}  # file optional => auto-name
          {"type":"tap","x":123,"y":456}
          {"type":"tap_pct","px":0.50,"py":0.75}   # uses device size
        """
        at = str((action or {}).get("type", "")).lower()

        # --- WAIT: drain/discard to avoid backlog ---
        if at == "wait":
            ms = int(action.get("ms", 0))
            end_t = _now_s() + (ms / 1000.0)
            while self._running and _now_s() < end_t:
                self._drain_discard_once()
                time.sleep(WAIT_DRAIN_TICK_S)
            return True, f"wait {ms}ms"

        # --- WAIT_CAPTURE: buffer lines for next step (prefeed) ---
        if at == "wait_capture":
            ms = int(action.get("ms", 0))
            end_t = _now_s() + (ms / 1000.0)
            captured: List[str] = []
            while self._running and _now_s() < end_t:
                captured.extend(self._drain_collect_once())
                time.sleep(WAIT_DRAIN_TICK_S)
            if captured:
                # prepend so they’re processed first
                self._prefeed_lines = captured + self._prefeed_lines
            return True, f"wait_capture {ms}ms ({len(captured)} lines)"

        # --- SCREENSHOT: save full frame to Reports/Screenshots ---
        if at == "screenshot":
            shots_dir = os.path.join(self.out_dir, "Screenshots")
            os.makedirs(shots_dir, exist_ok=True)
            fn = action.get("file")
            if not fn:
                ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                fn = f"shot_{ts}.png"
            fn = os.path.basename(str(fn))
            out_path = os.path.join(shots_dir, fn)

            if ANDROID_OK:
                try:
                    droid.ensure_server()
                    ser = droid.get_default_device_serial()
                    if not ser:
                        return False, "No Android device"
                    ok = droid.screencap_png_to(out_path, serial=ser) \
                         if hasattr(droid, "screencap_png_to") else _compat_screencap_to(out_path)
                    if not ok or not os.path.exists(out_path):
                        return False, f"screenshot failed: {out_path}"
                    return True, f"{out_path}"
                except Exception as e:
                    return False, f"screenshot error: {e}"
            else:
                ok = self._adb.screencap_png_to(out_path)
                return (ok, out_path if ok else "screenshot failed")

        # --- TAP absolute ---
        if at == "tap":
            x = int(action.get("x", -1)); y = int(action.get("y", -1))
            if x < 0 or y < 0:
                return False, "tap needs x/y"
            if ANDROID_OK:
                try:
                    droid.ensure_server()
                    ser = droid.get_default_device_serial()
                    if not ser: return False, "No Android device"
                    ok = droid.input_tap(x, y, serial=ser)
                    return ok, f"tap({x},{y}) {'ok' if ok else 'fail'}"
                except Exception as e:
                    return False, f"tap error: {e}"
            else:
                ok = self._adb.tap(x, y)
                return ok, f"tap({x},{y}) {'ok' if ok else 'fail'}"

        # --- TAP percentage ---
        if at == "tap_pct":
            px = float(action.get("px", -1))
            py = float(action.get("py", -1))
            if not (0 <= px <= 1 and 0 <= py <= 1):
                return False, "tap_pct needs px/py in [0..1]"
            try:
                if ANDROID_OK:
                    droid.ensure_server()
                    ser = droid.get_default_device_serial()
                    if not ser: return False, "No Android device"
                    if self._dev_w <= 0 or self._dev_h <= 0:
                        wh = droid.device_wm_size(serial=ser)
                        if wh: self._dev_w, self._dev_h = wh
                    if self._dev_w <= 0 or self._dev_h <= 0:
                        return False, "unknown device size"
                    x = int(round(px * self._dev_w)); y = int(round(py * self._dev_h))
                    ok = droid.input_tap(x, y, serial=ser)
                    return ok, f"tap_pct({px:.2f},{py:.2f})=>({x},{y}) {'ok' if ok else 'fail'}"
                else:
                    # fallback w/o device size: try screen-res via wm size
                    w,h = self._guess_wm_size()
                    if w <= 0 or h <= 0:
                        return False, "unknown device size"
                    x = int(round(px * w)); y = int(round(py * h))
                    ok = self._adb.tap(x, y)
                    return ok, f"tap_pct({px:.2f},{py:.2f})=>({x},{y}) {'ok' if ok else 'fail'}"
            except Exception as e:
                return False, f"tap_pct error: {e}"

        return False, f"unknown action: {at}"

    # ---- Wait drain helpers ----
    def _drain_discard_once(self) -> None:
        if not self._sock:
            return
        try:
            self._sock.setblocking(False)
            while True:
                try:
                    data = self._sock.recv(RECV_BLOCK_BYTES)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break
                if not data:
                    break
                # discard (do not append)
        finally:
            try: self._sock.setblocking(True)
            except Exception: pass

    def _drain_collect_once(self) -> List[str]:
        out: List[str] = []
        if not self._sock:
            return out
        local = ""
        try:
            self._sock.setblocking(False)
            while True:
                try:
                    data = self._sock.recv(RECV_BLOCK_BYTES)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break
                if not data:
                    break
                local += data.decode("utf-8", errors="ignore")
                while True:
                    p = local.find("\n")
                    if p < 0:
                        break
                    line = local[:p]
                    local = local[p+1:]
                    out.append(line.rstrip("\r"))
            if local:
                self._buf += local
        finally:
            try: self._sock.setblocking(True)
            except Exception: pass
        return out

    def _guess_wm_size(self) -> Tuple[int,int]:
        """Try to read device resolution via adbb shell wm size (fallback)."""
        try:
            out = subprocess.check_output(["adbb", "shell", "wm", "size"],
                                          stderr=subprocess.STDOUT, timeout=1.5).decode("utf-8","ignore")
            for line in out.splitlines():
                if ":" in line and "x" in line:
                    tail = line.split(":",1)[1].strip()
                    w,h = tail.split("x")
                    return (int(w), int(h))
        except Exception:
            pass
        return (0,0)


    # -- TCP connect with retries/backoff --
    def _connect_with_retries(self) -> Optional[socket.socket]:
        last_err = None
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            try:
                self.on_status(f"Connecting to {self.host}:{self.port} (attempt {attempt}/{RETRY_MAX_ATTEMPTS})…")
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(CONNECT_TIMEOUT_SEC)
                s.connect((self.host, self.port))
                s.settimeout(None)  # back to blocking; we manage cadence
                self._first_connect_done = True
                return s
            except Exception as e:
                last_err = e
                delay = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
                self.on_status(f"Connect failed: {e} — retrying in {delay:.1f}s")
                time.sleep(delay)
        self.on_status(f"Connect failed after {RETRY_MAX_ATTEMPTS} attempts: {last_err}")
        return None

    # -- Blocking-ish read: returns text, "" (no data yet), or None (closed/error) --
    def _recv_block(self) -> Optional[str]:
        if not self._sock:
            return None
        try:
            data = self._sock.recv(RECV_BLOCK_BYTES)
            if not data:
                return None
            return data.decode("utf-8", errors="ignore")
        except (BlockingIOError, InterruptedError):
            return ""
        except OSError:
            # happens when stop() shutdowns the socket
            return None
        except Exception:
            raise

    # -- Drain whatever is available quickly into the pre-start ring buffer --
    def _drain_to_ring(self) -> None:
        """Read everything available quickly and append to pre-match ring buffer."""
        if not self._sock:
            return
        self._sock.setblocking(False)
        try:
            while True:
                try:
                    data = self._sock.recv(RECV_BLOCK_BYTES)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    # socket closed while draining
                    break
                if not data:
                    break
                self._buf += data.decode("utf-8", errors="ignore")
                while True:
                    p = self._buf.find("\n")
                    if p < 0:
                        break
                    line = self._buf[:p]
                    self._buf = self._buf[p + 1:]
                    self._ring.append(line.rstrip("\r"))
        finally:
            try:
                self._sock.setblocking(True)
            except Exception:
                pass


# === CHUNK 7/7 — Android compat helpers (legacy TRE_android) ==========

def _compat_screencap_to(out_path: str) -> bool:
    """
    Fallback for older TRE_android versions that expose screencap_png(tmp_dir).
    """
    try:
        tmp_dir = os.path.dirname(out_path) or "."
        ser = droid.get_default_device_serial()
        tmp = droid.screencap_png(tmp_dir, prefix="__tmp__", serial=ser)
        if not tmp or not os.path.exists(tmp):
            return False
        import shutil
        try:
            shutil.move(tmp, out_path)
        except Exception:
            with open(tmp, "rb") as src, open(out_path, "wb") as dst:
                dst.write(src.read())
            try: os.remove(tmp)
            except Exception: pass
        return True
    except Exception:
        return False

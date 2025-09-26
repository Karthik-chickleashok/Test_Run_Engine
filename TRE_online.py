# TRE_online.py
# Online runner for Test Run Engine (TRE)
#
# Features:
# - Connect retries with exponential backoff
# - No flush on first connect (preserve boot logs)
# - Ring buffer + settle delay; feed buffer before live matching
# - Sequential-by-default step execution
# - Stop is immediate (shutdown socket)
# - Per-step timeout: default 3s, Step 1 special 120s; timeout semantics:
#     * find / sequence  => FAIL on timeout
#     * not_find         => PASS on timeout
# - Actions:
#     * tap, tap_pct (Android)
#     * screenshot (direct save + verify)
#     * wait (discard logs during wait)
#     * wait_capture (buffer logs during wait and feed to next step)
# - Emits RAW matched line to UI (UI is responsible for payload-only rendering)

from __future__ import annotations
import os, socket, datetime, traceback, time, collections
from typing import Dict, Any, List, Optional, Callable, Tuple, Deque

import TRE_json as tre
try:
    import TRE_android as droid
    ANDROID_OK = True
except Exception:
    ANDROID_OK = False


# ---------------- Tunables ----------------
CONNECT_TIMEOUT_SEC      = 5.0
RECV_BLOCK_BYTES         = 65536

RETRY_MAX_ATTEMPTS       = 5
RETRY_BACKOFF_BASE_S     = 0.5     # 0.5, 1, 2, 4, 8 (per attempt)

SETTLE_DELAY_S           = 3.0     # collect boot lines before matching
RING_BUFFER_MAX_LINES    = 10000

# Timeouts (finalized):
STEP1_TIMEOUT_S          = 120.0   # step #1 (boot window)
DEFAULT_STEP_TIMEOUT_S   = 3.0     # all other steps

IDLE_TICK_S              = 0.20    # loop cadence when idle/pacing
WAIT_DRAIN_TICK_S        = 0.05    # socket drain cadence during waits


# ---------------- Utilities ----------------
class LineRing:
    """Fixed-size ring buffer for text lines."""
    def __init__(self, max_lines: int = 10000):
        self.max_lines = max_lines
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


def _now_s() -> float:
    return time.monotonic()
    
# ---------------- Adb ----------------
class Adb:
    def __init__(self, exe: str = "adbb"):
        self.exe = exe

    def tap(self, x: int, y: int) -> bool:
        try:
            # prefer short, bounded call
            r = subprocess.run([self.exe, "t", str(x), str(y)], timeout=1.5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            if r.returncode == 0:
                return True
        except Exception:
            pass
        # fallback to standard shell tap
        try:
            r = subprocess.run([self.exe, "shell", "input", "tap", str(x), str(y)], timeout=1.5,
                               stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            return (r.returncode == 0)
        except Exception:
            return False

    def screencap_png_to(self, out_file: str) -> bool:
        try:
            with open(out_file, "wb") as f:
                r = subprocess.run([self.exe, "exec-out", "screencap", "-p"], timeout=3.0,
                                   stdout=f, stderr=subprocess.DEVNULL)
            return (r.returncode == 0 and os.path.exists(out_file) and os.path.getsize(out_file) > 0)
        except Exception:
            return False


# ---------------- Session Manager ----------------
class SessionManager:
    """
    Online session manager:
      - Connects to DLT TCP stream with retries
      - Preserves boot logs (no flush on first connect)
      - Ring buffer during settle; feeds pre-start lines
      - Sequential matching, per-step timeout
      - wait / wait_capture actions manage mid-test delays safely
    """
    def is_alive(self) -> bool:
        th = self._worker
        return bool(th and th.is_alive())

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
        self.port = port
        self.tests = [dict(t) for t in (tests or [])]
        self.out_dir = out_dir
        self.snapshot_interval = max(2, int(snapshot_interval or 10))

        self.on_status = on_status or (lambda m: None)
        self.on_steps_init = on_steps_init or (lambda lst: None)
        self.on_step_update = on_step_update or (lambda i,n,v,r,l: None)

        self._sock: Optional[socket.socket] = None
        self._running = False
        self._paused = False

        self._dev_w = 0
        self._dev_h = 0

        # text assembly + buffers
        self._buf = ""                               # partial line buffer
        self._ring = LineRing(RING_BUFFER_MAX_LINES) # pre-start buffer
        self._matching_enabled = False
        self._first_connect_done = False

        # prefeed lines (from wait_capture) to be fed to next step
        self._prefeed_lines: List[str] = []

        # step timing
        self._current_step_idx: Optional[int] = None
        self._current_step_start_s: float = 0.0

    # -------- lifecycle --------
    def start(self):
        # reset step state
        for t in self.tests:
            t.pop("_done", None)
            t.pop("_seq_idx", None)

        # connect with retries
        self._sock = self._connect_with_retries()
        if not self._sock:
            self.on_status("Failed to connect.")
            return
        self.on_status("Connected.")

        # initialize step table
        steps_info = []
        for idx, t in enumerate(self.tests, start=1):
            steps_info.append({"idx": idx, "name": t.get("name", f"Step {idx}"), "vc": self._describe_vc(t)})
        self.on_steps_init(steps_info)

        # settle phase: collect boot lines
        self._matching_enabled = False
        settle_until = _now_s() + SETTLE_DELAY_S
        self.on_status(f"Settling for {SETTLE_DELAY_S:.1f}s to capture boot logs…")
        self._running = True
        try:
            while self._running and _now_s() < settle_until:
                if self._paused:
                    self._drain_to_ring()
                    time.sleep(IDLE_TICK_S)
                    continue
                self._drain_to_ring()
                time.sleep(IDLE_TICK_S)
        except Exception as e:
            self.on_status(f"Settle error: {e}\n{traceback.format_exc()}")

        # feed pre-start lines
        pre_lines = self._ring.drain()
        self.on_status(f"Feeding {len(pre_lines)} buffered lines…")
        self._current_step_idx = self._first_unfinished_idx()
        self._reset_step_timer()
        for ln in pre_lines:
            if not self._running: break
            self._process_line_for_current(ln)

        # start live matching
        self._matching_enabled = True
        self.on_status("Live matching started.")

        # main loop
        try:
            while self._running:
                # process any prefeed lines produced by wait_capture
                while self._prefeed_lines and self._running and not self._paused:
                    ln = self._prefeed_lines.pop(0)
                    self._process_line_for_current(ln)

                # read from socket
                text = self._recv_block()
                if text is None:
                    self.on_status("Stream closed.")
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
                            # discard while paused to avoid backlog
                            continue
                        if not self._matching_enabled:
                            # extremely rare: disabled matching, keep to ring
                            self._ring.append(line.rstrip("\r"))
                            continue
                        self._process_line_for_current(line.rstrip("\r"))

                # timeout check even if no data
                self._check_step_timeout()
                time.sleep(IDLE_TICK_S)

        except Exception as e:
            self.on_status(f"Runner error: {e}\n{traceback.format_exc()}")
        finally:
            # ensure socket closed
            try:
                if self._sock:
                    try: self._sock.shutdown(socket.SHUT_RDWR)
                    except Exception: pass
            finally:
                try:
                    if self._sock: self._sock.close()
                except Exception: pass
                self._sock = None
                self._running = False
                self.on_status("Stopped.")

    def stop(self):
        # signal and drop socket so any recv unblocks
        self._stop.set()
        try:
            self._client.close()
        except Exception:
            pass
        # unpause if paused, so the loop can finish
        self._pause.clear()
        th = self._worker
        self._worker = None
        if th and th.is_alive():
            th.join(timeout=1.5)


    def pause(self):
        self._paused = True
        self.on_status("Paused.")

    def resume(self):
        self._paused = False
        self.on_status("Resumed.")

    # -------- connect & recv --------
    def _connect_with_retries(self) -> Optional[socket.socket]:
        last_err = None
        for attempt in range(1, RETRY_MAX_ATTEMPTS + 1):
            try:
                self.on_status(f"Connecting to {self.host}:{self.port} (attempt {attempt}/{RETRY_MAX_ATTEMPTS})…")
                s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
                s.settimeout(CONNECT_TIMEOUT_SEC)
                s.connect((self.host, self.port))
                s.settimeout(None)
                self._first_connect_done = True
                return s
            except Exception as e:
                last_err = e
                delay = RETRY_BACKOFF_BASE_S * (2 ** (attempt - 1))
                self.on_status(f"Connect failed: {e} — retrying in {delay:.1f}s")
                time.sleep(delay)
        self.on_status(f"Connect failed after {RETRY_MAX_ATTEMPTS} attempts: {last_err}")
        return None

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
            try: self._sock.setblocking(True)
            except Exception: pass

    # -------- sequential matcher --------
    def _first_unfinished_idx(self) -> Optional[int]:
        for i, t in enumerate(self.tests, start=1):
            if not t.get("_done"):
                return i
        return None

    def _current_timeout_s(self, idx: int, t: Dict[str, Any]) -> float:
        # Allow per-step override: "timeout": seconds
        if isinstance(t.get("timeout"), (int, float)) and t["timeout"] >= 0:
            return float(t["timeout"])
        if idx == 1:
            return STEP1_TIMEOUT_S
        return DEFAULT_STEP_TIMEOUT_S

    def _reset_step_timer(self) -> None:
        self._current_step_start_s = _now_s()

    def _check_step_timeout(self) -> None:
        idx = self._current_step_idx
        if idx is None:
            return
        t = self.tests[idx - 1]
        tmo = self._current_timeout_s(idx, t)
        if tmo <= 0:
            return
        if (_now_s() - self._current_step_start_s) >= tmo:
            # timeout reached -> result depends on step type
            name = t.get("name", f"Step {idx}")
            vc = self._describe_vc(t)
            if "not_find" in t:
                # forbidden never appeared => PASS
                t["_done"] = True
                self._emit(idx, name, vc, "PASS", f"[timeout {tmo:.0f}s: pattern not seen]")
            elif "action" in t:
                # action shouldn't normally time out, but mark FAIL for visibility
                t["_done"] = True
                self._emit(idx, name, vc, "FAIL", f"[timeout {tmo:.0f}s on action]")
            else:
                # find/sequence => FAIL
                t["_done"] = True
                self._emit(idx, name, vc, "FAIL", f"[timeout {tmo:.0f}s]")
            # advance
            self._current_step_idx = self._first_unfinished_idx()
            if self._current_step_idx is not None:
                self._reset_step_timer()

    def _process_line_for_current(self, line: str) -> None:
        """Process a line for the current step only (sequential)."""
        idx = self._current_step_idx
        if idx is None:
            return

        t = self.tests[idx - 1]
        name = t.get("name", f"Step {idx}")
        vc   = self._describe_vc(t)

        try:
            # ACTION executes immediately on its turn
            if "action" in t:
                ok, msg = self._perform_action(t["action"])
                t["_done"] = True
                self._emit(idx, name, vc, ("PASS" if ok else "FAIL"), msg)
                # After wait_capture, prefeed lines (already appended inside action)
                self._current_step_idx = self._first_unfinished_idx()
                if self._current_step_idx is not None:
                    self._reset_step_timer()
                return

            # FIND
            if "find" in t:
                cfg = t["find"]
                if tre.line_matches(line, cfg):
                    t["_done"] = True
                    # Emit RAW line; UI will extract payload
                    self._emit(idx, name, vc, "PASS", line)
                    self._current_step_idx = self._first_unfinished_idx()
                    if self._current_step_idx is not None:
                        self._reset_step_timer()
                return

            # NOT_FIND -> if forbidden pattern appears, fail immediately
            if "not_find" in t:
                cfg = t["not_find"]
                if tre.line_matches(line, cfg):
                    t["_done"] = True
                    self._emit(idx, name, vc, "FAIL", line)
                    self._current_step_idx = self._first_unfinished_idx()
                    if self._current_step_idx is not None:
                        self._reset_step_timer()
                return

            # SEQUENCE: progress left-to-right
            if "sequence" in t:
                seq = t.get("sequence", [])
                if not seq:
                    t["_done"] = True
                    self._emit(idx, name, vc, "PASS", "")
                    self._current_step_idx = self._first_unfinished_idx()
                    if self._current_step_idx is not None:
                        self._reset_step_timer()
                    return
                prog = t.setdefault("_seq_idx", 0)
                node = seq[prog]
                cfg = node if isinstance(node, dict) else {"pattern": node, "literal": True}
                if tre.line_matches(line, cfg):
                    t["_seq_idx"] = prog + 1
                    if t["_seq_idx"] >= len(seq):
                        t["_done"] = True
                        self._emit(idx, name, vc, "PASS", line)
                        self._current_step_idx = self._first_unfinished_idx()
                        if self._current_step_idx is not None:
                            self._reset_step_timer()
                return

            # Unknown rule
            t["_done"] = True
            self._emit(idx, name, vc, "ERROR", "Unknown rule")
            self._current_step_idx = self._first_unfinished_idx()
            if self._current_step_idx is not None:
                self._reset_step_timer()

        except Exception as e:
            t["_done"] = True
            self._emit(idx, name, vc, "ERROR", f"{type(e).__name__}: {e}")
            self._current_step_idx = self._first_unfinished_idx()
            if self._current_step_idx is not None:
                self._reset_step_timer()

    # -------- output helpers --------
    def _emit(self, idx: int, name: str, vc: str, result: str, line: Optional[str]):
        try:
            self.on_step_update(idx, name, vc, result, line)
        except Exception:
            pass

    def _describe_vc(self, t: Dict[str, Any]) -> str:
        if "find" in t: return t["find"].get("pattern","")
        if "not_find" in t: return f"NOT {t['not_find'].get('pattern','')}"
        if "sequence" in t:
            return " -> ".join([e if isinstance(e, str) else e.get("pattern","") for e in t.get("sequence",[])])
        if "action" in t:
            a = t["action"]; at = str(a.get("type","action")).lower()
            if at == "screenshot":
                fn = a.get("file")
                return f"[screenshot]{' → ' + fn if fn else ''}"
            if at in ("wait","wait_capture"):
                return f"[{at} {a.get('ms','')}ms]"
            return f"[{at}]"
        return "(unknown rule)"

    # -------- actions --------
    def _perform_action(self, action: Dict[str, Any]) -> Tuple[bool, str]:
        at = str((action or {}).get("type", "")).lower()

        # --- WAIT (discard logs during wait) ---
        if at == "wait":
            ms = int(action.get("ms", 0))
            end_t = _now_s() + (ms / 1000.0)
            # keep draining (discard) so socket doesn't backlog
            while self._running and _now_s() < end_t:
                self._drain_discard_once()
                time.sleep(WAIT_DRAIN_TICK_S)
            return True, f"wait {ms}ms"

        # --- WAIT_CAPTURE (buffer logs during wait for the NEXT step) ---
        if at == "wait_capture":
            ms = int(action.get("ms", 0))
            end_t = _now_s() + (ms / 1000.0)
            captured: List[str] = []
            while self._running and _now_s() < end_t:
                captured.extend(self._drain_collect_once())
                time.sleep(WAIT_DRAIN_TICK_S)
            # after wait, prefeed these lines into the next step
            if captured:
                # append to the front of prefeed so they are processed first
                self._prefeed_lines = captured + self._prefeed_lines
            return True, f"wait_capture {ms}ms ({len(captured)} lines)"

        # --- Screenshot (full frame, direct save + verify) ---
        if at == "screenshot":
            shots_dir = os.path.join(self.out_dir, "Screenshots")
            os.makedirs(shots_dir, exist_ok=True)
            fn = action.get("file")
            if not fn:
                ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
                fn = f"shot_{ts}.png"
            fn = os.path.basename(str(fn))
            out_path = os.path.join(shots_dir, fn)

            if not ANDROID_OK:
                return False, "Android not available"
            try:
                droid.ensure_server()
                ser = droid.get_default_device_serial()
                if not ser:
                    return False, "No Android device"
                # capture straight to final path
                ok = droid.screencap_png_to(out_path, serial=ser) \
                     if hasattr(droid, "screencap_png_to") else _compat_screencap_to(out_path)
                if not ok or not os.path.exists(out_path):
                    return False, f"screenshot failed: {out_path}"
                return True, f"{out_path}"
            except Exception as e:
                return False, f"screenshot error: {e}"

        # --- Tap absolute ---
        if at == "tap":
            if not ANDROID_OK:
                return False, "Android not available"
            try:
                droid.ensure_server()
                ser = droid.get_default_device_serial()
                if not ser: return False, "No Android device"
                x = int(action.get("x", -1)); y = int(action.get("y", -1))
                if x < 0 or y < 0:
                    return False, "tap needs x/y"
                ok = droid.input_tap(x, y, serial=ser)
                return ok, f"tap({x},{y}) {'ok' if ok else 'fail'}"
            except Exception as e:
                return False, f"tap error: {e}"

        # --- Tap percentage ---
        if at == "tap_pct":
            if not ANDROID_OK:
                return False, "Android not available"
            try:
                droid.ensure_server()
                ser = droid.get_default_device_serial()
                if not ser: return False, "No Android device"
                px = float(action.get("px", -1)); py = float(action.get("py", -1))
                if not (0 <= px <= 1 and 0 <= py <= 1):
                    return False, "tap_pct needs px/py in [0..1]"
                if self._dev_w <= 0 or self._dev_h <= 0:
                    wh = droid.device_wm_size(serial=ser)
                    if wh: self._dev_w, self._dev_h = wh
                if self._dev_w <= 0 or self._dev_h <= 0:
                    return False, "unknown device size"
                x = int(round(px * self._dev_w)); y = int(round(py * self._dev_h))
                ok = droid.input_tap(x, y, serial=ser)
                return ok, f"tap_pct({px:.2f},{py:.2f})=>({x},{y}) {'ok' if ok else 'fail'}"
            except Exception as e:
                return False, f"tap_pct error: {e}"

        return False, f"unknown action: {at}"

    # -------- wait helpers (socket draining) --------
    def _drain_discard_once(self) -> None:
        """Drain available data and discard (used by 'wait')."""
        if not self._sock: return
        try:
            self._sock.setblocking(False)
            while True:
                try:
                    data = self._sock.recv(RECV_BLOCK_BYTES)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break
                if not data: break
                # discard by not appending anywhere
        finally:
            try: self._sock.setblocking(True)
            except Exception: pass

    def _drain_collect_once(self) -> List[str]:
        """Drain available data and return lines (used by 'wait_capture')."""
        out: List[str] = []
        if not self._sock: return out
        try:
            self._sock.setblocking(False)
            local_buf = ""
            while True:
                try:
                    data = self._sock.recv(RECV_BLOCK_BYTES)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break
                if not data: break
                local_buf += data.decode("utf-8", errors="ignore")
                while True:
                    p = local_buf.find("\n")
                    if p < 0: break
                    line = local_buf[:p]
                    local_buf = local_buf[p+1:]
                    out.append(line.rstrip("\r"))
            # keep any partial remainder for normal loop
            if local_buf:
                # stash remainder into main buffer so main loop can complete it
                self._buf += local_buf
        finally:
            try: self._sock.setblocking(True)
            except Exception: pass
        return out


# ---- Compat helper for older TRE_android without screencap_png_to ----
def _compat_screencap_to(out_path: str) -> bool:
    """Fallback: capture to temp then move to out_path."""
    try:
        tmp_dir = os.path.dirname(out_path) or "."
        tmp = droid.screencap_png(tmp_dir, prefix="__tmp__", serial=droid.get_default_device_serial())
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

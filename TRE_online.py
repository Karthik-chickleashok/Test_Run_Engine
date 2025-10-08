# =====================================================================
# TRE_online.py — Online Runner / Engine  •  Version 2.3.2
# Test Run Engine (TRE)
#
# Focus (keeps previous behavior, adds stability):
#  - Default timeout stays 0.0 (no auto timeouts unless set per step)
#  - Strict, safe history catch-up (no instant PASS without a real match)
#  - Payload-only parser + payload_tap.log (1 line per payload)
#  - LOG_ONLY_MODE (when True, only logs payload; no matching)
#  - Sequential verification preserved (VERIFY_SEQUENTIAL = True)
#  - Actions: wait, wait_capture, screenshot, tap, tap_pct (unchanged)
#  - Stop: immediate (socket shutdown)
#  - Auto reconnect retained
#
# Notes:
#  - All previous toggles preserved but de-duplicated
#  - You can enable debug per-step via DEBUG_STEPS = {7} for example
# =====================================================================

from __future__ import annotations
import os, socket, time, datetime, traceback, subprocess, collections, re
from typing import Dict, Any, List, Optional, Callable, Tuple, Deque
from collections import deque

# Optional AI helper (safe no-op if missing)
try:
    import TRE_ai as ai
except Exception:
    class _AIShim:
        def is_enabled(self): return False
        def rca_summary(self, *a, **k): return ""
    ai = _AIShim()

import TRE_json as tre
try:
    import TRE_android as droid
    ANDROID_OK = True
except Exception:
    ANDROID_OK = False


# === CHUNK 0 — Paths & files =========================================
ROOT_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR    = os.path.join(ROOT_DIR, "Logs")
REPORTS_DIR = os.path.join(ROOT_DIR, "Reports")
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)

WIRE_TAP_FILE    = os.path.join(LOGS_DIR, "wire_tap.log")
MATCH_LOG_FILE   = os.path.join(LOGS_DIR, "match_debug.log")   # detailed matching/debug
PAYLOAD_TAP_FILE = os.path.join(LOGS_DIR, "payload_tap.log")   # payload-only stream
AI_LOG_FILE      = os.path.join(LOGS_DIR, "ai_debug.log")

def _safe_open(path, mode):
    try: return open(path, mode, encoding="utf-8", errors="ignore")
    except Exception: return None

wire_fh     = _safe_open(WIRE_TAP_FILE, "a")
_match_fh   = _safe_open(MATCH_LOG_FILE, "a")
_payload_fh = _safe_open(PAYLOAD_TAP_FILE, "a")
_ai_fh      = _safe_open(AI_LOG_FILE, "a")

def _ai_log(msg: str) -> None:
    if not _ai_fh: return
    try:
        _ai_fh.write(msg + ("\n" if not msg.endswith("\n") else ""))
        _ai_fh.flush()
    except Exception:
        pass


# === CHUNK 1 — Engine tunables & toggles (single source of truth) ====
CONNECT_TIMEOUT_SEC    = 5.0
RECV_BLOCK_BYTES       = 65536
RETRY_MAX_ATTEMPTS     = 5
RETRY_BACKOFF_BASE_S   = 0.5
SETTLE_DELAY_S         = 3.0
RING_BUFFER_MAX_LINES  = 20000

STEP1_TIMEOUT_S        = 120.0
DEFAULT_STEP_TIMEOUT_S = 0.0     # keep as requested

IDLE_TICK_S            = 0.20
WAIT_DRAIN_TICK_S      = 0.05

# Behavior
FORCE_PAYLOAD_ONLY     = True    # online: match payload only
VERIFY_SEQUENTIAL      = True    # strict order execution
LOG_ONLY_MODE          = False   # True => log payload only, skip matching

# Debug controls
MATCH_DEBUG            = True
DEBUG_STEPS: set[int]  = set()   # e.g. {7}
DEBUG_NAME_CONTAINS: List[str] = []  # e.g. ["Processing"]
DEBUG_VERBOSE_RAW      = False
HISTORY_MAX_PER_STEP   = 200     # for debugging
AUTO_RECONNECT         = True
RECONNECT_MAX          = 3
RECONNECT_DELAY_S      = 1.0

def _now_s() -> float: return time.monotonic()


# === CHUNK P — Payload sanitization and logging =======================
# Precompiled helpers for cleanup
_RX_REPEAT_ALLCAPS = re.compile(r"\b([A-Z]{3,10})(?:\1)+\b")   # OTAOTA -> OTA
_RX_EQ_TAIL        = re.compile(r"=\s*[A-Z]{2,10}\d*[a-z]?\b") # =CCU2x / =ABC123x
_RX_MULTI_WS       = re.compile(r"\s+")
_RX_NON_PRINT      = re.compile(r"[^\x20-\x7E\t ]+")

_DENY_TOKENS = {"TELETELE", "AOTA", "CCU2s", "CCU2c"}  # harmless junk remnants

def _sanitize_payload(payload: str) -> str:
    """Produce one clean, readable, single-line payload for logging & matching."""
    if not payload:
        return ""
    # flatten lines
    s = payload.replace("\r", " ").replace("\n", " ")
    # strip non-printables
    s = _RX_NON_PRINT.sub(" ", s)
    # remove known junk tokens
    for tok in _DENY_TOKENS:
        s = s.replace(tok, " ")
    # collapse repeated ALLCAPS
    s = _RX_REPEAT_ALLCAPS.sub(r"\1", s)
    # strip tail like =CCU2x
    s = _RX_EQ_TAIL.sub(" ", s)
    # normalize whitespace (and normalize spacing around >>)
    s = re.sub(r"\s*>>\s*", " >> ", s)
    s = _RX_MULTI_WS.sub(" ", s).strip()
    return s

def _log_payload_line(payload: str) -> None:
    if not payload or not _match_fh: return
    try:
        _match_fh.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {payload}\n")
        _match_fh.flush()
    except Exception:
        pass


# === CHUNK 2 — Small helpers =========================================
class LineRing:
    def __init__(self, max_lines: int = RING_BUFFER_MAX_LINES):
        self._dq: Deque[str] = collections.deque(maxlen=max_lines)
    def append(self, line: str) -> None: self._dq.append(line)
    def drain(self) -> List[str]: out = list(self._dq); self._dq.clear(); return out
    def __len__(self): return len(self._dq)


# === CHUNK 3 — Android fallback (adbb) ================================
class _AdbDirect:
    def __init__(self, exe: str = "adbb"): self.exe = exe
    def tap(self, x:int, y:int)->bool:
        try:
            r=subprocess.run([self.exe,"t",str(x),str(y)],timeout=1.5,
                             stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            if r.returncode==0: return True
        except Exception: pass
        try:
            r=subprocess.run([self.exe,"shell","input","tap",str(x),str(y)],
                             timeout=1.5,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
            return r.returncode==0
        except Exception: return False
    def screencap_png_to(self,out_file:str)->bool:
        try:
            with open(out_file,"wb") as f:
                r=subprocess.run([self.exe,"exec-out","screencap","-p"],
                                 timeout=4.0,stdout=f,stderr=subprocess.DEVNULL)
            return r.returncode==0 and os.path.exists(out_file) and os.path.getsize(out_file)>0
        except Exception: return False


# === CHUNK 4 — SessionManager (lifecycle, I/O, dispatch) =============
class SessionManager:
    def __init__(self,host:str,port:int,tests:List[Dict[str,Any]],out_dir:str,
                 snapshot_interval:int=10,on_status=None,on_steps_init=None,on_step_update=None):
        self.host, self.port = host,int(port)
        self.tests=[dict(t) for t in (tests or [])]
        self.out_dir=out_dir or REPORTS_DIR
        self.snapshot_interval=max(2,int(snapshot_interval or 10))
        self.on_status=on_status or (lambda m:None)
        self.on_steps_init=on_steps_init or (lambda lst:None)
        self.on_step_update=on_step_update or (lambda i,n,v,r,l:None)

        # connection / control
        self._sock=None; self._running=False; self._paused=False

        # assemble & buffers
        self._buf=""; self._ring=LineRing(); self._matching_enabled=False
        self._prefeed_lines: List[str] = []
        self._payload_history: Deque[str] = deque(maxlen=5000)  # CLEAN payload cache

        # actions
        self._action_ptr=1
        self._dev_w=0; self._dev_h=0; self._adb=_AdbDirect("adbb")

        # per-step debug
        self._hist: Dict[int, Deque[str]] = {}
        self._counts: Dict[int, int] = {}

    # ---- Debug helpers
    def _dbg_on(self, idx: Optional[int], name: Optional[str]) -> bool:
        if not MATCH_DEBUG: return False
        if not DEBUG_STEPS and not DEBUG_NAME_CONTAINS: return True
        if DEBUG_STEPS and idx is not None and idx in DEBUG_STEPS: return True
        if DEBUG_NAME_CONTAINS and name:
            low=name.lower()
            return any(sub.lower() in low for sub in DEBUG_NAME_CONTAINS)
        return False

    def _dbg(self, msg: str) -> None:
        if not _match_fh: return
        try:
            _match_fh.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
            _match_fh.flush()
        except Exception:
            pass

    # ---- Lifecycle
    def pause(self):  self._paused=True;  self.on_status("Paused.")
    def resume(self): self._paused=False; self.on_status("Resumed.")

    def stop(self):
        self._running=False; self._paused=False
        self._buf=""; self._ring.drain(); self._prefeed_lines.clear()
        try:
            if self._sock:
                try:self._sock.shutdown(socket.SHUT_RDWR)
                except Exception: pass
                try:self._sock.close()
                except Exception: pass
        finally:
            self._sock=None

    # ---- Start
    def start(self):
        # reset
        for t in self.tests:
            t.pop("_done",None); t.pop("_seq_idx",None); t.pop("_t0", None); t.pop("_count", None)
        self._ring=LineRing(); self._matching_enabled=False; self._action_ptr=1
        self._hist.clear(); self._counts.clear(); self._payload_history.clear()

        # connect
        s=self._connect_with_retries()
        if not s: self.on_status("Failed to connect."); return
        self._sock=s; self._running=True; self.on_status("Connected.")

        # init table
        now=time.monotonic()
        for i,t in enumerate(self.tests, start=1):
            t["_done"]=False; t["_seq_idx"]=0; t["_t0"]=now; t["_count"]=0
            self._hist[i]=collections.deque(maxlen=HISTORY_MAX_PER_STEP)
            self._counts[i]=0

        steps_info=[{"idx":i,"name":t.get("name",f"Step {i}"),"vc":self._describe_vc(t)}
                    for i,t in enumerate(self.tests,1)]
        try: self.on_steps_init(steps_info)
        except Exception: pass

        # settle
        settle_until=_now_s()+SETTLE_DELAY_S
        self.on_status(f"Settling for {SETTLE_DELAY_S:.1f}s to capture boot logs…")
        try:
            while self._running and _now_s()<settle_until:
                self._drain_to_ring(); time.sleep(IDLE_TICK_S)
        except Exception as e:
            self.on_status(f"Settle error: {e}\n{traceback.format_exc()}")

        # feed pre-start lines
        pre_lines=self._ring.drain()
        self.on_status(f"Feeding {len(pre_lines)} buffered lines…")
        for ln in pre_lines:
            if not self._running: break
            self._process_line_dispatch(ln)

        # main loop
        self._matching_enabled=True
        self.on_status("Live matching started.")
        disconnects=0
        try:
            while self._running:
                # prefeed (from wait_capture)
                while self._prefeed_lines and self._running and not self._paused:
                    ln=self._prefeed_lines.pop(0)
                    self._process_line_dispatch(ln)

                text=self._recv_block()
                if text is None:
                    if AUTO_RECONNECT and disconnects<RECONNECT_MAX:
                        disconnects+=1
                        self.on_status(f"Disconnected — reconnecting ({disconnects}/{RECONNECT_MAX})…")
                        s=self._reconnect()
                        if s:
                            self.on_status("Reconnected.")
                            continue
                    break

                if text:
                    self._buf+=text
                    while True:
                        p=self._buf.find("\n")
                        if p<0: break
                        line=self._buf[:p]; self._buf=self._buf[p+1:]
                        raw=line.rstrip("\r")
                        if wire_fh:
                            try: wire_fh.write(raw+"\n"); wire_fh.flush()
                            except Exception: pass
                        if self._paused:
                            self._ring.append(raw); continue
                        self._process_line_dispatch(raw)

                # no default timeouts (DEFAULT_STEP_TIMEOUT_S = 0.0) — only per-step if provided
                self._run_pending_actions()

                # strict, SAFE catch-up (optional): current step only, only PASS on real match
                cur = self._first_unfinished_idx()
                if cur is not None:
                    self._scan_history_for_step(cur)

                if self._all_done(): self._running=False
                time.sleep(IDLE_TICK_S)

        except Exception as e:
            self.on_status(f"Runner error: {e}\n{traceback.format_exc()}")
        finally:
            try: self._finalize_unfinished("stopped" if not self._running else "ended")
            except Exception: pass
            # flush logs
            for fh in (wire_fh,_match_fh,_payload_fh,_ai_fh):
                try:
                    if fh: fh.flush()
                except Exception: pass
            # close socket
            try:
                if self._sock:
                    try:self._sock.shutdown(socket.SHUT_RDWR)
                    except Exception: pass
            finally:
                try:
                    if self._sock: self._sock.close()
                except Exception: pass
                self._sock=None; self._running=False; self._paused=False
                self.on_status("Stopped.")

            # optional AI summary (unchanged behavior)
            try:
                if hasattr(ai, "is_enabled") and ai.is_enabled():
                    failed = []
                    for i, t in enumerate(self.tests, start=1):
                        res = t.get("_final_result")
                        if res and res != "PASS":
                            failed.append({
                                "_idx": i,
                                "name": t.get("name", f"Step {i}"),
                                "find": t.get("find"),
                                "not_find": t.get("not_find"),
                                "sequence": t.get("sequence"),
                                "action": t.get("action"),
                                "_final_result": res,
                                "_final_line": t.get("_final_line"),
                            })
                    if failed:
                        histories = {i: list(self._hist.get(i, [])) for i in range(1, len(self.tests)+1)}
                        rca = ai.rca_summary(failed, histories)
                        _ai_log("\n==== RCA SUMMARY ====\n" + rca + "\n=====================\n")
                        self.on_status("[AI] RCA summary written to ai_debug.log")


    # ---- Dispatch: parse -> sanitize -> log -> match (or log-only) ---
    def _process_line_dispatch(self, line: str) -> None:
        # 1) parse and sanitize payload
        try:
            _, _, _, payload_raw = tre.parse_dlt(line)
        except Exception:
            payload_raw = line
        payload = tre.sanitize_payload(payload_raw) if hasattr(tre, "sanitize_payload") else _sanitize_payload(payload_raw)

        # 2) append to payload history (for strict catch-up) and log as single line
        self._payload_history.append(payload)
        if _payload_fh:
            try:
                _payload_fh.write(payload + "\n")
                _payload_fh.flush()
            except Exception:
                pass

        if DEBUG_VERBOSE_RAW and self._dbg_on(None, None):
            self._dbg(f"[RAW] {line[:220]}")
            self._dbg(f"[PAYLOAD] {payload}")

        # 3) Logging-only mode?
        if LOG_ONLY_MODE:
            return

        # 4) Match in chosen mode (we keep sequential as requested)
        if VERIFY_SEQUENTIAL:
            self._process_line_sequential(line, payload)
        else:
            self._process_line_cumulative(line, payload)


    # === CHUNK 5 — Connect / I/O =====================================
    def _connect_with_retries(self) -> Optional[socket.socket]:
        last_err=None
        for attempt in range(1,RETRY_MAX_ATTEMPTS+1):
            try:
                self.on_status(f"Connecting to {self.host}:{self.port} (attempt {attempt}/{RETRY_MAX_ATTEMPTS})…")
                s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
                try:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_KEEPALIVE, 1)
                except Exception: pass
                s.settimeout(CONNECT_TIMEOUT_SEC); s.connect((self.host,self.port)); s.settimeout(None)
                return s
            except Exception as e:
                last_err=e; delay=RETRY_BACKOFF_BASE_S*(2**(attempt-1))
                self.on_status(f"Connect failed: {e} — retrying in {delay:.1f}s")
                time.sleep(delay)
        self.on_status(f"Connect failed after {RETRY_MAX_ATTEMPTS} attempts: {last_err}")
        return None

    def _reconnect(self) -> Optional[socket.socket]:
        try:
            if self._sock:
                try:self._sock.close()
                except Exception: pass
        except Exception: pass
        self._sock=None
        s=self._connect_with_retries()
        if s: self._sock=s
        return s

    def _recv_block(self) -> Optional[str]:
        if not self._sock: return None
        try:
            data=self._sock.recv(RECV_BLOCK_BYTES)
            if not data: return None
            return data.decode("utf-8", errors="ignore")
        except (BlockingIOError, InterruptedError):
            return ""
        except OSError:
            return None
        except Exception:
            raise

    def _drain_to_ring(self) -> None:
        if not self._sock: return
        self._sock.setblocking(False)
        try:
            while True:
                try:
                    data=self._sock.recv(RECV_BLOCK_BYTES)
                except (BlockingIOError, InterruptedError):
                    break
                except OSError:
                    break
                if not data:
                    break
                self._buf+=data.decode("utf-8", errors="ignore")
                while True:
                    p=self._buf.find("\n")
                    if p<0: break
                    line=self._buf[:p]; self._buf=self._buf[p+1:]
                    self._ring.append(line.rstrip("\r"))
        finally:
            try:self._sock.setblocking(True)
            except Exception: pass


    # === CHUNK 6 — Matching (sequential + cumulative) =================
    def _first_unfinished_idx(self) -> Optional[int]:
        for i, t in enumerate(self.tests, start=1):
            if not t.get("_done"):
                return i
        return None

    def _normalize_cfg(self, cfg: Dict[str,Any]) -> Dict[str,Any]:
        c = dict(cfg or {})
        # online: payload_only forced
        c["payload_only"] = True
        return c

    def _try_find(self, idx:int, cfg:Dict[str,Any], payload:str, name:str) -> bool:
        c = self._normalize_cfg(cfg)
        need  = int(c.get("min_count", 1))
        have  = self._counts.get(idx, 0)
        if self._dbg_on(idx, name):
            self._dbg(f"[FIND] step#{idx} need={need} have={have} pat='{c.get('pattern')}' | payload='{payload[:220]}'")
        if tre.line_matches(payload, c):
            have += 1
            self._counts[idx] = have
            if self._dbg_on(idx, name): self._dbg(f"[FIND] step#{idx} match count={have}")
            if have >= need:
                return True
        return False

    def _try_sequence(self, idx:int, t:Dict[str,Any], payload:str, name:str) -> bool:
        seq = t.get("sequence", []) or []
        prog = t.setdefault("_seq_idx", 0)
        if prog >= len(seq):  # already done
            return True
        node = seq[prog]
        c = node if isinstance(node, dict) else {"pattern": str(node), "literal": True}
        c = self._normalize_cfg(c)
        if self._dbg_on(idx, name):
            self._dbg(f"[SEQ] step#{idx} prog={prog}/{len(seq)} pat='{c.get('pattern')}' | payload='{payload[:220]}'")
        if tre.line_matches(payload, c):
            t["_seq_idx"] = prog + 1
            return t["_seq_idx"] >= len(seq)
        return False

    def _process_line_sequential(self, line: str, payload: str) -> None:
        idx = self._first_unfinished_idx()
        if idx is None: return
        t = self.tests[idx - 1]
        name = t.get("name", f"Step {idx}")
        vc   = self._describe_vc(t)
        self._hist[idx].append(payload)

        if "action" in t:
            ok,msg=self._perform_action(t["action"])
            t["_done"]=True; self._emit(idx,name,vc,("PASS" if ok else "FAIL"),msg)
            return

        if "find" in t:
            if self._try_find(idx, t["find"], payload, name):
                t["_done"]=True; self._emit(idx,name,vc,"PASS",payload)
            return

        if "not_find" in t:
            cfg = self._normalize_cfg(t["not_find"])
            if tre.line_matches(payload, cfg):
                t["_done"]=True; self._emit(idx,name,vc,"FAIL",payload)
            return

        if "sequence" in t:
            if self._try_sequence(idx, t, payload, name):
                t["_done"]=True; self._emit(idx,name,vc,"PASS",payload)
            return

        # unknown
        t["_done"]=True; self._emit(idx,name,vc,"ERROR","Unknown rule")

    def _process_line_cumulative(self, line: str, payload: str) -> None:
        for idx0, t in enumerate(self.tests):
            if t.get("_done"): continue
            idx = idx0 + 1
            name = t.get("name", f"Step {idx}")
            vc   = self._describe_vc(t)
            self._hist[idx].append(payload)

            if "action" in t:
                continue  # actions are run via _run_pending_actions()

            if "find" in t:
                if self._try_find(idx, t["find"], payload, name):
                    t["_done"]=True; self._emit(idx,name,vc,"PASS",payload)
                continue

            if "not_find" in t:
                cfg = self._normalize_cfg(t["not_find"])
                if tre.line_matches(payload, cfg):
                    t["_done"]=True; self._emit(idx,name,vc,"FAIL",payload)
                continue

            if "sequence" in t:
                if self._try_sequence(idx, t, payload, name):
                    t["_done"]=True; self._emit(idx,name,vc,"PASS",payload)
                continue

            t["_done"]=True; self._emit(idx,name,vc,"ERROR","Unknown rule")


    # === CHUNK 6b — Strict, safe catch-up from history ================
    def _scan_history_for_step(self, idx: int) -> None:
        """Only PASS/FAIL a step if a *real* match exists in preserved payload history."""
        if idx < 1 or idx > len(self.tests): 
            return
        t = self.tests[idx - 1]
        if t.get("_done"): return
        if "action" in t:  # only verification here
            return

        name = t.get("name", f"Step {idx}")
        vc   = self._describe_vc(t)

        def _norm_cfg(cfg: Dict[str, Any]) -> Dict[str, Any]:
            c = dict(cfg or {})
            c["payload_only"] = True
            return c

        # scan older payloads for a true match
        for payload in list(self._payload_history):
            if "find" in t:
                if tre.line_matches(payload, _norm_cfg(t["find"])):
                    t["_done"] = True
                    self._emit(idx, name, vc, "PASS", payload)
                    return
            elif "not_find" in t:
                if tre.line_matches(payload, _norm_cfg(t["not_find"])):
                    t["_done"] = True
                    self._emit(idx, name, vc, "FAIL", payload)
                    return
            elif "sequence" in t:
                seq = t.get("sequence", []) or []
                prog = t.setdefault("_seq_idx", 0)
                if prog >= len(seq):
                    t["_done"] = True
                    self._emit(idx, name, vc, "PASS", "")
                    return
                node = seq[prog]
                c = node if isinstance(node, dict) else {"pattern": str(node), "literal": True}
                c = _norm_cfg(c)
                if tre.line_matches(payload, c):
                    t["_seq_idx"] = prog + 1
                    if t["_seq_idx"] >= len(seq):
                        t["_done"] = True
                        self._emit(idx, name, vc, "PASS", payload)
                        return
        # If no match found, do nothing (no instant PASS).


    # === CHUNK 7 — Timeouts / finalize / emit / VC ====================
    def _current_timeout_s(self, idx:int, t:Dict[str,Any])->float:
        # default stays 0.0 unless per-step overrides
        if isinstance(t.get("timeout"), (int, float)) and t["timeout"] >= 0:
            return float(t["timeout"])
        return STEP1_TIMEOUT_S if idx == 1 else DEFAULT_STEP_TIMEOUT_S

    def _check_timeouts(self)->None:
        # With DEFAULT_STEP_TIMEOUT_S = 0.0 this only triggers if a step sets "timeout": N
        if not self._running: return
        now=_now_s()
        for idx,t in enumerate(self.tests,1):
            if t.get("_done"): continue
            t0=t.get("_t0") or now; t["_t0"]=t0
            tmo=self._current_timeout_s(idx,t)
            if tmo<=0 or (now-t0)<tmo: continue
            name=t.get("name",f"Step {idx}"); vc=self._describe_vc(t)
            if "not_find" in t:
                t["_done"]=True; self._emit(idx,name,vc,"PASS",f"[timeout {tmo:.0f}s: pattern not seen]")
            elif "action" in t:
                t["_done"]=True; self._emit(idx,name,vc,"FAIL",f"[timeout {tmo:.0f}s on action]")
            else:
                t["_done"]=True; self._emit(idx,name,vc,"FAIL",f"[timeout {tmo:.0f}s]")

    def _all_done(self)->bool:
        return all(t.get("_done") for t in self.tests)

    def _finalize_unfinished(self,reason:str="stopped")->None:
        for i,t in enumerate(self.tests,1):
            if t.get("_done"): continue
            name=t.get("name",f"Step {i}"); vc=self._describe_vc(t)
            if "not_find" in t:
                t["_done"]=True; self._emit(i,name,vc,"PASS",f"[{reason}: pattern not seen]")
            else:
                t["_done"]=True; self._emit(i,name,vc,"FAIL",f"[{reason}: pattern never seen]")

    def _emit(self,idx:int,name:str,vc:str,result:str,line:Optional[str]):
        try:
            if self.on_step_update: self.on_step_update(idx,name,vc,result,line)
            t = self.tests[idx-1]
            t["_final_result"] = result
            t["_final_line"]   = line
        except Exception: pass

    def _describe_vc(self,t:Dict[str,Any])->str:
        if "find" in t: return str(t["find"].get("pattern",""))
        if "not_find" in t: return f"NOT {t['not_find'].get('pattern','')}"
        if "sequence" in t:
            seq=t.get("sequence",[]); pats=[n if isinstance(n,str) else n.get("pattern","") for n in seq]
            return " | ".join(pats)
        if "action" in t:
            a=t["action"]; at=str(a.get("type",""))
            if at=="screenshot":
                fn=a.get("file"); return f"[screenshot]{' → '+fn if fn else ''}"
            if at in ("wait","wait_capture"):
                ms=a.get("ms",""); return f"[{at} {ms}ms]"
            if at in ("tap","tap_pct"): return f"[{at}]"
            return f"[{at}]"
        return "(unknown)"


    # === CHUNK 8 — Actions ============================================
    def _run_pending_actions(self)->None:
        while self._running and self._action_ptr <= len(self.tests):
            i = self._action_ptr
            t = self.tests[i - 1]
            if t.get("_done"): self._action_ptr += 1; continue
            if "action" not in t: break
            name = t.get("name", f"Step {i}")
            vc   = self._describe_vc(t)
            ok, msg = self._perform_action(t["action"])
            t["_done"] = True
            self._emit(i, name, vc, ("PASS" if ok else "FAIL"), msg)
            self._action_ptr += 1

    def _perform_action(self,action:Dict[str,Any])->Tuple[bool,str]:
        at = str((action or {}).get("type","")).lower()

        # WAIT
        if at == "wait":
            ms = int(action.get("ms", 0))
            end_t = _now_s() + (ms / 1000.0)
            while self._running and _now_s() < end_t:
                self._drain_discard_once(); time.sleep(WAIT_DRAIN_TICK_S)
            return True, f"wait {ms}ms"

        # WAIT_CAPTURE
        if at == "wait_capture":
            ms = int(action.get("ms", 0))
            end_t = _now_s() + (ms / 1000.0)
            captured: List[str] = []
            while self._running and _now_s() < end_t:
                captured.extend(self._drain_collect_once()); time.sleep(WAIT_DRAIN_TICK_S)
            if captured: self._prefeed_lines = captured + self._prefeed_lines
            return True, f"wait_capture {ms}ms ({len(captured)} lines)"

        # SCREENSHOT
        if at == "screenshot":
            shots_dir = os.path.join(self.out_dir, "Screenshots")
            os.makedirs(shots_dir, exist_ok=True)
            fn = action.get("file") or f"shot_{datetime.datetime.now():%Y-%m-%d_%H-%M-%S}.png"
            fn = os.path.basename(str(fn)); out_path = os.path.join(shots_dir, fn)
            if ANDROID_OK:
                try:
                    droid.ensure_server(); ser = droid.get_default_device_serial()
                    if not ser: return False, "No Android device"
                    ok = droid.screencap_png_to(out_path, serial=ser) \
                         if hasattr(droid, "screencap_png_to") else _compat_screencap_to(out_path)
                    if not ok or not os.path.exists(out_path): return False, f"screenshot failed: {out_path}"
                    return True, f"{out_path}"
                except Exception as e:
                    return False, f"screenshot error: {e}"
            else:
                ok = _AdbDirect("adbb").screencap_png_to(out_path)
                return (ok, out_path if ok else "screenshot failed")

        # TAP
        if at == "tap":
            x = int(action.get("x", -1)); y = int(action.get("y", -1))
            if x < 0 or y < 0: return False, "tap needs x/y"
            if ANDROID_OK:
                try:
                    droid.ensure_server(); ser = droid.get_default_device_serial()
                    if not ser: return False, "No Android device"
                    ok = droid.input_tap(x, y, serial=ser)
                    return ok, f"tap({x},{y}) {'ok' if ok else 'fail'}"
                except Exception as e:
                    return False, f"tap error: {e}"
            else:
                ok = _AdbDirect("adbb").tap(x, y); return ok, f"tap({x},{y}) {'ok' if ok else 'fail'}"

        # TAP_PCT
        if at == "tap_pct":
            px = float(action.get("px", -1)); py = float(action.get("py", -1))
            if not (0 <= px <= 1 and 0 <= py <= 1): return False, "tap_pct needs px/py in [0..1]"
            try:
                if ANDROID_OK:
                    droid.ensure_server(); ser = droid.get_default_device_serial()
                    if not ser: return False, "No Android device"
                    # try device size
                    w,h = 0,0
                    try:
                        wh = droid.device_wm_size(serial=ser)
                        if wh: w,h = wh
                    except Exception:
                        w=h=0
                    if w<=0 or h<=0:
                        w,h = self._guess_wm_size()
                    if w<=0 or h<=0: return False, "unknown device size"
                    x = int(round(px * w)); y = int(round(py * h))
                    ok = droid.input_tap(x, y, serial=ser)
                    return ok, f"tap_pct({px:.2f},{py:.2f})=>({x},{y}) {'ok' if ok else 'fail'}"
                else:
                    w,h = self._guess_wm_size()
                    if w<=0 or h<=0: return False, "unknown device size"
                    x = int(round(px * w)); y = int(round(py * h))
                    ok = _AdbDirect("adbb").tap(x, y)
                    return ok, f"tap_pct({px:.2f},{py:.2f})=>({x},{y}) {'ok' if ok else 'fail'}"
            except Exception as e:
                return False, f"tap_pct error: {e}"

        return False, f"unknown action: {at}"

    # Wait drain helpers
    def _drain_discard_once(self) -> None:
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
        finally:
            try: self._sock.setblocking(True)
            except Exception: pass

    def _drain_collect_once(self) -> List[str]:
        out: List[str] = []
        if not self._sock: return out
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
                if not data: break
                local += data.decode("utf-8", errors="ignore")
                while True:
                    p = local.find("\n")
                    if p < 0: break
                    one = local[:p]; local = local[p+1:]
                    out.append(one.rstrip("\r"))
            if local: self._buf += local
        finally:
            try: self._sock.setblocking(True)
            except Exception: pass
        return out

    def _guess_wm_size(self) -> Tuple[int,int]:
        try:
            out = subprocess.check_output(["adbb","shell","wm","size"],
                                          stderr=subprocess.STDOUT, timeout=1.5).decode("utf-8","ignore")
            for ln in out.splitlines():
                if ":" in ln and "x" in ln:
                    tail=ln.split(":",1)[1].strip()
                    w,h=tail.split("x"); return (int(w),int(h))
        except Exception: pass
        return (0,0)


# === CHUNK 9 — Android compat screencap helper =======================
def _compat_screencap_to(out_path: str) -> bool:
    try:
        tmp_dir = os.path.dirname(out_path) or "."
        ser = droid.get_default_device_serial()
        tmp = droid.screencap_png(tmp_dir, prefix="__tmp__", serial=ser)
        if not tmp or not os.path.exists(tmp): return False
        import shutil
        try: shutil.move(tmp, out_path)
        except Exception:
            with open(tmp, "rb") as src, open(out_path, "wb") as dst: dst.write(src.read())
            try: os.remove(tmp)
            except Exception: pass
        return True
    except Exception:
        return False

# =====================================================================
# End of file
# =====================================================================
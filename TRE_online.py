 # =====================================================================
# TRE_online.py — Online Runner / Engine  •  Version 2.3.1p (Parser Focus)
# Test Run Engine (TRE)
#
# Extended feature set:
#  - Payload-only parser & logger (CHUNK P)
#  - Optional LOG_ONLY_MODE (no matching, just payload logging)
#  - Optional normalization + token filters
#  - Sequential verification preserved
#  - Actions: wait, wait_capture, screenshot, tap, tap_pct
#  - Stop: immediate (socket shutdown)
#  - Auto reconnect (configurable)
# =====================================================================

from __future__ import annotations
import os, socket, time, datetime, traceback, subprocess, collections
from typing import Dict, Any, List, Optional, Callable, Tuple, Deque
from collections import deque

import TRE_ai as ai
import TRE_json as tre
try:
    import TRE_android as droid
    ANDROID_OK = True
except Exception:
    ANDROID_OK = False

# --- Paths ------------------------------------------------------------
ROOT_DIR    = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
LOGS_DIR    = os.path.join(ROOT_DIR, "Logs")
REPORTS_DIR = os.path.join(ROOT_DIR, "Reports")
os.makedirs(LOGS_DIR, exist_ok=True)
os.makedirs(REPORTS_DIR, exist_ok=True)
AI_LOG_FILE = os.path.join(LOGS_DIR, "ai_debug.log")

MATCH_DEBUG = True
DEBUG_STEPS = {7}            # only step 7’s FIND/SEQ traces
DEBUG_NAME_CONTAINS = []     # or ["Processing"] if you prefer by name
DEBUG_VERBOSE_RAW = False

WIRE_TAP_FILE    = os.path.join(LOGS_DIR, "wire_tap.log")
MATCH_LOG_FILE   = os.path.join(LOGS_DIR, "match_debug.log")
PAYLOAD_TAP_FILE = os.path.join(LOGS_DIR, "payload_tap.log")

# --- Network / Engine Tunables ---------------------------------------
CONNECT_TIMEOUT_SEC    = 5.0
RECV_BLOCK_BYTES       = 65536
RETRY_MAX_ATTEMPTS     = 5
RETRY_BACKOFF_BASE_S   = 0.5
SETTLE_DELAY_S         = 3.0
RING_BUFFER_MAX_LINES  = 20000
STEP1_TIMEOUT_S        = 120.0
DEFAULT_STEP_TIMEOUT_S = 0.0     # others default to no-timeout unless test overrides
IDLE_TICK_S            = 0.20
WAIT_DRAIN_TICK_S      = 0.05

# --- Matching / Debug / Behavior Toggles ------------------------------
FORCE_PAYLOAD_ONLY   = True       # force payload matching in ONLINE
MATCH_DEBUG          = True
DEBUG_STEPS: set[int]= set()      # e.g. {7}
DEBUG_NAME_CONTAINS  = []         # e.g. ["Processing"]
DEBUG_VERBOSE_RAW    = False
HISTORY_MAX_PER_STEP = 200        # per-step payload history (for debug)
AUTO_RECONNECT       = True       # reconnect when TCP drops
RECONNECT_MAX        = 3
RECONNECT_DELAY_S    = 1.0

def _now_s() -> float: return time.monotonic()

class LineRing:
    def __init__(self, max_lines: int = RING_BUFFER_MAX_LINES):
        self._dq: Deque[str] = collections.deque(maxlen=max_lines)
    def append(self, line: str) -> None: self._dq.append(line)
    def drain(self) -> List[str]: out = list(self._dq); self._dq.clear(); return out
    def __len__(self): return len(self._dq)

def _safe_open(path, mode):
    try: return open(path, mode, encoding="utf-8", errors="ignore")
    except Exception: return None

wire_fh     = _safe_open(WIRE_TAP_FILE, "a")
_match_fh   = _safe_open(MATCH_LOG_FILE, "a") if MATCH_DEBUG else None
_payload_fh = _safe_open(PAYLOAD_TAP_FILE, "a")

# ---------- CHUNK P: Robust payload cleanup, logging, and LOG-ONLY mode -----

# Behavior toggles
FORCE_PAYLOAD_ONLY           = True     # online: match payload only
VERIFY_SEQUENTIAL            = True     # strict order execution
MATCH_DEBUG                  = True     # write match_debug.log
LOG_ONLY_MODE                = False    # <-- set True to stream & log only (no matching)

# Logging controls
SANITIZE_NON_PRINTABLE       = True
NORMALIZE_PAYLOAD_WHITESPACE = True
PAYLOAD_LOG_FILTER_ANY       = []       # e.g. ["UCM_CHK", "OTA"]

# Known junk tokens occasionally leaking from headers
DENY_TOKENS                  = {"TELETELE", "AOTA"}

import re as _re

# Precompiled cleaners
_RX_REPEAT_ALLCAPS   = _re.compile(r"\b([A-Z]{3,10})(?:\1)+\b")     # OTAOTA->OTA
_RX_EQUALS_TAIL      = _re.compile(r"=\s*[A-Z]{2,10}\d*[a-z]?\b")   # =CCU2x, =ABC123x
_RX_MULTI_WS         = _re.compile(r"\s+")
_RX_STRIP_CONTROL    = _re.compile(r"[^\x20-\x7E\t ]+")             # keep printable + tab + space

# --- keep near CHUNK P ---

def _sanitize_payload(payload: str) -> str:
    """Clean payload for both logging and matching (single line, readable)."""
    if not payload:
        return payload

    # 1) flatten CR/LF to enforce one physical line per payload
    payload = payload.replace("\r", " ").replace("\n", " ")

    # 2) strip non-printables
    payload = "".join(ch for ch in payload if (32 <= ord(ch) <= 126) or ch in "\t ")

    # 3) optional: remove known junk tokens
    for tok in {"TELETELE", "AOTA", "CCU2s", "CCU2c"}:
        payload = payload.replace(tok, " ")


    # 4) collapse occasional repeated ALLCAPS (OTAOTA -> OTA)
    import re as _re
    payload = _re.sub(r"\b([A-Z]{3,10})(?:\1)+\b", r"\1", payload)

    # 5) strip header-ish tails like =CCU2x / =ABC123x
    payload = _re.sub(r"=\s*[A-Z]{2,10}\d*[a-z]?\b", " ", payload)

    # 6) normalize whitespace
    payload = _re.sub(r"\s+", " ", payload).strip()
    return payload


def _log_payload_line(payload: str) -> None:
    """Append the CLEANED single-line payload to match_debug.log."""
    if not payload or not _match_fh:
        return
    try:
        _match_fh.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {payload}\n")
        _match_fh.flush()
    except Exception:
        pass

# ---------- END CHUNK P ------------------------------------------------------




# =====================================================================
# Android fallback (adbb) — minimal -----------------------------------
# =====================================================================
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

# =====================================================================
# SESSION MANAGER
# =====================================================================
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
        self._sock=None; self._running=False; self._paused=False
        self._buf=""; self._ring=LineRing(); self._matching_enabled=False
        self._prefeed_lines: List[str] = []
        self._action_ptr=1
        self._dev_w=0; self._dev_h=0; self._adb=_AdbDirect("adbb")
        self._hist: Dict[int, Deque[str]] = {}
        self._counts: Dict[int, int] = {}
        self._payload_history = deque(maxlen=5000)  # keep last N cleaned payloads


    # ---- Payload logger wrapper --------------------------------------
    def _log_payload(self, payload: str) -> None:
        _log_payload_line(payload)

    # ---- Lifecycle ---------------------------------------------------
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

    # ---- Start -------------------------------------------------------
    def start(self):
        # reset state
        for t in self.tests:
            t.pop("_done",None); t.pop("_seq_idx",None); t.pop("_t0", None); t.pop("_count", None)
        self._ring=LineRing(); self._matching_enabled=False; self._action_ptr=1
        self._hist.clear(); self._counts.clear()

        # connect with retries
        s=self._connect_with_retries()
        if not s: self.on_status("Failed to connect."); return
        self._sock=s; self._running=True; self.on_status("Connected.")

        # init steps
        now=time.monotonic()
        for i,t in enumerate(self.tests, start=1):
            t["_done"]=False; t["_seq_idx"]=0; t["_t0"]=now; t["_count"]=0
            self._hist[i]=collections.deque(maxlen=HISTORY_MAX_PER_STEP)
            self._counts[i]=0
        steps_info=[{"idx":i,"name":t.get("name",f"Step {i}"),"vc":self._describe_vc(t)}
                    for i,t in enumerate(self.tests,1)]
        try: self.on_steps_init(steps_info)
        except Exception: pass

        # settle boot lines
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
                # prefeed from wait_capture first
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

                # timeouts + actions
                self._check_timeouts()
                self._run_pending_actions()
                # Catch-up: if the next step was already satisfied by earlier payloads, pass it now
                cur = self._first_unfinished_idx()
                if cur is not None:
                    self._scan_history_for_step(cur)


                # done?
                if self._all_done(): self._running=False
                time.sleep(IDLE_TICK_S)

        except Exception as e:
            self.on_status(f"Runner error: {e}\n{traceback.format_exc()}")
        finally:
            try: self._finalize_unfinished("stopped" if not self._running else "ended")
            except Exception: pass
            # flush logs
            for fh in (wire_fh,_match_fh,_payload_fh):
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
            # ensure payload debug flushed
            try:
                if _match_fh: _match_fh.flush()
            except Exception: pass
            
                        # --- AI RCA summary after run (if anything failed) ---
            try:
                if ai.is_enabled():
                    failed = []
                    for i, t in enumerate(self.tests, start=1):
                        res = t.get("_final_result")
                        if res and res != "PASS":
                            # include index so RCA can refer to it
                            t_summary = {
                                "_idx": i,
                                "name": t.get("name", f"Step {i}"),
                                "find": t.get("find"),
                                "not_find": t.get("not_find"),
                                "sequence": t.get("sequence"),
                                "action": t.get("action"),
                                "_final_result": res,
                                "_final_line": t.get("_final_line"),
                            }
                            failed.append(t_summary)

                    if failed:
                        # gather sanitized history per step (if you keep _hist)
                        histories = {}
                        try:
                            histories = {i: list(self._hist.get(i, [])) for i in range(1, len(self.tests)+1)}
                        except Exception:
                            histories = {}

                        rca = ai.rca_summary(failed, histories)
                        _ai_log("\n==== RCA SUMMARY ====\n" + rca + "\n=====================\n")
                        self.on_status("[AI] RCA summary written to ai_debug.log")
            except Exception:
                pass


    # ---- Core parsing / dispatch ------------------------------------
    def _process_line_dispatch(self, line: str) -> None:
        # 1) parse and sanitize payload
        try:
            _, _, _, payload_raw = tre.parse_dlt(line)
        except Exception:
            payload_raw = line
        payload = tre.sanitize_payload(payload_raw)

        # 2) log payload-only (one clean line) for your investigation
        if _payload_fh:
            try:
                _payload_fh.write(payload + "\n")
                _payload_fh.flush()
            except Exception:
                pass

        if DEBUG_VERBOSE_RAW:
            self._dbg(f"[RAW] line='{line[:220]}'")
            self._dbg(f"[PAYLOAD] '{payload}'")

        # 3) choose matching mode
        if VERIFY_SEQUENTIAL:
            self._process_line_sequential(line, payload)
        else:
            self._process_line_cumulative(line, payload)





    # ---- Connection helpers -----------------------------------------
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


 # ---- Sequential verifier (strict order) --------------------------
    def _first_unfinished_idx(self) -> Optional[int]:
        for i, t in enumerate(self.tests, start=1):
            if not t.get("_done"):
                return i
        return None

    def _process_line_sequential(self, line: str, payload: str) -> None:
        idx = self._first_unfinished_idx()
        if idx is None: return
        t = self.tests[idx - 1]
        name = t.get("name", f"Step {idx}")
        vc   = self._describe_vc(t)
        # keep payload history
        self._hist[idx].append(payload)

        # ACTION?
        if "action" in t:
            ok,msg=self._perform_action(t["action"])
            t["_done"]=True; self._emit(idx,name,vc,("PASS" if ok else "FAIL"),msg)
            return

        # FIND
        if "find" in t:
            if self._try_find(idx, t["find"], payload, name):
                t["_done"]=True; self._emit(idx,name,vc,"PASS",payload)
            return

        # NOT_FIND
        if "not_find" in t:
            cfg = self._normalize_cfg(t["not_find"])
            if tre.line_matches(payload, cfg):
                t["_done"]=True; self._emit(idx,name,vc,"FAIL",payload)
            return

        # SEQUENCE
        if "sequence" in t:
            done = self._try_sequence(idx, t, payload, name)
            if done:
                t["_done"]=True; self._emit(idx,name,vc,"PASS",payload)
            return

        # Unknown
        t["_done"]=True; self._emit(idx,name,vc,"ERROR","Unknown rule")
    
    
    def _scan_history_for_step(self, idx: int) -> bool:
        """Try to satisfy step idx from buffered payload history (for sequential catch-up)."""
        if idx < 1 or idx > len(self.tests):
            return False

        t    = self.tests[idx - 1]
        name = t.get("name", f"Step {idx}")
        vc   = self._describe_vc(t)

        # Actions aren't history-matched
        if "action" in t:
            return False

        # FIND
        if "find" in t:
            cfg = self._normalize_cfg(t["find"])
            for pl in self._payload_history:
                if tre.line_matches(pl, cfg):
                    t["_done"] = True
                    self._emit(idx, name, vc, "PASS", pl)
                    return True
            return False

        # NOT_FIND: history can only prove FAIL (if forbidden exists)
        if "not_find" in t:
            cfg = self._normalize_cfg(t["not_find"])
            for pl in self._payload_history:
                if tre.line_matches(pl, cfg):
                    t["_done"] = True
                    self._emit(idx, name, vc, "FAIL", pl)
                    return True
            return False

        # SEQUENCE (keeps advancing until fully satisfied)
        if "sequence" in t:
            seq = t.get("sequence", []) or []
            if not seq:
                t["_done"] = True
                self._emit(idx, name, vc, "PASS", "")
                return True

            prog = t.setdefault("_seq_idx", 0)
            while prog < len(seq):
                node = seq[prog]
                c = node if isinstance(node, dict) else {"pattern": str(node), "literal": True}
                c = self._normalize_cfg(c)
                # see if any history entry matches this node
                hit = any(tre.line_matches(pl, c) for pl in self._payload_history)
                if not hit:
                    return False
                prog += 1
                t["_seq_idx"] = prog

            # whole sequence satisfied
            t["_done"] = True
            # provide last matching line if convenient (optional)
            last_line = next((pl for pl in reversed(self._payload_history)
                              if tre.line_matches(pl, self._normalize_cfg(seq[-1] if isinstance(seq[-1], dict)
                                                                          else {"pattern": str(seq[-1]), "literal": True}))), "")
            self._emit(idx, name, vc, "PASS", last_line)
            return True

        # Unknown rule
        t["_done"] = True
        self._emit(idx, name, vc, "ERROR", "Unknown rule")
        return True

    
    # ---- Cumulative verifier (kept for compatibility) ----------------
    def _process_line_cumulative(self, line: str, payload: str) -> None:
        for idx0, t in enumerate(self.tests):
            if t.get("_done"): continue
            idx = idx0 + 1
            name = t.get("name", f"Step {idx}")
            vc   = self._describe_vc(t)
            self._hist[idx].append(payload)

            if "action" in t:
                continue  # actions run via _run_pending_actions()

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

    # ---- Match helpers -----------------------------------------------
    def _normalize_cfg(self, cfg: Dict[str,Any]) -> Dict[str,Any]:
        c = dict(cfg or {})
        # enforce payload-only unless caller already opted out
        c["payload_only"] = True if FORCE_PAYLOAD_ONLY else c.get("payload_only", True)
        return c

    def _try_find(self, idx:int, cfg:Dict[str,Any], payload:str, name:str) -> bool:
        c = self._normalize_cfg(cfg)
        need  = int(c.get("min_count", 1))
        maxi  = int(c.get("max_count", need))
        if DEBUG_VERBOSE_RAW or idx in DEBUG_STEPS or (DEBUG_NAME_CONTAINS and any(s.lower() in name.lower() for s in DEBUG_NAME_CONTAINS)):
            if _match_fh:
                try:
                    _match_fh.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] [FIND] step#{idx} '{name}' need={need} have={self._counts.get(idx,0)} pat='{c.get('pattern')}' literal={c.get('literal')} ignore_case={c.get('ignore_case')} payload_only={c.get('payload_only')} | line='{payload[:220]}'\n")
                    _match_fh.flush()
                except Exception: pass
        if tre.line_matches(payload, c):
            self._counts[idx] = self._counts.get(idx, 0) + 1
            have = self._counts[idx]
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
        if DEBUG_VERBOSE_RAW or idx in DEBUG_STEPS or (DEBUG_NAME_CONTAINS and any(s.lower() in name.lower() for s in DEBUG_NAME_CONTAINS)):
            if _match_fh:
                try:
                    _match_fh.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] [SEQ] step#{idx} '{name}' prog={prog}/{len(seq)} pat='{c.get('pattern')}' literal={c.get('literal')} ignore_case={c.get('ignore_case')} payload_only={c.get('payload_only')} | line='{payload[:220]}'\n")
                    _match_fh.flush()
                except Exception: pass
        if tre.line_matches(payload, c):
            t["_seq_idx"] = prog + 1
            return t["_seq_idx"] >= len(seq)
        return False

    # ---- Timeouts / finalize -----------------------------------------
    def _current_timeout_s(self, idx:int, t:Dict[str,Any])->float:
        if isinstance(t.get("timeout"), (int, float)) and t["timeout"] >= 0:
            return float(t["timeout"])
        return STEP1_TIMEOUT_S if idx == 1 else DEFAULT_STEP_TIMEOUT_S

    def _check_timeouts(self)->None:
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

    # ---- Emit / describe ---------------------------------------------
    def _emit(self,idx:int,name:str,vc:str,result:str,line:Optional[str]):
        try:
            if self.on_step_update: self.on_step_update(idx,name,vc,result,line)
            t = self.tests[idx-1]
            t["_final_result"] = result
            t["_final_line"]   = line
        except Exception: pass
        
                # --- AI debug on failure (non-blocking; safe to ignore errors) ---
        try:
            if result == "FAIL" and ai.is_enabled():
                # build a minimal step dict for context (pattern + name)
                step = {
                    "name": name,
                    "find": None,
                    "not_find": None,
                    "sequence": None
                }
                # try to attach whichever exists on the real test
                try:
                    t = self.tests[idx - 1]
                    for k in ("find", "not_find", "sequence", "action"):
                        if k in t: step[k] = t.get(k)
                except Exception:
                    pass

                # collect recent sanitized payloads for this step
                samples = list(self._hist.get(idx, []))[-30:] if hasattr(self, "_hist") else []

                # 1) narrow down relevant lines
                rel = ai.select_relevant_payload(step, samples, max_lines=12)
                if rel:
                    _ai_log(f"[step#{idx} {name}] relevant lines:")
                    for r in rel:
                        _ai_log(f"  • {r}")

                # 2) concise why-failed + suggested pattern
                why = ai.explain_failure(step, rel or samples)
                _ai_log(f"[step#{idx} {name}] explain:\n{why}")

                # also surface a short status in UI footer
                self.on_status(f"[AI] Step#{idx} '{name}' failed — see ai_debug.log")
        except Exception:
            # never break the run if AI/logging fails
            pass


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
            if at in ("tap","tap_pct"):
                return f"[{at}]"
            return f"[{at}]"
        return "(unknown)"

    # ---- Actions (sequential via _run_pending_actions) ----------------
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
                ok = self._adb.screencap_png_to(out_path)
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
                ok = self._adb.tap(x, y); return ok, f"tap({x},{y}) {'ok' if ok else 'fail'}"

        # TAP_PCT
        if at == "tap_pct":
            px = float(action.get("px", -1)); py = float(action.get("py", -1))
            if not (0 <= px <= 1 and 0 <= py <= 1): return False, "tap_pct needs px/py in [0..1]"
            try:
                if ANDROID_OK:
                    droid.ensure_server(); ser = droid.get_default_device_serial()
                    if not ser: return False, "No Android device"
                    if self._dev_w <= 0 or self._dev_h <= 0:
                        wh = droid.device_wm_size(serial=ser); 
                        if wh: self._dev_w, self._dev_h = wh
                    if self._dev_w <= 0 or self._dev_h <= 0: return False, "unknown device size"
                    x = int(round(px * self._dev_w)); y = int(round(py * self._dev_h))
                    ok = droid.input_tap(x, y, serial=ser)
                    return ok, f"tap_pct({px:.2f},{py:.2f})=>({x},{y}) {'ok' if ok else 'fail'}"
                else:
                    # fallback cannot compute pct without size; keep simple (optional)
                    return False, "tap_pct unavailable without device size"
            except Exception as e:
                return False, f"tap_pct error: {e}"

        return False, f"unknown action: {at}"

    # ---- Wait drain helpers ------------------------------------------
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

# ---- Compat helper for older TRE_android without screencap_png_to ----
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
# AI
# =====================================================================
def _ai_log(msg: str) -> None:
    try:
        with open(AI_LOG_FILE, "a", encoding="utf-8", errors="ignore") as fh:
            ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            fh.write(f"[{ts}] {msg}\n")
    except Exception:
        pass
# =====================================================================
# End of file
# =====================================================================

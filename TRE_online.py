# =====================================================================
# TRE_online.py — Online Runner / Engine  •  Version 2.4.0
# Test Run Engine (TRE)
# =====================================================================

from __future__ import annotations
import os, socket, time, datetime, traceback, subprocess, collections
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
MATCH_LOG_FILE   = os.path.join(LOGS_DIR, "match_debug.log")
PAYLOAD_TAP_FILE = os.path.join(LOGS_DIR, "payload_tap.log")
AI_LOG_FILE      = os.path.join(LOGS_DIR, "ai_debug.log")

def _safe_open(path, mode):
    try:
        return open(path, mode, encoding="utf-8", errors="ignore")
    except Exception:
        return None

wire_fh     = _safe_open(WIRE_TAP_FILE, "a")
_match_fh   = _safe_open(MATCH_LOG_FILE, "a")
_payload_fh = _safe_open(PAYLOAD_TAP_FILE, "a")
_ai_fh      = _safe_open(AI_LOG_FILE, "a")

def _ai_log(msg: str) -> None:
    if not _ai_fh:
        return
    try:
        _ai_fh.write(msg + ("\n" if not msg.endswith("\n") else ""))
        _ai_fh.flush()
    except Exception:
        pass

# === CHUNK 1 — Engine tunables ======================================
CONNECT_TIMEOUT_SEC    = 5.0
RECV_BLOCK_BYTES       = 65536
RETRY_MAX_ATTEMPTS     = 5
RETRY_BACKOFF_BASE_S   = 0.5
SETTLE_DELAY_S         = 3.0
RING_BUFFER_MAX_LINES  = 20000

STEP1_TIMEOUT_S        = 120.0
DEFAULT_STEP_TIMEOUT_S = 0.0   # default: no auto timeout unless specified
TIMEOUT_TICK_INTERVAL  = 0.5

IDLE_TICK_S            = 0.20
WAIT_DRAIN_TICK_S      = 0.05

FORCE_PAYLOAD_ONLY     = True
VERIFY_SEQUENTIAL      = True
LOG_ONLY_MODE          = False

MATCH_DEBUG            = True
DEBUG_STEPS: set[int]  = set()
DEBUG_NAME_CONTAINS: List[str] = []
DEBUG_VERBOSE_RAW      = False
HISTORY_MAX_PER_STEP   = 200
AUTO_RECONNECT         = True
RECONNECT_MAX          = 3
RECONNECT_DELAY_S      = 1.0

def _now_s() -> float:
    return time.monotonic()

# === CHUNK 2 — LineRing =============================================
class LineRing:
    def __init__(self, max_lines: int = RING_BUFFER_MAX_LINES):
        self._dq: Deque[str] = collections.deque(maxlen=max_lines)
    def append(self, line: str) -> None:
        self._dq.append(line)
    def drain(self) -> List[str]:
        out = list(self._dq); self._dq.clear(); return out
    def __len__(self):
        return len(self._dq)

# === CHUNK 3 — ADB fallback =========================================
class _AdbDirect:
    def __init__(self, exe: str = "adbb"):
        self.exe = exe
    def tap(self, x:int, y:int)->bool:
        for cmd in ([self.exe,"t",str(x),str(y)],
                    [self.exe,"shell","input","tap",str(x),str(y)]):
            try:
                r=subprocess.run(cmd,timeout=1.5,
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
                if r.returncode==0: return True
            except Exception:
                continue
        return False
    def screencap_png_to(self,out_file:str)->bool:
        try:
            with open(out_file,"wb") as f:
                r=subprocess.run([self.exe,"exec-out","screencap","-p"],
                                 timeout=4.0,stdout=f,stderr=subprocess.DEVNULL)
            return r.returncode==0 and os.path.exists(out_file) and os.path.getsize(out_file)>0
        except Exception:
            return False

# === CHUNK 4 — SessionManager =======================================
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
        self._buf=""; self._ring=LineRing()
        self._matching_enabled=False
        self._prefeed_lines: List[str] = []
        self._payload_history: Deque[str] = deque(maxlen=5000)
        self._action_ptr=1
        self._dev_w=0; self._dev_h=0; self._adb=_AdbDirect("adbb")
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
        if not _match_fh:
            return
        try:
            _match_fh.write(f"[{datetime.datetime.now():%Y-%m-%d %H:%M:%S}] {msg}\n")
            _match_fh.flush()
        except Exception:
            pass

    # ---- Lifecycle
    def pause(self):
        self._paused=True;  self.on_status("Paused.")
    def resume(self):
        self._paused=False; self.on_status("Resumed.")
    def stop(self):
        self._running=False; self._paused=False
        self._buf=""; self._ring.drain(); self._prefeed_lines.clear()
        try:
            if self._sock:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except Exception:
                    pass
                try:
                    self._sock.close()
                except Exception:
                    pass
        finally:
            self._sock=None

    # ---- Start
    def start(self):
        for t in self.tests:
            for k in ("_done","_seq_idx","_t0","_count","_final_result","_final_line"):
                t.pop(k,None)
        self._ring=LineRing(); self._matching_enabled=False; self._action_ptr=1
        self._hist.clear(); self._counts.clear(); self._payload_history.clear()

        s=self._connect_with_retries()
        if not s:
            self.on_status("Failed to connect.")
            return
        self._sock=s; self._running=True; self.on_status("Connected.")

        now=time.monotonic()
        for i,t in enumerate(self.tests, start=1):
            t.update({"_done":False,"_seq_idx":0,"_t0":now,"_count":0})
            self._hist[i]=collections.deque(maxlen=HISTORY_MAX_PER_STEP)
            self._counts[i]=0

        steps_info=[{"idx":i,"name":t.get("name",f"Step {i}"),"vc":self._describe_vc(t)} for i,t in enumerate(self.tests,1)]
        try:
            self.on_steps_init(steps_info)
        except Exception:
            pass

        settle_until=_now_s()+SETTLE_DELAY_S
        self.on_status(f"Settling {SETTLE_DELAY_S:.1f}s…")
        try:
            while self._running and _now_s()<settle_until:
                self._drain_to_ring(); time.sleep(IDLE_TICK_S)
        except Exception as e:
            self.on_status(f"Settle error: {e}")

        pre_lines=self._ring.drain()
        self.on_status(f"Feeding {len(pre_lines)} buffered lines…")
        for ln in pre_lines:
            if not self._running: break
            self._process_line_dispatch(ln)

        self._matching_enabled=True
        self.on_status("Live matching started.")
        disconnects=0
        last_timeout_check=_now_s()

        try:
            while self._running:
                # prefeed (wait_capture)
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
                            try:
                                wire_fh.write(raw+"\n"); wire_fh.flush()
                            except Exception:
                                pass
                        if self._paused:
                            self._ring.append(raw); continue
                        self._process_line_dispatch(raw)

                self._run_pending_actions()
                cur=self._first_unfinished_idx()
                if cur is not None:
                    self._scan_history_for_step(cur)

                # periodic timeouts check
                if _now_s()-last_timeout_check>TIMEOUT_TICK_INTERVAL:
                    self._check_timeouts(); last_timeout_check=_now_s()

                if self._all_done():
                    self._running=False
                time.sleep(IDLE_TICK_S)

        except Exception as e:
            self.on_status(f"Runner error: {e}\n{traceback.format_exc()}")
        finally:
            try:
                self._finalize_unfinished("stopped" if not self._running else "ended")
            except Exception:
                pass
            for fh in (wire_fh,_match_fh,_payload_fh,_ai_fh):
                try:
                    if fh: fh.flush()
                except Exception:
                    pass
            try:
                if self._sock:
                    try:
                        self._sock.shutdown(socket.SHUT_RDWR)
                    except Exception:
                        pass
                    self._sock.close()
            except Exception:
                pass
            self._sock=None; self._running=False; self._paused=False
            self.on_status("Stopped.")

            # Optional AI RCA
            try:
                if hasattr(ai,"is_enabled") and ai.is_enabled():
                    failed=[]
                    for i,t in enumerate(self.tests, start=1):
                        res=t.get("_final_result")
                        if res and res!="PASS":
                            failed.append({
                                "_idx":i,"name":t.get("name",f"Step {i}"),
                                "find":t.get("find"),"not_find":t.get("not_find"),
                                "sequence":t.get("sequence"),"action":t.get("action"),
                                "_final_result":res,"_final_line":t.get("_final_line"),
                            })
                    if failed:
                        histories={i:list(self._hist.get(i,[])) for i in range(1,len(self.tests)+1)}
                        rca=ai.rca_summary(failed,histories)
                        _ai_log("\n==== RCA SUMMARY ====\n"+rca+"\n=====================\n")
                        self.on_status("[AI] RCA summary written.")
            except Exception:
                pass

    # ---- Dispatch ---------------------------------------------------
    def _process_line_dispatch(self, line: str) -> None:
        try:
            _, _, _, payload_raw = tre.parse_dlt(line)
        except Exception:
            payload_raw = line
        payload = tre.sanitize_payload(payload_raw)

        self._payload_history.append(payload)
        if _payload_fh:
            try:
                _payload_fh.write(payload+"\n"); _payload_fh.flush()
            except Exception:
                pass

        if DEBUG_VERBOSE_RAW and self._dbg_on(None,None):
            self._dbg(f"[RAW] {line[:220]}")
            self._dbg(f"[PAYLOAD] {payload}")

        if LOG_ONLY_MODE:
            return
        if VERIFY_SEQUENTIAL:
            self._process_line_sequential(line,payload)
        else:
            self._process_line_cumulative(line,payload)

    # === Connect / I/O ==============================================
    def _connect_with_retries(self) -> Optional[socket.socket]:
        last_err=None
        for attempt in range(1,RETRY_MAX_ATTEMPTS+1):
            try:
                self.on_status(f"Connecting to {self.host}:{self.port} (attempt {attempt})…")
                s=socket.socket(socket.AF_INET,socket.SOCK_STREAM)
                s.settimeout(CONNECT_TIMEOUT_SEC)
                s.connect((self.host,self.port))
                s.settimeout(None)
                return s
            except Exception as e:
                last_err=e; delay=RETRY_BACKOFF_BASE_S*(2**(attempt-1))
                self.on_status(f"Connect failed: {e} — retrying in {delay:.1f}s")
                time.sleep(delay)
        self.on_status(f"Connect failed after {RETRY_MAX_ATTEMPTS} attempts: {last_err}")
        return None

    def _reconnect(self) -> Optional[socket.socket]:
        try:
            if self._sock: self._sock.close()
        except Exception:
            pass
        self._sock=None
        return self._connect_with_retries()

    def _recv_block(self) -> Optional[str]:
        if not self._sock:
            return None
        try:
            data=self._sock.recv(RECV_BLOCK_BYTES)
            if not data:
                return None
            return data.decode("utf-8","ignore")
        except (BlockingIOError,InterruptedError):
            return ""
        except OSError:
            return None
        except Exception:
            raise

    def _drain_to_ring(self) -> None:
        if not self._sock:
            return
        self._sock.setblocking(False)
        try:
            while True:
                try:
                    data=self._sock.recv(RECV_BLOCK_BYTES)
                except (BlockingIOError,InterruptedError,OSError):
                    break
                if not data:
                    break
                self._buf+=data.decode("utf-8","ignore")
                while True:
                    p=self._buf.find("\n")
                    if p<0: break
                    line=self._buf[:p]; self._buf=self._buf[p+1:]
                    self._ring.append(line.rstrip("\r"))
        finally:
            try:
                self._sock.setblocking(True)
            except Exception:
                pass

    # === Matching logic =============================================
    def _first_unfinished_idx(self)->Optional[int]:
        for i,t in enumerate(self.tests,start=1):
            if not t.get("_done"): return i
        return None

    def _normalize_cfg(self,cfg:Dict[str,Any])->Dict[str,Any]:
        c=dict(cfg or {}); c["payload_only"]=True; return c

    def _try_find(self,idx:int,cfg:Dict[str,Any],payload:str,name:str)->bool:
        c=self._normalize_cfg(cfg)
        need=int(c.get("min_count",1)); have=self._counts.get(idx,0)
        if self._dbg_on(idx,name):
            self._dbg(f"[FIND] step#{idx} need={need} have={have} pat='{c.get('pattern')}' | payload='{payload[:220]}'")
        if tre.line_matches(payload,c):
            have+=1; self._counts[idx]=have
            if self._dbg_on(idx,name): self._dbg(f"[FIND] match count={have}")
            if have>=need: return True
        return False

    def _try_sequence(self,idx:int,t:Dict[str,Any],payload:str,name:str)->bool:
        seq=t.get("sequence",[]) or []
        prog=t.setdefault("_seq_idx",0)
        if prog>=len(seq): return True
        node=seq[prog]; c=node if isinstance(node,dict) else {"pattern":str(node),"literal":True}
        c=self._normalize_cfg(c)
        if self._dbg_on(idx,name):
            self._dbg(f"[SEQ] step#{idx} prog={prog}/{len(seq)} pat='{c.get('pattern')}' | payload='{payload[:220]}'")
        if tre.line_matches(payload,c):
            t["_seq_idx"]=prog+1
            return t["_seq_idx"]>=len(seq)
        return False

    def _process_line_sequential(self,line:str,payload:str)->None:
        idx=self._first_unfinished_idx()
        if idx is None:
            return
        t=self.tests[idx-1]; name=t.get("name",f"Step {idx}"); vc=self._describe_vc(t)
        self._hist[idx].append(payload)
        if t.get("_done"):
            return  # guard double emit (race-safe)

        if "action" in t:
            ok,msg=self._perform_action(t["action"])
            t["_done"]=True; self._emit(idx,name,vc,("PASS" if ok else "FAIL"),msg)
            return

        if "find" in t:
            if self._try_find(idx,t["find"],payload,name):
                if not t.get("_done"):
                    t["_done"]=True; self._emit(idx,name,vc,"PASS",payload)
            return

        if "not_find" in t:
            cfg=self._normalize_cfg(t["not_find"])
            if tre.line_matches(payload,cfg):
                if not t.get("_done"):
                    t["_done"]=True; self._emit(idx,name,vc,"FAIL",payload)
            return

        if "sequence" in t:
            if self._try_sequence(idx,t,payload,name):
                if not t.get("_done"):
                    t["_done"]=True; self._emit(idx,name,vc,"PASS",payload)
            return

        # unknown
        t["_done"]=True; self._emit(idx,name,vc,"ERROR","Unknown rule")

    def _process_line_cumulative(self,line:str,payload:str)->None:
        for idx0,t in enumerate(self.tests):
            if t.get("_done"): continue
            idx=idx0+1; name=t.get("name",f"Step {idx}"); vc=self._describe_vc(t)
            self._hist[idx].append(payload)

            if "action" in t:
                continue  # actions executed by _run_pending_actions()

            if "find" in t:
                if self._try_find(idx,t["find"],payload,name):
                    t["_done"]=True; self._emit(idx,name,vc,"PASS",payload)
                continue

            if "not_find" in t:
                cfg=self._normalize_cfg(t["not_find"])
                if tre.line_matches(payload,cfg):
                    t["_done"]=True; self._emit(idx,name,vc,"FAIL",payload)
                continue

            if "sequence" in t:
                if self._try_sequence(idx,t,payload,name):
                    t["_done"]=True; self._emit(idx,name,vc,"PASS",payload)
                continue

            t["_done"]=True; self._emit(idx,name,vc,"ERROR","Unknown rule")

    # === Strict, safe catch-up from history ==========================
    def _scan_history_for_step(self, idx: int) -> None:
        if idx < 1 or idx > len(self.tests):
            return
        t=self.tests[idx-1]
        if t.get("_done"):
            return
        if "action" in t:
            return

        name=t.get("name",f"Step {idx}"); vc=self._describe_vc(t)

        def _norm(cfg: Dict[str,Any])->Dict[str,Any]:
            c=dict(cfg or {}); c["payload_only"]=True; return c

        for payload in list(self._payload_history):
            if t.get("_done"):
                return  # guard once emitted
            if
#!/usr/bin/env python3
# TRE_android.py — Android/HMI utilities for Test Run Engine
# Prefers 'adbb' (your renamed adb) and falls back to 'adb'. UI can override via config/field.
import os, sys, subprocess, shlex, json, time, re, tempfile, traceback
from typing import List, Dict, Any, Optional, Tuple
from xml.etree import ElementTree as ET

# ---------- ADB executable selection ----------
ADB_EXE = None  # resolved on first use or set via set_adb_executable()

def set_adb_executable(exepath: str):
    """Force a specific adb/adbb executable (absolute path or name on PATH)."""
    global ADB_EXE
    ADB_EXE = exepath

def get_adb_executable() -> str:
    """Resolve which ADB to use (config -> env -> adbb -> adb)."""
    global ADB_EXE
    if ADB_EXE:
        return ADB_EXE
    # 1) Read config in this folder
    try:
        _cfg = os.path.join(os.path.dirname(os.path.abspath(__file__)), "TRE_config.json")
        if os.path.isfile(_cfg):
            with open(_cfg, "r", encoding="utf-8") as f:
                data = json.load(f)
            exe = data.get("adb_exe")
            if exe:
                ADB_EXE = exe
                return ADB_EXE
    except Exception:
        pass
    # 2) Env var override
    exe_env = os.environ.get("TRE_ADB_EXE")
    if exe_env:
        ADB_EXE = exe_env
        return ADB_EXE
    # 3) Prefer 'adbb' then 'adb'
    ADB_EXE = "adbb" if os.name != "nt" else "adbb.exe"
    return ADB_EXE

def _run(cmd: List[str], timeout: int = 10):
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                           timeout=timeout, check=False, encoding="utf-8", errors="replace")
        return p.returncode, p.stdout.strip(), p.stderr.strip()
    except Exception as e:
        return 99, "", f"{type(e).__name__}: {e}"

def adb(args: List[str], timeout: int = 10):
    exe = get_adb_executable()
    return _run([exe] + args, timeout=timeout)

def adb_shell(cmdline: str, timeout: int = 10):
    return adb(["shell"] + shlex.split(cmdline), timeout=timeout)

def ensure_server():
    adb(["start-server"], timeout=5)

def adb_devices() -> List[Tuple[str, str]]:
    rc, out, err = adb(["devices"], timeout=6)
    if rc != 0: return []
    devs = []
    for ln in out.splitlines()[1:]:
        ln = ln.strip()
        if not ln: continue
        parts = ln.split()
        if len(parts) >= 2:
            devs.append((parts[0], parts[1]))
    return devs

def adb_connect(hostport: str):
    rc, out, err = adb(["connect", hostport], timeout=8)
    msg = out or err
    ok = ("connected" in (out.lower() if out else "") or
          "already connected" in (out.lower() if out else ""))
    return (ok or rc == 0), msg

def adb_disconnect(hostport: Optional[str] = None):
    args = ["disconnect"] + ([hostport] if hostport else [])
    rc, out, err = adb(args, timeout=6)
    return rc == 0, (out or err)

def get_default_device_serial() -> Optional[str]:
    devs = [d for d in adb_devices() if d[1] == "device"]
    return devs[0][0] if devs else None

# ---------- Basic device actions ----------
def device_wm_size(serial: Optional[str] = None) -> Optional[Tuple[int, int]]:
    args = ["-s", serial] if serial else []
    rc, out, err = _run([get_adb_executable()] + args + ["shell", "wm", "size"], timeout=6)
    if rc != 0: return None
    m = re.search(r"Physical size:\s*(\d+)\s*x\s*(\d+)", out)
    if not m:
        m = re.search(r"Override size:\s*(\d+)\s*x\s*(\d+)", out)
    return (int(m.group(1)), int(m.group(2))) if m else None

def input_tap(x: int, y: int, serial: Optional[str] = None) -> bool:
    args = ["-s", serial] if serial else []
    rc, _, _ = _run([get_adb_executable()] + args + ["shell", "input", "tap", str(x), str(y)], timeout=5)
    return rc == 0

def input_swipe(x1: int, y1: int, x2: int, y2: int, duration_ms: int = 300, serial: Optional[str] = None) -> bool:
    args = ["-s", serial] if serial else []
    rc, _, _ = _run([get_adb_executable()] + args + ["shell", "input", "swipe", str(x1), str(y1), str(x2), str(y2), str(duration_ms)], timeout=6)
    return rc == 0

def input_key(key: str, serial: Optional[str] = None) -> bool:
    args = ["-s", serial] if serial else []
    rc, _, _ = _run([get_adb_executable()] + args + ["shell", "input", "keyevent", key], timeout=5)
    return rc == 0

# ---------- Screenshots ----------
def screencap_png(out_dir: str, prefix: str = "shot", serial: Optional[str] = None) -> Optional[str]:
    """
    Fast screenshot using:  exec-out screencap -p
    Assumes device supports exec-out (your Option A works).
    """
    os.makedirs(out_dir, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    local_path = os.path.join(out_dir, f"{prefix}_{ts}.png")
    args = ["-s", serial] if serial else []

    try:
        p = subprocess.run([get_adb_executable()] + args + ["exec-out", "screencap", "-p"],
                           stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=15)
        if p.returncode == 0 and p.stdout:
            with open(local_path, "wb") as f:
                f.write(p.stdout)
            # Quick PNG sanity
            try:
                with open(local_path, "rb") as f:
                    ok = f.read(8) == b"\x89PNG\r\n\x1a\n"
                if ok and os.path.getsize(local_path) > 1024:
                    return local_path
            except Exception:
                pass
        # cleanup on failure
        try: os.remove(local_path)
        except: pass
    except Exception:
        pass
    return None


# ---------- UI Automator ----------
def dump_ui_xml(serial: Optional[str] = None, timeout: int = 8) -> Optional[str]:
    args = ["-s", serial] if serial else []
    rc, out, _ = adb(args + ["shell", "uiautomator", "dump"], timeout=timeout)
    if rc != 0: return None
    m = re.search(r"(?i)dump(?:ed)?\s+to:?\s+(\S+)", out or "")
    dump_path = m.group(1) if m else "/sdcard/window_dump.xml"
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xml")
    tmp.close()
    rc2, _, _ = adb(args + ["pull", dump_path, tmp.name], timeout=timeout)
    if rc2 == 0 and os.path.getsize(tmp.name) > 0:
        return tmp.name
    try: os.unlink(tmp.name)
    except: pass
    return None

def _parse_bounds(b: str) -> Optional[Tuple[int,int,int,int]]:
    m = re.match(r"\[(\d+),(\d+)\]\[(\d+),(\d+)\]", b or "")
    return (int(m.group(1)), int(m.group(2)), int(m.group(3)), int(m.group(4))) if m else None

def find_nodes(xml_path: str, text: Optional[str] = None, res_id: Optional[str] = None, desc: Optional[str] = None):
    nodes = []
    try:
        root = ET.parse(xml_path).getroot()
    except Exception:
        return nodes
    for n in root.iter():
        if n.tag.lower() != "node": continue
        at = n.attrib; ok = True
        if text is not None and text != at.get("text", ""): ok = False
        if res_id is not None and res_id != at.get("resource-id", ""): ok = False
        if desc is not None and desc != at.get("content-desc", ""): ok = False
        if ok:
            b = _parse_bounds(at.get("bounds", ""))
            if b: nodes.append({"attr": at, "bounds": b})
    return nodes

def bounds_center(b: Tuple[int,int,int,int]) -> Tuple[int,int]:
    x1,y1,x2,y2 = b; return (x1+x2)//2, (y1+y2)//2

def tap_first_match(serial: Optional[str] = None, text: Optional[str] = None, res_id: Optional[str] = None, desc: Optional[str] = None) -> bool:
    xml = dump_ui_xml(serial=serial)
    if not xml: return False
    try:
        nodes = find_nodes(xml, text=text, res_id=res_id, desc=desc)
        if not nodes: return False
        cx, cy = bounds_center(nodes[0]["bounds"])
        return input_tap(cx, cy, serial=serial)
    finally:
        try: os.unlink(xml)
        except: pass

# ---------- Popup rules ----------
def run_rules(rule_json_path: str, serial: Optional[str] = None, max_rounds: int = 5, delay_between: float = 0.8):
    with open(rule_json_path, "r", encoding="utf-8") as f:
        rules = json.load(f)
    if not isinstance(rules, list):
        raise ValueError("Rules JSON must be a list")
    log = []; rounds = 0
    while rounds < max_rounds:
        rounds += 1; applied = False
        for r in rules:
            txt = r.get("find_text"); rid = r.get("find_id"); dsc = r.get("find_desc"); action = r.get("action", "tap")
            xml = dump_ui_xml(serial=serial)
            if not xml:
                log.append({"round": rounds, "rule": r, "result": "xml_dump_failed"}); continue
            try:
                nodes = find_nodes(xml, text=txt, res_id=rid, desc=dsc)
                if nodes:
                    if action == "tap":
                        cx, cy = bounds_center(nodes[0]["bounds"])
                        ok = input_tap(cx, cy, serial=serial)
                        log.append({"round": rounds, "rule": r, "result": ("tapped" if ok else "tap_failed"), "coords": [cx, cy]})
                        applied = applied or ok
                    else:
                        log.append({"round": rounds, "rule": r, "result": f"unsupported_action:{action}"})
                else:
                    log.append({"round": rounds, "rule": r, "result": "not_found"})
            finally:
                try: os.unlink(xml)
                except: pass
        if not applied: break
        time.sleep(delay_between)
    return log

# ---------- CLI (optional) ----------
def main(argv: List[str]) -> int:
    if len(argv) < 2:
        print("Usage: devices|connect|disconnect|size|tap|swipe|key|screencap|find|runrules", flush=True); return 0
    cmd = argv[1].lower(); ensure_server()
    if cmd == "devices":
        for s, st in adb_devices(): print(f"{s}\t{st}")
        return 0
    if cmd == "connect":
        ok, msg = adb_connect(argv[2]); print(msg); return 0 if ok else 1
    if cmd == "disconnect":
        host = argv[2] if len(argv) >= 3 else None
        ok, msg = adb_disconnect(host); print(msg); return 0 if ok else 1
    if cmd == "size":
        print(device_wm_size(get_default_device_serial())); return 0
    if cmd == "tap":
        ok = input_tap(int(argv[2]), int(argv[3]), serial=get_default_device_serial()); print("OK" if ok else "FAIL"); return 0 if ok else 1
    if cmd == "swipe":
        dur = int(argv[6]) if len(argv) >= 7 else 300
        ok = input_swipe(int(argv[2]), int(argv[3]), int(argv[4]), int(argv[5]), dur, serial=get_default_device_serial())
        print("OK" if ok else "FAIL"); return 0 if ok else 1
    if cmd == "key":
        ok = input_key(argv[2], serial=get_default_device_serial()); print("OK" if ok else "FAIL"); return 0 if ok else 1
    if cmd == "screencap":
        p = screencap_png(argv[2] if len(argv) >= 3 else os.getcwd(), serial=get_default_device_serial()); print(p or "capture_failed"); return 0 if p else 1
    if cmd == "find":
        serial = get_default_device_serial(); text=None; rid=None; dsc=None; dry=False
        for a in argv[2:]:
            if a.startswith("text="): text=a.split("=",1)[1]
            elif a.startswith("id="): rid=a.split("=",1)[1]
            elif a.startswith("desc="): dsc=a.split("=",1)[1]
            elif a == "dry": dry=True
        xml = dump_ui_xml(serial=serial)
        if not xml: print("xml_dump_failed"); return 1
        try:
            nodes = find_nodes(xml, text=text, res_id=rid, desc=dsc); print(json.dumps(nodes[:3], indent=2))
            if nodes and not dry:
                cx, cy = bounds_center(nodes[0]["bounds"]); ok = input_tap(cx, cy, serial=serial); print("tap_OK" if ok else "tap_FAIL")
        finally:
            try: os.unlink(xml)
            except: pass
        return 0
    if cmd == "runrules":
        log = run_rules(argv[2], serial=get_default_device_serial()); print(json.dumps(log, indent=2)); return 0
    print("Unknown command."); return 2

if __name__ == "__main__":
    try: sys.exit(main(sys.argv))
    except SystemExit: raise
    except Exception:
        traceback.print_exc(); sys.exit(1)

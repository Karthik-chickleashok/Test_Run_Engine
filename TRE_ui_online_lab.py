# TRE_ui_online_lab.py — Stability Drop v2.2.0 (Pro)
# Online tab for Test Run Engine (TRE)

import os, subprocess, threading, time, traceback, json
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Dict, Any, Optional, Tuple



SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR   = os.path.dirname(SCRIPT_DIR)
REPORTS_DIR= os.path.join(ROOT_DIR, "Reports")
SHOTS_DIR  = os.path.join(ROOT_DIR, "Screenshots")
TESTS_DIR  = os.path.join(ROOT_DIR, "Test_cases")
os.makedirs(SHOTS_DIR, exist_ok=True)

# --- Online engine (live) ---
try:
    from TRE_online import SessionManager  # engine we stabilized earlier
    ONLINE_OK = True
    ONLINE_ERR = ""
except Exception as e:
    ONLINE_OK = False
    ONLINE_ERR = str(e)

# --- ADB wrapper (uses 'adbb' per your environment) ---
class Adb:
    def __init__(self, exe="adbb"):
        self.exe = exe

    def _run(self, *args, **kw):
        return subprocess.check_output([self.exe, *args], stderr=subprocess.STDOUT, **kw)

    def devices(self) -> str:
        try:
            return self._run("devices").decode(errors="ignore").strip()
        except Exception as e:
            return f"Error: {e}"

    def wm_size(self) -> Optional[Tuple[int,int]]:
        try:
            out = self._run("shell", "wm", "size").decode(errors="ignore")
            # output like: Physical size: 1080x2400
            for line in out.splitlines():
                if ":" in line and "x" in line:
                    tail = line.split(":",1)[1].strip()
                    w,h = tail.split("x")
                    return int(w), int(h)
        except Exception:
            pass
        return None

    def screencap_png_to(self, out_path: str) -> bool:
        try:
            with open(out_path, "wb") as f:
                subprocess.check_call([self.exe, "exec-out", "screencap", "-p"], stdout=f)
            return True
        except Exception:
            return False

    def tap(self, x: int, y: int) -> bool:
        try:
            subprocess.check_call([self.exe, "shell", "input", "tap", str(x), str(y)])
            return True
        except Exception:
            return False

# -------------------------------------------------------------------
# Online tab
# -------------------------------------------------------------------
def _read_tests_files(paths):
    steps_all = []
    for p in paths or []:
        try:
            with open(p, "r", encoding="utf-8") as f:
                arr = json.load(f)
            if isinstance(arr, list):
                steps_all.extend(arr)
        except Exception:
            pass
    return steps_all

def build_online_tab(app, notebook):
    p = app.PALETTE
    cfg = app.load_config()
    extract_payload = app.payload_extract  # already initialized in core

    tab = ttk.Frame(notebook, style="Card.TFrame", padding=12)
    notebook.add(tab, text="Online")

    # Grid: left controls 0..5, right preview 6..20
    for c in range(22):
        tab.grid_columnconfigure(c, weight=(2 if c >= 6 else 0))
    tab.grid_rowconfigure(10, weight=1)

    # ---------------- DLT Controls ----------------
    lf_dlt = ttk.LabelFrame(tab, text="DLT Controls", padding=10)
    lf_dlt.grid(row=0, column=0, columnspan=6, sticky="nsew", pady=(0,8))
    for c in range(6): lf_dlt.grid_columnconfigure(c, weight=1 if c in (1,3,5) else 0)

    ttk.Label(lf_dlt, text="Host").grid(row=0, column=0, sticky="w")
    ent_host = ttk.Entry(lf_dlt, width=16); ent_host.grid(row=0, column=1, sticky="w", padx=(4,8))
    ttk.Label(lf_dlt, text="Port").grid(row=0, column=2, sticky="w")
    ent_port = ttk.Entry(lf_dlt, width=8); ent_port.grid(row=0, column=3, sticky="w", padx=(4,8))

    # prefill
    ent_host.delete(0, tk.END); ent_host.insert(0, str(cfg.get("dlt_host","127.0.0.1")))
    ent_port.delete(0, tk.END); ent_port.insert(0, str(cfg.get("dlt_port",3490)))

    ttk.Label(lf_dlt, text="Output folder").grid(row=1, column=0, sticky="w", pady=(6,0))
    ent_out = ttk.Entry(lf_dlt); ent_out.grid(row=1, column=1, columnspan=3, sticky="we", padx=(4,8), pady=(6,0))
    ent_out.insert(0, app.report_dir or REPORTS_DIR)
    ttk.Button(lf_dlt, text="Browse", command=lambda: _pick_dir(ent_out, REPORTS_DIR)).grid(row=1, column=4, sticky="w", pady=(6,0))
    ttk.Button(lf_dlt, text="Save", command=lambda: _save_dlt(app, ent_host, ent_port)).grid(row=0, column=4, sticky="w")

    # ---------------- HMI Controls ----------------
    lf_hmi = ttk.LabelFrame(tab, text="HMI Controls (Android)", padding=10)
    lf_hmi.grid(row=1, column=0, columnspan=6, sticky="nsew", pady=(0,8))
    for c in range(6): lf_hmi.grid_columnconfigure(c, weight=1 if c in (1,3,5) else 0)

    adb = Adb("adbb")
    adb_info = tk.StringVar(value="ADB ready." if adb.devices() and "device" in adb.devices() else "ADB: connect device & press Detect")
    ttk.Label(lf_hmi, textvariable=adb_info, style="Muted.TLabel").grid(row=0, column=0, columnspan=6, sticky="w")

    ttk.Label(lf_hmi, text="Scale:", style="Muted.TLabel").grid(row=1, column=0, sticky="e", pady=(6,0))
    scale_mode = tk.StringVar(value="Fit")
    cbo_scale = ttk.Combobox(lf_hmi, textvariable=scale_mode, values=["Fit","100%","50%","33%","25%","200%"], width=10, state="readonly")
    cbo_scale.grid(row=1, column=1, sticky="w", padx=(6,12), pady=(6,0))

    ttk.Button(lf_hmi, text="Detect", command=lambda: adb_info.set(adb.devices())).grid(row=1, column=2, sticky="w", pady=(6,0))
    ttk.Button(lf_hmi, text="Start Preview", style="Accent.TButton",
               command=lambda: _start_preview(app, canvas, scale_mode, adb_info, adb)).grid(row=1, column=3, sticky="w", pady=(6,0))
    ttk.Button(lf_hmi, text="Stop Preview",
               command=lambda: _stop_preview(app)).grid(row=1, column=4, sticky="w", pady=(6,0))
    ttk.Button(lf_hmi, text="Save Screenshot",
               command=lambda: _save_screenshot(app, adb, adb_info)).grid(row=1, column=5, sticky="w", pady=(6,0))

    last_tap = tk.StringVar(value="Last tap: —")
        # ---------------- Online Tests (load JSON directly in Online) ----------------
    online_tests_files = []   # list of file paths (strings)

    lf_tests = ttk.LabelFrame(tab, text="Online Tests (JSON)", padding=10)
    lf_tests.grid(row=2, column=0, columnspan=6, sticky="nsew", pady=(0,8))
    lf_tests.grid_columnconfigure(0, weight=1)
    lf_tests.grid_rowconfigure(0, weight=1)

    lst_tests = tk.Listbox(lf_tests, selectmode=tk.EXTENDED, height=6)
    lst_tests.grid(row=0, column=0, sticky="nsew")
    scr_tests = ttk.Scrollbar(lf_tests, orient="vertical", command=lst_tests.yview)
    scr_tests.grid(row=0, column=1, sticky="ns")
    lst_tests.configure(yscrollcommand=scr_tests.set)

    btn_bar = ttk.Frame(lf_tests)
    btn_bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6,0))

    def _refresh_tests_listbox():
        lst_tests.delete(0, tk.END)
        for pth in online_tests_files:
            lst_tests.insert(tk.END, pth)

    def _add_tests():
        paths = filedialog.askopenfilenames(
            title="Select test cases (JSON)",
            initialdir=TESTS_DIR,
            filetypes=[("JSON files", "*.json"), ("All files", "*.*")]
        )
        for p in paths:
            if p and (p not in online_tests_files):
                online_tests_files.append(p)
        _refresh_tests_listbox()

    def _remove_tests():
        sel = list(reversed(lst_tests.curselection()))
        for i in sel:
            try:
                del online_tests_files[i]
            except Exception:
                pass
        _refresh_tests_listbox()

    def _clear_tests():
        online_tests_files.clear()
        _refresh_tests_listbox()

    def _load_from_offline():
        try:
            src = getattr(app, "off_tests", []) or []
            online_tests_files.clear()
            online_tests_files.extend(list(src))
            _refresh_tests_listbox()
        except Exception:
            pass

    ttk.Button(btn_bar, text="Add…",    command=_add_tests).grid(row=0, column=0, padx=(0,6))
    ttk.Button(btn_bar, text="Remove",  command=_remove_tests).grid(row=0, column=1, padx=(0,6))
    ttk.Button(btn_bar, text="Clear",   command=_clear_tests).grid(row=0, column=2, padx=(0,12))
    ttk.Button(btn_bar, text="Load from Offline", command=_load_from_offline).grid(row=0, column=3, padx=(0,6))
    ttk.Label(lf_hmi, textvariable=last_tap, style="Muted.TLabel").grid(row=2, column=0, columnspan=6, sticky="w", pady=(6,0))

    # ---------------- HMI Preview (right) ----------------
    lf_preview = ttk.LabelFrame(tab, text="HMI Preview", padding=10)
    lf_preview.grid(row=0, column=6, columnspan=15, rowspan=7, sticky="nsew", padx=(8,0), pady=(0,8))
    for c in range(6): lf_preview.grid_columnconfigure(c, weight=1)
    lf_preview.grid_rowconfigure(0, weight=1)

    canvas = tk.Canvas(lf_preview, bg="#111")
    canvas.grid(row=0, column=0, columnspan=6, sticky="nsew")

    _pv = {"img":None,"iw":0,"ih":0,"dw":0,"dh":0,"x0":0,"y0":0,"scale":1.0}
    _dev_size: Tuple[int,int] = (0,0)
    _preview_on = {"run": False}
    _preview_ms = 400
    _shots_dir = SHOTS_DIR

    def _apply_scale(img: tk.PhotoImage, cw: int, ch: int) -> Tuple[tk.PhotoImage, float]:
        iw, ih = img.width(), img.height()
        m = scale_mode.get()
        if m == "Fit":
            import math
            fx = math.ceil(iw / cw) if iw > cw else 1
            fy = math.ceil(ih / ch) if ih > ch else 1
            f = max(fx, fy, 1)
            if f > 1: return img.subsample(f, f), 1.0/f
            return img, 1.0
        if m == "100%": return img, 1.0
        if m == "50%":  return img.subsample(2,2), 0.5
        if m == "33%":  return img.subsample(3,3), 1/3
        if m == "25%":  return img.subsample(4,4), 0.25
        if m == "200%": return img.zoom(2,2), 2.0
        return img, 1.0

    def _update_dev_size():
        nonlocal _dev_size
        wh = adb.wm_size()
        if wh: _dev_size = wh

    def _capture_once() -> Optional[str]:
        ts = time.strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(_shots_dir, f"preview_{ts}.png")
        ok = adb.screencap_png_to(path)
        if ok and os.path.exists(path):
            return path
        try:
            if os.path.exists(path): os.remove(path)
        except Exception: pass
        return None

    def _show_latest():
        path = _capture_once()
        if not path or not os.path.exists(path): return
        try:
            img = tk.PhotoImage(file=path)
        except Exception:
            try: os.remove(path)
            except Exception: pass
            return
        iw, ih = img.width(), img.height()
        cw = max(200, int(canvas.winfo_width() or 900))
        ch = max(200, int(canvas.winfo_height() or 700))
        disp, sc = _apply_scale(img, cw, ch)
        dw, dh = disp.width(), disp.height()
        x0, y0 = max(0,(cw-dw)//2), max(0,(ch-dh)//2)
        canvas.delete("all"); canvas.create_image(x0, y0, anchor="nw", image=disp)
        _pv.update({"img":disp,"iw":iw,"ih":ih,"dw":dw,"dh":dh,"x0":x0,"y0":y0,"scale":sc})
        try: os.remove(path)
        except Exception: pass

    def _loop():
        if not _preview_on["run"]: return
        _show_latest()
        app.root.after(_preview_ms, _loop)

    def _on_canvas_resize(_e=None):
        if scale_mode.get() == "Fit" and _preview_on["run"]:
            _show_latest()
    canvas.bind("<Configure>", _on_canvas_resize)

    def on_click(ev):
        if not _pv["img"]: return
        x0,y0 = _pv["x0"], _pv["y0"]; dw,dh = _pv["dw"], _pv["dh"]
        iw,ih = _pv["iw"], _pv["ih"]; sc = _pv["scale"]
        if not (x0 <= ev.x <= x0+dw and y0 <= ev.y <= y0+dh): return
        rx, ry = ev.x - x0, ev.y - y0
        ox, oy = int(rx/sc), int(ry/sc)
        dx, dy = (_dev_size if _dev_size != (0,0) else (iw, ih))
        tx = int(ox * dx / iw); ty = int(oy * dy / ih)
        ok = adb.tap(tx, ty)
        last_tap.set(f"Last tap: ({tx},{ty})")
        try:
            app.root.clipboard_clear()
            app.root.clipboard_append(
                f'{{"action": {{"type":"tap","x":{tx},"y":{ty}}}}}\n'
                f'{{"action": {{"type":"tap_pct","px":{tx/max(dx,1):.4f},"py":{ty/max(dy,1):.4f}}}}}'
            )
        except Exception:
            pass
    canvas.bind("<Button-1>", on_click)

    def _start_preview(app, canvas_, scale_var, info_var, adb_obj: Adb):
        _update_dev_size()
        _preview_on["run"] = True
        info_var.set("Preview running.")
        _loop()

    def _stop_preview(app):
        _preview_on["run"] = False

    def _save_screenshot(app, adb_obj: Adb, info_var):
        shots_dir = os.path.join(app.report_dir or REPORTS_DIR, "Screenshots")
        os.makedirs(shots_dir, exist_ok=True)
        ts = time.strftime("%Y-%m-%d_%H-%M-%S")
        out = os.path.join(shots_dir, f"shot_{ts}.png")
        if adb_obj.screencap_png_to(out) and os.path.exists(out):
            messagebox.showinfo("Screenshot", f"Saved:\n{out}")
        else:
            messagebox.showerror("Screenshot", "Failed.")

    # ---------------- Execution Controls ----------------
    lf_exec = ttk.LabelFrame(tab, text="Execution Controls", padding=10)
    lf_exec.grid(row=3, column=0, columnspan=6, sticky="nsew", pady=(0,8))
    for c in range(6): lf_exec.grid_columnconfigure(c, weight=1 if c in (2,3,4,5) else 0)

    status = tk.StringVar(value=("Online engine unavailable" if not ONLINE_OK else "Disconnected."))
    prog = ttk.Progressbar(lf_exec, style="Color.Horizontal.TProgressbar", mode="indeterminate", length=220)
    prog.grid(row=0, column=4, sticky="e", padx=(12,0)); prog.grid_remove()

    online_mgr: Optional[SessionManager] = None
    lock = threading.Lock()

    def _reset_progress(tree, rows: Dict[int,str]):
        rows.clear()
        for i in tree.get_children(): tree.delete(i)

    def _apply_tag(tree, iid, status_text):
        s = (status_text or "").strip().upper()
        tag = "running" if s not in ("PASS","FAIL","ERROR") else s.lower()
        tree.item(iid, tags=(tag,))

    def start_online():
        nonlocal online_mgr
        if not ONLINE_OK:
            return messagebox.showerror("Online", f"Could not import TRE_online:\n{ONLINE_ERR}")
        # Prefer Online Tests if user loaded any; else fall back to Offline-selected tests
        tests = _read_tests_files(online_tests_files) if online_tests_files else (app.current_tests or [])
        if not tests:
            return messagebox.showerror("Missing", "Load test cases (Online pane or Offline tab) first.")

        host = ent_host.get().strip() or "127.0.0.1"
        try: port = int(ent_port.get().strip() or "3490")
        except: return messagebox.showerror("Invalid", "Port must be integer.")
        out_dir = ent_out.get().strip() or (app.report_dir or REPORTS_DIR)
        if not os.path.isdir(out_dir):
            return messagebox.showerror("Invalid", "Select a valid output folder.")
        try: app.save_config({"dlt_host": host, "dlt_port": port})
        except Exception: pass

        _reset_progress(tree, rows)
        status.set("Starting…")
        btn_start.config(state="disabled"); btn_stop.config(state="normal")
        btn_pause.config(state="normal");   btn_resume.config(state="normal")
        prog.grid(); prog.start(10)

        def worker():
            mgr = SessionManager(host=host, port=port, tests=tests, out_dir=out_dir,
                                 on_status=lambda m: app.root.after(0, status.set, m),
                                 on_steps_init=lambda s: app.root.after(0, _on_steps_init, s, tree, rows, _apply_tag),
                                 on_step_update=lambda i,n,v,r,l: app.root.after(0, _on_step_update, i, v, r, l, tree, rows, _apply_tag, extract_payload))
            with lock: online_mgr = mgr
            try:
                mgr.start()
            except Exception as e:
                app.root.after(0, lambda: (
                    prog.stop(), prog.grid_remove(),
                    btn_start.config(state="normal"),
                    btn_stop.config(state="disabled"),
                    btn_pause.config(state="disabled"),
                    btn_resume.config(state="disabled"),
                    status.set(f"Error: {e}")
                ))

        threading.Thread(target=worker, daemon=True).start()

    def stop_online():
        with lock:
            mgr = online_mgr
        if not mgr:
            return
        status.set("Stopping…")
        def w():
            try:
                mgr.stop()
            finally:
                app.root.after(0, lambda: (
                    prog.stop(), prog.grid_remove(),
                    btn_start.config(state="normal"),
                    btn_stop.config(state="disabled"),
                    btn_pause.config(state="disabled"),
                    btn_resume.config(state="disabled"),
                    status.set("Stopped.")
                ))
        threading.Thread(target=w, daemon=True).start()


    def pause_online():
        with lock:
            if online_mgr: online_mgr.pause(); status.set("Paused.")

    def resume_online():
        with lock:
            if online_mgr: online_mgr.resume(); status.set("Resumed.")

    btn_start = ttk.Button(lf_exec, text="Start", style="Accent.TButton", command=start_online); btn_start.grid(row=0, column=0, sticky="w")
    btn_pause = ttk.Button(lf_exec, text="Pause", command=pause_online, state="disabled");  btn_pause.grid(row=0, column=1, sticky="w")
    btn_resume= ttk.Button(lf_exec, text="Resume", command=resume_online, state="disabled");btn_resume.grid(row=0, column=2, sticky="w")
    btn_stop  = ttk.Button(lf_exec, text="Stop",   command=stop_online, state="disabled");  btn_stop.grid(row=0, column=3, sticky="w")
    ttk.Label(lf_exec, textvariable=status, style="Muted.TLabel").grid(row=0, column=5, sticky="e")

    # ---------------- Execution Progress ----------------
    tk.Label(tab, text="Execution Progress", font=("Segoe UI", 14, "bold"),
             fg=p["accent"], bg=p["card"]).grid(row=8, column=0, sticky="w", pady=(4,4))
    fr_prog = ttk.Frame(tab); fr_prog.grid(row=9, column=0, columnspan=21, sticky="nsew")
    fr_prog.grid_columnconfigure(0, weight=1); fr_prog.grid_rowconfigure(0, weight=1)
    tree = ttk.Treeview(fr_prog, columns=("idx","name","vc","result","line"), show="headings", height=14)
    tree.grid(row=0, column=0, sticky="nsew")
    vsb = ttk.Scrollbar(fr_prog, orient="vertical", command=tree.yview); vsb.grid(row=0, column=1, sticky="ns")
    hsb = ttk.Scrollbar(fr_prog, orient="horizontal", command=tree.xview); hsb.grid(row=1, column=0, sticky="we")
    tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
    for c,t,w in (("idx","#",60),("name","Test step",280),("vc","VC",520),("result","Result",120),("line","Line",800)):
        tree.heading(c, text=t); tree.column(c, width=w, stretch=(c!="result"))
    tree.tag_configure("running", background=p["row_running_bg"], foreground=p["row_running_fg"])
    tree.tag_configure("pass",    background=p["row_pass_bg"],    foreground=p["row_pass_fg"])
    tree.tag_configure("fail",    background=p["row_fail_bg"],    foreground=p["row_fail_fg"])
    tree.tag_configure("error",   background=p["row_error_bg"],   foreground=p["row_error_fg"])

    rows: Dict[int,str] = {}
    app.online_tree = tree  # external access if needed

    return tab

# --- helpers used by tab ---
def _pick_dir(entry: ttk.Entry, initial: str):
    p = filedialog.askdirectory(initialdir=initial)
    if p:
        entry.delete(0, tk.END); entry.insert(0, p)

def _save_dlt(app, ent_host, ent_port):
    host = ent_host.get().strip() or "127.0.0.1"
    port = ent_port.get().strip() or "3490"
    try:
        port_int = int(port)
    except:
        return messagebox.showerror("Invalid", "Port must be integer.")
    cfg = app.load_config(); cfg.update({"dlt_host": host, "dlt_port": port_int})
    app.save_config(cfg)
    messagebox.showinfo("Saved", "DLT Host/Port saved.")

def _on_steps_init(steps, tree: ttk.Treeview, rows: Dict[int,str], _apply_tag):
    rows.clear()
    for st in steps:
        iid = tree.insert("", tk.END, values=(st["idx"], st["name"], st.get("vc",""), "running…", ""), tags=("running",))
        rows[int(st["idx"])] = iid
        _apply_tag(tree, iid, "RUNNING")
        tree.see(iid)

def _on_step_update(idx: int, vc: str, result: str, line: Optional[str],
                    tree: ttk.Treeview, rows: Dict[int,str], _apply_tag, extract_payload):
    iid = rows.get(int(idx))
    if not iid: return
    tree.set(iid, "result", result)
    tree.set(iid, "line", extract_payload(line or ""))
    _apply_tag(tree, iid, result)
    tree.see(iid)

# -------------------------------------------------------------------
# Backward-compatible entrypoint (what launcher expects)
# -------------------------------------------------------------------
def attach_online_tab(app, notebook=None):
    if notebook is None:
        notebook = app.nb
    return build_online_tab(app, notebook)

__all__ = ["attach_online_tab", "build_online_tab"]

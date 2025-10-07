# =====================================================================
# TRE_ui.pyw — Unified UI (Offline + Online + HMI)   •   Version 2.2.0u
# Test Run Engine (TRE)
#
# This file merges the previously separate UI files:
#   - TRE_ui_core.py
#   - TRE_ui_online_lab.py
#
# Baseline restored from last stable design you confirmed and then unified.
# Stop/Start wiring prepared; Online engine is TRE_online.SessionManager.
#
# NOTE:
# - Keep TRE_online.py and TRE_json.py as-is (we’ll add a tiny Stop patch).
# - Remove TRE_ui_core.py, TRE_ui_online_lab.py (no longer used).
# - Config file lives at Code/TRE_config.json (payload extractor + DLT host/port).
# =====================================================================

# === CHUNK 1/8 — Imports, Paths, Theme, Utilities ====================

import os, sys, json, time, threading, traceback, webbrowser, re, datetime
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Dict, Any, List, Optional, Callable, Tuple

# engines
try:
    import TRE_json as tre
except Exception as e:
    tk.Tk().withdraw()
    messagebox.showerror("TRE_json import error", str(e))
    sys.exit(1)

try:
    from TRE_online import SessionManager  # engine
except Exception as e:
    SessionManager = None
    _ONLINE_IMPORT_ERR = str(e)

# App meta
APP_NAME     = "Test Run Engine"
APP_VERSION  = "2.2.0u"
AUTHOR_NAME  = "Karthik Chickleashok"
AUTHOR_EMAIL = "karthik.chickel@gmail.com"

# Paths
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR   = os.path.dirname(SCRIPT_DIR)
LOGS_DIR   = os.path.join(ROOT_DIR, "Logs")
REPORTS_DIR= os.path.join(ROOT_DIR, "Reports")
TESTS_DIR  = os.path.join(ROOT_DIR, "Test_cases")
SHOTS_DIR  = os.path.join(ROOT_DIR, "Screenshots")
CONFIG_PATH= os.path.join(SCRIPT_DIR, "TRE_config.json")
for d in (LOGS_DIR, REPORTS_DIR, TESTS_DIR, SHOTS_DIR):
    os.makedirs(d, exist_ok=True)

# Theme palette (Classic Blue/Grey)
PALETTE = {
    "app":"#f8fafc","card":"#ffffff","fg":"#0f172a","muted":"#475569",
    "accent":"#2563eb","accent_fg":"#ffffff","header_bg":"#0f172a","header_fg":"#ffffff","progress":"#2563eb",
    "row_running_bg":"#e5e7eb","row_running_fg":"#374151",
    "row_pass_bg":"#d1fae5","row_pass_fg":"#065f46",
    "row_fail_bg":"#fee2e2","row_fail_fg":"#7f1d1d",
    "row_error_bg":"#fde68a","row_error_fg":"#92400e",
}

def _initdir(p): return p if os.path.isdir(p) else ROOT_DIR


# === CHUNK 2/8 — Payload extractor + Config I/O =======================

def make_payload_extractor(cfg: Dict[str, Any]) -> Callable[[str], str]:
    """
    Extracts payload message from a raw log line using:
      1) payload_regex (single capture group)
      2) payload_mode: 'after_last_pipe'|'after_last_bracket'|'after_last_colon'
      3) heuristic fallback
    """
    if not isinstance(cfg, dict):
        cfg = {}
    regex_pat = cfg.get("payload_regex")
    mode = cfg.get("payload_mode")

    if isinstance(regex_pat, str) and regex_pat.strip():
        try:
            rx = re.compile(regex_pat)
            def _rx(s: str) -> str:
                if not s: return s
                m = rx.search(s)
                if m and m.groups():
                    return (m.group(1) or "").strip()
                return s
            return _rx
        except Exception:
            pass

    if mode == "after_last_pipe":
        return lambda s: (s.rsplit("|", 1)[-1].strip() if s else s)
    if mode == "after_last_bracket":
        return lambda s: (s.rsplit("]", 1)[-1].strip() if s else s)
    if mode == "after_last_colon":
        return lambda s: (s.rsplit(":", 1)[-1].strip() if s else s)

    def _heuristic(s: str) -> str:
        if not s: return s
        s = s.replace("\x00", "").strip()
        if ']' in s:
            pos = s.rfind(']')
            if pos != -1 and pos + 1 < len(s):
                tail = s[pos+1:].strip(" -:|")
                if tail: s = tail
        if '|' in s:
            parts = [p.strip() for p in s.split('|') if p.strip()]
            if len(parts) > 1:
                s = parts[-1]
        if ':' in s:
            left, right = s.rsplit(':', 1)
            if len(right.strip()) >= 3:
                s = right.strip()
        return s
    return _heuristic


def load_config() -> Dict[str, Any]:
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_config_patch(patch: Dict[str, Any]) -> Dict[str, Any]:
    base = load_config()
    base.update(patch or {})
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(base, f, indent=2)
    except Exception as e:
        messagebox.showerror("Config save failed", str(e))
    return base


# === CHUNK 3/8 — App class: root, header, notebook, tabs ==============

class TREApp:
    def _on_send_curl(self):
        import threading, subprocess
        import tkinter as tk
        from tkinter import Toplevel, Text, ttk

        # Popup window
        top = Toplevel(self.root)
        top.title("Send to DUT (curl / shell)")
        top.transient(self.root)
        top.grab_set()

        # Text input
        frm = ttk.Frame(top, padding=10)
        frm.pack(fill="both", expand=True)
        ttk.Label(frm, text="Command to execute on this PC (e.g. curl ...):").pack(anchor="w", pady=(0,6))
        txt = Text(frm, height=5, width=92, wrap="none")
        txt.pack(fill="both", expand=True)
        # optional: prefill last used
        try:
            if getattr(self, "_last_send_cmd", ""):
                txt.insert("1.0", self._last_send_cmd)
        except Exception:
            pass

        # Buttons
        btns = ttk.Frame(frm)
        btns.pack(anchor="e", pady=(8,0))
        def do_send():
            cmd = txt.get("1.0", "end-1c").strip()
            if not cmd:
                top.destroy(); return
            self._last_send_cmd = cmd
            top.destroy()
            self._online_status.set("Sending…")

            def worker():
                try:
                    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=60)
                    if r.returncode == 0:
                        out = (r.stdout or "").strip()
                        msg = "Sent OK" + (f": {out[:160]}" if out else "")
                    else:
                        err = (r.stderr or "").strip()
                        msg = f"Send FAIL ({r.returncode})" + (f": {err[:160]}" if err else "")
                except Exception as e:
                    msg = f"Send error: {e}"
                # update UI safely
                try:
                    self.root.after(0, self._online_status.set, msg)
                except Exception:
                    pass

            threading.Thread(target=worker, daemon=True).start()

        def do_close():
            top.destroy()

        ttk.Button(btns, text="Cancel", command=do_close).pack(side="right", padx=(6,0))
        ttk.Button(btns, text="Send", command=do_send).pack(side="right")
        top.bind("<Escape>", lambda e: do_close())
        top.bind("<Control-Return>", lambda e: do_send())
        txt.focus_set()



    def __init__(self):
        self.cfg = load_config()
        self.payload_extract = make_payload_extractor(self.cfg)

        self.root = tk.Tk()
        self.root.title(f"{APP_NAME} {APP_VERSION}")
        self.root.geometry("1440x980")
        self._style()
        self._header()

        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=12)

        # State shared with Online
        self.current_tests: List[Dict[str, Any]] = []
        self.report_dir = REPORTS_DIR

        # Build tabs
        self._build_offline_tab()
        self._build_online_tab()
        self._build_help_tab()
        self._build_about_tab()

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.minsize(1260, 920)

    # ---- Styling / Header ----
    def _style(self):
        self.root.configure(bg=PALETTE["app"])
        st = ttk.Style(self.root)
        st.theme_use("clam")
        st.configure("Card.TFrame", background=PALETTE["card"])
        st.configure("TFrame", background=PALETTE["card"])
        st.configure("TLabel", background=PALETTE["card"], foreground=PALETTE["fg"])
        st.configure("Muted.TLabel", background=PALETTE["card"], foreground=PALETTE["muted"])
        st.configure("Accent.TButton", background=PALETTE["accent"], foreground=PALETTE["accent_fg"],
                     font=("Segoe UI", 10, "bold"), padding=8)
        st.map("Accent.TButton", background=[("active", PALETTE["accent"])])
        st.configure("Color.Horizontal.TProgressbar", troughcolor=PALETTE["card"], background=PALETTE["progress"])

    def _header(self):
        h = tk.Frame(self.root, bg=PALETTE["header_bg"])
        h.pack(fill="x")
        tk.Label(h, text=APP_NAME, fg=PALETTE["header_fg"], bg=PALETTE["header_bg"],
                 font=("Segoe UI", 18, "bold"), padx=12, pady=10).pack(side="left")
        tk.Button(h, text="Help",
                  command=lambda: messagebox.showinfo("Help",
                      f"{APP_NAME} {APP_VERSION}\n\n"
                      "Offline: Add Logs + Test cases, then Run to generate HTML/CSV/JSON.\n"
                      "Online: Live DLT execution with HMI preview.\n"
                      "Settings: Code/TRE_config.json (payload_regex/mode, saved DLT host/port)."
                  ),
                  bg=PALETTE["header_bg"], fg=PALETTE["header_fg"], bd=0, padx=12, pady=6).pack(side="right", padx=8, pady=8)
        tk.Button(h, text="About",
                  command=lambda: messagebox.showinfo("About",
                      f"{APP_NAME} {APP_VERSION}\n© {AUTHOR_NAME}\n{AUTHOR_EMAIL}"),
                  bg=PALETTE["header_bg"], fg=PALETTE["header_fg"], bd=0, padx=12, pady=6).pack(side="right", padx=8, pady=8)

    def run(self):
        self.root.mainloop()

    def _on_close(self):
        try:
            if hasattr(self, "_online_preview_on"):
                self._online_preview_on["run"] = False
            if hasattr(self, "_online_mgr") and self._online_mgr:
                self._online_mgr.stop()
        except Exception:
            pass
        self.root.after(150, self.root.destroy)

    # === CHUNK 4/8 — Offline tab =====================================

    def _build_offline_tab(self):
        tab = ttk.Frame(self.nb, style="Card.TFrame", padding=12)
        self.nb.add(tab, text="Offline")

        for i in range(6):
            tab.grid_columnconfigure(i, weight=1 if i in (1,3,5) else 0)

        # Logs
        ttk.Label(tab, text="Log files").grid(row=0, column=0, sticky="w")
        fL = ttk.Frame(tab); fL.grid(row=1, column=0, columnspan=2, sticky="nsew")
        fL.grid_columnconfigure(0, weight=1); fL.grid_rowconfigure(0, weight=1)
        self.lst_logs = tk.Listbox(fL, selectmode=tk.EXTENDED, height=8); self.lst_logs.grid(row=0, column=0, sticky="nsew")
        sbL = ttk.Scrollbar(fL, orient="vertical", command=self.lst_logs.yview); sbL.grid(row=0, column=1, sticky="ns")
        self.lst_logs.config(yscrollcommand=sbL.set)

        fbL = ttk.Frame(tab); fbL.grid(row=2, column=0, columnspan=2, sticky="w", pady=(6,12))
        ttk.Button(fbL, text="Add…",    command=self._off_add_logs).grid(row=0, column=0, padx=4)
        ttk.Button(fbL, text="Remove",  command=lambda: self._remove_selected(self.lst_logs, self._off_logs)).grid(row=0, column=1, padx=4)
        ttk.Button(fbL, text="Clear",   command=lambda: self._clear_list(self.lst_logs, self._off_logs)).grid(row=0, column=2, padx=4)

        # Tests
        ttk.Label(tab, text="Test cases / suite (JSON)").grid(row=0, column=3, sticky="w")
        fT = ttk.Frame(tab); fT.grid(row=1, column=3, columnspan=2, sticky="nsew")
        fT.grid_columnconfigure(0, weight=1); fT.grid_rowconfigure(0, weight=1)
        self.lst_tests = tk.Listbox(fT, selectmode=tk.EXTENDED, height=8); self.lst_tests.grid(row=0, column=0, sticky="nsew")
        sbT = ttk.Scrollbar(fT, orient="vertical", command=self.lst_tests.yview); sbT.grid(row=0, column=1, sticky="ns")
        self.lst_tests.config(yscrollcommand=sbT.set)

        fbT = ttk.Frame(tab); fbT.grid(row=2, column=3, columnspan=2, sticky="w", pady=(6,12))
        ttk.Button(fbT, text="Add…",    command=self._off_add_tests).grid(row=0, column=0, padx=4)
        ttk.Button(fbT, text="Remove",  command=lambda: self._remove_selected(self.lst_tests, self._off_tests)).grid(row=0, column=1, padx=4)
        ttk.Button(fbT, text="Clear",   command=lambda: self._clear_list(self.lst_tests, self._off_tests)).grid(row=0, column=2, padx=4)

        # Output + options
        ttk.Label(tab, text="Output folder:").grid(row=3, column=0, sticky="w")
        self.off_ent_out = ttk.Entry(tab); self.off_ent_out.grid(row=3, column=1, sticky="we")
        self.off_ent_out.insert(0, REPORTS_DIR)
        ttk.Button(tab, text="Browse", command=self._off_pick_out).grid(row=3, column=2, sticky="w")
        ttk.Label(tab, text="Preview limit (0 = full):").grid(row=3, column=3, sticky="w")
        self.off_ent_prev = ttk.Entry(tab, width=10); self.off_ent_prev.insert(0, "0"); self.off_ent_prev.grid(row=3, column=4, sticky="w")

        self._off_html = tk.BooleanVar(value=True)
        self._off_json = tk.BooleanVar(value=False)
        self._off_csv  = tk.BooleanVar(value=False)
        self._off_open = tk.BooleanVar(value=True)
        togg = ttk.Frame(tab); togg.grid(row=4, column=3, columnspan=3, sticky="w")
        ttk.Checkbutton(togg, text="Generate HTML", variable=self._off_html).grid(row=0, column=0, padx=(0,16))
        ttk.Checkbutton(togg, text="Generate JSON", variable=self._off_json).grid(row=0, column=1, padx=(0,16))
        ttk.Checkbutton(togg, text="Generate CSV",  variable=self._off_csv ).grid(row=0, column=2, padx=(0,16))
        ttk.Checkbutton(togg, text="Open first HTML after run", variable=self._off_open).grid(row=0, column=3, padx=(0,16))

        tk.Label(tab, text="Execution progress (per step)", font=("Segoe UI", 14, "bold"),
                 fg=PALETTE["accent"], bg=PALETTE["card"]).grid(row=5, column=0, sticky="w", pady=(8,4))

        fP = ttk.Frame(tab); fP.grid(row=6, column=0, columnspan=6, sticky="nsew")
        tab.grid_rowconfigure(6, weight=1)
        fP.grid_columnconfigure(0, weight=1); fP.grid_rowconfigure(0, weight=1)

        cols = ("idx","name","vc","result","line")
        self.off_tree = ttk.Treeview(fP, columns=cols, show="headings", height=14)
        self.off_tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(fP, orient="vertical", command=self.off_tree.yview); vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(fP, orient="horizontal", command=self.off_tree.xview); hsb.grid(row=1, column=0, sticky="we")
        self.off_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        for c,t,w in (("idx","#",60),("name","Test step",280),("vc","VC",520),("result","Result",120),("line","Line",800)):
            self.off_tree.heading(c, text=t); self.off_tree.column(c, width=w, stretch=(c!="result"))
        self._tree_tags(self.off_tree)

        # Run controls
        self._off_logs: List[str] = []
        self._off_tests: List[str] = []
        self._off_status = tk.StringVar(value="Ready.")
        self._off_prog = ttk.Progressbar(tab, style="Color.Horizontal.TProgressbar", mode="indeterminate", length=280)
        self._off_prog.grid(row=7, column=2, sticky="w", pady=(12,8)); self._off_prog.grid_remove()
        ttk.Button(tab, text="Run", style="Accent.TButton", command=self._off_run).grid(row=7, column=0, sticky="w", pady=(12,8))
        ttk.Button(tab, text="Re-run last", command=self._off_run).grid(row=7, column=1, sticky="w", pady=(12,8))
        ttk.Label(tab, textvariable=self._off_status, style="Muted.TLabel").grid(row=7, column=3, sticky="w", columnspan=3)

    # --- Offline helpers ---
    def _tree_tags(self, tree: ttk.Treeview):
        tree.tag_configure("running", background=PALETTE["row_running_bg"], foreground=PALETTE["row_running_fg"])
        tree.tag_configure("pass",    background=PALETTE["row_pass_bg"],    foreground=PALETTE["row_pass_fg"])
        tree.tag_configure("fail",    background=PALETTE["row_fail_bg"],    foreground=PALETTE["row_fail_fg"])
        tree.tag_configure("error",   background=PALETTE["row_error_bg"],   foreground=PALETTE["row_error_fg"])

    def _apply_row_tag(self, tree, iid, status_text):
        s = (status_text or "").strip().upper()
        tag = "running" if s not in ("PASS","FAIL","ERROR") else s.lower()
        tree.item(iid, tags=(tag,))

    def _off_add_logs(self):
        self._add_files(self.lst_logs, self._off_logs,
                        [("Logs","*.log *.txt *.csv"),("All","*.*")],
                        "Select logs", LOGS_DIR)

    def _off_add_tests(self):
        self._add_files(self.lst_tests, self._off_tests,
                        [("JSON","*.json"),("All","*.*")],
                        "Select test cases", TESTS_DIR, validate_json=True)
        self._reload_current_tests()

    def _off_pick_out(self):
        p = filedialog.askdirectory(initialdir=_initdir(REPORTS_DIR))
        if p:
            self.off_ent_out.delete(0, tk.END); self.off_ent_out.insert(0, p)

    def _add_files(self, listbox, store, types, title, initialdir, validate_json=False):
        paths = filedialog.askopenfilenames(title=title, filetypes=types, initialdir=_initdir(initialdir))
        for p in paths:
            if p and p not in store:
                if validate_json:
                    ok, msg = tre.validate_tests_json(p)
                    messagebox.showinfo("Test JSON", f"{os.path.basename(p)}\n{msg}")
                store.append(p); listbox.insert(tk.END, p)

    def _remove_selected(self, listbox, store):
        for idx in reversed(listbox.curselection()):
            try:
                store.remove(listbox.get(idx))
            except Exception:
                pass
            listbox.delete(idx)
        if store is self._off_tests:
            self._reload_current_tests()

    def _clear_list(self, listbox, store):
        store.clear(); listbox.delete(0, tk.END)
        if store is self._off_tests:
            self._reload_current_tests()

    def _read_tests_files(self, paths: List[str]) -> List[Dict[str, Any]]:
        steps: List[Dict[str, Any]] = []
        for p in paths:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    arr = json.load(f)
                if isinstance(arr, list):
                    steps.extend(arr)
            except Exception:
                pass
        return steps

    def _reload_current_tests(self):
        self.current_tests = self._read_tests_files(self._off_tests)

    # --- Offline run ---
    def _off_progress_add(self, idx, name, vc):
        iid = self.off_tree.insert("", tk.END, values=(idx, name, vc, "running…", ""), tags=("running",))
        self._apply_row_tag(self.off_tree, iid, "RUNNING")
        self.off_tree.see(iid); self.root.update_idletasks()
        return iid

    def _off_progress_set(self, iid, status, line=""):
        self.off_tree.set(iid, "result", status)
        self.off_tree.set(iid, "line", self.payload_extract(line or ""))
        self._apply_row_tag(self.off_tree, iid, status)
        self.off_tree.see(iid); self.root.update_idletasks()

    def _off_run(self):
        if not self._off_logs: return messagebox.showerror("Missing", "Add log files")
        if not self._off_tests: return messagebox.showerror("Missing", "Add test cases (JSON)")
        out_dir = self.off_ent_out.get().strip() or REPORTS_DIR
        if not os.path.isdir(out_dir): return messagebox.showerror("Invalid", "Select a valid output folder")
        try:
            prev = int(self.off_ent_prev.get().strip() or "0")
        except:
            return messagebox.showerror("Invalid", "Preview limit must be integer")

        # clear table
        for i in self.off_tree.get_children(): self.off_tree.delete(i)
        self._off_prog.grid(); self._off_prog.start(10)
        self._off_status.set("Running…")

        def worker():
            try:
                generated_all = []
                first_html_local = None
                for log in list(self._off_logs):
                    lines = list(tre.iter_log(log))
                    for test in list(self._off_tests):
                        # live per-step quick pass
                        with open(test, "r", encoding="utf-8") as f:
                            tests = json.load(f)
                        for i, t in enumerate(tests, start=1):
                            name = t.get("name","unnamed")
                            vc = (t.get("find",{}).get("pattern","") if "find" in t else
                                  t.get("not_find",{}).get("pattern","") if "not_find" in t else
                                  " -> ".join([(e if isinstance(e,str) else e.get("pattern",""))
                                               for e in t.get("sequence",[])]) if "sequence" in t else
                                  f"[{t.get('action',{}).get('type','action')}]")
                            iid = self._off_progress_add(i, name, vc)
                            try:
                                if "find" in t:       det = tre.check_find(lines, t["find"])
                                elif "not_find" in t: det = tre.check_not_find(lines, t["not_find"])
                                elif "sequence" in t: det = tre.check_sequence(lines, t)
                                elif "action" in t:   det = {"pass": True, "detail": {"line": f"[action] {t['action'].get('type','')}" }}
                                else:                 det = {"pass": False, "detail": {"error": "Unknown"}}
                                status = "PASS" if det.get("pass") else "FAIL"
                                line   = (det.get("detail") or {}).get("line")
                                self.root.after(0, self._off_progress_set, iid, status, line or "")
                            except Exception:
                                self.root.after(0, self._off_progress_set, iid, "ERROR", "")
                        # produce outputs
                        rpt = tre.run_checks(log, test)
                        tre.adjust_line_numbers(rpt, 0)
                        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
                        logstem  = os.path.splitext(os.path.basename(log))[0]
                        testname = os.path.basename(test)
                        teststem = os.path.splitext(testname)[0]
                        base = teststem
                        html_name = f"{base}_{logstem}_{'LATEST'}.html"
                        json_name = f"{base}_{logstem}_{'LATEST'}.json"
                        csv_name  = f"{base}_{logstem}_{'LATEST'}.csv"

                        if self._off_html.get():
                            p = os.path.join(out_dir, html_name)
                            tre.to_html(rpt, p, global_preview_limit=prev,
                                        title=f"{testname} · {logstem}",
                                        generator_info=f"{APP_NAME} {APP_VERSION} — {AUTHOR_NAME} <{AUTHOR_EMAIL}>")
                            generated_all.append(p); first_html_local = first_html_local or p
                        if self._off_json.get():
                            p = os.path.join(out_dir, json_name)
                            with open(p,"w",encoding="utf-8") as f: json.dump(rpt, f, indent=2, ensure_ascii=False)
                            generated_all.append(p)
                        if self._off_csv.get():
                            p = os.path.join(out_dir, csv_name); tre.to_csv(rpt, p, log_name=os.path.basename(log), test_name=testname)
                            generated_all.append(p)

                def done():
                    self._off_prog.stop(); self._off_prog.grid_remove()
                    self._off_status.set("Done")
                    self.report_dir = out_dir
                    self.current_tests = self._read_tests_files(self._off_tests)
                    if first_html_local and self._off_open.get():
                        webbrowser.open(first_html_local)
                    if generated_all:
                        messagebox.showinfo("Done", "\n".join(generated_all))
                    else:
                        messagebox.showinfo("Done", "No outputs.")
                self.root.after(0, done)
            except Exception:
                err = traceback.format_exc()
                def fail():
                    self._off_prog.stop(); self._off_prog.grid_remove()
                    self._off_status.set("Error")
                    messagebox.showerror("Run error", err)
                self.root.after(0, fail)

        threading.Thread(target=worker, daemon=True).start()

    # === CHUNK 5/8 — Online tab: DLT, HMI, Online Tests, Exec =========

    def _build_online_tab(self):
        tab = ttk.Frame(self.nb, style="Card.TFrame", padding=12)
        self.nb.add(tab, text="Online")

        for c in range(22):
            tab.grid_columnconfigure(c, weight=(2 if c>=6 else 0))
        tab.grid_rowconfigure(10, weight=1)

        # ---- DLT Controls ----
        lf_dlt = ttk.LabelFrame(tab, text="DLT Controls", padding=10)
        lf_dlt.grid(row=0, column=0, columnspan=6, sticky="nsew", pady=(0,8))
        for c in range(6): lf_dlt.grid_columnconfigure(c, weight=1 if c in (1,3,5) else 0)

        ttk.Label(lf_dlt, text="Host").grid(row=0, column=0, sticky="w")
        self.on_ent_host = ttk.Entry(lf_dlt, width=16); self.on_ent_host.grid(row=0, column=1, sticky="w", padx=(4,8))
        ttk.Label(lf_dlt, text="Port").grid(row=0, column=2, sticky="w")
        self.on_ent_port = ttk.Entry(lf_dlt, width=8); self.on_ent_port.grid(row=0, column=3, sticky="w", padx=(4,8))

        # prefill from config
        self.on_ent_host.delete(0, tk.END); self.on_ent_host.insert(0, str(self.cfg.get("dlt_host","127.0.0.1")))
        self.on_ent_port.delete(0, tk.END); self.on_ent_port.insert(0, str(self.cfg.get("dlt_port",3490)))

        ttk.Label(lf_dlt, text="Output folder").grid(row=1, column=0, sticky="w", pady=(6,0))
        self.on_ent_out = ttk.Entry(lf_dlt); self.on_ent_out.grid(row=1, column=1, columnspan=3, sticky="we", padx=(4,8), pady=(6,0))
        self.on_ent_out.insert(0, self.report_dir or REPORTS_DIR)
        ttk.Button(lf_dlt, text="Browse", command=lambda: self._pick_dir(self.on_ent_out, REPORTS_DIR)).grid(row=1, column=4, sticky="w", pady=(6,0))
        ttk.Button(lf_dlt, text="Save", command=self._save_dlt).grid(row=0, column=4, sticky="w")

        # ---- HMI Controls ----
        lf_hmi = ttk.LabelFrame(tab, text="HMI Controls (Android)", padding=10)
        lf_hmi.grid(row=1, column=0, columnspan=6, sticky="nsew", pady=(0,8))
        for c in range(6): lf_hmi.grid_columnconfigure(c, weight=1 if c in (1,3,5) else 0)

        self._adb_devices_text = tk.StringVar(value="ADB: press Detect")
        ttk.Label(lf_hmi, textvariable=self._adb_devices_text, style="Muted.TLabel").grid(row=0, column=0, columnspan=6, sticky="w")

        ttk.Label(lf_hmi, text="Scale:", style="Muted.TLabel").grid(row=1, column=0, sticky="e", pady=(6,0))
        self._scale_mode = tk.StringVar(value="Fit")
        ttk.Combobox(lf_hmi, textvariable=self._scale_mode, values=["Fit","100%","50%","33%","25%","200%"],
                     width=10, state="readonly").grid(row=1, column=1, sticky="w", padx=(6,12), pady=(6,0))

        ttk.Button(lf_hmi, text="Detect", command=self._adb_detect).grid(row=1, column=2, sticky="w", pady=(6,0))
        ttk.Button(lf_hmi, text="Start Preview", style="Accent.TButton", command=self._preview_start).grid(row=1, column=3, sticky="w", pady=(6,0))
        ttk.Button(lf_hmi, text="Stop Preview", command=self._preview_stop).grid(row=1, column=4, sticky="w", pady=(6,0))
        ttk.Button(lf_hmi, text="Save Screenshot", command=self._preview_save).grid(row=1, column=5, sticky="w", pady=(6,0))

        self._last_tap = tk.StringVar(value="Last tap: —")
        ttk.Label(lf_hmi, textvariable=self._last_tap, style="Muted.TLabel").grid(row=2, column=0, columnspan=6, sticky="w", pady=(6,0))

        # ---- HMI Preview (right) ----
        lf_prev = ttk.LabelFrame(tab, text="HMI Preview", padding=10)
        lf_prev.grid(row=0, column=6, columnspan=15, rowspan=7, sticky="nsew", padx=(8,0), pady=(0,8))
        for c in range(6): lf_prev.grid_columnconfigure(c, weight=1)
        lf_prev.grid_rowconfigure(0, weight=1)
        self._preview_canvas = tk.Canvas(lf_prev, bg="#111"); self._preview_canvas.grid(row=0, column=0, columnspan=6, sticky="nsew")
        self._preview_canvas.bind("<Configure>", lambda e: self._preview_refresh_if_fit())
        self._preview_canvas.bind("<Button-1>", self._preview_tap)

        self._online_preview_on = {"run": False}
        self._preview_state = {"img":None,"iw":0,"ih":0,"dw":0,"dh":0,"x0":0,"y0":0,"scale":1.0}
        self._dev_size: Tuple[int,int] = (0,0)
        self._preview_ms = 400

        # ---- Online Tests pane ----
        self._online_tests_files: List[str] = []
        lf_tests = ttk.LabelFrame(tab, text="Online Tests (JSON)", padding=10)
        lf_tests.grid(row=2, column=0, columnspan=6, sticky="nsew", pady=(0,8))
        lf_tests.grid_columnconfigure(0, weight=1); lf_tests.grid_rowconfigure(0, weight=1)

        self.on_lst_tests = tk.Listbox(lf_tests, selectmode=tk.EXTENDED, height=6)
        self.on_lst_tests.grid(row=0, column=0, sticky="nsew")
        scr_on_tests = ttk.Scrollbar(lf_tests, orient="vertical", command=self.on_lst_tests.yview)
        scr_on_tests.grid(row=0, column=1, sticky="ns")
        self.on_lst_tests.configure(yscrollcommand=scr_on_tests.set)

        bar = ttk.Frame(lf_tests); bar.grid(row=1, column=0, columnspan=2, sticky="w", pady=(6,0))
        ttk.Button(bar, text="Add…",           command=self._on_add_online_tests).grid(row=0, column=0, padx=(0,6))
        ttk.Button(bar, text="Remove",         command=self._on_remove_online_tests).grid(row=0, column=1, padx=(0,6))
        ttk.Button(bar, text="Clear",          command=self._on_clear_online_tests).grid(row=0, column=2, padx=(0,12))
        ttk.Button(bar, text="Load from Offline", command=self._on_load_from_offline).grid(row=0, column=3, padx=(0,6))

        # ---- Execution Controls ----
        lf_exec = ttk.LabelFrame(tab, text="Execution Controls", padding=10)
        lf_exec.grid(row=3, column=0, columnspan=6, sticky="nsew", pady=(0,8))
        for c in range(6): lf_exec.grid_columnconfigure(c, weight=1 if c in (2,3,4,5) else 0)

        self._online_status = tk.StringVar(value=("Online engine unavailable" if SessionManager is None else "Disconnected."))
        self._online_prog = ttk.Progressbar(lf_exec, style="Color.Horizontal.TProgressbar", mode="indeterminate", length=220)
        self._online_prog.grid(row=0, column=4, sticky="e", padx=(12,0)); self._online_prog.grid_remove()

        self._online_mgr: Optional[SessionManager] = None
        self._online_lock = threading.Lock()

        self._btn_start = ttk.Button(lf_exec, text="Start", style="Accent.TButton", command=self._online_start); self._btn_start.grid(row=0, column=0, sticky="w")
        self._btn_pause = ttk.Button(lf_exec, text="Pause", command=self._online_pause, state="disabled"); self._btn_pause.grid(row=0, column=1, sticky="w")
        self._btn_resume= ttk.Button(lf_exec, text="Resume", command=self._online_resume, state="disabled"); self._btn_resume.grid(row=0, column=2, sticky="w")
        self._btn_stop  = ttk.Button(lf_exec, text="Stop",   command=self._online_stop, state="disabled");  self._btn_stop.grid(row=0, column=3, sticky="w")
        self._btn_send = ttk.Button(lf_exec, text="Send", command=self._on_send_curl); self._btn_send.grid(row=1, column=0, padx=5, pady=2, sticky="w")


        ttk.Label(lf_exec, textvariable=self._online_status, style="Muted.TLabel").grid(row=0, column=5, sticky="e")

        # ---- Execution Progress ----
        tk.Label(tab, text="Execution Progress", font=("Segoe UI", 14, "bold"),
                 fg=PALETTE["accent"], bg=PALETTE["card"]).grid(row=8, column=0, sticky="w", pady=(4,4))
        fr_prog = ttk.Frame(tab); fr_prog.grid(row=9, column=0, columnspan=21, sticky="nsew")
        tab.grid_rowconfigure(9, weight=1)
        fr_prog.grid_columnconfigure(0, weight=1); fr_prog.grid_rowconfigure(0, weight=1)
        self.on_tree = ttk.Treeview(fr_prog, columns=("idx","name","vc","result","line"), show="headings", height=14)
        self.on_tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(fr_prog, orient="vertical", command=self.on_tree.yview); vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(fr_prog, orient="horizontal", command=self.on_tree.xview); hsb.grid(row=1, column=0, sticky="we")
        self.on_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        for c,t,w in (("idx","#",60),("name","Test step",280),("vc","VC",520),("result","Result",120),("line","Line",800)):
            self.on_tree.heading(c, text=t); self.on_tree.column(c, width=w, stretch=(c!="result"))
        self._tree_tags(self.on_tree)

    # === CHUNK 6/8 — Online helpers: Tests, DLT save, Progress hooks ===

    # Online tests listbox ops
    def _on_add_online_tests(self):
        paths = filedialog.askopenfilenames(title="Select test cases (JSON)",
                                            initialdir=_initdir(TESTS_DIR),
                                            filetypes=[("JSON","*.json"),("All","*.*")])
        for p in paths:
            if p and (p not in self._online_tests_files):
                self._online_tests_files.append(p)
        self._refresh_online_tests()

    def _on_remove_online_tests(self):
        sel = list(reversed(self.on_lst_tests.curselection()))
        for i in sel:
            try: del self._online_tests_files[i]
            except Exception: pass
        self._refresh_online_tests()

    def _on_clear_online_tests(self):
        self._online_tests_files.clear()
        self._refresh_online_tests()

    def _on_load_from_offline(self):
        self._online_tests_files = list(getattr(self, "_off_tests", []))
        self._refresh_online_tests()

    def _refresh_online_tests(self):
        self.on_lst_tests.delete(0, tk.END)
        for p in self._online_tests_files:
            self.on_lst_tests.insert(tk.END, p)

    # DLT save
    def _save_dlt(self):
        host = self.on_ent_host.get().strip() or "127.0.0.1"
        port = self.on_ent_port.get().strip() or "3490"
        try:
            port_int = int(port)
        except:
            return messagebox.showerror("Invalid", "Port must be integer.")
        self.cfg = save_config_patch({"dlt_host": host, "dlt_port": port_int})
        self.payload_extract = make_payload_extractor(self.cfg)
        messagebox.showinfo("Saved", "DLT Host/Port saved.")

    # File dialog helper
    def _pick_dir(self, entry: ttk.Entry, initial: str):
        p = filedialog.askdirectory(initialdir=_initdir(initial))
        if p:
            entry.delete(0, tk.END); entry.insert(0, p)

    # Progress add/set used by engine callbacks
    def _on_progress_add(self, idx, name, vc):
        iid = self.on_tree.insert("", tk.END, values=(idx, name, vc, "running…", ""), tags=("running",))
        self._apply_row_tag(self.on_tree, iid, "RUNNING")
        self.on_tree.see(iid)
        return iid

    def _on_progress_set(self, iid, status, line=""):
        self.on_tree.set(iid, "result", status)
        self.on_tree.set(iid, "line", self.payload_extract(line or ""))
        self._apply_row_tag(self.on_tree, iid, status)
        self.on_tree.see(iid)

    # === CHUNK 7/9 — Online: Start / Pause / Resume / Stop (PATCH) ========

    def _online_start(self):
        if SessionManager is None:
            return messagebox.showerror("Online", f"Online engine not available.")

        # prefer Online Tests; fallback to Offline selection
        tests = self._read_tests_files(self._online_tests_files) if self._online_tests_files else (self.current_tests or [])
        if not tests:
            return messagebox.showerror("Missing", "Load test cases (Online pane or Offline tab) first.")

        host = self.on_ent_host.get().strip() or "127.0.0.1"
        try:
            port = int(self.on_ent_port.get().strip() or "3490")
        except Exception:
            return messagebox.showerror("Invalid", "Port must be integer.")
        out_dir = self.on_ent_out.get().strip() or (self.report_dir or REPORTS_DIR)
        if not os.path.isdir(out_dir):
            return messagebox.showerror("Invalid", "Select a valid output folder.")

        # reset table
        for i in self.on_tree.get_children():
            self.on_tree.delete(i)

        self._online_status.set("Starting…")
        self._btn_start.config(state="disabled")
        self._btn_stop.config(state="normal")
        self._btn_pause.config(state="normal")
        self._btn_resume.config(state="normal")
        self._online_prog.grid()
        self._online_prog.start(10)

        def on_steps_init(steps):
            for st in steps:
                iid = self._on_progress_add(st["idx"], st["name"], st.get("vc",""))
                # map idx->iid
                self._online_row_iids[int(st["idx"])] = iid

        def on_step_update(i, name, vc, result, line):
            iid = self._online_row_iids.get(int(i))
            if iid:
                self._on_progress_set(iid, result, line)

        self._online_row_iids = {}
        mgr = SessionManager(
            host=host,
            port=port,
            tests=tests,
            out_dir=out_dir,
            on_status=lambda m: self.root.after(0, self._online_status.set, m),
            on_steps_init=lambda s: self.root.after(0, on_steps_init, s),
            on_step_update=lambda i,n,v,r,l: self.root.after(0, on_step_update, i,n,v,r,l),
        )
        with self._online_lock:
            self._online_mgr = mgr

        # run engine in its own thread
        def worker():
            try:
                mgr.start()  # blocking in that thread
            except Exception as e:
                # FIX: capture the error message right here instead of using e in lambda
                err_msg = f"Error: {e}"
                self.root.after(0, self._online_status.set, err_msg)
            finally:
                # when engine ends on its own (auto-stop after last step or error), reset UI
                def finalize():
                    self._online_prog.stop()
                    self._online_prog.grid_remove()
                    self._btn_start.config(state="normal")
                    self._btn_stop.config(state="disabled")
                    self._btn_pause.config(state="disabled")
                    self._btn_resume.config(state="disabled")
                self.root.after(0, finalize)
        threading.Thread(target=worker, daemon=True).start()

    def _online_stop(self):
        with self._online_lock:
            mgr = self._online_mgr
        self._online_status.set("Stopping…")
        def w():
            try:
                if mgr:
                    mgr.stop()   # triggers socket shutdown; loop exits next tick
            finally:
                self.root.after(0, lambda: (
                    self._online_prog.stop(), self._online_prog.grid_remove(),
                    self._btn_start.config(state="normal"),
                    self._btn_stop.config(state="disabled"),
                    self._btn_pause.config(state="disabled"),
                    self._btn_resume.config(state="disabled"),
                    self._online_status.set("Stopped.")
                ))
        threading.Thread(target=w, daemon=True).start()

    def _online_pause(self):
        with self._online_lock:
            if self._online_mgr:
                self._online_mgr.pause()
                self._online_status.set("Paused.")

    def _online_resume(self):
        with self._online_lock:
            if self._online_mgr:
                self._online_mgr.resume()
                self._online_status.set("Resumed.")


# --- CHUNK 8 START — HMI preview: detect, scale, capture, show, tap, save ---

# ADB helpers (shell calls implemented in TRE_online/TRE_android for actions;
# here we only need preview capture using adbb exec-out)

    def _adb_detect(self):
        try:
            import subprocess
            out = subprocess.check_output(["adbb", "devices"], stderr=subprocess.STDOUT).decode("utf-8", errors="ignore")
            self._adb_devices_text.set(out.strip())
        except Exception as e:
            self._adb_devices_text.set(f"Error: {e}")

    def _update_dev_size(self):
        try:
            import subprocess
            out = subprocess.check_output(["adbb", "shell", "wm", "size"], stderr=subprocess.STDOUT).decode("utf-8", errors="ignore")
            for line in out.splitlines():
                if ":" in line and "x" in line:
                    tail = line.split(":",1)[1].strip()
                    w,h = tail.split("x")
                    self._dev_size = (int(w), int(h)); return
        except Exception:
            pass
        self._dev_size = (0,0)

    def _preview_start(self):
        # ensure device exists
        try:
            import subprocess
            out = subprocess.check_output(["adbb", "devices"], stderr=subprocess.STDOUT).decode("utf-8", errors="ignore")
            if "device" not in out:
                self._adb_devices_text.set(f"No device:\n{out}")
                messagebox.showerror("HMI Preview", "No Android device detected via 'adbb devices'.")
                return
        except Exception as e:
            self._adb_devices_text.set(f"Error: {e}")
            return
        self._update_dev_size()
        self._online_preview_on["run"] = True
        self._adb_devices_text.set("Preview running.")
        self._preview_loop()

    def _preview_stop(self):
        self._online_preview_on["run"] = False

    def _preview_loop(self):
        if not self._online_preview_on["run"]: return
        try:
            self._preview_show_latest()
        except Exception as e:
            self._online_preview_on["run"] = False
            messagebox.showerror("HMI Preview", f"Capture failed:\n{e}")
            return
        self.root.after(self._preview_ms, self._preview_loop)

    def _apply_scale(self, img: tk.PhotoImage, cw: int, ch: int) -> Tuple[tk.PhotoImage, float]:
        iw, ih = img.width(), img.height()
        m = self._scale_mode.get()
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

    def _preview_capture_once(self) -> Optional[str]:
        """
        Capture a single PNG frame from the device.
        Strategy:
          1) Try fast path: adbb exec-out screencap -p  (binary to file)
          2) If that fails or PNG signature invalid, try two-step fallback:
             shell screencap -p /sdcard/__tre_cap.png  -> pull to SHOTS_DIR
        Returns: local file path or None
        """
        # IMPORTANT: use datetime for microseconds (%f); time.strftime doesn't support %f
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        path = os.path.join(SHOTS_DIR, f"preview_{ts}.png")

        def _is_png_ok(p: str) -> bool:
            try:
                if not os.path.exists(p) or os.path.getsize(p) < 32:
                    return False
                with open(p, "rb") as f:
                    sig = f.read(8)
                return sig == b"\x89PNG\r\n\x1a\n"
            except Exception:
                return False

        # 1) fast path: exec-out
        try:
            import subprocess
            with open(path, "wb") as f:
                r = subprocess.run(
                    ["adbb", "exec-out", "screencap", "-p"],
                    timeout=3.0, stdout=f, stderr=subprocess.DEVNULL
                )
            if r.returncode == 0 and _is_png_ok(path):
                return path
        except Exception:
            pass
        # clean any partial
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass

        # 2) fallback: shell + pull
        try:
            import subprocess
            remote = "/sdcard/__tre_cap.png"
            # capture to remote
            r1 = subprocess.run(
                ["adbb", "shell", "screencap", "-p", remote],
                timeout=3.0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if r1.returncode != 0:
                return None
            # pull to local
            r2 = subprocess.run(
                ["adbb", "pull", remote, path],
                timeout=3.0, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL
            )
            if r2.returncode == 0 and _is_png_ok(path):
                return path
        except Exception:
            pass

        # failure
        try:
            if os.path.exists(path):
                os.remove(path)
        except Exception:
            pass
        return None

    def _preview_show_latest(self):
        """
        Capture and paint the latest frame on the canvas.
        Uses the payload scaler and pins a reference to PhotoImage to avoid Tk GC.
        Raises a ValueError with a helpful message if capture fails.
        """
        p = self._preview_capture_once()
        if not p or not os.path.exists(p):
            raise ValueError("Capture failed: no frame. Check device/ADB and try Detect again.")

        # Load the PNG; Tk supports PNG on Tk 8.6+, which your setup uses.
        try:
            img = tk.PhotoImage(file=p)
        except Exception as e:
            # Clean up the temp file then bubble up a clearer error
            try:
                os.remove(p)
            except Exception:
                pass
            raise ValueError(f"Capture failed: invalid PNG ({e}). Try the fallback commands manually.")

        iw, ih = img.width(), img.height()
        cw = max(200, int(self._preview_canvas.winfo_width() or 900))
        ch = max(200, int(self._preview_canvas.winfo_height() or 700))
        disp, sc = self._apply_scale(img, cw, ch)
        dw, dh = disp.width(), disp.height()
        x0, y0 = max(0, (cw - dw) // 2), max(0, (ch - dh) // 2)

        self._preview_canvas.delete("all")
        self._preview_canvas.create_image(x0, y0, anchor="nw", image=disp)
        self._preview_canvas.image = disp  # pin to avoid GC
        self._preview_state.update({
            "img": disp, "iw": iw, "ih": ih, "dw": dw, "dh": dh, "x0": x0, "y0": y0, "scale": sc
        })

        # best-effort cleanup of the temp file
        try:
            os.remove(p)
        except Exception:
            pass

    def _preview_refresh_if_fit(self):
        if self._scale_mode.get() == "Fit" and self._online_preview_on["run"]:
            self._preview_show_latest()

    def _preview_tap(self, ev):
        st = self._preview_state
        if not st["img"]: return
        x0,y0 = st["x0"], st["y0"]; dw,dh = st["dw"], st["dh"]
        iw,ih = st["iw"], st["ih"]; sc = st["scale"]
        if not (x0 <= ev.x <= x0+dw and y0 <= ev.y <= y0+dh): return
        rx, ry = ev.x - x0, ev.y - y0
        ox, oy = int(rx/sc), int(ry/sc)
        dx, dy = (self._dev_size if self._dev_size != (0,0) else (iw, ih))
        tx = int(ox * dx / iw); ty = int(oy * dy / ih)
        self._last_tap.set(f"Last tap: ({tx},{ty})")
        try:
            self.root.clipboard_clear()
            self.root.clipboard_append(
                f'{{"action": {{"type":"tap","x":{tx},"y":{ty}}}}}\n'
                f'{{"action": {{"type":"tap_pct","px":{tx/max(dx,1):.4f},"py":{ty/max(dy,1):.4f}}}}}'
            )
        except Exception:
            pass

    def _preview_save(self):
        shots_dir = os.path.join(self.on_ent_out.get().strip() or REPORTS_DIR, "Screenshots")
        os.makedirs(shots_dir, exist_ok=True)
        ts = datetime.datetime.now().strftime("%Y-%m-%d_%H-%M-%S")
        out = os.path.join(shots_dir, f"shot_{ts}.png")
        try:
            import subprocess
            with open(out, "wb") as f:
                r = subprocess.run(["adbb", "exec-out", "screencap", "-p"], timeout=3.0,
                                   stdout=f, stderr=subprocess.DEVNULL)
            if r.returncode == 0 and os.path.exists(out):
                messagebox.showinfo("Screenshot", f"Saved:\n{out}")
            else:
                messagebox.showerror("Screenshot", "Failed.")
        except Exception as e:
            messagebox.showerror("Screenshot", str(e))

    # --- CHUNK 8 END ---
    # --- CHUNK 9 START — Help & About tabs (place INSIDE class TREApp) ---

    def _build_help_tab(self):
        tab = ttk.Frame(self.nb, style="Card.TFrame", padding=12)
        self.nb.add(tab, text="Help")
        text = (
            f"{APP_NAME} {APP_VERSION}\n\n"
            "Offline:\n"
            " 1) Add logs\n"
            " 2) Add Test cases (JSON)\n"
            " 3) Choose output folder\n"
            " 4) Run (HTML/CSV/JSON as selected)\n\n"
            "Online:\n"
            " • Set DLT host/port (Save to remember)\n"
            " • Load Test JSON (Online pane or use 'Load from Offline')\n"
            " • Optional HMI Preview (Detect → Start Preview)\n"
            " • Start to execute steps live\n\n"
            "Line column payload:\n"
            " Controlled by Code/TRE_config.json\n"
            "  - payload_regex (single capture group)\n"
            "  - payload_mode: after_last_pipe | after_last_bracket | after_last_colon\n"
        )
        tk.Message(
            tab, text=text, width=860,
            bg=PALETTE["card"], fg=PALETTE["fg"], font=("Segoe UI", 10)
        ).pack(anchor="w")

    def _build_about_tab(self):
        tab = ttk.Frame(self.nb, style="Card.TFrame", padding=12)
        self.nb.add(tab, text="About")
        tk.Label(
            tab, text=f"{APP_NAME} {APP_VERSION}",
            font=("Segoe UI", 16, "bold"), fg=PALETTE["accent"], bg=PALETTE["card"]
        ).pack(anchor="w", pady=(0,8))
        tk.Label(tab, text=f"© {AUTHOR_NAME}", bg=PALETTE["card"]).pack(anchor="w")
        tk.Label(tab, text=f"{AUTHOR_EMAIL}", bg=PALETTE["card"]).pack(anchor="w")

    # --- CHUNK 9 END ---

# === Entrypoint =======================================================

if __name__ == "__main__":
    app = TREApp()
    app.run()

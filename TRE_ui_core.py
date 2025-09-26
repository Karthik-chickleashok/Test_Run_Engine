# TRE_ui_core.py — Stability Drop v2.2.0
# Core UI for Test Run Engine (TRE)
# - Owns Tk root + Notebook
# - Builds Offline tab (logs vs test cases) inside __init__
# - Provides helpers for Online tab attachment, Help/About tabs, and run()
# - Centralizes theme + config + payload extraction
#
# Public API (used by TRE_ui.pyw and TRE_ui_online_lab.py):
#   AppCore(APP_NAME, APP_VERSION, AUTHOR_NAME, AUTHOR_EMAIL)
#   .nb                         -> ttk.Notebook
#   .add_disabled_online_tab(error: str)
#   .attach_help_tab()
#   .attach_about_tab()
#   .run()
#   .load_config() / .save_config(dict)
#   .payload_extract(str) -> str (payload-only text for "Line" column)
#   .current_tests        -> list[dict] (merged tests from Offline tab)
#   .report_dir           -> str (last output folder; Online tab uses this)
#
# Requires:
#   TRE_json.py  (stable JSON/HTML/CSV engines we built earlier)

import os, sys, json, threading, traceback, webbrowser, datetime, re
import tkinter as tk
from tkinter import ttk, filedialog, messagebox
from typing import Dict, Any, List, Optional, Tuple, Callable

# ---- External stable engine ----
try:
    import TRE_json as tre
except Exception as e:
    tk.Tk().withdraw()
    messagebox.showerror("Import error", f"TRE_json import failed:\n{e}")
    sys.exit(1)

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ROOT_DIR   = os.path.dirname(SCRIPT_DIR)
LOGS_DIR   = os.path.join(ROOT_DIR, "Logs")
REPORTS_DIR= os.path.join(ROOT_DIR, "Reports")
TESTS_DIR  = os.path.join(ROOT_DIR, "Test_cases")
SHOTS_DIR  = os.path.join(ROOT_DIR, "Screenshots")
CONFIG_PATH= os.path.join(SCRIPT_DIR, "TRE_config.json")
for d in (LOGS_DIR, REPORTS_DIR, TESTS_DIR, SHOTS_DIR):
    os.makedirs(d, exist_ok=True)

# --------------- Payload extractor (configurable) ----------------
def make_payload_extractor(cfg: Dict[str, Any]) -> Callable[[str], str]:
    """
    Returns a function that extracts the payload/message part from a raw DLT line.
    Priority:
      1) payload_regex: a regex with ONE capturing group; group(1) returned on match
      2) payload_mode: 'after_last_pipe' | 'after_last_bracket' | 'after_last_colon'
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
            # fall through to mode/heuristic if regex is bad
            pass

    if mode == "after_last_pipe":
        return lambda s: (s.rsplit("|", 1)[-1].strip() if s else s)
    if mode == "after_last_bracket":
        return lambda s: (s.rsplit("]", 1)[-1].strip() if s else s)
    if mode == "after_last_colon":
        return lambda s: (s.rsplit(":", 1)[-1].strip() if s else s)

    # Heuristic fallback
    def _heuristic(s: str) -> str:
        if not s: return s
        s = s.replace("\x00", "").strip()
        # After last ']'
        if ']' in s:
            pos = s.rfind(']')
            if pos != -1 and pos + 1 < len(s):
                tail = s[pos+1:].strip(" -:|")
                if tail:
                    s = tail
        # Pipe-separated: take last
        if '|' in s:
            parts = [p.strip() for p in s.split('|') if p.strip()]
            if len(parts) > 1:
                s = parts[-1]
        # Fallback after last colon
        if ':' in s:
            left, right = s.rsplit(':', 1)
            if len(right.strip()) >= 3:
                s = right.strip()
        return s

    return _heuristic

# --------------- Theme ----------------
THEMES = {
    "Classic Blue/Grey": {
        "app":"#f8fafc","card":"#ffffff","fg":"#0f172a","muted":"#475569",
        "accent":"#2563eb","accent_fg":"#ffffff","header_bg":"#0f172a","header_fg":"#ffffff","progress":"#2563eb",
        "row_running_bg":"#e5e7eb","row_running_fg":"#374151",
        "row_pass_bg":"#d1fae5","row_pass_fg":"#065f46",
        "row_fail_bg":"#fee2e2","row_fail_fg":"#7f1d1d",
        "row_error_bg":"#fde68a","row_error_fg":"#92400e",
    }
}

def _initdir(p): return p if os.path.isdir(p) else ROOT_DIR

# --------------- AppCore ----------------
class AppCore:
    def __init__(self, app_name: str, app_version: str, author_name: str, author_email: str):
        self.APP_NAME    = app_name
        self.APP_VERSION = app_version
        self.AUTHOR_NAME = author_name
        self.AUTHOR_EMAIL= author_email
        self.PALETTE     = THEMES["Classic Blue/Grey"]

        # Tk root + style
        self.root = tk.Tk()
        self.root.title(f"{self.APP_NAME} {self.APP_VERSION}")
        self.root.geometry("1440x980")
        self.style = ttk.Style(self.root)
        self._apply_theme()

        # Header + Notebook
        self._build_header()
        self.nb = ttk.Notebook(self.root)
        self.nb.pack(fill="both", expand=True, padx=12, pady=12)

        # Config once; payload extractor shared across panes
        self._boot_cfg = self.load_config()
        self.payload_extract: Callable[[str], str] = make_payload_extractor(self._boot_cfg)

        # State shared with Online tab
        self.current_tests: List[Dict[str, Any]] = []
        self.report_dir = REPORTS_DIR

        # Offline tab (stable)
        self.off_logs: List[str] = []
        self.off_tests: List[str] = []
        self.off_progress_tree: Optional[ttk.Treeview] = None
        self.off_prog: Optional[ttk.Progressbar] = None
        self.off_status = tk.StringVar(value="Ready.")
        self.stop_event_off = threading.Event()
        self._build_offline_tab()

        # Window close
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.root.minsize(1260, 920)

    # ---------- Theme / Header ----------
    def _apply_theme(self):
        p = self.PALETTE
        self.root.configure(bg=p["app"])
        self.style.theme_use("clam")
        # General surfaces
        self.style.configure("Card.TFrame", background=p["card"])
        self.style.configure("TFrame", background=p["card"])
        self.style.configure("TLabel", background=p["card"], foreground=p["fg"])
        self.style.configure("Muted.TLabel", background=p["card"], foreground=p["muted"])
        # Buttons / Progress
        self.style.configure("Accent.TButton",
                             background=p["accent"], foreground=p["accent_fg"],
                             font=("Segoe UI", 10, "bold"), padding=8)
        self.style.map("Accent.TButton", background=[("active", p["accent"])])
        self.style.configure("Color.Horizontal.TProgressbar",
                             troughcolor=p["card"], background=p["progress"])

    def _build_header(self):
        p = self.PALETTE
        header = tk.Frame(self.root, bg=p["header_bg"])
        header.pack(fill="x")
        tk.Label(header, text=self.APP_NAME, fg=p["header_fg"], bg=p["header_bg"],
                 font=("Segoe UI", 18, "bold"), padx=12, pady=10).pack(side="left")
        tk.Button(header, text="Help",
                  command=lambda: messagebox.showinfo(
                      "Help",
                      f"{self.APP_NAME} {self.APP_VERSION}\n\n"
                      "Offline: compare Logs vs Test cases, see per-step progress, export HTML/CSV/JSON.\n"
                      "Online: live DLT with HMI preview (Android), per-step progress.\n\n"
                      "Settings live in Code/TRE_config.json.\n"
                      "Line payload display can be controlled via payload_regex or payload_mode.\n"
                  ),
                  bg=p["header_bg"], fg=p["header_fg"], bd=0, padx=12, pady=6).pack(side="right", padx=8, pady=8)
        tk.Button(header, text="About",
                  command=lambda: messagebox.showinfo(
                      "About",
                      f"{self.APP_NAME} {self.APP_VERSION}\n© {self.AUTHOR_NAME}\n{self.AUTHOR_EMAIL}"
                  ),
                  bg=p["header_bg"], fg=p["header_fg"], bd=0, padx=12, pady=6).pack(side="right", padx=8, pady=8)

    # ---------- Config ----------
    def load_config(self) -> Dict[str, Any]:
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}

    def save_config(self, cfg: Dict[str, Any]):
        try:
            base = self.load_config()
            base.update(cfg)
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(base, f, indent=2)
            # if payload rules changed, rebuild extractor live
            self.payload_extract = make_payload_extractor(base)
            messagebox.showinfo("Saved", "Settings saved.")
        except Exception as e:
            messagebox.showerror("Save failed", str(e))

    # ---------- Common helpers ----------
    def add_files(self, listbox, store, types, title, initialdir, validate_json=False):
        paths = filedialog.askopenfilenames(title=title, filetypes=types, initialdir=_initdir(initialdir))
        for p in paths:
            if p and p not in store:
                if validate_json:
                    ok, msg = tre.validate_tests_json(p)
                    messagebox.showinfo("Test JSON", f"{os.path.basename(p)}\n{msg}")
                store.append(p)
                listbox.insert(tk.END, p)
        if store is self.off_tests:
            self._reload_current_tests()

    def remove_selected(self, listbox, store):
        for idx in reversed(listbox.curselection()):
            store.remove(listbox.get(idx))
            listbox.delete(idx)
        if store is self.off_tests:
            self._reload_current_tests()

    def clear_list(self, listbox, store):
        store.clear()
        listbox.delete(0, tk.END)
        if store is self.off_tests:
            self._reload_current_tests()

    def _apply_tag(self, tree, iid, status_text):
        s = (status_text or "").strip().upper()
        tag = "running" if s not in ("PASS","FAIL","ERROR") else s.lower()
        tree.item(iid, tags=(tag,))

    # ---------- Offline Tab (Stable) ----------
    def _build_offline_tab(self):
        p = self.PALETTE
        off_tab = ttk.Frame(self.nb, style="Card.TFrame", padding=12)
        self.nb.add(off_tab, text="Offline")
        for i in range(6):
            off_tab.grid_columnconfigure(i, weight=1 if i in (1,3,5) else 0)

        # Logs
        ttk.Label(off_tab, text="Log files").grid(row=0, column=0, sticky="w")
        logs_frame = ttk.Frame(off_tab)
        logs_frame.grid(row=1, column=0, sticky="nsew", columnspan=2)
        logs_frame.grid_columnconfigure(0, weight=1)
        logs_frame.grid_rowconfigure(0, weight=1)
        lst_logs = tk.Listbox(logs_frame, selectmode=tk.EXTENDED, height=8)
        lst_logs.grid(row=0, column=0, sticky="nsew")
        scrollL = ttk.Scrollbar(logs_frame, orient="vertical", command=lst_logs.yview)
        scrollL.grid(row=0, column=1, sticky="ns")
        lst_logs.config(yscrollcommand=scrollL.set)
        self.lst_logs = lst_logs

        btnsL = ttk.Frame(off_tab)
        btnsL.grid(row=2, column=0, sticky="w", pady=(6,12), columnspan=2)
        ttk.Button(btnsL, text="Add…",
                   command=lambda: self.add_files(self.lst_logs, self.off_logs,
                                                  [("Logs","*.log *.txt *.csv"), ("All","*.*")],
                                                  "Select logs", LOGS_DIR)).grid(row=0, column=0, padx=4)
        ttk.Button(btnsL, text="Remove",
                   command=lambda: self.remove_selected(self.lst_logs, self.off_logs)).grid(row=0, column=1, padx=4)
        ttk.Button(btnsL, text="Clear",
                   command=lambda: self.clear_list(self.lst_logs, self.off_logs)).grid(row=0, column=2, padx=4)

        # Tests
        ttk.Label(off_tab, text="Test cases / suite (JSON)").grid(row=0, column=3, sticky="w")
        tests_frame = ttk.Frame(off_tab)
        tests_frame.grid(row=1, column=3, sticky="nsew", columnspan=2)
        tests_frame.grid_columnconfigure(0, weight=1)
        tests_frame.grid_rowconfigure(0, weight=1)
        lst_tests = tk.Listbox(tests_frame, selectmode=tk.EXTENDED, height=8)
        lst_tests.grid(row=0, column=0, sticky="nsew")
        scrollT = ttk.Scrollbar(tests_frame, orient="vertical", command=lst_tests.yview)
        scrollT.grid(row=0, column=1, sticky="ns")
        lst_tests.config(yscrollcommand=scrollT.set)
        self.lst_tests = lst_tests

        btnsT = ttk.Frame(off_tab)
        btnsT.grid(row=2, column=3, sticky="w", pady=(6,12), columnspan=2)
        ttk.Button(btnsT, text="Add…",
                   command=lambda: self.add_files(self.lst_tests, self.off_tests,
                                                  [("JSON","*.json"), ("All","*.*")],
                                                  "Select test cases", TESTS_DIR, validate_json=True)).grid(row=0, column=0, padx=4)
        ttk.Button(btnsT, text="Remove",
                   command=lambda: self.remove_selected(self.lst_tests, self.off_tests)).grid(row=0, column=1, padx=4)
        ttk.Button(btnsT, text="Clear",
                   command=lambda: self.clear_list(self.lst_tests, self.off_tests)).grid(row=0, column=2, padx=4)

        # Output & options
        ttk.Label(off_tab, text="Output folder:").grid(row=3, column=0, sticky="w")
        self.off_ent_outdir = ttk.Entry(off_tab)
        self.off_ent_outdir.grid(row=3, column=1, sticky="we")
        self.off_ent_outdir.insert(0, REPORTS_DIR)
        ttk.Button(off_tab, text="Browse",
                   command=lambda: (lambda p=filedialog.askdirectory(initialdir=_initdir(REPORTS_DIR)):
                                    (self.off_ent_outdir.delete(0, tk.END), self.off_ent_outdir.insert(0, p)) if p else None)()).grid(row=3, column=2, sticky="w")

        ttk.Label(off_tab, text="Preview limit (0 = full):").grid(row=3, column=3, sticky="w")
        self.off_ent_prev = ttk.Entry(off_tab, width=10)
        self.off_ent_prev.insert(0, "0")
        self.off_ent_prev.grid(row=3, column=4, sticky="w")

        # Output toggles
        self.off_var_html = tk.BooleanVar(value=True)
        self.off_var_json = tk.BooleanVar(value=False)
        self.off_var_csv  = tk.BooleanVar(value=False)
        self.off_var_open = tk.BooleanVar(value=True)
        tog = ttk.Frame(off_tab)
        tog.grid(row=4, column=3, columnspan=3, sticky="w")
        ttk.Checkbutton(tog, text="Generate HTML", variable=self.off_var_html).grid(row=0, column=0, padx=(0,16))
        ttk.Checkbutton(tog, text="Generate JSON", variable=self.off_var_json).grid(row=0, column=1, padx=(0,16))
        ttk.Checkbutton(tog, text="Generate CSV",  variable=self.off_var_csv ).grid(row=0, column=2, padx=(0,16))
        ttk.Checkbutton(tog, text="Open first HTML after run", variable=self.off_var_open).grid(row=0, column=3, padx=(0,16))

        # Progress label (colored via tk.Label, not ttk)
        tk.Label(off_tab, text="Execution progress (per step)",
                 font=("Segoe UI", 14, "bold"),
                 fg=self.PALETTE["accent"], bg=self.PALETTE["card"]).grid(row=5, column=0, sticky="w", pady=(8,4))

        # Progress table
        prog_frame = ttk.Frame(off_tab)
        prog_frame.grid(row=6, column=0, columnspan=6, sticky="nsew")
        off_tab.grid_rowconfigure(6, weight=1)
        prog_frame.grid_columnconfigure(0, weight=1)
        prog_frame.grid_rowconfigure(0, weight=1)

        cols = ("idx","name","vc","result","line")
        self.off_progress_tree = ttk.Treeview(prog_frame, columns=cols, show="headings", height=14)
        self.off_progress_tree.grid(row=0, column=0, sticky="nsew")
        vsb = ttk.Scrollbar(prog_frame, orient="vertical", command=self.off_progress_tree.yview)
        vsb.grid(row=0, column=1, sticky="ns")
        hsb = ttk.Scrollbar(prog_frame, orient="horizontal", command=self.off_progress_tree.xview)
        hsb.grid(row=1, column=0, sticky="we")
        self.off_progress_tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)

        # Headings/columns
        for c, t, w in (("idx","#",60), ("name","Test step",280), ("vc","VC",520),
                        ("result","Result",120), ("line","Line",800)):
            self.off_progress_tree.heading(c, text=t)
            self.off_progress_tree.column(c, width=w, stretch=(c!="result"))

        # Row tag colors
        self.off_progress_tree.tag_configure("running", background=p["row_running_bg"], foreground=p["row_running_fg"])
        self.off_progress_tree.tag_configure("pass",    background=p["row_pass_bg"],    foreground=p["row_pass_fg"])
        self.off_progress_tree.tag_configure("fail",    background=p["row_fail_bg"],    foreground=p["row_fail_fg"])
        self.off_progress_tree.tag_configure("error",   background=p["row_error_bg"],   foreground=p["row_error_fg"])

        # Run controls
        self.off_run_btn   = ttk.Button(off_tab, text="Run", style="Accent.TButton", command=self._off_run_click)
        self.off_rerun_btn = ttk.Button(off_tab, text="Re-run last", command=self._off_rerun_click)
        self.off_run_btn.grid(row=7, column=0, sticky="w", pady=(12,8))
        self.off_rerun_btn.grid(row=7, column=1, sticky="w", pady=(12,8))

        self.off_prog = ttk.Progressbar(off_tab, style="Color.Horizontal.TProgressbar",
                                        mode="indeterminate", length=280)
        self.off_prog.grid(row=7, column=2, sticky="w", pady=(12,8))
        self.off_prog.grid_remove()

        ttk.Label(off_tab, textvariable=self.off_status, style="Muted.TLabel").grid(row=7, column=3, sticky="w", columnspan=3)

    # ---------- Offline run logic ----------
    def _off_progress_add(self, idx, step_name, vc_text):
        iid = self.off_progress_tree.insert("", tk.END,
                                            values=(idx, step_name, vc_text, "running…", ""),
                                            tags=("running",))
        self._apply_tag(self.off_progress_tree, iid, "RUNNING")
        self.off_progress_tree.see(iid)
        self.root.update_idletasks()
        return iid

    def _off_progress_set(self, iid, status, line=""):
        # Show payload-only in UI using configured extractor
        self.off_progress_tree.set(iid, "result", status)
        self.off_progress_tree.set(iid, "line", self.payload_extract(line or ""))
        self._apply_tag(self.off_progress_tree, iid, status)
        self.off_progress_tree.see(iid)
        self.root.update_idletasks()

    def _do_one_pair_off(self, log_path, test_path, out_dir, preview_limit):
        # Per-step live progress + final report generation via TRE_json
        lines = list(tre.iter_log(log_path))
        with open(test_path, "r", encoding="utf-8") as f:
            tests = json.load(f)

        # Live per-step (quick pass using TRE_json helpers)
        for idx, t in enumerate(tests, start=1):
            name = t.get("name","unnamed")
            vc = (t.get("find",{}).get("pattern","") if "find" in t else
                  t.get("not_find",{}).get("pattern","") if "not_find" in t else
                  " -> ".join([(e if isinstance(e,str) else e.get("pattern",""))
                               for e in t.get("sequence",[])]) if "sequence" in t else
                  f"[{t.get('action',{}).get('type','action')}]")
            iid = self._off_progress_add(idx, name, vc)
            try:
                if "find" in t:
                    det = tre.check_find(lines, t["find"])
                elif "not_find" in t:
                    det = tre.check_not_find(lines, t["not_find"])
                elif "sequence" in t:
                    det = tre.check_sequence(lines, t)
                elif "action" in t:
                    # Offline: do not execute actions; just informational
                    det = {"pass": True, "detail": {"line": f"[action] {t['action'].get('type','')}" }}
                else:
                    det = {"pass": False, "detail": {"error": "Unknown mode"}}
                status_text = "PASS" if det.get("pass") else "FAIL"
                line_text = (det.get("detail") or {}).get("line") if isinstance(det, dict) else ""
                self._off_progress_set(iid, status_text, line_text or "")
            except Exception:
                self._off_progress_set(iid, "ERROR")
            if self.stop_event_off.is_set():
                break

        # Final report (HTML/JSON/CSV)
        rpt = tre.run_checks(log_path, test_path)
        tre.adjust_line_numbers(rpt, 0)
        ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
        logstem  = os.path.splitext(os.path.basename(log_path))[0]
        testname = os.path.basename(test_path)
        teststem = os.path.splitext(testname)[0]
        base = teststem
        html_name = f"{base}_{logstem}_{'LATEST'}.html"
        json_name = f"{base}_{logstem}_{'LATEST'}.json"
        csv_name  = f"{base}_{logstem}_{'LATEST'}.csv"

        gen = []
        first_html = None
        if self.off_var_html.get():
            p = os.path.join(out_dir, html_name)
            tre.to_html(rpt, p, global_preview_limit=preview_limit,
                        title=f"{testname} · {logstem}",
                        generator_info=f"{self.APP_NAME} {self.APP_VERSION} — {self.AUTHOR_NAME} <{self.AUTHOR_EMAIL}>")
            gen.append(p); first_html = first_html or p
        if self.off_var_json.get():
            p = os.path.join(out_dir, json_name)
            with open(p, "w", encoding="utf-8") as f:
                json.dump(rpt, f, indent=2, ensure_ascii=False)
            gen.append(p)
        if self.off_var_csv.get():
            p = os.path.join(out_dir, csv_name)
            tre.to_csv(rpt, p, log_name=os.path.basename(log_path), test_name=testname)
            gen.append(p)

        return rpt, gen, first_html

    def _off_run_session(self, out_dir, preview_limit):
        # Clear table
        for i in self.off_progress_tree.get_children():
            self.off_progress_tree.delete(i)

        reports_ctx = []
        first_html_local = None
        generated_all = []

        for log in list(self.off_logs):
            for test in list(self.off_tests):
                if self.stop_event_off.is_set():
                    break
                rpt, gen, first = self._do_one_pair_off(log, test, out_dir, preview_limit)
                reports_ctx.append((rpt, os.path.basename(test), os.path.basename(log)))
                generated_all.extend(gen)
                if first and not first_html_local:
                    first_html_local = first
            if self.stop_event_off.is_set():
                break

        # Expose for Online tab convenience
        self.report_dir = out_dir
        self.current_tests = self._read_tests_files(self.off_tests)

        if first_html_local and self.off_var_open.get():
            webbrowser.open(first_html_local)
        messagebox.showinfo("Done", "\n".join(generated_all) if generated_all else "No outputs")

    def _off_run_click(self):
        if not self.off_logs:
            return messagebox.showerror("Missing", "Add log files")
        if not self.off_tests:
            return messagebox.showerror("Missing", "Add test cases (JSON)")
        out_dir = self.off_ent_outdir.get().strip() or REPORTS_DIR
        if not os.path.isdir(out_dir):
            return messagebox.showerror("Invalid", "Select a valid output folder")
        try:
            prev = int(self.off_ent_prev.get().strip() or "0")
        except:
            return messagebox.showerror("Invalid", "Preview limit must be integer")

        self.off_prog.grid()
        self.off_prog.start(10)
        self.off_run_btn.config(state="disabled")
        self.off_rerun_btn.config(state="disabled")
        self.off_status.set("Running…")

        threading.Thread(target=lambda: self._run_off_worker(out_dir, prev), daemon=True).start()

    def _run_off_worker(self, out_dir, prev):
        try:
            self._off_run_session(out_dir, prev)
            self.root.after(0, lambda: (
                self.off_prog.stop(), self.off_prog.grid_remove(),
                self.off_run_btn.config(state="normal"),
                self.off_rerun_btn.config(state="normal"),
                self.off_status.set("Done")
            ))
        except Exception:
            err = traceback.format_exc()
            self.root.after(0, lambda: (
                self.off_prog.stop(), self.off_prog.grid_remove(),
                self.off_run_btn.config(state="normal"),
                self.off_rerun_btn.config(state="normal"),
                self.off_status.set("Error"),
                messagebox.showerror("Run error", err)
            ))

    def _off_rerun_click(self):
        if not self.off_logs or not self.off_tests:
            return messagebox.showwarning("Re-run", "Add logs and tests first.")
        self._off_run_click()

    # ---------- helpers for Online tab ----------
    def _read_tests_files(self, paths: List[str]) -> List[Dict[str, Any]]:
        steps_all: List[Dict[str, Any]] = []
        for p in paths:
            try:
                with open(p, "r", encoding="utf-8") as f:
                    arr = json.load(f)
                if isinstance(arr, list):
                    steps_all.extend(arr)
            except Exception:
                pass
        return steps_all

    def _reload_current_tests(self):
        self.current_tests = self._read_tests_files(self.off_tests)

    # ---------- Optional: placeholder Online tab if lab fails ----------
    def add_disabled_online_tab(self, error: Optional[str]):
        tab = ttk.Frame(self.nb, style="Card.TFrame", padding=12)
        self.nb.add(tab, text="Online")
        msg = "Online tab failed to load."
        if error:
            msg += f"\n\n{error}"
        ttk.Label(tab, text=msg, style="Muted.TLabel").pack(anchor="w")

    # ---------- Help/About ----------
    def attach_help_tab(self):
        tab = ttk.Frame(self.nb, style="Card.TFrame", padding=12)
        self.nb.add(tab, text="Help")
        text = (
            f"{self.APP_NAME} {self.APP_VERSION}\n\n"
            "Quick Start (Offline):\n"
            "  1) Add log files from Logs folder.\n"
            "  2) Add test cases (JSON) from Test_cases.\n"
            "  3) Choose output folder (Reports) and Run.\n\n"
            "Quick Start (Online):\n"
            "  - In Online tab, set DLT Host/Port, Start.\n"
            "  - Use HMI preview to tap/record steps; screenshots saved under Reports/Screenshots.\n\n"
            "Payload Display:\n"
            "  - Edit Code/TRE_config.json to set 'payload_regex' (one capture group) or 'payload_mode'\n"
            "    ('after_last_pipe' | 'after_last_bracket' | 'after_last_colon').\n"
        )
        tk.Message(tab, text=text, width=840, bg=self.PALETTE["card"], fg=self.PALETTE["fg"],
                   font=("Segoe UI", 10)).pack(anchor="w")

    def attach_about_tab(self):
        tab = ttk.Frame(self.nb, style="Card.TFrame", padding=12)
        self.nb.add(tab, text="About")
        tk.Label(tab, text=f"{self.APP_NAME} {self.APP_VERSION}", font=("Segoe UI", 16, "bold"),
                 fg=self.PALETTE["accent"], bg=self.PALETTE["card"]).pack(anchor="w", pady=(0,8))
        tk.Label(tab, text=f"© {self.AUTHOR_NAME}", bg=self.PALETTE["card"]).pack(anchor="w")
        tk.Label(tab, text=f"{self.AUTHOR_EMAIL}", bg=self.PALETTE["card"]).pack(anchor="w")

    # ---------- Run ----------
    def run(self):
        self.root.mainloop()

    def _on_close(self):
        # If needed: signal any online/worker threads to stop (online tab manages its own)
        try:
            self.stop_event_off.set()
        except Exception:
            pass
        self.root.after(150, self.root.destroy)

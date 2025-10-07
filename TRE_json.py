#!/usr/bin/env python3
# TRE_json.py — core checks & report utilities used by TRE_ui.pyw / TRE_online.py
# Minimal, compatible implementation.

import os, re, json, csv, html, datetime
from typing import List, Dict, Any, Tuple

# ---------- Matching ----------
# TRE_json.py
import re

TRE_JSON_DEBUG = False
def _dbg(msg: str):
    if TRE_JSON_DEBUG:
        try: print("[TRE_JSON]", msg)
        except Exception: pass

_ctrl_rx = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\u200b-\u200f\u202a-\u202e]")
def _strip_controls(s: str) -> str:
    return _ctrl_rx.sub("", s)

def _norm_ws(s: str) -> str:
    s = s.replace("\t"," ").replace("\u00A0"," ").replace("\u2009"," ")
    return " ".join(s.split())

_punct_class = r"\(\)\[\]\{\}<>\|/\\,:;"
_rx_punct    = re.compile(rf"\s*([{_punct_class}])\s*")
_rx_tokens   = [
    (re.compile(r"\s*>>\s*"), ">>"),
    (re.compile(r"\s*<<\s*"), "<<"),
    (re.compile(r"\s*->\s*"), "->"),
    (re.compile(r"\s*=>\s*"), "=>"),
    (re.compile(r"\s*=\s*"),  "="),
]
def _squash_punct(s: str) -> str:
    s = s.replace("»"," >> ").replace("→","->")
    s = _rx_punct.sub(r"\1", s)
    for rx, rep in _rx_tokens:
        s = rx.sub(rep, s)
    return s

_anchor_rx = re.compile(r"[A-Za-z][A-Za-z0-9_]{2,}")
def _best_anchor_from_pattern(p: str):
    m = _anchor_rx.search(p)
    return m.group(0) if m else None

def _extract_payload_heuristic(line: str) -> str:
    s = line.lstrip()
    # quick trims of timestamps/[]/() if present
    for rx in (
        re.compile(r"^\[[^\]]*\]\s*"),
        re.compile(r"^\([^\)]*\)\s*"),
        re.compile(r"^\d{2,4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s*"),
        re.compile(r"^\d{1,2}:\d{2}:\d{2}(?:\.\d+)?\s*"),
    ):
        m = rx.match(s)
        if m: s = s[m.end():].lstrip()
    # after first strong sep
    for sep in (": ", "] ", "} ", "> "):
        p = s.find(sep)
        if 0 < p < 80:
            rest = s[p+len(sep):].lstrip()
            if re.search(r"[A-Za-z0-9]", rest):
                s = rest
                break
    return s

def line_matches(line: str, cfg: dict) -> bool:
    if not isinstance(cfg, dict):
        cfg = {"pattern": str(cfg), "literal": True}

    pat = str(cfg.get("pattern", ""))
    if not pat:
        return False

    literal      = bool(cfg.get("literal", False))
    equals       = bool(cfg.get("equals", False))
    ignore_case  = cfg.get("ignore_case", True) is not False
    norm_ws      = cfg.get("normalize_ws", True) is not False
    flex_punct   = cfg.get("flex_punct", True)  is not False
    wildcard     = cfg.get("wildcard", True)    is not False
    payload_only = cfg.get("payload_only", True) is not False
    use_anchor   = cfg.get("payload_anchor", True) is not False

    raw = _strip_controls(line)

    # --- build candidate targets (try in order) ---
    candidates = []

    if payload_only:
        # 1) payload heuristic
        candidates.append(_extract_payload_heuristic(raw))
        # 2) anchor slice (from first stable token in pattern)
        if use_anchor:
            anc = _best_anchor_from_pattern(pat)
            if anc:
                hay = raw if not ignore_case else raw.lower()
                ned = anc if not ignore_case else anc.lower()
                pos = hay.find(ned)
                if pos >= 0:
                    candidates.append(raw[pos:])
        # 3) simple colon slice
        p = raw.find(": ")
        if 0 < p < 120:
            candidates.append(raw[p+2:].lstrip())

    # 4) always include whole raw line as last fallback
    candidates.append(raw)

    # prep pattern once
    pat_s = pat
    if norm_ws:
        pat_s = _norm_ws(pat_s)
    if flex_punct:
        pat_s = _squash_punct(pat_s)
    if ignore_case:
        pat_s = pat_s.lower()

    # literal wildcard -> compile once
    pat_re = None
    if literal and wildcard:
        pat_escaped = re.escape(pat_s).replace(r"\*", ".*")
        pat_re = re.compile(f"^{pat_escaped}$" if equals else pat_escaped)

    for idx, tgt0 in enumerate(candidates, start=1):
        tgt = tgt0
        if norm_ws:   tgt = _norm_ws(tgt)
        if flex_punct: tgt = _squash_punct(tgt)
        if ignore_case: tgt = tgt.lower()

        if literal:
            if wildcard and pat_re:
                ok = pat_re.search(tgt) is not None
            else:
                ok = (tgt == pat_s) if equals else (pat_s in tgt)
        else:
            flags = re.IGNORECASE if ignore_case else 0
            try:
                ok = re.search(pat, tgt, flags) is not None
            except re.error:
                ok = (pat_s in tgt)

        if TRE_JSON_DEBUG:
            _dbg(f"cand#{idx} ok={ok} | pat='{pat_s}' | tgt='{tgt[:160]}'")

        if ok:
            return True

    return False




# ---------- Log reading ----------

def iter_log(path: str):
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for i, ln in enumerate(f, start=1):
            yield ln.rstrip("\r\n")


# ---------- Checkers ----------

def check_find(lines: List[str], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """ cfg: {pattern, literal?, min_count?} """
    minc = int(cfg.get("min_count", 1) or 1)
    count = 0
    first_line = None
    first_index = None
    for idx, line in enumerate(lines, start=1):
        if line_matches(line, cfg):
            count += 1
            if first_line is None:
                first_line = line
                first_index = idx
            if count >= minc:
                break
    return {
        "pass": count >= minc,
        "detail": {"vc": cfg.get("pattern",""), "count": count, "line": first_line, "index": first_index}
    }


def check_not_find(lines: List[str], cfg: Dict[str, Any]) -> Dict[str, Any]:
    """ cfg: {pattern, literal?} """
    for idx, line in enumerate(lines, start=1):
        if line_matches(line, cfg):
            return {
                "pass": False,
                "detail": {"vc": cfg.get("pattern",""), "line": line, "index": idx}
            }
    return {
        "pass": True,
        "detail": {"vc": cfg.get("pattern","")}
    }


def _norm_seq_elem(el, default_literal: bool) -> Dict[str, Any]:
    if isinstance(el, dict):
        d = dict(el)
        if "pattern" not in d:
            d["pattern"] = ""
        if "literal" not in d:
            d["literal"] = default_literal
        return d
    return {"pattern": str(el), "literal": default_literal}


def check_sequence(lines: List[str], step: Dict[str, Any]) -> Dict[str, Any]:
    """
    step: { "sequence": [ str | {pattern, literal?}, ... ], "literal"? }
    Matches each element in order.
    """
    seq = step.get("sequence", [])
    if not isinstance(seq, list):
        raise ValueError("sequence must be a list")
    default_literal = bool(step.get("literal") or step.get("sequence_literal"))
    seq_norm = [_norm_seq_elem(el, default_literal) for el in seq]

    pos = 0
    last_line = None
    for idx, line in enumerate(lines, start=1):
        if pos >= len(seq_norm):
            break
        if line_matches(line, seq_norm[pos]):
            last_line = line
            pos += 1
            if pos == len(seq_norm):
                return {
                    "pass": True,
                    "detail": {"vc": " -> ".join(e["pattern"] for e in seq_norm),
                               "seq_idx": pos, "line": last_line, "index": idx}
                }
    return {
        "pass": False,
        "detail": {"vc": " -> ".join(e["pattern"] for e in seq_norm), "seq_idx": pos}
    }


# ---------- Runner ----------

def run_checks(log_path: str, test_path: str) -> Dict[str, Any]:
    lines = list(iter_log(log_path))
    with open(test_path, "r", encoding="utf-8") as f:
        tests = json.load(f)
    if not isinstance(tests, list):
        raise ValueError("Top-level JSON must be a list of steps")

    results = []
    any_fail = False
    for t in tests:
        name = t.get("name", "unnamed")
        mode = "test"
        detail = {}
        ok = False
        try:
            if "find" in t:
                r = check_find(lines, t["find"])
                ok = r["pass"]; detail = r["detail"]
            elif "not_find" in t:
                r = check_not_find(lines, t["not_find"])
                ok = r["pass"]; detail = r["detail"]
            elif "sequence" in t:
                r = check_sequence(lines, t)
                ok = r["pass"]; detail = r["detail"]
            else:
                mode = "info"; ok = False; detail = {"error": "No valid check in step"}
        except Exception as e:
            ok = False; detail = {"error": f"{type(e).__name__}: {e}"}

        if not ok and mode == "test" and "error" not in detail:
            any_fail = True

        results.append({
            "name": name,
            "mode": mode,
            "pass": bool(ok),
            "detail": detail
        })

    summary_pass = not any_fail
    return {"summary_pass": summary_pass, "results": results}


# ---------- Report utilities ----------

def _html_head(title: str) -> str:
    return f"""<!doctype html>
<html><head><meta charset="utf-8">
<title>{html.escape(title)}</title>
<style>
body{{font-family:Segoe UI,Arial,sans-serif;background:#fff;color:#111;margin:24px}}
h1{{margin:0 0 16px 0}}
small.muted{{color:#667}}
table{{border-collapse:collapse;width:100%;}}
th,td{{border:1px solid #ddd;padding:8px;vertical-align:top}}
th{{background:#f7f7f7;text-align:left}}
.status-PASS{{background:#dcfce7}}
.status-FAIL{{background:#fee2e2}}
.status-ERROR{{background:#ffedd5}}
.status-running{{background:#f1f5f9}}
code{{white-space:pre-wrap;word-break:break-word}}
</style>
</head><body>
"""

def _html_tail() -> str:
    return "</body></html>"

def to_html(report: Dict[str, Any], path: str, global_preview_limit: int = 200,
            title: str = "Report", generator_info: str = ""):
    title_full = title or "Report"
    out = []
    out.append(_html_head(title_full))
    out.append(f"<h1>{html.escape(title_full)}</h1>")
    if generator_info:
        out.append(f'<small class="muted">{html.escape(generator_info)}</small><br><br>')
    out.append(f"<p><b>Summary:</b> {'PASS' if report.get('summary_pass') else 'FAIL'}</p>")
    out.append("<table><tr><th>#</th><th>Test step</th><th>VC</th><th>Result</th><th>Detail</th></tr>")
    for i, r in enumerate(report.get("results", []), start=1):
        name = r.get("name","")
        mode = r.get("mode","test")
        ok = r.get("pass", False)
        det = r.get("detail", {}) or {}
        vc = str(det.get("vc",""))
        status = "running" if mode == "info" and not ok and "error" not in det else ("PASS" if ok else ("ERROR" if "error" in det else "FAIL"))
        detail_items = []
        for k in ("count","seq_idx","index"):
            if k in det: detail_items.append(f"{k}: {det[k]}")
        if "line" in det and det["line"] is not None:
            detail_items.append("line:")
            # never truncate: show full
            detail_items.append(f"<code>{html.escape(det['line'])}</code>")
        if "error" in det:
            detail_items.append(f"error: <code>{html.escape(str(det['error']))}</code>")
        detail_html = "<br>".join(detail_items) if detail_items else ""
        out.append(f"<tr class='status-{status}'><td>{i}</td><td>{html.escape(name)}</td><td>{html.escape(vc)}</td><td>{status}</td><td>{detail_html}</td></tr>")
    out.append("</table>")
    out.append(_html_tail())
    with open(path, "w", encoding="utf-8") as f:
        f.write("".join(out))


def to_csv(report: Dict[str, Any], path: str, log_name: str = "", test_name: str = ""):
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["#","Test step","VC","Result","Count","SeqIdx","Index","Line","Error","Log","Test"])
        for i, r in enumerate(report.get("results", []), start=1):
            det = r.get("detail", {}) or {}
            mode = r.get("mode","test")
            result = "running" if mode == "info" and not r.get("pass") and "error" not in det else ("PASS" if r.get("pass") else ("ERROR" if "error" in det else "FAIL"))
            w.writerow([
                i,
                r.get("name",""),
                det.get("vc",""),
                result,
                det.get("count",""),
                det.get("seq_idx",""),
                det.get("index",""),
                det.get("line",""),
                det.get("error",""),
                log_name, test_name
            ])


def adjust_line_numbers(report: Dict[str, Any], offset: int = 0):
    """If you need to adjust 1-based indices, do it here (kept for compatibility)."""
    try:
        if not offset:
            return
        for r in report.get("results", []):
            d = r.get("detail", {})
            if "index" in d and isinstance(d["index"], int):
                d["index"] = max(1, d["index"] + offset)
    except Exception:
        pass


# ---------- Validator ----------

def validate_tests_json(path: str) -> Tuple[bool, str]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception as e:
        return False, f"JSON load error: {e}"

    if not isinstance(data, list):
        return False, "Top-level must be a list"

    for i, t in enumerate(data, start=1):
        if not isinstance(t, dict):
            return False, f"Step {i}: must be an object"
        if "name" not in t:
            t["name"] = f"step_{i}"
        modes = [k for k in ("find","not_find","sequence") if k in t]
        if not modes:
            return False, f"Step {i} ('{t.get('name')}'): missing one of find/not_find/sequence"
        if len(modes) > 1:
            return False, f"Step {i} ('{t.get('name')}'): multiple modes present ({modes})"

        if "find" in t:
            fnd = t["find"]
            if not isinstance(fnd, dict) or "pattern" not in fnd:
                return False, f"Step {i} ('{t.get('name')}'): find must be an object with 'pattern'"
            if "min_count" in fnd and not isinstance(fnd["min_count"], int):
                return False, f"Step {i} ('{t.get('name')}'): find.min_count must be integer"

        if "not_find" in t:
            nf = t["not_find"]
            if not isinstance(nf, dict) or "pattern" not in nf:
                return False, f"Step {i} ('{t.get('name')}'): not_find must be an object with 'pattern'"

        if "sequence" in t:
            seq = t["sequence"]
            if not isinstance(seq, list):
                return False, f"Step {i} ('{t.get('name')}'): sequence must be a list"
            # allow strings or dicts with 'pattern'
            for j, el in enumerate(seq, start=1):
                if isinstance(el, dict):
                    if "pattern" not in el:
                        return False, f"Step {i} ('{t.get('name')}') seq[{j}]: dict needs 'pattern'"
                elif not isinstance(el, str):
                    return False, f"Step {i} ('{t.get('name')}') seq[{j}]: must be string or dict"

    return True, f"OK — {len(data)} step(s) validated"

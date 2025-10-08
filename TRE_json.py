
#!/usr/bin/env python3
# TRE_json.py — core checks & report utilities used by TRE_ui.pyw / TRE_online.py
# Minimal, compatible implementation.

import os, re, json, csv, html, datetime
from typing import List, Dict, Any, Tuple

# ---------- Matching ----------
# TRE_json.py
# --- Add near top imports if not present ---
import re

# --- Robust DLT line splitter ---
_DLTCOLON = re.compile(r'::+')  # matches :: or ::: etc.

# tokens we know are noise in the payload stream
_DENY_TOKENS = {"TELETELE", "AOTA", "CCU2s", "CCU2c"}

# tail fragments like =CCU2x / =ABC123x that sometimes get glued to payloads
_re_tail_eq_tag = re.compile(r"=\s*[A-Z]{2,10}\d*[a-z]?\b")

def _split_dlt_header_and_payload(line: str):
    """
    Try to split a DLT-ish line into (ecu, app, ctx, payload).
    Heuristics:
      - Many tools emit 'ECU::APP::CTX: payload...'
      - Sometimes more fields exist; we take last 3 '::' separated chunks before payload.
      - If we can't parse, return (None, None, None, original_line).
    """
    s = line.strip()

    # If there are '::' separators, try to use them
    if '::' in s:
        parts = _DLTCOLON.split(s)
        # parts = [maybe prefix, ecu, app, ctx, tail...]
        # Find a plausible payload separator: the last ':' (single) or first space after ctx.
        # Simple heuristic: take last part as payload; 2-4 previous as ctx/app/ecu if available.
        if len(parts) >= 4:
            payload = parts[-1].lstrip(": ").strip()
            ctx = parts[-2].strip()
            app = parts[-3].strip()
            ecu = parts[-4].strip()
            # Sometimes there are more than 4; we only care about last four
            return (ecu or None, app or None, ctx or None, payload or "")
        elif len(parts) == 3:
            # ecu::app::payload
            ecu, app, payload = (parts[0].strip() or None,
                                 parts[1].strip() or None,
                                 parts[2].lstrip(": ").strip())
            return (ecu, app, None, payload or "")
        elif len(parts) == 2:
            # header::payload
            header, payload = parts[0].strip(), parts[1].lstrip(": ").strip()
            return (None, header or None, None, payload or "")

    # Fallback: look for a single colon after some header-ish token
    if ': ' in s:
        hdr, payload = s.split(': ', 1)
        return (None, None, None, payload.strip())

    # As last resort, whole line is "payload"
    return (None, None, None, s)

def _text_match(hay: str, pat: str, *, literal: bool, ignore_case: bool, equals: bool) -> bool:
    if ignore_case:
        hay_cmp = hay.lower()
        pat_cmp = pat.lower()
    else:
        hay_cmp = hay
        pat_cmp = pat

    if equals:
        return hay_cmp == pat_cmp

    if literal:
        return pat_cmp in hay_cmp

    # regex
    flags = re.IGNORECASE if ignore_case else 0
    try:
        return re.search(pat, hay, flags=flags) is not None
    except re.error:
        # If pattern is invalid regex, fallback to substring
        return pat_cmp in hay_cmp

def line_matches(line: str, cfg: dict) -> bool:
    """
    cfg keys we honor:
      - pattern (str)           [required]
      - literal (bool)          default True for your use-case
      - ignore_case (bool)      default False
      - equals (bool)           default False (exact match)
      - payload_only (bool)     default True in ONLINE (set by TRE_online)
      - ecu / app / ctx (str)   optional header constraints (respect literal/ignore_case/equals)
      - min_count (int)         handled by caller (TRE_online)
    """
    if not isinstance(cfg, dict):
        return False

    pattern      = str(cfg.get("pattern", ""))
    if not pattern:
        return False

    literal      = bool(cfg.get("literal", True))
    ignore_case  = bool(cfg.get("ignore_case", False))
    equals       = bool(cfg.get("equals", False))
    payload_only = bool(cfg.get("payload_only", False))

    # Header constraints (optional)
    need_ecu = cfg.get("ecu")
    need_app = cfg.get("app")
    need_ctx = cfg.get("ctx")

    ecu, app, ctx, payload = _split_dlt_header_and_payload(line)

    # 1) If header filters are provided, verify them first
    if need_ecu is not None:
        if not _text_match(ecu or "", str(need_ecu), literal=literal, ignore_case=ignore_case, equals=equals):
            return False
    if need_app is not None:
        if not _text_match(app or "", str(need_app), literal=literal, ignore_case=ignore_case, equals=equals):
            return False
    if need_ctx is not None:
        if not _text_match(ctx or "", str(need_ctx), literal=literal, ignore_case=ignore_case, equals=equals):
            return False

    # 2) Choose haystack
    hay = payload if payload_only else line

    # 3) Payload (or full-line) pattern match
    return _text_match(hay, pattern, literal=literal, ignore_case=ignore_case, equals=equals)


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

def sanitize_payload(payload: str) -> str:
        if not payload:
            return ""
        s = payload.replace("\r", " ").replace("\n", " ")
        s = "".join(ch for ch in s if (32 <= ord(ch) <= 126) or ch in "\t ")
        for tok in {"TELETELE", "AOTA", "CCU2s", "CCU2c"}:
            s = s.replace(tok, " ")
        s = _re.sub(r"\b([A-Z]{3,10})(?:\1)+\b", r"\1", s)          # OTAOTA -> OTA
        s = _re.sub(r"=\s*[A-Z]{2,10}\d*[a-z]?\b", " ", s)          # strip '=CCU2x'
        s = _re.sub(r"\s*>>\s*", " >> ", s)                         # normalize >>
        s = _re.sub(r"\s+", " ", s).strip()                         # squeeze spaces
        return s

    # keep old names working too
_sanitize = sanitize_payload
_sanitize_payload = sanitize_payload
    
def line_matches(line: str, cfg: dict) -> bool:
    """
    Payload-first matcher with robust 'literal + *** wildcard' support.

    Behavior:
      - If cfg['literal'] == True, the pattern is treated literally except:
          * '***' is a non-greedy wildcard ('.*?')
          * spaces are flexible (one or more)
          * '>>' spacing is flexible ('\\s*>>\\s*')
      - If cfg['literal'] == False, treat pattern as a normal regex.
      - Default matching is case-insensitive (can be flipped with ignore_case=False).
      - If payload_only == True (default), we try to peel payload from the DLT line first.
      - We sanitize candidates: one physical line, printable only, remove '=CCU2...' tails,
        collapse whitespace, normalize '>>' spacing.
    """
    import re

    # ---- config ----
    if not isinstance(cfg, dict):
        cfg = {"pattern": str(cfg), "literal": True}

    pat         = str(cfg.get("pattern", "")) or ""
    if not pat:
        return False

    literal     = bool(cfg.get("literal", False))
    equals      = bool(cfg.get("equals", False))
    ignore_case = cfg.get("ignore_case", True) is not False
    payload_only= cfg.get("payload_only", True) is not False
    use_anchor  = cfg.get("payload_anchor", True) is not False

    # ---- small local helpers (self-contained; no other deps) ----
    def _flatten_printable(s: str) -> str:
        if not s:
            return ""
        s = s.replace("\r", " ").replace("\n", " ")
        # printable ASCII + tabs/spaces
        s = "".join(ch for ch in s if (32 <= ord(ch) <= 126) or ch in "\t ")
        return s

    _re_tail_eq_tag = re.compile(r"=\s*[A-Z]{2,10}\d*[a-z]?\b")

    def _sanitize(s: str) -> str:
        """
        Normalize a payload for matching/logging:
          - flatten CR/LF to a single physical line
          - keep printable ASCII + tabs/spaces
          - remove known junk tokens (AOTA / TELETELE / CCU2s / CCU2c)
          - collapse repeated ALLCAPS tokens (e.g., OTAOTA -> OTA)
          - strip header-like tails (=CCU2x, =ABC123x)
          - normalize spacing around '>>'
          - collapse all whitespace to single spaces
        """
        if not s:
            return ""

        # flatten
        s = s.replace("\r", " ").replace("\n", " ")

        # printable only (keep tab/space)
        s = "".join(ch for ch in s if (32 <= ord(ch) <= 126) or ch in "\t ")

        # known junk tokens
        for tok in _DENY_TOKENS:
            s = s.replace(tok, " ")

        # collapse repeated ALLCAPS (OTAOTA -> OTA)
        s = re.sub(r"\b([A-Z]{3,10})(?:\1)+\b", r"\1", s)

        # strip tail tags like '=CCU2x'
        s = _re_tail_eq_tag.sub(" ", s)

        # normalize spacing around >>
        s = re.sub(r"\s*>>\s*", " >> ", s)

        # collapse whitespace
        s = re.sub(r"\s+", " ", s).strip()
        return s


    def _extract_payload(line: str) -> str:
        """
        Best-effort payload slice from a full DLT row.
          1) If '[EVALUATION]:' exists, return from there.
          2) Else take substring after the LAST ']'.
          3) Else try colon slice (common header: '...: payload').
          4) Else whole line trimmed.
        """
        if not isinstance(line, str):
            try:
                line = line.decode("utf-8", "ignore")
            except Exception:
                line = str(line)

        # 1) anchor on [EVALUATION]:
        pos = line.rfind("[EVALUATION]:")
        if pos != -1:
            return line[pos:].strip()

        # 2) after last closing bracket (end of args/headers)
        rbr = line.rfind("]")
        if rbr != -1 and rbr + 1 < len(line):
            tail = line[rbr + 1 :].strip()
            if tail:
                return tail

        # 3) colon slice (header: '...: payload')
        p = line.find(": ")
        if 0 < p < 180:  # keep this conservative
            tail = line[p + 2 :].lstrip()
            if tail:
                return tail

    # 4) fallback
    return line.strip()

    # ---- Payload cleaning helpers (public) --------------------------------


    # ---- Unified payload sanitizer (simple, robust) ----
  


    
    def _build_candidates(src: str) -> list[str]:
        raw = _flatten_printable(src)
        cands: list[str] = []
        if payload_only:
            # payload heuristic
            cands.append(_extract_payload(raw))
            # anchor slice (from first stable token in pattern)
            if use_anchor:
                # take a reasonable anchor: first long-ish word/letter block of pat
                m = re.search(r"[A-Za-z0-9_]{3,}", pat)
                if m:
                    anc = m.group(0)
                    hay = raw if not ignore_case else raw.lower()
                    ned = anc if not ignore_case else anc.lower()
                    pos = hay.find(ned)
                    if pos >= 0:
                        cands.append(raw[pos:])
            # simple colon slice
            p = raw.find(": ")
            if 0 < p < 120:
                cands.append(raw[p + 2 :].lstrip())
        # always include full raw
        cands.append(raw)
        # sanitize all
        return [_sanitize(x) for x in cands]

    def _literal_to_regex(p: str) -> str:
        # protect *** tokens
        token = "___TRE_STARSTARSTAR___"
        p = p.replace("***", token)
        # escape everything literally
        p = re.escape(p)
        # restore *** as non-greedy wildcard
        p = p.replace(re.escape(token), r".*?")
        # flexible spaces
        p = p.replace(r"\ ", r"\s+")
        # flexible >> spacing (escaped form '\>\>')
        p = p.replace(r"\>\>", r"\s*>>\s*")
        token = "___TRE_STARSTARSTAR___"
        p = p.replace("***", token)

        # escape everything literally
        p = re.escape(p)

        # restore *** as non-greedy wildcard
        p = p.replace(re.escape(token), r".*?")

        # flexible spaces
        p = p.replace(r"\ ", r"\s+")

        # flexible >> spacing (escaped form '\>\>')
        p = p.replace(r"\>\>", r"\s*>>\s*")
        return p

    # ---- prepare pattern ----
    pat_src = pat
    flags = re.IGNORECASE if ignore_case else 0

    if literal:
        rx = _literal_to_regex(pat_src)
        if equals:
            rx = f"^{rx}$"
        try:
            pat_re = re.compile(rx, flags)
        except re.error:
            # fallback to plain substring if compilation fails
            pat_re = None
    else:
        try:
            pat_re = re.compile(pat_src, flags)
        except re.error:
            pat_re = None

    # ---- candidates ----
    candidates = _build_candidates(line)

    # ---- try all ----
    for idx, tgt in enumerate(candidates, 1):
        try:
            if literal:
                if pat_re is not None:
                    ok = pat_re.search(tgt) is not None
                else:
                    # substring fallback with flexible spaces (' ' in pat -> \s+ already)
                    ok = (pat_src.lower() in tgt.lower()) if ignore_case else (pat_src in tgt)
            else:
                if pat_re is not None:
                    ok = pat_re.search(tgt) is not None
                else:
                    ok = (pat_src.lower() in tgt.lower()) if ignore_case else (pat_src in tgt)
        except Exception:
            ok = False

        if 'TRE_JSON_DEBUG' in globals() and TRE_JSON_DEBUG:
            try:
                _dbg(f"cand#{idx} ok={ok} | pat='{pat_src}' | tgt='{tgt[:200]}'")
            except Exception:
                pass

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

# --- add or replace in TRE_json.py -----------------------------------
import re as _re

_PAYLOAD_TAIL = _re.compile(r"\]\s*(.*)$")             # after last ']' to end
_JUNK_TOKENS  = ("TELETELE", "AOTA")
_TAIL_EQ_TAG  = _re.compile(r"=\s*[A-Z]{2,10}\d*[a-z]?\b")   # =CCU2x, =ABC123 etc.

def parse_dlt(line: str):
    """
    Return (ecu, app, ctx, payload) but be liberal in what we accept.
    If we can't parse headers, return (None, None, None, line).
    """
    try:
        # Common DLT format: ... ECUID APID CTID ... [ARGS] PAYLOAD
        # We won't over-parse headers here; payload extraction below.
        payload = extract_payload(line)
        return (None, None, None, payload)
    except Exception:
        return (None, None, None, line)

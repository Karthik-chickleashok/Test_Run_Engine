# TRE_ai.py — single place for all ChatGPT calls
from __future__ import annotations
import os, json, typing as _t

_CLIENT = None
_ERR = None
try:
    from openai import OpenAI
    _API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
    if _API_KEY:
        _CLIENT = OpenAI(api_key=_API_KEY)
    else:
        _ERR = "OPENAI_API_KEY not set"
except Exception as e:
    _ERR = f"OpenAI SDK missing or init failed: {e}"

_MODEL = "gpt-4.1-mini"

def is_enabled() -> bool:
    return bool(_CLIENT)

def validate_rules(rules_json_str: str) -> str:
    """Return a fixed/normalized JSON string for rules. If AI is disabled: passthrough."""
    if not is_enabled():
        return rules_json_str
    schema = {"type":"array","items":{"type":"object"},"minItems":1}
    prompt = (
        "You are a validator for TRE rule files.\n"
        "Return a valid JSON array of rules. Normalize mistakes:\n"
        "- Use literal:true when pattern has *, (, ) or >>\n"
        "- Prefer payload_only:true unless explicitly turned off\n"
        "- Ensure each rule has a 'name'\n"
        "- For find: {pattern, literal, min_count>=1}\n"
        "Do NOT invent steps.\n\n"
        f"{rules_json_str}\n"
    )
    try:
        rsp = _CLIENT.responses.create(
            model=_MODEL,
            input=prompt,
            response_format={"type":"json_schema","json_schema":{"name":"Rules","schema":schema}},
        )
        return rsp.output[0].content[0].text
    except Exception:
        return rules_json_str  # fail safe

def nl_to_testcase(natural: str) -> str:
    """Natural sentence -> single test-step JSON (string). If AI disabled: literal fallback."""
    if not is_enabled():
        return json.dumps({
            "name": natural[:60],
            "find": {"pattern": natural, "literal": True, "min_count": 1, "payload_only": True}
        }, ensure_ascii=False, indent=2)
    schema = {
        "type":"object",
        "properties":{
            "name":{"type":"string"},
            "find":{"type":"object","properties":{
                "pattern":{"type":"string"},"literal":{"type":"boolean"},
                "min_count":{"type":"integer","minimum":1},
                "timeout":{"type":"number","minimum":0},
                "payload_only":{"type":"boolean"}
            },"required":["pattern"]}
        },
        "required":["name","find"],
        "additionalProperties": True
    }
    prompt = (
        "Convert to a TRE 'find' step JSON. Prefer literal patterns. "
        "Normalize spaces around '>>'. Use payload_only:true unless told otherwise.\n\n"
        f"{natural}\n"
    )
    try:
        rsp = _CLIENT.responses.create(
            model=_MODEL,
            input=prompt,
            response_format={"type":"json_schema","json_schema":{"name":"TestCase","schema":schema}},
        )
        return rsp.output[0].content[0].text
    except Exception:
        return json.dumps({
            "name": natural[:60],
            "find": {"pattern": natural, "literal": True, "min_count": 1, "payload_only": True}
        }, ensure_ascii=False, indent=2)

def explain_failure(step: dict, samples: _t.List[str]) -> str:
    """Short why-failed + suggested safer literal pattern."""
    if not is_enabled():
        return "[TRE_AI disabled] Set OPENAI_API_KEY to enable."
    joined = "\n".join(samples[:10]) if samples else "(no samples)"
    prompt = (
        "Given a rule and sanitized payload samples from DLT, explain likely mismatch reasons "
        "and propose a safe literal pattern with anchors (not over-matching). Keep it short.\n\n"
        f"Rule:\n{json.dumps(step, ensure_ascii=False, indent=2)}\n\n"
        f"Samples:\n{joined}"
    )
    try:
        rsp = _CLIENT.responses.create(model=_MODEL, input=prompt)
        return rsp.output_text.strip()
    except Exception as e:
        return f"[explain_failure error] {e}"

def select_relevant_payload(step: dict, payload_samples: list[str], max_lines: int = 12) -> list[str]:
    """
    Narrow a long payload sample list to the most relevant lines for this rule.
    If AI disabled, do a simple keyword filter using tokens from the pattern.
    """
    if not payload_samples:
        return []

    # Fallback heuristic (no AI): pick lines containing any token from pattern
    if not is_enabled():
        pat = str(step.get("find", {}).get("pattern", step.get("pattern", "")))
        toks = [t for t in pat.replace(">>", " ").replace("(", " ").replace(")", " ").split() if len(t) >= 3]
        hits = [ln for ln in payload_samples if any(tok.lower() in ln.lower() for tok in toks)]
        return hits[:max_lines] or payload_samples[:max_lines]

    joined = "\n".join(payload_samples[:200])  # cap for token safety
    prompt = (
        "You are helping debug a DLT payload matcher.\n"
        "Given a rule and many sanitized payload lines, pick ONLY the lines most relevant "
        "to this rule failing. Return one JSON array of strings (each a payload line), "
        f"capped at {max_lines} lines.\n\n"
        f"Rule:\n{json.dumps(step, ensure_ascii=False, indent=2)}\n\n"
        "Payload lines:\n" + joined
    )
    schema = {"type":"array","items":{"type":"string"}}
    try:
        rsp = _CLIENT.responses.create(
            model=_MODEL,
            input=prompt,
            response_format={"type":"json_schema","json_schema":{"name":"RelLines","schema":schema}},
        )
        lines = json.loads(rsp.output[0].content[0].text)
        return lines[:max_lines]
    except Exception:
        return payload_samples[:max_lines]

def explain_failure(step: dict, samples: list[str]) -> str:
    if not is_enabled():
        return "[TRE_AI disabled] Set OPENAI_API_KEY to enable."
    joined = "\n".join(samples[:12]) if samples else "(no samples)"
    prompt = (
        "You are assisting a log matcher for automotive DLT payloads.\n"
        "Explain concisely why the rule may not match and propose ONE safer literal pattern. "
        "Prefer escaping special chars and keeping '>>' spacing flexible (use '\\s*>>\\s*'). "
        "Keep answer under 10 lines.\n\n"
        f"Rule:\n{json.dumps(step, ensure_ascii=False, indent=2)}\n\n"
        f"Sanitized payload samples:\n{joined}\n"
    )
    try:
        rsp = _CLIENT.responses.create(model=_MODEL, input=prompt)
        return rsp.output_text.strip()
    except Exception as e:
        return f"[explain_failure error] {e}"

def rca_summary(failed_steps: list[dict], histories: dict[int, list[str]]) -> str:
    """
    failed_steps: list of step dicts (as in online_mgr.tests) where result != PASS
    histories: { step_idx -> [sanitized payload lines observed for that step] }
    Returns a short plain-text RCA summary.
    """
    if not failed_steps:
        return "All steps passed. No RCA needed."

    if not is_enabled():
        # Minimal local summary
        names = ", ".join([s.get("name","(unnamed)") for s in failed_steps])
        return f"Failed steps: {names}. Enable OPENAI_API_KEY for detailed RCA."

    # Build a compact JSON-able bundle to keep tokens low
    bundle = []
    for s in failed_steps:
        idx = s.get("_idx") or s.get("idx")
        bundle.append({
            "idx": idx,
            "name": s.get("name"),
            "rule": {k:v for k,v in s.items() if k in ("find","not_find","sequence","action")},
            "history": histories.get(idx, [])[:80],  # cap per step
            "last_result": s.get("_final_result"),
            "last_line": s.get("_final_line"),
        })

    prompt = (
        "You are a test-run RCA assistant. Summarize concisely why these steps likely failed, "
        "grouping by root causes (pattern too strict, spacing/case issues, missing literal, wrong order, etc.). "
        "Include 1-line suggestions per step (e.g., revised pattern or timeout). Keep under 25 lines.\n\n"
        f"{json.dumps(bundle, ensure_ascii=False)[:12000]}"  # hard cap for safety
    )
    try:
        rsp = _CLIENT.responses.create(model=_MODEL, input=prompt)
        return rsp.output_text.strip()
    except Exception as e:
        return f"[rca_summary error] {e}"

def suggest_timeouts(step_timings: dict[int, list[float]], default_timeout: float = 3.0) -> dict[int, float]:
    """
    step_timings: { step_idx -> [seconds_from_start_when_line_seen, ...] }
    Returns: { step_idx -> suggested_timeout_seconds }
    Heuristic (no AI): pick p90 + 30% headroom, min(default_timeout), max 120s for step#1.
    """
    out = {}
    for idx, times in step_timings.items():
        if not times:
            out[idx] = default_timeout if idx != 1 else 120.0
            continue
        arr = sorted(times)
        p90 = arr[int(0.9 * (len(arr)-1))] if len(arr) > 1 else arr[0]
        headroom = p90 * 1.3
        if idx == 1:
            out[idx] = min(max(headroom, 5.0), 120.0)
        else:
            out[idx] = max(headroom, default_timeout)
    return out

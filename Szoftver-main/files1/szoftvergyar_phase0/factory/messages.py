"""Üzenetprotokoll (1.4) és egyszerű, függőségmentes sémavalidátor (12.2)."""
from __future__ import annotations
import json, time

MESSAGE_TYPES = {
    "TASK_ASSIGN", "SPEC_READY", "SCHEMA_READY", "CODE_SUBMITTED", "SECURITY_RESULT", "REVIEW_RESULT", "CI_RESULT",
    "DOCS_READY", "ESCALATE_HUMAN", "HALT", "ROLLBACK_DONE", "PROPOSED_MODULE_CHANGE", "PROPOSED_CONFIG_CHANGE",
    "MODULE_APPLIED", "MODULE_REVERTED", "DESIGN_READY", "CONTEXT_PACK", "ALERT", "INCIDENT_TICKET", "DEBT_TASK",
    "LANE_CHANGED", "NO_PROGRESS", "INGRESS_QUARANTINE", "FIX_CACHE_HIT", "TESTS_READY", "DOR_FAILED", "AUDIT_CHAIN_BROKEN",
}
AGENTS = {"supervisor", "product_owner", "master_coder", "security", "qa", "git_devops", "db_architect", "tech_writer", "ux_designer", "human", "system"}

class MessageError(ValueError):
    pass

def make(task_id: str, frm: str, to: str, mtype: str, payload: dict | None = None, iteration: int = 0, **extra) -> dict:
    m = {"task_id": task_id, "iteration": iteration, "from": frm, "to": to, "type": mtype,
         "payload": payload or {}, "tokens_used": 0, "ts": time.time()}
    m.update(extra)
    validate(m)
    return m

def validate(m: dict) -> dict:
    for k in ("task_id", "from", "to", "type", "payload", "iteration"):
        if k not in m:
            raise MessageError(f"hiányzó mező: {k}")
    if m["type"] not in MESSAGE_TYPES:
        raise MessageError(f"ismeretlen üzenettípus: {m['type']}")
    if m["from"] not in AGENTS or m["to"] not in AGENTS:
        raise MessageError("ismeretlen küldő/címzett")
    if not isinstance(m["payload"], dict) or not isinstance(m["iteration"], int):
        raise MessageError("payload dict, iteration int kell")
    if m["type"] == "TASK_ASSIGN":
        for k in ("lane", "iteration_cap", "task_token_cap"):
            if k not in m["payload"] and k not in m:
                raise MessageError(f"TASK_ASSIGN mező hiányzik: {k}")
    return m

# --- mini JSON-séma validátor (12.2): type, required, properties, enum, maxLength, items, additionalProperties ---
_T = {"string": str, "integer": int, "number": (int, float), "boolean": bool, "object": dict, "array": list, "null": type(None)}

def schema_errors(data, schema: dict, path: str = "$") -> list[str]:
    errs: list[str] = []
    t = schema.get("type")
    if t:
        py = _T[t]
        ok = isinstance(data, py) and not (t in ("integer", "number") and isinstance(data, bool))
        if not ok:
            return [f"{path}: típus {t} kell"]
    if "enum" in schema and data not in schema["enum"]:
        errs.append(f"{path}: nem megengedett érték")
    if isinstance(data, str) and "maxLength" in schema and len(data) > schema["maxLength"]:
        errs.append(f"{path}: túl hosszú (max {schema['maxLength']})")
    if isinstance(data, dict):
        for r in schema.get("required", []):
            if r not in data:
                errs.append(f"{path}.{r}: kötelező")
        props = schema.get("properties", {})
        for k, v in data.items():
            if k in props:
                errs += schema_errors(v, props[k], f"{path}.{k}")
            elif schema.get("additionalProperties") is False:
                errs.append(f"{path}.{k}: nem engedélyezett mező")
    if isinstance(data, list) and "items" in schema:
        for i, it in enumerate(data):
            errs += schema_errors(it, schema["items"], f"{path}[{i}]")
    return errs

def dumps(m: dict) -> str:
    return json.dumps(m, ensure_ascii=False, sort_keys=True)

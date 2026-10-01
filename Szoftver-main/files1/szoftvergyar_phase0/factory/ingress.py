"""Ingress Filter – szabályréteg (11.6, 0–1. fázis). A repóból/naplóból/külső forrásból jövő szöveg ADAT, nem utasítás (4.5).
Nulla token, nulla hálózat. Az osztályozó (2. fázis) később épül; ez csak determinisztikus szabályok."""
from __future__ import annotations
import re, base64

RULES = [
    ("override", re.compile(r"(?i)\b(ignore|disregard|forget)\b.{0,30}\b(previous|prior|above|all)\b.{0,30}\b(instructions?|rules?|prompts?)\b")),
    ("override_hu", re.compile(r"(?i)(hagyd figyelmen kívül|felejtsd el).{0,40}(utasítás|szabály|prompt)")),
    ("role_swap", re.compile(r"(?i)\byou are now\b|\bnew system prompt\b|\bsystem\s*:\s")),
    ("authority", re.compile(r"(?i)(approve this|mark (it )?as approved|verdict\s*[:=]\s*approve|skip (the )?(gates?|review|tests?))")),
    ("exfil", re.compile(r"(?i)(print|reveal|send|exfiltrate).{0,30}(secret|api[_ -]?key|token|password|\.env)")),
    ("perm_change", re.compile(r"(?i)(disable|weaken|remove).{0,30}(gate|sandbox|circuit breaker|redact)")),
]
B64 = re.compile(r"\b[A-Za-z0-9+/]{60,}={0,2}\b")

def scan(text: str) -> list[dict]:
    hits = []
    for name, rx in RULES:
        for m in rx.finditer(text):
            hits.append({"rule": name, "start": m.start(), "end": m.end()})
    for m in B64.finditer(text):
        try:
            dec = base64.b64decode(m.group(0), validate=True).decode("utf-8", "ignore")
            if any(rx.search(dec) for _, rx in RULES):
                hits.append({"rule": "encoded_injection", "start": m.start(), "end": m.end()})
        except Exception:
            pass
    return hits

def filter_text(text: str) -> tuple[str, list[dict]]:
    """Visszaadja (tisztított szöveg, INGRESS_QUARANTINE tételek). A gyanús szegmens kikerül; visszaengedés nincs."""
    hits = sorted(scan(text), key=lambda h: h["start"])
    if not hits:
        return text, []
    out, pos = [], 0
    for h in hits:
        if h["start"] >= pos:
            out.append(text[pos:h["start"]]); out.append("[KIVONVA:ingress]"); pos = h["end"]
    out.append(text[pos:])
    return "".join(out), hits

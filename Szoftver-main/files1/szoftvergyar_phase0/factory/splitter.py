"""Túl nagy kérés lépésekre bontása (1.7, spec 4.4/5b): a Supervisor kódja, nulla token.
Csak a MECHANIKUS bontást végzi (fájlonként csoportosít, a túl nagy elemet sorhatáron szeletel). A szemantikus bontás
(függvényenként, szakaszonként) a Coder/PO ágensek dolga az 1. fázisban; ez a réteg a végső védőháló."""
from __future__ import annotations

CHARS_PER_TOKEN = 4          # ugyanaz a becslés, mint gateway.est_tokens (len // 4)


class Unsplittable(Exception):
    """A kötelező keret (prompt-váz + kimenet) már önmagában nem fér bele: emberi döntés kell."""


def _split_text(text: str, max_chars: int) -> list[str]:
    """Sorhatáron szeletel; ha egyetlen sor is túl hosszú, karakterhatáron."""
    parts, cur, cur_len = [], [], 0
    for line in text.splitlines(keepends=True):
        while len(line) > max_chars:                       # extra hosszú sor: kemény vágás
            if cur:
                parts.append("".join(cur)); cur, cur_len = [], 0
            parts.append(line[:max_chars]); line = line[max_chars:]
        if cur_len + len(line) > max_chars and cur:
            parts.append("".join(cur)); cur, cur_len = [], 0
        cur.append(line); cur_len += len(line)
    if cur:
        parts.append("".join(cur))
    return parts or [""]


def plan_steps(items: list[tuple[str, str]], input_budget_tokens: int) -> list[list[dict]]:
    """items: [(név, szöveg)]. Visszaad: lépések listája, egy lépés = [{name, text, part, parts}], ahol egy lépés
    összes szövege beleér a keretbe. Sorrendtartó, az elemek nem keverednek szét (egy elem darabjai egymás után jönnek)."""
    if input_budget_tokens < 1:
        raise Unsplittable(f"nincs hely a tartalomnak (keret: {input_budget_tokens} token)")
    max_chars = input_budget_tokens * CHARS_PER_TOKEN
    pieces = []
    for name, text in items:
        parts = _split_text(text, max_chars)
        for i, t in enumerate(parts):
            pieces.append({"name": name, "text": t, "part": i + 1, "parts": len(parts)})
    steps, cur, cur_len = [], [], 0
    for pc in pieces:
        if cur and cur_len + len(pc["text"]) > max_chars:
            steps.append(cur); cur, cur_len = [], 0
        cur.append(pc); cur_len += len(pc["text"])
    if cur:
        steps.append(cur)
    return steps

"""Adósság-nyilvántartás (11.2, 10.4; 1. fázis: csak rögzít). A puha kapu találataiból a CI KÓD hoz létre tételt, az ágens nem.

Fájl: `state/tech_debt.jsonl` (a `state/**` védett zóna, az ágensek nem írhatják). Soronként egy JSON; append-only: a tétel lezárása
egy külön `close` esemény. Duplikátumszűrés (rule, file) szerint; `max_open_items` fölött a puha kapu BEZÁR (a hívó kemény hibaként kezeli).
Az auditba csak darabszám és szabálykód kerül, a találat szövege nem."""
from __future__ import annotations
import json, os, time

ORIGIN = "soft_gate"


class DebtRegister:
    def __init__(self, path: str, max_open_items: int = 30, audit=None, origin: str = ORIGIN):
        self.path, self.max_open, self.audit, self.origin = path, int(max_open_items), audit, origin

    def _events(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        out = []
        with open(self.path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except ValueError:
                    continue            # sérült sor: kihagyjuk (nem találhatunk ki tételt), a többi olvasható marad
        return out

    def open_items(self) -> list[dict]:
        items, closed = {}, set()
        for e in self._events():
            if e.get("event") == "close":
                closed.add(e.get("id"))
            elif e.get("id"):
                items[e["id"]] = e
        return [v for k, v in items.items() if k not in closed]

    def open_count(self) -> int:
        return len(self.open_items())

    def new_items(self, findings) -> list[dict]:
        """A még nem nyilvántartott, egyedi (rule, file) találatok. Nem ír."""
        seen = {(i["rule"], i["file"]) for i in self.open_items()}
        out = []
        for f in findings:
            k = (f["rule"], f["file"])
            if k not in seen:
                seen.add(k); out.append(f)
        return out

    def fits(self, findings) -> bool:
        """Belefér-e az új tétel a keretbe? Ha nem: a puha kapu zárva van (a találatok kemény hibák)."""
        return self.open_count() + len(self.new_items(findings)) <= self.max_open

    def add(self, task_id: str, findings) -> dict:
        """Rögzíti az új tételeket. Visszatér: {added, duplicates}. A hívónak előbb `fits()`-et kell ellenőriznie; ha nem fér, ValueError."""
        new = self.new_items(findings)
        if self.open_count() + len(new) > self.max_open:
            raise ValueError("az adósságlista megtelt: a puha kapu zárva")
        if new:
            os.makedirs(os.path.dirname(self.path) or ".", exist_ok=True)
            base = len(self._events())
            with open(self.path, "a", encoding="utf-8") as f:
                for n, it in enumerate(new, 1):
                    rec = {"id": f"D-{base + n}", "origin": self.origin, "task_id": task_id, "rule": it["rule"], "file": it["file"],
                           "line": it.get("line"), "message": str(it.get("message", ""))[:200], "status": "open", "ts": round(time.time(), 3)}
                    f.write(json.dumps(rec, ensure_ascii=False) + "\n")
        dup = len(findings) - len(new)
        if self.audit:
            self.audit.append("ci", "DEBT_RECORDED", {"task_id": task_id, "origin": self.origin, "added": len(new), "duplicates": dup,
                                                     "rules": sorted({i["rule"] for i in new}), "open": self.open_count()})
        return {"added": len(new), "duplicates": dup}

    def close(self, ids) -> int:
        """Lezárja a megadott tételeket (ember vagy a későbbi DEBT_TASK hívja). Visszatér: a ténylegesen lezártak száma."""
        open_ids = {i["id"] for i in self.open_items()}
        todo = [i for i in ids if i in open_ids]
        if todo:
            with open(self.path, "a", encoding="utf-8") as f:
                for i in todo:
                    f.write(json.dumps({"event": "close", "id": i, "ts": round(time.time(), 3)}) + "\n")
        return len(todo)

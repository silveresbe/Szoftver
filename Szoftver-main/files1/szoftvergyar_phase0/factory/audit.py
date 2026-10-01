"""Audit Chain (12.5): hash-láncolt, append-only napló; kizárólag kód írja. hash = SHA-256(prev_hash + bejegyzés)."""
from __future__ import annotations
import hashlib, json, os, time, threading

GENESIS = "0" * 64

class AuditChainBroken(Exception):
    pass

class AuditLog:
    def __init__(self, path: str):
        self.path = path
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self._head = self._read_head()

    @staticmethod
    def _h(prev: str, body: str) -> str:
        return hashlib.sha256((prev + body).encode("utf-8")).hexdigest()

    def _read_head(self) -> str:
        head = GENESIS
        if os.path.exists(self.path):
            with open(self.path, encoding="utf-8") as f:
                for line in f:
                    if line.strip():
                        head = json.loads(line)["hash"]
        return head

    @property
    def head(self) -> str:
        return self._head

    def append(self, agent: str, event: str, detail: dict | None = None) -> dict:
        with self._lock:
            n = 0
            if os.path.exists(self.path):
                with open(self.path, encoding="utf-8") as f:
                    n = sum(1 for l in f if l.strip())
            entry = {"seq": n + 1, "ts": round(time.time(), 3), "agent": agent, "event": event, "detail": detail or {}}
            body = json.dumps(entry, ensure_ascii=False, sort_keys=True)
            entry["prev_hash"] = self._head
            entry["hash"] = self._h(self._head, body)
            with open(self.path, "a", encoding="utf-8") as f:
                f.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")
                f.flush(); os.fsync(f.fileno())
            self._head = entry["hash"]
            return entry

    def verify(self) -> int:
        """Végigellenőrzi a láncot; törés esetén AuditChainBroken. Visszaadja a bejegyzések számát."""
        prev, n = GENESIS, 0
        if not os.path.exists(self.path):
            return 0
        with open(self.path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                if not line.strip():
                    continue
                e = json.loads(line)
                if e.get("prev_hash") != prev:
                    raise AuditChainBroken(f"törés a(z) {i}. sorban: prev_hash eltér")
                body_e = {k: v for k, v in e.items() if k not in ("prev_hash", "hash")}
                body = json.dumps(body_e, ensure_ascii=False, sort_keys=True)
                if self._h(prev, body) != e["hash"]:
                    raise AuditChainBroken(f"törés a(z) {i}. sorban: hash eltér")
                prev, n = e["hash"], n + 1
        if prev != self._head:
            raise AuditChainBroken("a lánc feje nem egyezik")
        return n

"""Tartós állapot (6.4): SQLite WAL. Feladatok, iterációszámlálók, checkpointok, kvóta-számlálók, idempotencia, emberi kapuk, üzenetsor."""
from __future__ import annotations
import json, os, sqlite3, threading, time

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(task_id TEXT PRIMARY KEY, status TEXT NOT NULL, lane TEXT, iteration INTEGER DEFAULT 0,
  iteration_cap INTEGER, token_cap INTEGER, tokens_used INTEGER DEFAULT 0, deps TEXT DEFAULT '[]', data TEXT DEFAULT '{}', updated REAL);
CREATE TABLE IF NOT EXISTS checkpoints(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, step TEXT, data TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS quota_daily(model TEXT, day TEXT, tokens INTEGER DEFAULT 0, requests INTEGER DEFAULT 0, PRIMARY KEY(model, day));
CREATE TABLE IF NOT EXISTS idempotency(key TEXT PRIMARY KEY, result TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS human_gates(id INTEGER PRIMARY KEY AUTOINCREMENT, task_id TEXT, reason TEXT, status TEXT DEFAULT 'PENDING', decision TEXT, ts REAL);
CREATE TABLE IF NOT EXISTS bus(id INTEGER PRIMARY KEY AUTOINCREMENT, to_agent TEXT, task_id TEXT, msg TEXT, status TEXT DEFAULT 'NEW', ts REAL);
"""

class State:
    def __init__(self, path: str):
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        self.path = path
        self._lock = threading.RLock()
        self.db = sqlite3.connect(path, check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript(SCHEMA)
        self.db.commit()

    def _x(self, sql, args=()):
        with self._lock:
            cur = self.db.execute(sql, args)
            self.db.commit()
            return cur

    # --- feladatok ---
    def upsert_task(self, task_id, status, **kw):
        if self.get_task(task_id) is None:
            self._x("INSERT INTO tasks(task_id,status,lane,iteration_cap,token_cap,deps,data,updated) VALUES(?,?,?,?,?,?,?,?)",
                    (task_id, status, kw.get("lane"), kw.get("iteration_cap"), kw.get("token_cap"),
                     json.dumps(kw.get("deps", [])), json.dumps(kw.get("data", {})), time.time()))
        else:
            self._x("UPDATE tasks SET status=?, updated=? WHERE task_id=?", (status, time.time(), task_id))

    def get_task(self, task_id):
        r = self._x("SELECT * FROM tasks WHERE task_id=?", (task_id,)).fetchone()
        return dict(r) if r else None

    def set_task_fields(self, task_id, **kw):
        for k, v in kw.items():
            if k not in ("iteration", "tokens_used", "lane", "iteration_cap", "token_cap", "data"):
                raise ValueError(k)
            self._x(f"UPDATE tasks SET {k}=?, updated=? WHERE task_id=?", (json.dumps(v) if k == "data" else v, time.time(), task_id))

    def get_task_data(self, task_id) -> dict:
        t = self.get_task(task_id)
        if not t:
            return {}
        try:
            d = json.loads(t["data"] or "{}")
        except ValueError:
            return {}
        return d if isinstance(d, dict) else {}

    def update_task_data(self, task_id, **kw) -> dict:
        """Strukturált mezők összefésülése a feladat data-jába (a többi mezőt nem írja felül)."""
        with self._lock:
            d = self.get_task_data(task_id)
            d.update(kw)
            self.set_task_fields(task_id, data=d)
            return d

    def open_tasks(self):
        return [dict(r) for r in self._x("SELECT * FROM tasks WHERE status NOT IN ('DONE','DISCARDED')").fetchall()]

    # --- checkpoint ---
    def checkpoint(self, task_id, step, data: dict):
        self._x("INSERT INTO checkpoints(task_id,step,data,ts) VALUES(?,?,?,?)", (task_id, step, json.dumps(data), time.time()))

    def last_checkpoint(self, task_id):
        r = self._x("SELECT * FROM checkpoints WHERE task_id=? ORDER BY id DESC LIMIT 1", (task_id,)).fetchone()
        return None if not r else {"step": r["step"], "data": json.loads(r["data"]), "ts": r["ts"]}

    # --- napi kvóta (tartós) ---
    @staticmethod
    def day_key(now=None):
        return time.strftime("%Y-%m-%d", time.gmtime(now or time.time()))

    def add_daily(self, model, tokens, requests=1, now=None):
        d = self.day_key(now)
        self._x("INSERT OR IGNORE INTO quota_daily(model,day) VALUES(?,?)", (model, d))
        self._x("UPDATE quota_daily SET tokens=tokens+?, requests=requests+? WHERE model=? AND day=?", (tokens, requests, model, d))

    def get_daily(self, model, now=None):
        r = self._x("SELECT tokens,requests FROM quota_daily WHERE model=? AND day=?", (model, self.day_key(now))).fetchone()
        return (r["tokens"], r["requests"]) if r else (0, 0)

    # --- idempotencia ---
    def idem_get(self, key):
        r = self._x("SELECT result FROM idempotency WHERE key=?", (key,)).fetchone()
        return None if not r else json.loads(r["result"])

    def idem_put(self, key, result):
        self._x("INSERT OR REPLACE INTO idempotency(key,result,ts) VALUES(?,?,?)", (key, json.dumps(result), time.time()))

    # --- emberi kapu ---
    def open_human_gate(self, task_id, reason):
        return self._x("INSERT INTO human_gates(task_id,reason,ts) VALUES(?,?,?)", (task_id, reason, time.time())).lastrowid

    def pending_gates(self):
        return [dict(r) for r in self._x("SELECT * FROM human_gates WHERE status='PENDING'").fetchall()]

    def decide_gate(self, gate_id, decision):
        self._x("UPDATE human_gates SET status='DECIDED', decision=? WHERE id=?", (decision, gate_id))

    # --- üzenetsor ---
    def bus_put(self, to_agent, task_id, msg_json):
        return self._x("INSERT INTO bus(to_agent,task_id,msg,ts) VALUES(?,?,?,?)", (to_agent, task_id, msg_json, time.time())).lastrowid

    def bus_take(self, to_agent):
        with self._lock:
            r = self.db.execute("SELECT * FROM bus WHERE to_agent=? AND status='NEW' ORDER BY id LIMIT 1", (to_agent,)).fetchone()
            if not r:
                return None
            self.db.execute("UPDATE bus SET status='TAKEN' WHERE id=?", (r["id"],))
            self.db.commit()
            return json.loads(r["msg"])

    def close(self):
        self.db.close()

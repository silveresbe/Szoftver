"""Metrikák (6.6, 12.1/4): append-only JSONL. Nem a hash-lánc része (az a döntéseké), csak mérés.
Az 1. fázistól kötelező: lane_distribution, no_progress_events, tokens_per_task."""
from __future__ import annotations
import json, os, threading, time
from collections import Counter, defaultdict

FACTORY_METRICS = {"lane_distribution", "autofix_iterations_saved", "soft_debt_created", "fix_cache_hit_rate",
                   "review_cache_hit_rate", "ingress_quarantines", "no_progress_events", "tokens_saved_estimate"}
PER_AGENT = {"tokens_used", "calls", "rejections", "iterations", "wait_seconds", "failures"}


class Metrics:
    def __init__(self, path: str, clock=time.time):
        self.path, self.clock = path, clock
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)

    def _write(self, rec: dict) -> None:
        rec = {"ts": round(self.clock(), 3), **rec}
        with self._lock, open(self.path, "a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

    def agent(self, agent: str, metric: str, value: float = 1, task_id: str | None = None) -> None:
        if metric not in PER_AGENT:
            raise ValueError(f"ismeretlen ágens-metrika: {metric}")
        self._write({"kind": "agent", "agent": agent, "metric": metric, "value": value, "task_id": task_id})

    def factory(self, metric: str, value: float = 1, **labels) -> None:
        if metric not in FACTORY_METRICS:
            raise ValueError(f"ismeretlen gyári metrika: {metric}")
        self._write({"kind": "factory", "metric": metric, "value": value, **labels})

    def task_done(self, task_id: str, lane: str, tokens: int, iterations: int) -> None:
        """Feladat lezárásakor: ebből lesz a tokens_per_task és a lane_distribution."""
        self._write({"kind": "task", "task_id": task_id, "lane": lane, "tokens": tokens, "iterations": iterations})

    def read(self) -> list[dict]:
        if not os.path.exists(self.path):
            return []
        with open(self.path, encoding="utf-8") as f:
            return [json.loads(l) for l in f if l.strip()]

    def summary(self) -> dict:
        recs = self.read()
        tasks = [r for r in recs if r["kind"] == "task"]
        lanes = Counter(r["lane"] for r in tasks)
        per_agent: dict = defaultdict(lambda: defaultdict(float))
        fac: dict = defaultdict(float)
        for r in recs:
            if r["kind"] == "agent":
                per_agent[r["agent"]][r["metric"]] += r["value"]
            elif r["kind"] == "factory":
                fac[r["metric"]] += r["value"]
        calls = {a: m.get("calls", 0) for a, m in per_agent.items()}
        return {
            "tasks": len(tasks),
            "lane_distribution": dict(lanes),
            "tokens_per_task": (sum(t["tokens"] for t in tasks) / len(tasks)) if tasks else 0,
            "avg_iterations": (sum(t["iterations"] for t in tasks) / len(tasks)) if tasks else 0,
            "no_progress_events": fac.get("no_progress_events", 0),
            "rejection_rate": {a: (m.get("rejections", 0) / calls[a]) if calls[a] else 0 for a, m in per_agent.items()},
            "per_agent": {a: dict(m) for a, m in per_agent.items()},
            "factory": dict(fac),
        }

"""Task replay and audit reconstruction runtime helpers.

A task history újraépítésére szolgáló egyszerű, determinisztikus wrapper.
A cél: a QA döntés és a Supervisor route utólag reprodukálható legyen a
mentett state + audit log alapján.
"""
from __future__ import annotations

from collections.abc import Iterable
from typing import Any


class TaskReplayError(RuntimeError):
    """A task replay során felmerült hiba."""


class TaskReplay:
    """Rebuilds task lifecycle from persisted task record and audit log."""

    @staticmethod
    def replay_task(task_id: str, record: Any, audit: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
        if record is None:
            raise TaskReplayError(f"unknown task: {task_id}")

        history = list(getattr(record, "history", []) or [])
        events = list(audit or [])
        final = {
            "task_id": getattr(record, "task_id", task_id),
            "state": getattr(record, "state", "PENDING"),
            "lane": getattr(record, "lane", "S2"),
            "iteration": getattr(record, "iteration", 0),
            "history": history,
            "audit_events": events,
            "data": getattr(record, "data", {}) or {},
        }
        return final

    @staticmethod
    def summarize(task_id: str, record: Any, audit: Iterable[dict[str, Any]] | None = None) -> dict[str, Any]:
        replay = TaskReplay.replay_task(task_id, record, audit)
        last = replay["history"][-1] if replay["history"] else {}
        return {
            "task_id": task_id,
            "state": replay["state"],
            "iteration": replay["iteration"],
            "last_action": last.get("next_action"),
            "last_route": last.get("route"),
            "last_status": last.get("status"),
            "audit_count": len(replay["audit_events"]),
        }


__all__ = ["TaskReplay", "TaskReplayError"]


if __name__ == "__main__":
    print(TaskReplay.summarize("T-1", type("R", (), {"task_id": "T-1", "state": "ACCEPTED", "lane": "S2", "iteration": 2, "history": [{"next_action": "handoff_to_owner", "route": "accepted", "status": "ACCEPT"}], "data": {}})()))

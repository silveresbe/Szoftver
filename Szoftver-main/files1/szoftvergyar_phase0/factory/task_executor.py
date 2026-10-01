"""Task persistence and runtime execution layer.

Ez a modul a state machine, a QA döntés és a Supervisor route runtime
végrehajtását valós, menthető task-persistenciával köti össze.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .task_runtime import TaskRuntime
from .task_state import TASK_STATES


@dataclass
class TaskRecord:
    task_id: str
    state: str = TASK_STATES["PENDING"]
    lane: str = "S2"
    iteration: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    history: list[dict[str, Any]] = field(default_factory=list)
    audit: list[dict[str, Any]] = field(default_factory=list)


class TaskStore:
    """Egyszerű in-memory task store a runtime számára."""

    def __init__(self):
        self._tasks: dict[str, TaskRecord] = {}

    def get(self, task_id: str) -> TaskRecord | None:
        return self._tasks.get(task_id)

    def create(self, task_id: str, *, lane: str = "S2", data: dict[str, Any] | None = None) -> TaskRecord:
        if task_id in self._tasks:
            return self._tasks[task_id]
        rec = TaskRecord(task_id=task_id, lane=lane, data=data or {})
        self._tasks[task_id] = rec
        return rec

    def save(self, rec: TaskRecord) -> TaskRecord:
        self._tasks[rec.task_id] = rec
        return rec

    def list(self) -> list[TaskRecord]:
        return list(self._tasks.values())


class TaskAudit:
    """Audit log a task lifecyclehez."""

    def __init__(self):
        self.events: list[dict[str, Any]] = []

    def append(self, task_id: str, event: str, payload: dict[str, Any] | None = None):
        self.events.append({
            "task_id": task_id,
            "event": event,
            "payload": payload or {},
        })
        return self.events[-1]


class TaskExecutor:
    """Runtime executor: QA döntés -> state update -> action routing."""

    def __init__(self, store: TaskStore | None = None, audit: TaskAudit | None = None):
        self.store = store or TaskStore()
        self.audit = audit or TaskAudit()

    def create_task(self, task_id: str, *, lane: str = "S2", data: dict[str, Any] | None = None) -> TaskRecord:
        rec = self.store.create(task_id, lane=lane, data=data)
        self.audit.append(task_id, "TASK_CREATED", {"lane": lane, "data": data or {}})
        return rec

    def apply_qa_result(self, task_id: str, qa_result: dict[str, Any] | str | None) -> dict[str, Any]:
        rec = self.store.get(task_id)
        if rec is None:
            raise ValueError(f"unknown task: {task_id}")

        decision = TaskRuntime.apply_qa_result(
            task_id,
            rec.state,
            qa_result,
            summary=str((qa_result or {}).get("summary") if isinstance(qa_result, dict) else "QA review completed."),
            reason=str((qa_result or {}).get("reason") if isinstance(qa_result, dict) else "No reason provided."),
            score=float((qa_result or {}).get("score", 0.0) if isinstance(qa_result, dict) else 0.0),
        )

        rec.state = decision.next_state
        rec.iteration += 1
        rec.history.append({
            "status": decision.status,
            "next_action": decision.next_action,
            "recipient": decision.recipient,
            "route": decision.route,
            "score": decision.score,
        })
        rec.audit.append({
            "event": "QA_RESULT_APPLIED",
            "status": decision.status,
            "next_state": decision.next_state,
            "next_action": decision.next_action,
            "recipient": decision.recipient,
            "route": decision.route,
        })

        self.audit.append(task_id, "QA_RESULT_APPLIED", {
            "status": decision.status,
            "next_state": decision.next_state,
            "next_action": decision.next_action,
            "recipient": decision.recipient,
            "route": decision.route,
        })

        self.store.save(rec)
        return {
            "task_id": task_id,
            "state": rec.state,
            "next_action": decision.next_action,
            "recipient": decision.recipient,
            "route": decision.route,
            "message": decision,
        }


__all__ = ["TaskRecord", "TaskStore", "TaskAudit", "TaskExecutor"]


if __name__ == "__main__":
    ex = TaskExecutor()
    ex.create_task("T-1", lane="S2")
    print(ex.apply_qa_result("T-1", {"status": "ACCEPT", "score": 0.92, "summary": "OK", "reason": "good"}))

"""Phase 1 runtime foundation for the Szoftvergyár system.

This layer adds the first real execution environment on top of the phase 0 flow:
- task persistence
- runtime execution state
- queue / scheduler bootstrap
- audit trail
- task lifecycle orchestration

The goal is to turn the phase 0 workflow engine into a runnable operational system.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional


@dataclass
class TaskRecord:
    """Persisted task record."""

    task_id: str
    state: str = "PENDING"
    lane: str = "S1"
    spec: dict[str, Any] = field(default_factory=dict)
    patch_files: dict[str, Any] | list[str] | tuple[str, ...] | None = None
    test_results: dict[str, Any] | None = None
    result: dict[str, Any] = field(default_factory=dict)
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    updated_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TaskAuditEntry:
    """Single audit entry for a task lifecycle action."""

    task_id: str
    event: str
    state: str
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskStore:
    """In-memory persistence layer for phase 1 runtime foundation."""

    def __init__(self):
        self._tasks: dict[str, TaskRecord] = {}
        self._audit: list[TaskAuditEntry] = []

    def create_task(
        self,
        task_id: str,
        *,
        lane: str = "S1",
        spec: dict[str, Any] | None = None,
        patch_files: dict[str, Any] | list[str] | tuple[str, ...] | None = None,
        test_results: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        if task_id in self._tasks:
            raise ValueError(f"task already exists: {task_id}")

        record = TaskRecord(
            task_id=task_id,
            state="PENDING",
            lane=lane,
            spec=spec or {},
            patch_files=patch_files,
            test_results=test_results,
            metadata=metadata or {},
        )
        self._tasks[task_id] = record
        self._audit.append(TaskAuditEntry(task_id, "created", "PENDING", details={"lane": lane}))
        return record

    def get_task(self, task_id: str) -> Optional[TaskRecord]:
        return self._tasks.get(task_id)

    def list_tasks(self) -> list[TaskRecord]:
        return list(self._tasks.values())

    def update_state(self, task_id: str, state: str, *, result: dict[str, Any] | None = None, metadata: dict[str, Any] | None = None) -> TaskRecord:
        record = self._tasks.get(task_id)
        if record is None:
            raise KeyError(f"task not found: {task_id}")

        record.state = state
        record.updated_at = datetime.now(timezone.utc).isoformat()
        if result is not None:
            record.result = result
        if metadata is not None:
            record.metadata.update(metadata)

        self._audit.append(TaskAuditEntry(task_id, "state_changed", state, details={"result": result or {}, "metadata": metadata or {}}))
        return record

    def append_audit(self, task_id: str, event: str, state: str, **details: Any) -> TaskAuditEntry:
        entry = TaskAuditEntry(task_id=task_id, event=event, state=state, details=details)
        self._audit.append(entry)
        return entry

    def get_audit(self, task_id: str) -> list[TaskAuditEntry]:
        return [entry for entry in self._audit if entry.task_id == task_id]


class TaskScheduler:
    """Minimal queue / scheduling bootstrap for phase 1."""

    def __init__(self):
        self._pending: list[str] = []

    def enqueue(self, task_id: str) -> None:
        if task_id not in self._pending:
            self._pending.append(task_id)

    def next(self) -> Optional[str]:
        if not self._pending:
            return None
        return self._pending.pop(0)

    def queue_size(self) -> int:
        return len(self._pending)


class TaskExecutor:
    """Phase 1 executor runtime wrapper around the phase 0 workflow engine."""

    def __init__(self, store: TaskStore | None = None):
        self.store = store or TaskStore()
        self.scheduler = TaskScheduler()

    def create_task(
        self,
        task_id: str,
        *,
        lane: str = "S1",
        spec: dict[str, Any] | None = None,
        patch_files: dict[str, Any] | list[str] | tuple[str, ...] | None = None,
        test_results: dict[str, Any] | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> TaskRecord:
        record = self.store.create_task(task_id, lane=lane, spec=spec, patch_files=patch_files, test_results=test_results, metadata=metadata)
        self.scheduler.enqueue(task_id)
        return record

    def run_pending(self, runner: Any) -> list[dict[str, Any]]:
        """Execute scheduled tasks using a provided runner object.

        runner is expected to expose a run(task_id, spec, patch_files, test_results, ...) method.
        """
        results: list[dict[str, Any]] = []
        while True:
            task_id = self.scheduler.next()
            if task_id is None:
                break
            task = self.store.get_task(task_id)
            if task is None:
                continue

            task_result = runner.run(
                task_id=task.task_id,
                spec=task.spec,
                patch_files=task.patch_files,
                test_results=task.test_results,
            )
            self.store.update_state(task.task_id, state=task_result.get("next_state", "PENDING"), result=task_result)
            self.store.append_audit(task.task_id, "executed", task_result.get("next_state", "PENDING"), result=task_result)
            results.append(task_result)
        return results


__all__ = [
    "TaskRecord",
    "TaskAuditEntry",
    "TaskStore",
    "TaskScheduler",
    "TaskExecutor",
]

"""Phase 1 runtime API.

This module exposes the first usable operational interface on top of the phase 0
workflow engine and the phase 1 in-memory runtime primitives.

Primary functions:
- create_task(...)
- run_task(...)
- get_task(task_id)
- list_tasks()
- resume_task(task_id, human_decision)
"""
from __future__ import annotations

from typing import Any

from factory.workflow_orchestrator import WorkflowOrchestrator
from phase1_runtime import TaskExecutor, TaskRecord, TaskStore


class RuntimeAPI:
    """Thin runtime façade for phase 1 orchestration."""

    def __init__(self, store: TaskStore | None = None, executor: TaskExecutor | None = None):
        self.store = store or TaskStore()
        self.executor = executor or TaskExecutor(self.store)
        self.orchestrator = WorkflowOrchestrator()

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
        record = self.executor.create_task(
            task_id,
            lane=lane,
            spec=spec,
            patch_files=patch_files,
            test_results=test_results,
            metadata=metadata,
        )
        self.store.append_audit(task_id, "created", record.state, lane=lane)
        return record

    def run_task(
        self,
        task_id: str,
        *,
        spec: dict[str, Any] | None = None,
        patch_files: dict[str, Any] | list[str] | tuple[str, ...] | None = None,
        test_results: dict[str, Any] | None = None,
        current_state: str = "IN_PROGRESS",
        iteration: int = 0,
        actor: str = "supervisor",
    ) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            task = self.create_task(task_id, spec=spec or {}, patch_files=patch_files, test_results=test_results)

        effective_spec = spec or task.spec or {}
        effective_patch = patch_files if patch_files is not None else task.patch_files
        effective_tests = test_results if test_results is not None else task.test_results

        self.store.update_state(task_id, "IN_PROGRESS", metadata={"iteration": iteration, "actor": actor})
        result = self.orchestrator.run(
            task_id,
            effective_spec,
            effective_patch,
            effective_tests,
            current_state=current_state,
            iteration=iteration,
            actor=actor,
        )

        self.store.update_state(
            task_id,
            result.next_state,
            result=result.to_dict(),
            metadata={"next_action": result.next_action, "recipient": result.recipient, "route": result.route},
        )
        self.store.append_audit(
            task_id,
            "workflow_completed",
            result.next_state,
            next_action=result.next_action,
            recipient=result.recipient,
            route=result.route,
        )
        return result.to_dict()

    def resume_task(
        self,
        task_id: str,
        human_decision: str,
        *,
        actor: str = "supervisor",
    ) -> dict[str, Any]:
        task = self.store.get_task(task_id)
        if task is None:
            raise KeyError(f"task not found: {task_id}")

        result = self.orchestrator.run(
            task_id,
            task.spec,
            task.patch_files,
            task.test_results,
            current_state=task.state,
            iteration=0,
            actor=actor,
            human_decision=human_decision,
        )

        self.store.update_state(
            task_id,
            result.next_state,
            result=result.to_dict(),
            metadata={"next_action": result.next_action, "recipient": result.recipient, "route": result.route},
        )
        self.store.append_audit(
            task_id,
            "human_resolution",
            result.next_state,
            human_decision=human_decision,
            next_action=result.next_action,
            recipient=result.recipient,
        )
        return result.to_dict()

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        task = self.store.get_task(task_id)
        return None if task is None else task.to_dict()

    def list_tasks(self) -> list[dict[str, Any]]:
        return [task.to_dict() for task in self.store.list_tasks()]


DEFAULT_RUNTIME_API = RuntimeAPI()

__all__ = ["RuntimeAPI", "DEFAULT_RUNTIME_API"]


if __name__ == "__main__":
    api = RuntimeAPI()
    api.create_task(
        "TASK-API-1",
        spec={
            "required_files": ["factory/qa.py"],
            "acceptance_criteria": ["patch file", "tests pass"],
        },
        patch_files={"factory/qa.py": "ok"},
        test_results={"unit": True},
    )
    result = api.run_task("TASK-API-1", iteration=1)
    print(result)

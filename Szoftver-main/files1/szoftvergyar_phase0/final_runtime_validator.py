"""Final runtime validation bundle for the Szoftvergyár task execution engine.

This file serves as the last validation layer before the runtime is considered
stable and production-ready.
"""
from __future__ import annotations

from task_lifecycle import TaskLifecycle, LifecycleManager, VALID_STATES, ALLOWED_TRANSITIONS
from phase1_runtime_api import RuntimeAPI


class FinalRuntimeValidator:
    """Runtime sanity checker used to validate lifecycle consistency."""

    def __init__(self):
        self.manager = LifecycleManager()

    def validate_state_machine(self) -> dict[str, bool]:
        checks = {}

        for state in VALID_STATES:
            checks[f"state_{state}_is_known"] = TaskLifecycle.valid_state(state)

        for from_state, allowed in ALLOWED_TRANSITIONS.items():
            for to_state in allowed:
                checks[f"{from_state}->{to_state}"] = TaskLifecycle.can_transition(from_state, to_state)

        return checks

    def validate_runtime_task(self, task_id: str, spec: dict, patch_files: dict | None = None, test_results: dict | None = None) -> dict:
        api = RuntimeAPI()
        api.create_task(task_id, spec=spec, patch_files=patch_files, test_results=test_results)
        result = api.run_task(task_id, iteration=1)

        if result["next_state"] == "HUMAN_ESCALATION":
            resumed = api.resume_task(task_id, "continue")
            result["resolved_after_human"] = resumed

        return {
            "task_id": task_id,
            "result": result,
            "state_valid": result["next_state"] in VALID_STATES,
            "transitions_checked": self.validate_state_machine(),
        }

    def validate_illegal_transition(self) -> dict:
        try:
            TaskLifecycle.transition("ACCEPTED", "IN_PROGRESS")
            return {"illegal_transition": "unexpectedly_allowed"}
        except ValueError:
            return {"illegal_transition": "blocked"}


if __name__ == "__main__":
    validator = FinalRuntimeValidator()
    print(validator.validate_state_machine())
    print(validator.validate_illegal_transition())
    print(
        validator.validate_runtime_task(
            "FINAL-CHECK-001",
            {"required_files": ["factory/qa.py"], "acceptance_criteria": ["patch file", "tests pass"]},
            {"factory/qa.py": "ok"},
            {"unit": True},
        )
    )

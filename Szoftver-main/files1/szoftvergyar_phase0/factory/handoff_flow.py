"""End-to-end workflow glue for the phase 0 agent system.

This module coordinates the large handoff pipeline:
- lane classification (if available)
- Product Owner spec readiness
- QA review decision
- Supervisor routing
- Human escalation when clarification is required
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .qa import QA
from .supervisor import Supervisor
from .task_state import TASK_STATES, TaskStateMachine


class HandoffFlow:
    """A single orchestration object that wires QA and Supervisor together."""

    def __init__(self, qa: QA | None = None, supervisor: Supervisor | None = None):
        self.qa = qa or QA()
        self.supervisor = supervisor or Supervisor()

    def review_patch(
        self,
        task_id: str,
        spec: Mapping[str, Any],
        patch_files: Mapping[str, Any] | list[str] | tuple[str, ...] | None,
        test_results: Mapping[str, Any] | None = None,
        *,
        iteration: int = 0,
        actor: str = "supervisor",
    ) -> dict[str, Any]:
        """Run QA review and immediately route the decision through Supervisor."""
        decision = self.qa.run(
            task_id=task_id,
            spec=dict(spec),
            patch_files=patch_files,
            test_results=test_results,
            iteration=iteration,
        )
        routed = self.supervisor.route_qa_decision(task_id, decision, iteration=iteration, actor=actor)
        return {
            "task_id": task_id,
            "qa_decision": decision,
            "supervisor_route": routed,
            "state": routed["state"],
            "next_action": routed["next_action"],
            "recipient": routed["recipient"],
        }

    def handle_needs_info(
        self,
        task_id: str,
        *,
        reason: str = "Needs clarification from human or product owner.",
        actor: str = "supervisor",
        gate_id: str = "",
    ) -> dict[str, Any]:
        """Explicit escalation path when QA can not approve or reject without extra info."""
        return self.supervisor.escalate_human(
            task_id,
            actor=actor,
            reason=reason,
            gate_id=gate_id,
        )

    def accept_or_escalate(
        self,
        task_id: str,
        decision: Mapping[str, Any] | str,
        *,
        iteration: int = 0,
        actor: str = "supervisor",
    ) -> dict[str, Any]:
        """Convenience wrapper that resolves a raw decision into a route or escalation."""
        normalized = self.supervisor.normalize_qa_result(decision)
        state, next_action, recipient = TaskStateMachine.qa_decision_to_state(normalized)
        if state == TASK_STATES["NEEDS_INFO"]:
            return self.handle_needs_info(task_id, reason=str(normalized.get("reason") or "Needs clarification."), actor=actor)
        return self.supervisor.route_qa_decision(task_id, normalized, iteration=iteration, actor=actor)


DEFAULT_HANDOFF_FLOW = HandoffFlow()

__all__ = ["HandoffFlow", "DEFAULT_HANDOFF_FLOW"]


if __name__ == "__main__":
    flow = HandoffFlow()
    result = flow.review_patch(
        "TASK-42",
        {"required_files": ["factory/qa.py"], "acceptance_criteria": ["patch file", "tests pass"]},
        {"factory/qa.py": "ok"},
        {"unit": True},
        iteration=1,
    )
    print(result)

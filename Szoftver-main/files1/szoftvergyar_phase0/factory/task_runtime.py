"""Runtime task router and QA decision adapter.

Ez a modul a task lifecycle, a QA döntés és a Supervisor route kombinációját
kötő runtime réteget adja meg. Cél: a pipeline ne csak modellezett legyen,
hanem szerkezett, validált és követhető runtime döntéseket produkáljon.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from .task_state import TASK_STATES, TaskStateMachine


@dataclass
class TaskRuntimeDecision:
    task_id: str
    current_state: str
    next_state: str
    next_action: str
    recipient: str
    status: str
    score: float
    summary: str
    reason: str
    route: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)


class TaskRuntime:
    """Runtime adapter a QA döntésének Supervisor / state machine integrációjára."""

    @staticmethod
    def normalize_decision(result: dict[str, Any] | str | None) -> str:
        if result is None:
            return "NEEDS_INFO"
        if isinstance(result, str):
            val = result.strip().upper()
            return val if val in {"ACCEPT", "REJECT", "NEEDS_INFO"} else "NEEDS_INFO"
        status = str(result.get("status") or result.get("verdict") or "NEEDS_INFO").upper()
        return status if status in {"ACCEPT", "REJECT", "NEEDS_INFO"} else "NEEDS_INFO"

    @staticmethod
    def apply_qa_result(
        task_id: str,
        current_state: str,
        qa_result: dict[str, Any] | str | None,
        *,
        summary: str | None = None,
        reason: str | None = None,
        score: float | None = None,
    ) -> TaskRuntimeDecision:
        """A QA eredményéből előállítja a következő runtime state-et és route-ot."""
        if not task_id:
            raise ValueError("task_id cannot be empty")

        decision = TaskRuntime.normalize_decision(qa_result)
        state, next_action, recipient = TaskStateMachine.qa_decision_to_state(decision)

        if current_state not in TaskStateMachine.valid_states():
            raise ValueError(f"ismeretlen állapot: {current_state}")

        # A QA review előtt kötelező, hogy a task már a QA_REVIEW fázisban legyen.
        # Ha nem, a runtime explicit, validált átmenetként kezeli.
        if current_state == TASK_STATES["IN_PROGRESS"]:
            current_state = TaskStateMachine.transition(current_state, "QA_REVIEW")

        if decision == "ACCEPT":
            next_state = state
            route = "accepted"
        elif decision == "REJECT":
            next_state = state
            route = "revision_required"
        else:
            next_state = state
            route = "needs_info"

        metadata: dict[str, Any] = {
            "decision": decision,
            "source_state": current_state,
            "next_action": next_action,
            "recipient": recipient,
            "score": float(score or 0.0),
        }

        return TaskRuntimeDecision(
            task_id=task_id,
            current_state=current_state,
            next_state=next_state,
            next_action=next_action,
            recipient=recipient,
            status=decision,
            score=float(score or 0.0),
            summary=summary or "QA review completed.",
            reason=reason or "No reason provided.",
            route=route,
            metadata=metadata,
        )


__all__ = ["TaskRuntimeDecision", "TaskRuntime"]


if __name__ == "__main__":
    d = TaskRuntime.apply_qa_result(
        "TASK-1",
        TASK_STATES["IN_PROGRESS"],
        {"status": "ACCEPT", "score": 0.94},
        summary="OK",
        reason="meets criteria",
    )
    print(d)

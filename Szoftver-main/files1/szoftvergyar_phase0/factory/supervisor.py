"""Supervisor integration: QA handoff, route selection and task state machine.

Ez a modul köt össze három dolgot:
- a QA eredményét,
- a Supervisor döntését,
- a task lifecycle explicit állapotátmeneteit.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .messages import make as make_message
from .qa import QA
from .task_state import TASK_STATES, TaskStateMachine


class PhaseNotEnabled(RuntimeError):
    """A fázis nem engedélyezett a szakaszban."""


class Supervisor:
    """Koordinátor a task lifecycle és a QA handoff között."""

    ACCEPTED = TASK_STATES["ACCEPTED"]
    REJECTED = TASK_STATES["REJECTED"]
    NEEDS_INFO = TASK_STATES["NEEDS_INFO"]

    def __init__(self, enabled_phases: set[str] | None = None, audit=None):
        self.enabled_phases = enabled_phases or {"plan", "build", "qa", "handoff"}
        self.audit = audit

    def startup(self):
        return {"status": "ok", "enabled_phases": sorted(self.enabled_phases)}

    def check_phase(self, phase: str) -> bool:
        """Ellenőrzi, hogy a fázis engedélyezett-e."""
        if phase not in self.enabled_phases:
            raise PhaseNotEnabled(f"phase '{phase}' is not enabled")
        return True

    def normalize_qa_result(self, qa_result: Mapping[str, Any] | str) -> dict[str, Any]:
        """A QA-válasz normálása a Supervisor közös formájára."""
        if isinstance(qa_result, Mapping):
            result = dict(qa_result)
            status = QA.normalize_status(result)
            result.setdefault("status", status)
            result.setdefault("score", float(result.get("score", 0.0) or 0.0))
            result.setdefault("summary", "QA review completed.")
            result.setdefault("reason", "No reason provided.")
            return result
        return {
            "status": QA.normalize_status(str(qa_result)),
            "score": 0.0,
            "summary": "QA review completed.",
            "reason": "No reason provided.",
        }

    def handle_qa_result(
        self,
        task_id: str,
        qa_result: Mapping[str, Any] | str,
        *,
        iteration: int = 0,
        actor: str = "supervisor",
    ) -> dict[str, Any]:
        """A QA döntés átalakítása explicit task route-ra és üzenetre."""
        result = self.normalize_qa_result(qa_result)
        return self.route_qa_decision(task_id, result, iteration=iteration, actor=actor)

    def route_qa_decision(
        self,
        task_id: str,
        qa_result: Mapping[str, Any],
        *,
        iteration: int = 0,
        actor: str = "supervisor",
    ) -> dict[str, Any]:
        """QA döntését fordítja állapotgépi route-ra és üzenetre."""
        if not task_id:
            raise ValueError("task_id cannot be empty")

        normalized = self.normalize_qa_result(qa_result)
        state, next_action, recipient = TaskStateMachine.qa_decision_to_state(normalized)
        score = float((normalized or {}).get("score", 0.0) or 0.0)
        summary = str((normalized or {}).get("summary") or "QA review completed.")
        reason = str((normalized or {}).get("reason") or "No reason provided.")

        if state == self.ACCEPTED:
            payload = {
                "task_id": task_id,
                "status": "ACCEPT",
                "score": score,
                "summary": summary,
                "reason": reason,
                "decision": "accept",
            }
        elif state == self.REJECTED:
            payload = {
                "task_id": task_id,
                "status": "REJECT",
                "score": score,
                "summary": summary,
                "reason": reason,
                "decision": "reject",
                "required_fix": "inspect_qa_failures",
            }
        else:
            payload = {
                "task_id": task_id,
                "status": "NEEDS_INFO",
                "score": score,
                "summary": summary,
                "reason": reason,
                "decision": "needs_info",
                "required_input": (normalized or {}).get("evidence") or {},
            }

        message = make_message(
            task_id=task_id,
            frm=actor,
            to=recipient,
            mtype="QA_HANDOFF",
            payload=payload,
            iteration=iteration,
            state=state,
            action=next_action,
        )

        return {
            "task_id": task_id,
            "state": state,
            "next_action": next_action,
            "recipient": recipient,
            "score": round(max(0.0, min(1.0, score)), 3),
            "summary": summary,
            "reason": reason,
            "message": message,
        }

    def run_qa_handoff(
        self,
        task_id: str,
        qa_result: Mapping[str, Any],
        *,
        iteration: int = 0,
        actor: str = "supervisor",
    ) -> dict[str, Any]:
        """Alias a route_qa_decision számára; kompatibilitás miatt megtartva."""
        return self.route_qa_decision(task_id, qa_result, iteration=iteration, actor=actor)

    def transition(self, current_state: str, event: str) -> str:
        """Explicit task state transition wrapper."""
        return TaskStateMachine.transition(current_state, event)

    def route_task(self, task_id: str, qa_result: Mapping[str, Any], *, iteration: int = 0, actor: str = "supervisor") -> dict[str, Any]:
        """Végső task route: QA döntésének Supervisor hívása."""
        result = self.route_qa_decision(task_id, qa_result, iteration=iteration, actor=actor)
        if result["state"] == self.ACCEPTED:
            return {
                "task_id": task_id,
                "route": "accepted",
                "next_action": "handoff_to_owner",
                "recipient": "product_owner",
                "message": result["message"],
            }
        if result["state"] == self.REJECTED:
            return {
                "task_id": task_id,
                "route": "revision_required",
                "next_action": "return_for_revision",
                "recipient": "master_coder",
                "message": result["message"],
            }
        return {
            "task_id": task_id,
            "route": "needs_info",
            "next_action": "request_clarification",
            "recipient": "product_owner",
            "message": result["message"],
        }


DEFAULT_SUPERVISOR = Supervisor


if __name__ == "__main__":
    sup = Supervisor()
    print(sup.startup())
    print(
        sup.route_qa_decision(
            "TASK-001",
            {"status": "ACCEPT", "score": 1.0, "summary": "OK", "reason": "meets criteria"},
            iteration=2,
        )
    )

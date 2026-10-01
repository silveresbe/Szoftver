"""Supervisor integration: handoff from QA to task routing.

Ez a modul a QA döntését a Supervisor által elfogadott, strukturált task-state
hívásokhoz köti össze. A cél: a pipeline ugyanazt a döntést produkálja, amit a
logika szándékozik.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .messages import make as make_message


class PhaseNotEnabled(RuntimeError):
    """A fázis nem engedélyezett a szakaszban."""


class Supervisor:
    """Koordinátor a task lifecycle és a QA handoff között."""

    ACCEPTED = "accepted"
    REJECTED = "rejected"
    NEEDS_INFO = "needs_info"

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

    def run_qa_handoff(
        self,
        task_id: str,
        qa_result: Mapping[str, Any],
        *,
        iteration: int = 0,
        actor: str = "supervisor",
    ) -> dict[str, Any]:
        """A QA döntését konvertálja Supervisor state-re / message-re.

        A visszatérési payload tartalmazza a végső task állapotot, a következő
        akciót és a handoff-t a Product Owner vagy a Coder felé.
        """
        if not task_id:
            raise ValueError("task_id cannot be empty")

        status = str(qa_result.get("status", "NEEDS_INFO")).upper()
        score = float(qa_result.get("score", 0.0) or 0.0)
        summary = str(qa_result.get("summary") or "QA review completed.")
        reason = str(qa_result.get("reason") or "No reason provided.")

        if status == "ACCEPT":
            next_state = self.ACCEPTED
            next_action = "handoff_to_owner"
            recipient = "product_owner"
            payload = {
                "task_id": task_id,
                "status": "ACCEPT",
                "score": score,
                "summary": summary,
                "reason": reason,
                "decision": "accept",
            }
        elif status == "REJECT":
            next_state = self.REJECTED
            next_action = "return_for_revision"
            recipient = "master_coder"
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
            next_state = self.NEEDS_INFO
            next_action = "request_clarification"
            recipient = "product_owner"
            payload = {
                "task_id": task_id,
                "status": "NEEDS_INFO",
                "score": score,
                "summary": summary,
                "reason": reason,
                "decision": "needs_info",
                "required_input": qa_result.get("evidence") or {},
            }

        message = make_message(
            task_id=task_id,
            frm=actor,
            to=recipient,
            mtype="QA_HANDOFF",
            payload=payload,
            iteration=iteration,
            state=next_state,
            action=next_action,
        )

        return {
            "task_id": task_id,
            "state": next_state,
            "status": status,
            "score": round(max(0.0, min(1.0, score)), 3),
            "next_action": next_action,
            "recipient": recipient,
            "summary": summary,
            "reason": reason,
            "message": message,
        }

    def route_task(
        self,
        task_id: str,
        qa_result: Mapping[str, Any],
        *,
        iteration: int = 0,
        actor: str = "supervisor",
    ) -> dict[str, Any]:
        """Végső task route: QA döntésének Supervisor hívása."""
        decision = self.run_qa_handoff(task_id, qa_result, iteration=iteration, actor=actor)

        if decision["state"] == self.ACCEPTED:
            return {
                "task_id": task_id,
                "route": "accepted",
                "next_action": "handoff_to_owner",
                "recipient": "product_owner",
                "message": decision["message"],
            }
        if decision["state"] == self.REJECTED:
            return {
                "task_id": task_id,
                "route": "revision_required",
                "next_action": "return_for_revision",
                "recipient": "master_coder",
                "message": decision["message"],
            }
        return {
            "task_id": task_id,
            "route": "needs_info",
            "next_action": "request_clarification",
            "recipient": "product_owner",
            "message": decision["message"],
        }


DEFAULT_SUPERVISOR = Supervisor


if __name__ == "__main__":
    sup = Supervisor()
    print(sup.startup())
    print(
        sup.run_qa_handoff(
            "TASK-001",
            {"status": "ACCEPT", "score": 1.0, "summary": "OK", "reason": "meets criteria"},
            iteration=2,
        )
    )

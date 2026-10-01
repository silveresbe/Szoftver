"""Task lifecycle state machine for QA handoff and routing.

Ez a modul a feladat életciklusának végső, explicit transition-logikáját
adja meg. A cél: minden döntés (ACCEPT / REJECT / NEEDS_INFO) egyértelmű
állapotátmenetből származzon, amit a Supervisor és a QA is ugyanazokból a
konstansokból használhat.
"""
from __future__ import annotations

from collections.abc import Mapping
from typing import Any


TASK_STATES = {
    "PENDING": "PENDING",
    "IN_PROGRESS": "IN_PROGRESS",
    "QA_REVIEW": "QA_REVIEW",
    "ACCEPTED": "ACCEPTED",
    "REJECTED": "REJECTED",
    "NEEDS_INFO": "NEEDS_INFO",
    "HUMAN_ESCALATION": "HUMAN_ESCALATION",
    "DONE": "DONE",
    "DISCARDED": "DISCARDED",
    "HALTED": "HALTED",
}

QA_DECISIONS = {"ACCEPT", "REJECT", "NEEDS_INFO"}

STATE_TRANSITIONS = {
    TASK_STATES["PENDING"]: {
        "START": TASK_STATES["IN_PROGRESS"],
        "PAUSE": TASK_STATES["PENDING"],
        "HALT": TASK_STATES["HALTED"],
    },
    TASK_STATES["IN_PROGRESS"]: {
        "QA_REVIEW": TASK_STATES["QA_REVIEW"],
        "HALT": TASK_STATES["HALTED"],
        "DISCARD": TASK_STATES["DISCARDED"],
    },
    TASK_STATES["QA_REVIEW"]: {
        "ACCEPT": TASK_STATES["ACCEPTED"],
        "REJECT": TASK_STATES["REJECTED"],
        "NEEDS_INFO": TASK_STATES["NEEDS_INFO"],
        "ESCALATE": TASK_STATES["HUMAN_ESCALATION"],
    },
    TASK_STATES["ACCEPTED"]: {
        "DONE": TASK_STATES["DONE"],
        "ESCALATE": TASK_STATES["HUMAN_ESCALATION"],
    },
    TASK_STATES["REJECTED"]: {
        "RETRY": TASK_STATES["IN_PROGRESS"],
        "ESCALATE": TASK_STATES["HUMAN_ESCALATION"],
    },
    TASK_STATES["NEEDS_INFO"]: {
        "RESOLVE": TASK_STATES["PENDING"],
        "ESCALATE": TASK_STATES["HUMAN_ESCALATION"],
    },
    TASK_STATES["HUMAN_ESCALATION"]: {
        "CONTINUE": TASK_STATES["PENDING"],
        "DISCARD": TASK_STATES["DISCARDED"],
        "HALT": TASK_STATES["HALTED"],
    },
    TASK_STATES["DONE"]: {},
    TASK_STATES["DISCARDED"]: {},
    TASK_STATES["HALTED"]: {},
}


class TaskStateMachineError(ValueError):
    """A task state machine érvénytelen állapotátmenetért."""


class TaskStateMachine:
    """Explicit task lifecycle state machine.

    A döntés logika a QA és a Supervisor közös nyelvét használja: a QA
    eredményéből a transition() függvény mindig egy előre definiált következő
    állapotot ad vissza.
    """

    @staticmethod
    def valid_states() -> set[str]:
        return set(TASK_STATES.values())

    @staticmethod
    def transition(current_state: str, event: str) -> str:
        if current_state not in STATE_TRANSITIONS:
            raise TaskStateMachineError(f"ismeretlen állapot: {current_state}")
        next_state = STATE_TRANSITIONS[current_state].get(event)
        if next_state is None:
            raise TaskStateMachineError(
                f"érvénytelen átmenet: {current_state} --({event})-->"
            )
        return next_state

    @staticmethod
    def qa_decision_to_state(decision: str | Mapping[str, Any]) -> tuple[str, str, str]:
        """QA döntéséből adja a következő állapotot és route-ot.

        Visszatér: (state, next_action, recipient)
        """
        if isinstance(decision, Mapping):
            status = str(decision.get("status") or decision.get("verdict") or "NEEDS_INFO").upper()
        else:
            status = str(decision).upper()

        status = status if status in QA_DECISIONS else "NEEDS_INFO"

        if status == "ACCEPT":
            return TASK_STATES["ACCEPTED"], "handoff_to_owner", "product_owner"
        if status == "REJECT":
            return TASK_STATES["REJECTED"], "return_for_revision", "master_coder"
        return TASK_STATES["NEEDS_INFO"], "request_clarification", "product_owner"


__all__ = [
    "TASK_STATES",
    "QA_DECISIONS",
    "STATE_TRANSITIONS",
    "TaskStateMachine",
    "TaskStateMachineError",
    "TaskStateMachineError",
]


if __name__ == "__main__":
    print(TaskStateMachine.qa_decision_to_state("ACCEPT"))
    print(TaskStateMachine.transition(TASK_STATES["IN_PROGRESS"], "QA_REVIEW"))

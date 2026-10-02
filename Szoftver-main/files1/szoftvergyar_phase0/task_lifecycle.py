"""Phase 1 task lifecycle and runtime stabilization.

This module completes the task lifecycle contract for the runtime API:
- explicit state transitions
- retry / resume / human resolution handling
- consistent result payloads
- lifecycle audit support

The design follows the phase 0 workflow decisions but makes them runtime-safe
and operationally consistent.
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any


VALID_STATES = {
    "PENDING",
    "IN_PROGRESS",
    "QA_REVIEW",
    "ACCEPTED",
    "REJECTED",
    "NEEDS_INFO",
    "HUMAN_ESCALATION",
    "RESUMED",
    "HALTED",
    "DISCARDED",
    "COMPLETED",
}


ALLOWED_TRANSITIONS = {
    "PENDING": {"IN_PROGRESS", "HALTED", "DISCARDED"},
    "IN_PROGRESS": {"QA_REVIEW", "HALTED", "DISCARDED"},
    "QA_REVIEW": {"ACCEPTED", "REJECTED", "NEEDS_INFO", "HALTED", "DISCARDED"},
    "ACCEPTED": {"COMPLETED", "HALTED", "DISCARDED"},
    "REJECTED": {"IN_PROGRESS", "HALTED", "DISCARDED"},
    "NEEDS_INFO": {"HUMAN_ESCALATION", "HALTED", "DISCARDED"},
    "HUMAN_ESCALATION": {"RESUMED", "HALTED", "DISCARDED"},
    "RESUMED": {"IN_PROGRESS", "QA_REVIEW", "HALTED", "DISCARDED"},
    "HALTED": {"RESUMED", "DISCARDED"},
    "DISCARDED": set(),
    "COMPLETED": {"HALTED", "DISCARDED"},
}


@dataclass
class RuntimeTaskResult:
    """Stable payload returned by the runtime for each task execution."""

    task_id: str
    current_state: str
    next_state: str
    next_action: str
    recipient: str
    qa_status: str
    score: float
    summary: str
    reason: str
    route: str
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class LifecycleEvent:
    """Lifecycle event for audit purposes."""

    task_id: str
    event: str
    from_state: str
    to_state: str
    at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TaskLifecycle:
    """Central state machine for the phase 1 runtime lifecycle."""

    @staticmethod
    def valid_state(state: str) -> bool:
        return state in VALID_STATES

    @staticmethod
    def can_transition(from_state: str, to_state: str) -> bool:
        return from_state in ALLOWED_TRANSITIONS and to_state in ALLOWED_TRANSITIONS[from_state]

    @staticmethod
    def transition(from_state: str, to_state: str) -> str:
        if not TaskLifecycle.valid_state(from_state):
            raise ValueError(f"unknown from_state: {from_state}")
        if not TaskLifecycle.valid_state(to_state):
            raise ValueError(f"unknown to_state: {to_state}")
        if not TaskLifecycle.can_transition(from_state, to_state):
            raise ValueError(f"illegal transition: {from_state} -> {to_state}")
        return to_state

    @staticmethod
    def normalize_human_decision(decision: str) -> str:
        value = str(decision or "").strip().upper()
        valid = {"CONTINUE", "HALT", "DISCARD", "RESUME"}
        if value == "RESUME":
            return "RESUMED"
        if value == "CONTINUE":
            return "RESUMED"
        if value == "HALT":
            return "HALTED"
        if value == "DISCARD":
            return "DISCARDED"
        raise ValueError(f"unsupported human decision: {decision}")


class LifecycleManager:
    """Helps ensure a valid lifecycle and records transition events."""

    def __init__(self):
        self.events: list[LifecycleEvent] = []

    def apply_transition(self, task_id: str, from_state: str, to_state: str, **details: Any) -> LifecycleEvent:
        updated = TaskLifecycle.transition(from_state, to_state)
        event = LifecycleEvent(task_id=task_id, event="state_transition", from_state=from_state, to_state=updated, details=details)
        self.events.append(event)
        return event

    def resolve_human(self, task_id: str, current_state: str, decision: str, **details: Any) -> LifecycleEvent:
        normalized = TaskLifecycle.normalize_human_decision(decision)
        if current_state not in {"HUMAN_ESCALATION", "HALTED"}:
            raise ValueError(f"human resolution not allowed from state: {current_state}")
        next_state = TaskLifecycle.transition(current_state, normalized)
        event = LifecycleEvent(task_id=task_id, event="human_resolution", from_state=current_state, to_state=next_state, details={"decision": decision, **details})
        self.events.append(event)
        return event


__all__ = [
    "VALID_STATES",
    "ALLOWED_TRANSITIONS",
    "RuntimeTaskResult",
    "LifecycleEvent",
    "TaskLifecycle",
    "LifecycleManager",
]

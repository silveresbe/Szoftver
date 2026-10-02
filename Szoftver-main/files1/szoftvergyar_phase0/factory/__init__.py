"""Szoftvergyár – 0. fázis (váz). Spec: szoftvergyar_ugynok_rendszer_v1_5.md (12.1)."""

from .handoff_flow import DEFAULT_HANDOFF_FLOW, HandoffFlow
from .qa import QA
from .supervisor import Supervisor
from .task_state import TaskStateMachine

__version__ = "0.1.0"

__all__ = [
    "__version__",
    "HandoffFlow",
    "DEFAULT_HANDOFF_FLOW",
    "QA",
    "Supervisor",
    "TaskStateMachine",
]

"""Complete end-to-end workflow orchestration for phase 0 agent system.

This module is the final coordinator that drives the entire task pipeline:
- spec/DOR validation (via product owner)
- QA review
- Supervisor routing
- human escalation and resolution
- task lifecycle state transitions
- final deterministic output contract
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Optional

from .dor import check as dor_check
from .qa import QA
from .supervisor import Supervisor
from .task_state import TASK_STATES, TaskStateMachine


@dataclass
class WorkflowPhaseResult:
    """A single phase result in the workflow."""

    phase: str  # spec, qa, supervisor, human_escalation, completed
    status: str  # success, error, needs_info, escalated
    state: str  # current task state
    message: str
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass
class TaskWorkflowResult:
    """Final unified task workflow result."""

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
    phase_results: list[WorkflowPhaseResult] = field(default_factory=list)
    qa_result: dict[str, Any] = field(default_factory=dict)
    supervisor_route: dict[str, Any] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class WorkflowOrchestrator:
    """Coordinates the complete task workflow from spec to final state."""

    def __init__(
        self,
        qa: QA | None = None,
        supervisor: Supervisor | None = None,
    ):
        self.qa = qa or QA()
        self.supervisor = supervisor or Supervisor()
        self.phases: list[WorkflowPhaseResult] = []

    def validate_spec(self, spec: dict[str, Any]) -> tuple[bool, list[str]]:
        """Validate spec via DOR check."""
        errors = dor_check(spec)
        return len(errors) == 0, errors

    def run_qa(
        self,
        task_id: str,
        spec: dict[str, Any],
        patch_files: dict[str, Any] | list[str] | tuple[str, ...] | None,
        test_results: dict[str, Any] | None,
        iteration: int,
    ) -> tuple[dict[str, Any], str]:
        """Run QA review and return decision and status."""
        result = self.qa.run(
            task_id=task_id,
            spec=dict(spec),
            patch_files=patch_files,
            test_results=test_results,
            iteration=iteration,
        )
        status = result.get("status", "NEEDS_INFO")
        return result, status

    def route_qa_decision(
        self,
        task_id: str,
        qa_result: dict[str, Any],
        iteration: int,
        actor: str,
    ) -> dict[str, Any]:
        """Route QA decision through Supervisor."""
        return self.supervisor.route_qa_decision(
            task_id, qa_result, iteration=iteration, actor=actor
        )

    def handle_escalation(
        self,
        task_id: str,
        reason: str,
        gate_id: str = "",
        actor: str = "supervisor",
    ) -> dict[str, Any]:
        """Escalate task to human."""
        return self.supervisor.escalate_human(
            task_id, actor=actor, reason=reason, gate_id=gate_id
        )

    def resolve_escalation(
        self,
        task_id: str,
        human_decision: str,
        actor: str = "supervisor",
    ) -> dict[str, Any]:
        """Resolve human escalation (CONTINUE, HALT, DISCARD)."""
        return self.supervisor.resolve_from_escalation(
            task_id, actor=actor, human_decision=human_decision
        )

    def run(
        self,
        task_id: str,
        spec: dict[str, Any],
        patch_files: dict[str, Any] | list[str] | tuple[str, ...] | None = None,
        test_results: dict[str, Any] | None = None,
        *,
        current_state: str = TASK_STATES["IN_PROGRESS"],
        iteration: int = 0,
        actor: str = "supervisor",
        human_decision: str | None = None,
    ) -> TaskWorkflowResult:
        """Execute the complete workflow pipeline.

        Flow:
        1. Validate spec (DOR check)
        2. Run QA review
        3. Route decision through Supervisor
        4. Handle escalation if NEEDS_INFO
        5. Return unified result
        """
        self.phases = []

        # Phase 1: Spec validation
        spec_valid, spec_errors = self.validate_spec(spec)
        if not spec_valid:
            self.phases.append(
                WorkflowPhaseResult(
                    phase="spec",
                    status="error",
                    state=current_state,
                    message="Spec DOR validation failed",
                    metadata={"errors": spec_errors},
                )
            )
            return TaskWorkflowResult(
                task_id=task_id,
                current_state=current_state,
                next_state=TASK_STATES["PENDING"],
                next_action="spec_repair",
                recipient="product_owner",
                qa_status="SPEC_ERROR",
                score=0.0,
                summary="Spec validation failed (DOR)",
                reason="\n".join(spec_errors[:5]),
                route="spec_repair_needed",
                phase_results=self.phases,
                metadata={"spec_errors": spec_errors},
            )

        self.phases.append(
            WorkflowPhaseResult(
                phase="spec",
                status="success",
                state=current_state,
                message="Spec validation passed",
            )
        )

        # Phase 2: QA review
        qa_result, qa_status = self.run_qa(
            task_id, spec, patch_files, test_results, iteration
        )
        self.phases.append(
            WorkflowPhaseResult(
                phase="qa",
                status="success",
                state=current_state,
                message=f"QA review completed: {qa_status}",
                metadata={"score": qa_result.get("score", 0.0)},
            )
        )

        # Phase 3: Supervisor routing
        routed = self.route_qa_decision(task_id, qa_result, iteration, actor)
        next_state = routed["state"]
        next_action = routed["next_action"]
        recipient = routed["recipient"]

        self.phases.append(
            WorkflowPhaseResult(
                phase="supervisor",
                status="success",
                state=next_state,
                message=f"Supervisor routed decision to {recipient}",
                metadata={"route": routed.get("route", "")},
            )
        )

        # Determine route
        if next_state == TASK_STATES["ACCEPTED"]:
            route = "accepted"
        elif next_state == TASK_STATES["REJECTED"]:
            route = "revision_required"
        elif next_state == TASK_STATES["NEEDS_INFO"]:
            route = "needs_info"
        elif next_state == TASK_STATES["HUMAN_ESCALATION"]:
            route = "human_escalation"
        else:
            route = "unknown"

        # Phase 4: Handle escalation if needed
        if next_state == TASK_STATES["HUMAN_ESCALATION"]:
            if human_decision:
                resolved = self.resolve_escalation(
                    task_id, human_decision, actor=actor
                )
                next_state = resolved["state"]
                next_action = resolved["action"]
                route = f"human_escalation_resolved_{human_decision.lower()}"
                self.phases.append(
                    WorkflowPhaseResult(
                        phase="human_escalation",
                        status="success",
                        state=next_state,
                        message=f"Human decision resolved: {human_decision}",
                    )
                )
            else:
                self.phases.append(
                    WorkflowPhaseResult(
                        phase="human_escalation",
                        status="escalated",
                        state=next_state,
                        message="Task escalated to human, awaiting decision",
                    )
                )

        # Phase 5: Completed
        self.phases.append(
            WorkflowPhaseResult(
                phase="completed",
                status="success",
                state=next_state,
                message=f"Workflow completed with state: {next_state}",
            )
        )

        return TaskWorkflowResult(
            task_id=task_id,
            current_state=current_state,
            next_state=next_state,
            next_action=next_action,
            recipient=recipient,
            qa_status=qa_status,
            score=float(qa_result.get("score", 0.0) or 0.0),
            summary=str(qa_result.get("summary", "Workflow completed.")),
            reason=str(qa_result.get("reason", "No reason provided.")),
            route=route,
            phase_results=self.phases,
            qa_result=qa_result,
            supervisor_route=routed,
            metadata={
                "iteration": iteration,
                "actor": actor,
                "phases_executed": len(self.phases),
            },
        )


DEFAULT_ORCHESTRATOR = WorkflowOrchestrator()

__all__ = ["WorkflowOrchestrator", "TaskWorkflowResult", "WorkflowPhaseResult", "DEFAULT_ORCHESTRATOR"]


if __name__ == "__main__":
    orch = WorkflowOrchestrator()
    result = orch.run(
        "TASK-FINAL-001",
        {
            "required_files": ["factory/qa.py"],
            "acceptance_criteria": ["patch file", "tests pass"],
            "stories": [{"id": "S-1", "title": "test"}],
            "scope_in": ["QA module"],
            "scope_out": ["deployment"],
            "assumptions": [{"text": "python 3.10+", "needs_confirmation": True}],
        },
        {"factory/qa.py": "ok"},
        {"unit": True},
        current_state=TASK_STATES["IN_PROGRESS"],
        iteration=1,
    )
    print(f"Task ID: {result.task_id}")
    print(f"State: {result.current_state} → {result.next_state}")
    print(f"Action: {result.next_action}")
    print(f"Recipient: {result.recipient}")
    print(f"Route: {result.route}")
    print(f"Score: {result.score}")
    print(f"Summary: {result.summary}")
    print(f"Phases: {len(result.phase_results)}")
    for phase in result.phase_results:
        print(f"  - {phase.phase}: {phase.status}")

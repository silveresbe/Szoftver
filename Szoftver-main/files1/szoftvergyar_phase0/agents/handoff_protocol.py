"""Handoff protocol and multi-agent orchestration contract.

This defines the exact flow between agents, token budgeting, quality gates,
rollback mechanisms, and cost-benefit analysis for the Szoftvergyár runtime.

Key principles:
- Every handoff is explicit and logged
- Token budgets are enforced per task
- Quality gates are mandatory
- Rollback is automatic on regression
- Learning/improvement is allowed within budget
- Cost-benefit analysis determines success
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional, Literal
from enum import Enum
import uuid


class HandoffStatus(Enum):
    """Handoff lifecycle states."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    REJECTED = "rejected"
    ROLLED_BACK = "rolled_back"
    FAILED = "failed"


class QualityLevel(Enum):
    """Code quality assessment levels."""

    EXCELLENT = 0.9
    GOOD = 0.7
    ACCEPTABLE = 0.5
    POOR = 0.3
    REJECTED = 0.0


@dataclass
class HandoffPayload:
    """The actual data passed between agents."""

    from_agent: str
    to_agent: str
    task_id: str
    action: str  # "code_generation", "review", "test", "deploy", etc.
    input_data: dict[str, Any]
    context: dict[str, Any] = field(default_factory=dict)
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TokenBudgetAllocation:
    """Token budget for a specific handoff/task."""

    task_id: str
    total_tokens: int
    used_tokens: int = 0
    remaining_tokens: int = field(init=False)
    per_iteration_limit: int = 1000  # Max tokens per single attempt
    max_iterations: int = 3  # Max attempts before human escalation
    reset_on_approval: bool = False

    def __post_init__(self):
        self.remaining_tokens = self.total_tokens - self.used_tokens

    def consume(self, tokens: int) -> bool:
        if tokens > self.remaining_tokens:
            return False
        self.used_tokens += tokens
        self.remaining_tokens -= tokens
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class QualityGate:
    """Quality assessment for generated code."""

    gate_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    code_quality_score: float = 0.0  # 0.0-1.0
    linter_passed: bool = False
    type_checker_passed: bool = False
    security_scan_passed: bool = False
    unit_tests_passed: bool = False
    test_coverage: float = 0.0  # percentage
    issues_found: list[str] = field(default_factory=list)
    overall_status: Literal["approved", "rejected", "needs_revision"] = "needs_revision"
    quality_threshold: float = 0.7  # Minimum acceptable score
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def evaluate(self) -> bool:
        """Determine if code passes quality gate."""
        if self.code_quality_score < self.quality_threshold:
            self.overall_status = "rejected"
            return False

        if not (self.linter_passed and self.unit_tests_passed):
            self.overall_status = "needs_revision"
            return False

        self.overall_status = "approved"
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class CostBenefitAnalysis:
    """Evaluate whether a development iteration was worthwhile."""

    analysis_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    task_id: str = ""
    iteration_number: int = 0
    tokens_spent: int = 0
    baseline_quality: float = 0.0  # Quality before change
    new_quality: float = 0.0  # Quality after change
    improvement_delta: float = field(init=False)
    cost_benefit_ratio: float = field(init=False)
    is_learning_phase: bool = False
    recommendation: Literal["accept", "rollback", "retry"] = "accept"
    reasoning: str = ""
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self):
        self.improvement_delta = self.new_quality - self.baseline_quality

        # Cost-benefit: quality gained per token spent
        # Higher ratio = better (more value per token)
        if self.tokens_spent > 0:
            self.cost_benefit_ratio = self.improvement_delta / self.tokens_spent
        else:
            self.cost_benefit_ratio = 0.0

        # Decision logic
        if self.improvement_delta > 0.15:
            # Significant improvement
            self.recommendation = "accept"
            self.reasoning = f"improvement_delta={self.improvement_delta:.2f}, cost_ratio={self.cost_benefit_ratio:.4f}"
        elif self.improvement_delta > 0.0 and self.is_learning_phase:
            # Positive trend in learning phase, worth iterating
            self.recommendation = "retry"
            self.reasoning = f"learning_phase, minor improvement={self.improvement_delta:.2f}"
        elif self.improvement_delta > 0.0:
            # Marginal improvement, check token efficiency
            if self.cost_benefit_ratio > 0.001:
                self.recommendation = "accept"
                self.reasoning = f"marginal_improvement={self.improvement_delta:.2f}, acceptable ratio={self.cost_benefit_ratio:.4f}"
            else:
                self.recommendation = "retry"
                self.reasoning = f"poor_token_efficiency, ratio={self.cost_benefit_ratio:.4f}"
        else:
            # Regression or no change
            self.recommendation = "rollback"
            self.reasoning = f"regression_or_no_improvement, delta={self.improvement_delta:.2f}"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class Handoff:
    """Complete handoff record with all metadata."""

    handoff_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    payload: HandoffPayload = field(default_factory=lambda: HandoffPayload("", "", "", "", {}))
    token_budget: TokenBudgetAllocation = field(default_factory=lambda: TokenBudgetAllocation("", 5000))
    quality_gate: Optional[QualityGate] = None
    cost_benefit: Optional[CostBenefitAnalysis] = None
    status: HandoffStatus = HandoffStatus.PROPOSED
    git_backup_commit: str = ""  # Git commit hash before changes
    approval_by_watcher: bool = False
    approval_timestamp: str = ""
    iteration_count: int = 0
    rejection_reason: str = ""
    timestamp_created: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    timestamp_completed: str = ""

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["status"] = self.status.value
        if self.quality_gate:
            data["quality_gate"] = self.quality_gate.to_dict()
        if self.cost_benefit:
            data["cost_benefit"] = self.cost_benefit.to_dict()
        if self.payload:
            data["payload"] = self.payload.to_dict()
        if self.token_budget:
            data["token_budget"] = self.token_budget.to_dict()
        return data


class HandoffOrchestrator:
    """
    Central orchestrator for multi-agent handoffs.

    Manages the complete flow:
    1. Handoff proposal
    2. Token budget allocation
    3. Git backup creation
    4. Agent execution
    5. Quality gate evaluation
    6. Cost-benefit analysis
    7. Rollback or commit decision
    8. Audit logging
    """

    def __init__(self):
        self.handoffs: dict[str, Handoff] = {}
        self.audit_log_file = "handoff_audit.jsonl"

    def initiate_handoff(
        self,
        from_agent: str,
        to_agent: str,
        task_id: str,
        action: str,
        input_data: dict[str, Any],
        token_budget: int = 5000,
        quality_threshold: float = 0.7,
    ) -> Handoff:
        """
        Initiate a new handoff between agents.

        Returns a Handoff record that tracks the entire interaction.
        """
        payload = HandoffPayload(from_agent=from_agent, to_agent=to_agent, task_id=task_id, action=action, input_data=input_data)

        budget = TokenBudgetAllocation(task_id=task_id, total_tokens=token_budget)

        quality_gate = QualityGate(task_id=task_id, quality_threshold=quality_threshold)

        handoff = Handoff(payload=payload, token_budget=budget, quality_gate=quality_gate)

        self.handoffs[handoff.handoff_id] = handoff

        self._audit_log("handoff_initiated", handoff.handoff_id, {"from": from_agent, "to": to_agent, "task": task_id})

        return handoff

    def approve_handoff(self, handoff_id: str, by_agent: str = "watcher_agent") -> Handoff:
        """Watcher approves the handoff."""
        handoff = self.handoffs.get(handoff_id)
        if handoff is None:
            raise KeyError(f"handoff not found: {handoff_id}")

        handoff.status = HandoffStatus.APPROVED
        handoff.approval_by_watcher = True
        handoff.approval_timestamp = datetime.now(timezone.utc).isoformat()

        self._audit_log("handoff_approved", handoff_id, {"by": by_agent})

        return handoff

    def reject_handoff(self, handoff_id: str, reason: str, by_agent: str = "watcher_agent") -> Handoff:
        """Watcher rejects the handoff."""
        handoff = self.handoffs.get(handoff_id)
        if handoff is None:
            raise KeyError(f"handoff not found: {handoff_id}")

        handoff.status = HandoffStatus.REJECTED
        handoff.rejection_reason = reason

        self._audit_log("handoff_rejected", handoff_id, {"by": by_agent, "reason": reason})

        return handoff

    def begin_execution(self, handoff_id: str) -> Handoff:
        """Mark handoff as in-progress."""
        handoff = self.handoffs.get(handoff_id)
        if handoff is None:
            raise KeyError(f"handoff not found: {handoff_id}")

        if handoff.status != HandoffStatus.APPROVED:
            raise ValueError(f"cannot execute non-approved handoff: {handoff.status.value}")

        handoff.status = HandoffStatus.IN_PROGRESS
        self._audit_log("handoff_execution_started", handoff_id, {})

        return handoff

    def record_quality_assessment(self, handoff_id: str, quality_gate: QualityGate) -> Handoff:
        """Record the quality gate assessment."""
        handoff = self.handoffs.get(handoff_id)
        if handoff is None:
            raise KeyError(f"handoff not found: {handoff_id}")

        handoff.quality_gate = quality_gate
        handoff.iteration_count += 1

        self._audit_log("quality_gate_assessed", handoff_id, quality_gate.to_dict())

        return handoff

    def record_cost_benefit(self, handoff_id: str, analysis: CostBenefitAnalysis) -> dict[str, Any]:
        """
        Record cost-benefit analysis and decide next action.

        Returns decision: {"action": "accept" | "rollback" | "retry", ...}
        """
        handoff = self.handoffs.get(handoff_id)
        if handoff is None:
            raise KeyError(f"handoff not found: {handoff_id}")

        handoff.cost_benefit = analysis

        decision = analysis.recommendation

        if decision == "rollback":
            handoff.status = HandoffStatus.ROLLED_BACK
            self._audit_log("handoff_rolled_back", handoff_id, analysis.to_dict())
            return {
                "action": "rollback",
                "git_commit": handoff.git_backup_commit,
                "reason": analysis.reasoning,
                "tokens_wasted": analysis.tokens_spent,
            }

        elif decision == "retry":
            if handoff.iteration_count >= handoff.token_budget.max_iterations:
                handoff.status = HandoffStatus.FAILED
                self._audit_log("handoff_max_iterations_reached", handoff_id, {"iterations": handoff.iteration_count})
                return {"action": "escalate_to_human", "reason": "max_iterations_exceeded", "iterations": handoff.iteration_count}

            # Reset quality gate for next iteration
            handoff.quality_gate = QualityGate(task_id=handoff.payload.task_id, quality_threshold=handoff.quality_gate.quality_threshold)

            self._audit_log("handoff_retry", handoff_id, {"iteration": handoff.iteration_count})
            return {"action": "retry", "iteration": handoff.iteration_count, "tokens_remaining": handoff.token_budget.remaining_tokens}

        else:  # accept
            handoff.status = HandoffStatus.COMPLETED
            handoff.timestamp_completed = datetime.now(timezone.utc).isoformat()
            self._audit_log("handoff_completed", handoff_id, analysis.to_dict())
            return {
                "action": "accept",
                "cost_benefit_ratio": analysis.cost_benefit_ratio,
                "improvement": analysis.improvement_delta,
                "tokens_used": analysis.tokens_spent,
            }

    def get_handoff(self, handoff_id: str) -> Optional[Handoff]:
        """Retrieve a handoff by ID."""
        return self.handoffs.get(handoff_id)

    def list_handoffs(self, task_id: str | None = None, status: HandoffStatus | None = None) -> list[Handoff]:
        """List handoffs, optionally filtered by task or status."""
        result = list(self.handoffs.values())

        if task_id:
            result = [h for h in result if h.payload.task_id == task_id]

        if status:
            result = [h for h in result if h.status == status]

        return result

    def _audit_log(self, event: str, handoff_id: str, details: dict[str, Any]) -> None:
        """Write handoff event to audit log."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "handoff_id": handoff_id,
            "details": details,
        }
        import json

        with open(self.audit_log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")


__all__ = [
    "HandoffStatus",
    "QualityLevel",
    "HandoffPayload",
    "TokenBudgetAllocation",
    "QualityGate",
    "CostBenefitAnalysis",
    "Handoff",
    "HandoffOrchestrator",
]

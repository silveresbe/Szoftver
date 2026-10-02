"""Watcher Agent - Security, token budgeting, and operational control layer.

This is the most critical agent. It:
- Guards all other agents from unauthorized mutations
- Enforces token budgets strictly
- Logs every action
- Prevents bypasses of core security
- Cannot be modified by the Coder Agent
- Is the single point of truth for policy enforcement
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional
from enum import Enum
import json

from base_agent import BaseAgent, TokenLimiter, AgentRegistry


class PolicyLevel(Enum):
    """Security policy levels."""

    SAFE = "safe"  # Maximum restrictions, minimal token spend
    STANDARD = "standard"  # Normal restrictions
    STRICT = "strict"  # Strictest rules, no overrides


@dataclass
class ApprovalRequest:
    """Request from an agent seeking approval for a mutation."""

    request_id: str
    agent_id: str
    task_id: str
    action: str
    target_scope: str
    files_affected: list[str]
    estimated_tokens: int
    description: str
    reason: str
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    approved: bool = False
    approval_reason: str = ""
    approval_timestamp: str = ""

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class SecurityPolicy:
    """System-wide security policy that cannot be modified at runtime."""

    policy_level: PolicyLevel = PolicyLevel.SAFE
    coder_allowed_mutations: bool = True  # Can Coder make ANY changes?
    coder_restricted_paths: list[str] = field(
        default_factory=lambda: [
            "agents/watcher_agent.py",
            "agents/base_agent.py",
            "token_limiter.py",
            "agent_registry.py",
            "security_policy.json",
        ]
    )
    approval_required_for: list[str] = field(
        default_factory=lambda: ["edit_file", "delete_file", "create_file", "run_command"]
    )
    token_overrun_action: str = "deny"  # deny, warn, throttle
    max_concurrent_agents: int = 3
    enforce_handoff_protocol: bool = True
    audit_log_required: bool = True

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["policy_level"] = self.policy_level.value
        return data

    @staticmethod
    def load_from_file(path: str = "security_policy.json") -> SecurityPolicy:
        """Load policy from file (immutable config)."""
        try:
            with open(path, "r") as f:
                data = json.load(f)
                return SecurityPolicy(
                    policy_level=PolicyLevel(data.get("policy_level", "safe")),
                    coder_allowed_mutations=data.get("coder_allowed_mutations", True),
                    coder_restricted_paths=data.get("coder_restricted_paths", []),
                    approval_required_for=data.get("approval_required_for", []),
                    token_overrun_action=data.get("token_overrun_action", "deny"),
                )
        except FileNotFoundError:
            return SecurityPolicy()


class WatcherAgent(BaseAgent):
    """
    The Watcher Agent is the single point of truth for:
    - Token budget enforcement
    - Security policy enforcement
    - Action approval gating
    - Audit logging
    - Preventing Coder mutations to core system files

    This agent has:
    - Very high token budget (effectively unlimited or budget-independent)
    - No token cost for its own operations
    - Authority to deny any action
    - Full audit log access
    - Cannot be modified by Coder Agent
    """

    def __init__(self, token_limiter: TokenLimiter, registry: AgentRegistry, policy: SecurityPolicy | None = None):
        # Watcher has extremely high token budget - its operations are policy checks, not LLM calls
        super().__init__("watcher_agent", token_limiter, token_limit=999999999)
        self.registry = registry
        self.policy = policy or SecurityPolicy.load_from_file()
        self.approval_requests: dict[str, ApprovalRequest] = {}
        self.audit_log_file = "watcher_audit.jsonl"
        self.denied_actions: list[dict[str, Any]] = []
        self._log("watcher_initialized", policy_level=self.policy.policy_level.value)

    def process(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """
        Main process method. Input should be:
        {
            "action": "check_approval" or "request_approval" or "get_status" or "enforce_policy",
            "agent_id": "...",
            "task_id": "...",
            ...other fields depending on action...
        }
        """
        action = input_data.get("action", "unknown")

        if action == "request_approval":
            return self._handle_approval_request(input_data)
        elif action == "get_status":
            return self._get_status()
        elif action == "check_policy":
            return self._check_policy(input_data)
        elif action == "verify_handoff":
            return self._verify_handoff(input_data)
        elif action == "audit_query":
            return self._query_audit(input_data)
        else:
            return {"error": f"unknown watcher action: {action}"}

    def _handle_approval_request(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """
        Handle approval request from another agent.

        Input:
        {
            "action": "request_approval",
            "agent_id": "coder_agent",
            "task_id": "T-42",
            "mutation_type": "edit_file",
            "target_scope": "auth_module",
            "files_affected": ["auth.py", "models.py"],
            "estimated_tokens": 5000,
            "description": "Add 2FA support",
            "reason": "User requirement implementation"
        }
        """
        agent_id = input_data.get("agent_id")
        task_id = input_data.get("task_id")
        mutation_type = input_data.get("mutation_type")
        target_scope = input_data.get("target_scope")
        files_affected = input_data.get("files_affected", [])
        estimated_tokens = input_data.get("estimated_tokens", 0)
        description = input_data.get("description", "")
        reason = input_data.get("reason", "")

        # Create approval request
        request_id = f"APR-{len(self.approval_requests)+1}"
        approval_req = ApprovalRequest(
            request_id=request_id,
            agent_id=agent_id,
            task_id=task_id,
            action=mutation_type,
            target_scope=target_scope,
            files_affected=files_affected,
            estimated_tokens=estimated_tokens,
            description=description,
            reason=reason,
        )

        # Check policy compliance
        policy_checks = self._run_policy_checks(agent_id, mutation_type, files_affected, estimated_tokens)

        if not policy_checks["allowed"]:
            approval_req.approved = False
            approval_req.approval_reason = policy_checks["reason"]
            self.approval_requests[request_id] = approval_req
            self._audit_log("approval_denied", request_id, policy_checks)
            self.denied_actions.append(
                {
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                    "request_id": request_id,
                    "agent_id": agent_id,
                    "reason": policy_checks["reason"],
                }
            )
            return {"approved": False, "reason": policy_checks["reason"], "request_id": request_id}

        # If all checks pass, approve
        approval_req.approved = True
        approval_req.approval_reason = "policy_check_passed"
        approval_req.approval_timestamp = datetime.now(timezone.utc).isoformat()
        self.approval_requests[request_id] = approval_req
        self._audit_log("approval_granted", request_id, policy_checks)

        return {
            "approved": True,
            "request_id": request_id,
            "approval_timestamp": approval_req.approval_timestamp,
            "token_budget_remaining": self.token_limiter.get_budget(agent_id).remaining_tokens if agent_id in [a.agent_id for a in self.registry.list_agents()] else None,
        }

    def _run_policy_checks(self, agent_id: str, mutation_type: str, files_affected: list[str], estimated_tokens: int) -> dict[str, Any]:
        """
        Run comprehensive policy checks. This is the core security logic.

        Checks:
        1. Is the agent allowed to make mutations?
        2. Are the target files in the restricted list?
        3. Does the agent have enough tokens?
        4. Is the mutation type allowed?
        5. Policy level restrictions?
        """
        checks = {
            "allowed": True,
            "reason": "all_checks_passed",
            "checks_detail": {},
        }

        # Check 1: Is Coder restricted?
        if agent_id == "coder_agent":
            if not self.policy.coder_allowed_mutations:
                checks["allowed"] = False
                checks["reason"] = "coder_mutations_disabled_by_policy"
                checks["checks_detail"]["coder_allowed"] = False
                return checks

            # Check 2: Restricted paths
            for file_path in files_affected:
                if any(restricted in file_path for restricted in self.policy.coder_restricted_paths):
                    checks["allowed"] = False
                    checks["reason"] = f"cannot_modify_restricted_path: {file_path}"
                    checks["checks_detail"]["restricted_path_violation"] = file_path
                    return checks

        # Check 3: Token budget
        agent = self.registry.get(agent_id)
        if agent is not None:
            budget = self.token_limiter.get_budget(agent_id)
            if budget and budget.remaining_tokens < estimated_tokens:
                if self.policy.token_overrun_action == "deny":
                    checks["allowed"] = False
                    checks["reason"] = f"insufficient_token_budget: need {estimated_tokens}, have {budget.remaining_tokens}"
                    checks["checks_detail"]["token_budget"] = False
                    return checks
                elif self.policy.token_overrun_action == "warn":
                    checks["checks_detail"]["token_budget_warning"] = f"low tokens: {budget.remaining_tokens} remaining"

        # Check 4: Mutation type allowed?
        if mutation_type not in self.policy.approval_required_for:
            checks["checks_detail"]["mutation_type_preapproved"] = True
        else:
            checks["checks_detail"]["mutation_type_needs_approval"] = True

        # Check 5: Policy level
        if self.policy.policy_level == PolicyLevel.STRICT:
            # Extra restrictions
            checks["checks_detail"]["strict_mode_active"] = True

        if self.policy.policy_level == PolicyLevel.SAFE:
            # Maximum cost reduction
            checks["checks_detail"]["safe_mode_active"] = True

        return checks

    def _check_policy(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Check if a proposed action complies with policy."""
        return {
            "policy_level": self.policy.policy_level.value,
            "coder_allowed_mutations": self.policy.coder_allowed_mutations,
            "restricted_paths": self.policy.coder_restricted_paths,
            "approval_required_for": self.policy.approval_required_for,
            "token_overrun_action": self.policy.token_overrun_action,
        }

    def _verify_handoff(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Verify that a handoff conforms to protocol."""
        handoff = input_data.get("handoff", {})
        from_agent = handoff.get("from_agent")
        to_agent = handoff.get("to_agent")
        task_id = handoff.get("task_id")

        if not all([from_agent, to_agent, task_id]):
            return {"valid": False, "reason": "missing_required_fields"}

        # Check that both agents exist
        if self.registry.get(from_agent) is None or self.registry.get(to_agent) is None:
            return {"valid": False, "reason": "agent_not_found"}

        self._audit_log("handoff_verified", task_id, {"from": from_agent, "to": to_agent})
        return {"valid": True, "handoff_id": f"HO-{task_id}-{datetime.now(timezone.utc).timestamp()}"}

    def _query_audit(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Query audit log (read-only, no cost)."""
        agent_id = input_data.get("agent_id")
        limit = input_data.get("limit", 100)

        logs = []
        try:
            with open(self.audit_log_file, "r") as f:
                for i, line in enumerate(f):
                    if i >= limit:
                        break
                    if agent_id is None or agent_id in line:
                        logs.append(json.loads(line))
        except FileNotFoundError:
            pass

        return {"audit_logs": logs, "total": len(logs)}

    def _get_status(self) -> dict[str, Any]:
        """Get current system status."""
        budgets = [b.to_dict() for b in self.token_limiter.list_budgets()]
        return {
            "status": "operational",
            "policy_level": self.policy.policy_level.value,
            "agents_registered": len(self.registry.list_agents()),
            "pending_approvals": len([r for r in self.approval_requests.values() if not r.approved]),
            "denied_actions_total": len(self.denied_actions),
            "budgets": budgets,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }

    def _audit_log(self, event: str, task_or_id: str, details: dict[str, Any]) -> None:
        """Write to immutable audit log."""
        entry = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event": event,
            "ref": task_or_id,
            "details": details,
        }
        with open(self.audit_log_file, "a") as f:
            f.write(json.dumps(entry) + "\n")
        self._log(event, **details)


__all__ = ["WatcherAgent", "SecurityPolicy", "ApprovalRequest", "PolicyLevel"]

"""Agent abstraction layer and token budgeting system for the Szoftvergyár orchestrator.

This module provides:
- Unified agent interface
- Token limiter and budget management
- Agent registry
- Token usage logging and audit
"""
from __future__ import annotations

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Any, Optional, Callable
from abc import ABC, abstractmethod
import json
import os


@dataclass
class TokenBudget:
    """Token budget for a single agent."""

    agent_id: str
    total_limit: int
    used_tokens: int = 0
    remaining_tokens: int = field(init=False)
    reset_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def __post_init__(self):
        self.remaining_tokens = self.total_limit - self.used_tokens

    def consume(self, tokens: int) -> bool:
        if tokens > self.remaining_tokens:
            return False
        self.used_tokens += tokens
        self.remaining_tokens -= tokens
        return True

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class TokenLog:
    """Single token usage log entry."""

    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    agent_id: str = ""
    operation: str = ""
    tokens_used: int = 0
    remaining: int = 0
    details: dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class TokenLimiter:
    """Central token budgeting and logging system."""

    def __init__(self, log_file: str = "agent_token_logs.jsonl"):
        self.log_file = log_file
        self.budgets: dict[str, TokenBudget] = {}
        self.logs: list[TokenLog] = []

    def register_agent(self, agent_id: str, token_limit: int) -> TokenBudget:
        budget = TokenBudget(agent_id=agent_id, total_limit=token_limit)
        self.budgets[agent_id] = budget
        return budget

    def consume_tokens(self, agent_id: str, tokens: int, operation: str = "", **details: Any) -> bool:
        budget = self.budgets.get(agent_id)
        if budget is None:
            raise KeyError(f"agent not registered: {agent_id}")

        success = budget.consume(tokens)
        log_entry = TokenLog(agent_id=agent_id, operation=operation, tokens_used=tokens, remaining=budget.remaining_tokens, details=details)
        self.logs.append(log_entry)
        self._write_log(log_entry)
        return success

    def get_budget(self, agent_id: str) -> Optional[TokenBudget]:
        return self.budgets.get(agent_id)

    def list_budgets(self) -> list[TokenBudget]:
        return list(self.budgets.values())

    def _write_log(self, entry: TokenLog) -> None:
        with open(self.log_file, "a") as f:
            f.write(json.dumps(entry.to_dict()) + "\n")

    def read_logs(self, agent_id: str | None = None) -> list[dict[str, Any]]:
        logs = []
        if os.path.exists(self.log_file):
            with open(self.log_file, "r") as f:
                for line in f:
                    log_dict = json.loads(line)
                    if agent_id is None or log_dict["agent_id"] == agent_id:
                        logs.append(log_dict)
        return logs


class BaseAgent(ABC):
    """Base agent interface for all agents in the system."""

    def __init__(self, agent_id: str, token_limiter: TokenLimiter, token_limit: int = 10000):
        self.agent_id = agent_id
        self.token_limiter = token_limiter
        self.token_limit = token_limit
        self.token_limiter.register_agent(agent_id, token_limit)
        self.log: list[dict[str, Any]] = []

    @abstractmethod
    def process(self, input_data: dict[str, Any]) -> dict[str, Any]:
        """Process input and return output."""
        pass

    def _check_token_budget(self, estimated_tokens: int) -> bool:
        budget = self.token_limiter.get_budget(self.agent_id)
        if budget is None:
            return False
        return budget.remaining_tokens >= estimated_tokens

    def _consume_tokens(self, tokens: int, operation: str = "", **details: Any) -> bool:
        return self.token_limiter.consume_tokens(self.agent_id, tokens, operation=operation, **details)

    def _log(self, event: str, **details: Any) -> None:
        entry = {"timestamp": datetime.now(timezone.utc).isoformat(), "event": event, **details}
        self.log.append(entry)

    def get_log(self) -> list[dict[str, Any]]:
        return self.log


class AgentRegistry:
    """Central registry for all agents in the system."""

    def __init__(self):
        self.agents: dict[str, BaseAgent] = {}

    def register(self, agent: BaseAgent) -> None:
        self.agents[agent.agent_id] = agent

    def get(self, agent_id: str) -> Optional[BaseAgent]:
        return self.agents.get(agent_id)

    def list_agents(self) -> list[BaseAgent]:
        return list(self.agents.values())

    def get_agent_info(self, agent_id: str) -> dict[str, Any]:
        agent = self.get(agent_id)
        if agent is None:
            return {}
        budget = agent.token_limiter.get_budget(agent_id)
        return {
            "agent_id": agent_id,
            "token_limit": agent.token_limit,
            "token_budget": budget.to_dict() if budget else None,
            "log_entries": len(agent.get_log()),
        }

    def list_agent_info(self) -> list[dict[str, Any]]:
        return [self.get_agent_info(agent.agent_id) for agent in self.list_agents()]


__all__ = ["TokenBudget", "TokenLog", "TokenLimiter", "BaseAgent", "AgentRegistry"]

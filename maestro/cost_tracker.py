"""Cost tracking and budget enforcement for Maestro.

Tracks cumulative cost across all specialist calls. Warns at 80% budget,
halts at 100%. Persists cost state to JSON for crash recovery.

Cost estimation strategies:
- Claude API: exact token usage from API response
- Codex/Grok/Antigravity CLI: estimated via (duration × rate_per_sec)
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class BudgetStatus(str, Enum):
    """Current budget state."""
    OK = "ok"
    WARNING = "warning"     # >= 80% spent
    EXCEEDED = "exceeded"   # >= 100% spent


class BudgetExceededError(Exception):
    """Raised when budget is exceeded and user hasn't confirmed continuation."""
    pass


# Estimated cost per second for CLI-based specialists (no token-level billing).
# These are rough estimates — acceptable per plan spec (±20% accuracy target).
SPECIALIST_RATE_PER_SEC: dict[str, float] = {
    "claude_code": 0.005,    # ~$18/hr based on Opus usage
    "codex": 0.003,          # ~$10.8/hr
    "stitch": 0.004,         # ~$14.4/hr (image generation)
    "grok_build": 0.003,     # ~$10.8/hr
    "antigravity": 0.002,    # ~$7.2/hr
    "grader_sonnet": 0.001,  # ~$3.6/hr (cheap model)
    "grader_opus": 0.005,    # ~$18/hr (vision grading)
}


@dataclass
class CostEntry:
    """A single cost record for one specialist invocation."""
    timestamp: float
    specialist: str
    task_id: str
    duration_sec: float
    estimated_cost_usd: float
    token_usage: Optional[dict] = None  # {"input": N, "output": N} if available
    method: str = "time_based"          # "time_based" | "token_based"

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "specialist": self.specialist,
            "task_id": self.task_id,
            "duration_sec": round(self.duration_sec, 2),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "token_usage": self.token_usage,
            "method": self.method,
        }


@dataclass
class CostTracker:
    """Tracks and enforces budget across all specialist calls.

    Usage:
        tracker = CostTracker.from_env()
        tracker.record("claude_code", "t1", 45.2)
        status = tracker.check_budget()
    """
    max_budget_usd: float
    entries: list[CostEntry] = field(default_factory=list)
    total_spent_usd: float = 0.0
    _persist_path: Optional[Path] = None

    @classmethod
    def from_env(cls, persist_dir: Optional[Path] = None) -> CostTracker:
        """Create tracker from MAX_USD env var.

        Args:
            persist_dir: Directory to save cost state (default: .maestro/)
        """
        max_usd = float(os.environ.get("MAX_USD", "10.0"))
        tracker = cls(max_budget_usd=max_usd)

        if persist_dir:
            tracker._persist_path = persist_dir / "cost_state.json"
            tracker._load_persisted()

        return tracker

    def record(
        self,
        specialist: str,
        task_id: str,
        duration_sec: float,
        *,
        token_usage: Optional[dict] = None,
        explicit_cost_usd: Optional[float] = None,
    ) -> CostEntry:
        """Record cost for a specialist invocation.

        Args:
            specialist: Name matching SPECIALIST_RATE_PER_SEC key
            task_id: Task identifier for tracking
            duration_sec: How long the call took
            token_usage: If available, exact token counts from API
            explicit_cost_usd: Override cost calculation with exact amount

        Returns:
            The created CostEntry
        """
        if explicit_cost_usd is not None:
            cost = explicit_cost_usd
            method = "explicit"
        elif token_usage:
            cost = self._estimate_from_tokens(token_usage)
            method = "token_based"
        else:
            rate = SPECIALIST_RATE_PER_SEC.get(specialist, 0.003)
            cost = duration_sec * rate
            method = "time_based"

        entry = CostEntry(
            timestamp=time.time(),
            specialist=specialist,
            task_id=task_id,
            duration_sec=duration_sec,
            estimated_cost_usd=cost,
            token_usage=token_usage,
            method=method,
        )

        self.entries.append(entry)
        self.total_spent_usd += cost
        self._persist()

        return entry

    def check_budget(self) -> BudgetStatus:
        """Check current budget status.

        Returns:
            BudgetStatus.OK / WARNING / EXCEEDED
        """
        ratio = self.total_spent_usd / self.max_budget_usd if self.max_budget_usd > 0 else 0
        if ratio >= 1.0:
            return BudgetStatus.EXCEEDED
        elif ratio >= 0.8:
            return BudgetStatus.WARNING
        return BudgetStatus.OK

    def enforce_budget(self) -> None:
        """Raise BudgetExceededError if budget is exceeded.

        Call this BEFORE starting a new specialist task.
        """
        status = self.check_budget()
        if status == BudgetStatus.EXCEEDED:
            raise BudgetExceededError(
                f"Budget exceeded: ${self.total_spent_usd:.2f} / ${self.max_budget_usd:.2f} "
                f"({self.utilization_pct:.1f}%). Waiting for user confirmation to continue."
            )

    @property
    def remaining_usd(self) -> float:
        return max(0.0, self.max_budget_usd - self.total_spent_usd)

    @property
    def utilization_pct(self) -> float:
        if self.max_budget_usd <= 0:
            return 0.0
        return (self.total_spent_usd / self.max_budget_usd) * 100

    def summary(self) -> dict:
        """Cost summary for logging / display."""
        by_specialist: dict[str, float] = {}
        for entry in self.entries:
            by_specialist[entry.specialist] = (
                by_specialist.get(entry.specialist, 0.0) + entry.estimated_cost_usd
            )

        return {
            "total_spent_usd": round(self.total_spent_usd, 4),
            "max_budget_usd": self.max_budget_usd,
            "remaining_usd": round(self.remaining_usd, 4),
            "utilization_pct": round(self.utilization_pct, 1),
            "status": self.check_budget().value,
            "by_specialist": {k: round(v, 4) for k, v in by_specialist.items()},
            "total_calls": len(self.entries),
        }

    def _estimate_from_tokens(self, token_usage: dict) -> float:
        """Estimate cost from token counts (Claude API pricing)."""
        input_tokens = token_usage.get("input", 0)
        output_tokens = token_usage.get("output", 0)
        # Approximate Claude pricing (Sonnet-level)
        input_cost = (input_tokens / 1_000_000) * 3.0    # $3/M input
        output_cost = (output_tokens / 1_000_000) * 15.0  # $15/M output
        return input_cost + output_cost

    def _persist(self) -> None:
        """Save cost state to disk for crash recovery."""
        if not self._persist_path:
            return
        self._persist_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            "max_budget_usd": self.max_budget_usd,
            "total_spent_usd": self.total_spent_usd,
            "entries": [e.to_dict() for e in self.entries],
        }
        # Atomic write: write to temp then rename
        tmp_path = self._persist_path.with_suffix(".tmp")
        with open(tmp_path, "w") as f:
            json.dump(data, f, indent=2)
        tmp_path.replace(self._persist_path)

    def _load_persisted(self) -> None:
        """Load cost state from disk if exists."""
        if not self._persist_path or not self._persist_path.exists():
            return
        try:
            with open(self._persist_path) as f:
                data = json.load(f)
            self.total_spent_usd = data.get("total_spent_usd", 0.0)
            self.max_budget_usd = data.get("max_budget_usd", self.max_budget_usd)
            # Restore entries (lightweight — don't need full CostEntry objects for resume)
            for entry_data in data.get("entries", []):
                self.entries.append(CostEntry(
                    timestamp=entry_data["timestamp"],
                    specialist=entry_data["specialist"],
                    task_id=entry_data["task_id"],
                    duration_sec=entry_data["duration_sec"],
                    estimated_cost_usd=entry_data["estimated_cost_usd"],
                    token_usage=entry_data.get("token_usage"),
                    method=entry_data.get("method", "time_based"),
                ))
        except (json.JSONDecodeError, KeyError, TypeError):
            pass  # Corrupted state — start fresh, log warning

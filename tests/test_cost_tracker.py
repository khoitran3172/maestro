"""Tests for maestro.cost_tracker — budget tracking and enforcement."""

import json
import os
import time
from pathlib import Path

import pytest

from maestro.cost_tracker import (
    BudgetExceededError,
    BudgetStatus,
    CostEntry,
    CostTracker,
)


class TestCostTracker:
    def test_from_env_default(self, monkeypatch):
        """Default budget is $10 when MAX_USD not set."""
        monkeypatch.delenv("MAX_USD", raising=False)
        tracker = CostTracker.from_env()
        assert tracker.max_budget_usd == 10.0

    def test_from_env_custom(self, monkeypatch):
        """MAX_USD env var sets budget."""
        monkeypatch.setenv("MAX_USD", "25.50")
        tracker = CostTracker.from_env()
        assert tracker.max_budget_usd == 25.50

    def test_record_time_based(self):
        """Time-based cost estimation uses specialist rate."""
        tracker = CostTracker(max_budget_usd=100.0)
        entry = tracker.record("claude_code", "t1", duration_sec=60.0)
        assert entry.method == "time_based"
        assert entry.estimated_cost_usd == 60.0 * 0.005  # $0.30
        assert tracker.total_spent_usd == entry.estimated_cost_usd

    def test_record_token_based(self):
        """Token-based cost uses input/output pricing."""
        tracker = CostTracker(max_budget_usd=100.0)
        entry = tracker.record(
            "claude_code", "t1", duration_sec=30.0,
            token_usage={"input": 1000, "output": 500},
        )
        assert entry.method == "token_based"
        assert entry.estimated_cost_usd > 0

    def test_record_explicit_cost(self):
        """Explicit cost overrides estimation."""
        tracker = CostTracker(max_budget_usd=100.0)
        entry = tracker.record(
            "claude_code", "t1", duration_sec=30.0,
            explicit_cost_usd=1.50,
        )
        assert entry.estimated_cost_usd == 1.50
        assert entry.method == "explicit"

    def test_budget_ok(self):
        tracker = CostTracker(max_budget_usd=100.0)
        tracker.record("codex", "t1", 10.0)
        assert tracker.check_budget() == BudgetStatus.OK

    def test_budget_warning_at_80_percent(self):
        tracker = CostTracker(max_budget_usd=1.0)
        # codex rate = $0.003/s → 267s = ~$0.80
        tracker.record("codex", "t1", 267.0)
        assert tracker.check_budget() == BudgetStatus.WARNING

    def test_budget_exceeded_at_100_percent(self):
        tracker = CostTracker(max_budget_usd=1.0)
        tracker.record("codex", "t1", 400.0)  # $1.20 > $1.00
        assert tracker.check_budget() == BudgetStatus.EXCEEDED

    def test_enforce_budget_raises(self):
        tracker = CostTracker(max_budget_usd=0.01)
        tracker.record("claude_code", "t1", 60.0)  # Will exceed $0.01
        with pytest.raises(BudgetExceededError):
            tracker.enforce_budget()

    def test_remaining_usd(self):
        tracker = CostTracker(max_budget_usd=10.0)
        tracker.record("codex", "t1", 100.0, explicit_cost_usd=3.0)
        assert tracker.remaining_usd == 7.0

    def test_utilization_pct(self):
        tracker = CostTracker(max_budget_usd=10.0)
        tracker.record("codex", "t1", 100.0, explicit_cost_usd=5.0)
        assert tracker.utilization_pct == 50.0

    def test_summary_by_specialist(self):
        tracker = CostTracker(max_budget_usd=100.0)
        tracker.record("claude_code", "t1", 10.0, explicit_cost_usd=1.0)
        tracker.record("codex", "t2", 10.0, explicit_cost_usd=2.0)
        tracker.record("claude_code", "t3", 10.0, explicit_cost_usd=0.5)

        summary = tracker.summary()
        assert summary["by_specialist"]["claude_code"] == 1.5
        assert summary["by_specialist"]["codex"] == 2.0
        assert summary["total_calls"] == 3

    def test_cumulative_cost(self):
        """Multiple records accumulate correctly."""
        tracker = CostTracker(max_budget_usd=100.0)
        tracker.record("codex", "t1", 10.0, explicit_cost_usd=1.0)
        tracker.record("codex", "t2", 10.0, explicit_cost_usd=2.0)
        tracker.record("codex", "t3", 10.0, explicit_cost_usd=3.0)
        assert tracker.total_spent_usd == 6.0


class TestCostTrackerPersistence:
    def test_persist_and_restore(self, tmp_path):
        """Cost state survives restart via JSON persistence."""
        # Create and record
        tracker1 = CostTracker(max_budget_usd=50.0, _persist_path=tmp_path / "cost.json")
        tracker1.record("claude_code", "t1", 10.0, explicit_cost_usd=2.50)
        tracker1.record("codex", "t2", 20.0, explicit_cost_usd=1.25)

        # Restore
        tracker2 = CostTracker(max_budget_usd=50.0, _persist_path=tmp_path / "cost.json")
        tracker2._load_persisted()
        assert tracker2.total_spent_usd == 3.75
        assert len(tracker2.entries) == 2

    def test_corrupted_state_starts_fresh(self, tmp_path):
        """Corrupted JSON doesn't crash — starts fresh."""
        cost_file = tmp_path / "cost.json"
        cost_file.write_text("{invalid json")

        tracker = CostTracker(max_budget_usd=50.0, _persist_path=cost_file)
        tracker._load_persisted()
        assert tracker.total_spent_usd == 0.0

"""Tests for the Maestro benchmark evaluation harness."""

from __future__ import annotations

from pathlib import Path
import pytest

from maestro.eval.harness import run_all_benchmarks


@pytest.mark.asyncio
async def test_evaluation_harness(tmp_path: Path) -> None:
    results = await run_all_benchmarks(tmp_path)
    
    # Verify we ran exactly 3 benchmarks
    assert len(results) == 3
    
    # Benchmark 1: Todo App
    todo = next(r for r in results if r["name"] == "Todo App")
    assert todo["status"] == "completed"
    assert len(todo["tasks"]) == 2
    assert any(t["specialist"] == "claude_code" for t in todo["tasks"])
    assert any(t["specialist"] == "codex" for t in todo["tasks"])
    
    # Benchmark 2: Blog Engine (simulated a feedback loop retry on codex)
    blog = next(r for r in results if r["name"] == "Blog Engine")
    assert blog["status"] == "completed"
    assert len(blog["tasks"]) == 3
    codex_task = next(t for t in blog["tasks"] if t["specialist"] == "codex")
    assert codex_task["retries"] == 1  # 1 retry was simulated
    
    # Benchmark 3: Landing Page
    landing = next(r for r in results if r["name"] == "Landing Page")
    assert landing["status"] == "completed"
    assert len(landing["tasks"]) == 2
    
    # Verify overall elapsed time and costs are tracked
    for r in results:
        assert r["duration_sec"] > 0
        assert r["cost_usd"] > 0

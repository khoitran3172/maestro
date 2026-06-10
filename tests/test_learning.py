"""Tests for the Maestro learning layer and MAESTRO_LESSONS.md generation."""

from __future__ import annotations

import json
from pathlib import Path
import pytest

from maestro.db.store import MaestroStore
from maestro.learning import generate_lessons_learned


@pytest.mark.asyncio
async def test_generate_lessons_learned(tmp_path: Path) -> None:
    db_path = tmp_path / "maestro.db"
    store = MaestroStore(db_path)
    await store.initialize()

    # Create run
    run_id = await store.create_run("Lessons Test Project", max_budget_usd=5.0)

    # Specialist 1: Excellent performance
    await store.create_task(run_id, "t_learn_1", "claude_code", phase=1)
    await store.update_task_status(
        "t_learn_1", "done", grade_score=98.0, duration_sec=8.5, estimated_cost=0.025
    )
    await store.record_cost(run_id, "claude_code", 8.5, 0.025, task_id="t_learn_1")

    # Specialist 2: Low success / retried performance
    await store.create_task(run_id, "t_learn_2", "codex", phase=2)
    # Kick back task (kicked_back)
    await store.update_task_status("t_learn_2", "kicked_back")
    await store.record_feedback(
        task_id="t_learn_2",
        run_id=run_id,
        iteration=1,
        grade_score=45.0,
        rubric_failures=["tests_pass", "builds"],
        issues_text="Syntactical lint issues on database connection.",
    )
    # Second attempt succeeds
    await store.update_task_status(
        "t_learn_2", "done", grade_score=90.0, duration_sec=12.0, estimated_cost=0.035
    )
    await store.record_cost(run_id, "codex", 12.0, 0.035, task_id="t_learn_2")

    # Complete the run
    await store.update_run_cost(run_id, 0.060)
    await store.update_run_status(run_id, "completed")
    await store.close()

    # Generate MD lessons file
    output_md = tmp_path / "MAESTRO_LESSONS.md"
    generate_lessons_learned(db_path, run_id, output_path=output_md)

    assert output_md.exists()
    content = output_md.read_text(encoding="utf-8")

    # Verify content has project structure, metrics, and advice recommendations
    assert "Lessons Test Project" in content
    assert run_id in content
    assert "claude_code" in content
    assert "codex" in content
    assert "100.0%" in content  # success rate claude_code
    assert "Syntactical lint issues" in content
    assert "Rubric Fail: tests_pass" in content
    assert "Rubric Fail: builds" in content
    assert "Advice" in content or "Bài học kinh nghiệm" in content

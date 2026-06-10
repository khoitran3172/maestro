"""Tests for maestro.db.store — SQLite data access layer."""

import json
from pathlib import Path

import pytest

from maestro.db.store import MaestroStore


@pytest.fixture
async def store(tmp_path):
    """Create a fresh MaestroStore for each test."""
    db_path = tmp_path / ".maestro" / "test.db"
    async with MaestroStore(db_path) as s:
        yield s


# ─── Runs ───


class TestRunsCRUD:
    @pytest.mark.asyncio
    async def test_create_run(self, store):
        run_id = await store.create_run("my-project", max_budget_usd=10.0)
        assert len(run_id) == 12

    @pytest.mark.asyncio
    async def test_create_run_custom_id(self, store):
        run_id = await store.create_run("proj", run_id="custom-id-123")
        assert run_id == "custom-id-123"

    @pytest.mark.asyncio
    async def test_get_run(self, store):
        run_id = await store.create_run("my-project", max_budget_usd=25.0)
        run = await store.get_run(run_id)
        assert run is not None
        assert run["project_name"] == "my-project"
        assert run["status"] == "running"
        assert run["max_budget_usd"] == 25.0

    @pytest.mark.asyncio
    async def test_get_run_not_found(self, store):
        assert await store.get_run("nonexistent") is None

    @pytest.mark.asyncio
    async def test_update_run_status(self, store):
        run_id = await store.create_run("proj")
        await store.update_run_status(run_id, "completed")
        run = await store.get_run(run_id)
        assert run["status"] == "completed"

    @pytest.mark.asyncio
    async def test_update_run_cost(self, store):
        run_id = await store.create_run("proj")
        await store.update_run_cost(run_id, 4.20)
        run = await store.get_run(run_id)
        assert run["total_spent_usd"] == 4.20

    @pytest.mark.asyncio
    async def test_get_latest_run(self, store):
        import asyncio
        await store.create_run("first", run_id="r1")
        await asyncio.sleep(0.01)
        await store.create_run("second", run_id="r2")
        latest = await store.get_latest_run()
        assert latest["run_id"] == "r2"


# ─── Tasks ───


class TestTasksCRUD:
    @pytest.mark.asyncio
    async def test_create_task(self, store):
        run_id = await store.create_run("proj")
        task_id = await store.create_task(run_id, "t1", "codex", phase=3)
        assert task_id == "t1"

    @pytest.mark.asyncio
    async def test_get_task(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=3, prompt="build it")
        task = await store.get_task("t1")
        assert task["specialist"] == "codex"
        assert task["phase"] == 3
        assert task["status"] == "pending"
        assert task["input_prompt"] == "build it"

    @pytest.mark.asyncio
    async def test_get_tasks_by_run(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "stitch", phase=2)
        await store.create_task(run_id, "t2", "codex", phase=3)
        await store.create_task(run_id, "t3", "claude_code", phase=1)
        tasks = await store.get_tasks_by_run(run_id)
        assert len(tasks) == 3
        # Ordered by phase
        assert tasks[0]["phase"] == 1
        assert tasks[1]["phase"] == 2
        assert tasks[2]["phase"] == 3

    @pytest.mark.asyncio
    async def test_get_tasks_by_status(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        await store.create_task(run_id, "t2", "codex", phase=2)
        await store.update_task_status("t1", "done")
        pending = await store.get_tasks_by_status(run_id, "pending")
        done = await store.get_tasks_by_status(run_id, "done")
        assert len(pending) == 1
        assert len(done) == 1

    @pytest.mark.asyncio
    async def test_update_task_status(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        await store.update_task_status("t1", "running")
        task = await store.get_task("t1")
        assert task["status"] == "running"
        assert task["started_at"] is not None

    @pytest.mark.asyncio
    async def test_update_task_done(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        await store.update_task_status(
            "t1", "done",
            grade_score=92.5,
            grade_feedback="Excellent work",
            duration_sec=45.3,
            estimated_cost=0.15,
            output_artifact="/output/result.py",
        )
        task = await store.get_task("t1")
        assert task["status"] == "done"
        assert task["grade_score"] == 92.5
        assert task["completed_at"] is not None
        assert task["duration_sec"] == 45.3

    @pytest.mark.asyncio
    async def test_kicked_back_increments_retry(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        await store.update_task_status("t1", "kicked_back")
        task = await store.get_task("t1")
        assert task["retry_count"] == 1
        # Kick back again
        await store.update_task_status("t1", "queued")  # kicked_back → queued
        await store.update_task_status("t1", "running")
        await store.update_task_status("t1", "grading")
        await store.update_task_status("t1", "kicked_back")
        task = await store.get_task("t1")
        assert task["retry_count"] == 2

    @pytest.mark.asyncio
    async def test_count_tasks_by_status(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        await store.create_task(run_id, "t2", "codex", phase=2)
        await store.create_task(run_id, "t3", "codex", phase=3)
        await store.update_task_status("t1", "done")
        await store.update_task_status("t2", "done")

        counts = await store.count_tasks_by_status(run_id)
        assert counts.get("done", 0) == 2
        assert counts.get("pending", 0) == 1

    @pytest.mark.asyncio
    async def test_session_id_persisted(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "claude_code", phase=1)
        await store.update_task_status("t1", "done", session_id="sess-xyz")
        task = await store.get_task("t1")
        assert task["session_id"] == "sess-xyz"


# ─── Artifacts ───


class TestArtifactsCRUD:
    @pytest.mark.asyncio
    async def test_create_artifact(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        aid = await store.create_artifact(
            "t1", run_id, "/output/app.py",
            content_hash="abc123",
            file_type="py",
            size_bytes=1024,
        )
        assert len(aid) == 12

    @pytest.mark.asyncio
    async def test_get_artifacts_by_task(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        await store.create_artifact("t1", run_id, "/a.py", "hash1", file_type="py")
        await store.create_artifact("t1", run_id, "/b.py", "hash2", file_type="py")
        artifacts = await store.get_artifacts_by_task("t1")
        assert len(artifacts) == 2

    @pytest.mark.asyncio
    async def test_check_integrity_unchanged(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        aid = await store.create_artifact("t1", run_id, "/a.py", "abc123")
        assert await store.check_artifact_integrity(aid, "abc123") is True

    @pytest.mark.asyncio
    async def test_check_integrity_changed(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        aid = await store.create_artifact("t1", run_id, "/a.py", "abc123")
        assert await store.check_artifact_integrity(aid, "def456") is False


# ─── Feedback History ───


class TestFeedbackHistory:
    @pytest.mark.asyncio
    async def test_record_and_get_feedback(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        await store.record_feedback(
            "t1", run_id, iteration=1,
            grade_score=65.0,
            rubric_failures=["Tests failing", "No error handling"],
            issues_text="Please add try/catch blocks",
        )
        history = await store.get_feedback_history("t1")
        assert len(history) == 1
        assert history[0]["grade_score"] == 65.0
        assert history[0]["rubric_failures"] == ["Tests failing", "No error handling"]

    @pytest.mark.asyncio
    async def test_multiple_feedback_ordered(self, store):
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        await store.record_feedback("t1", run_id, iteration=1, grade_score=50.0)
        await store.record_feedback("t1", run_id, iteration=2, grade_score=72.0)
        await store.record_feedback("t1", run_id, iteration=3, grade_score=90.0)
        history = await store.get_feedback_history("t1")
        assert len(history) == 3
        assert [h["iteration"] for h in history] == [1, 2, 3]
        assert [h["grade_score"] for h in history] == [50.0, 72.0, 90.0]


# ─── Cost Log ───


class TestCostLog:
    @pytest.mark.asyncio
    async def test_record_cost(self, store):
        run_id = await store.create_run("proj")
        cid = await store.record_cost(
            run_id, "codex", duration_sec=30.0, estimated_cost=0.09,
            task_id="t1", method="time_based",
        )
        assert len(cid) == 12

    @pytest.mark.asyncio
    async def test_get_total_cost(self, store):
        run_id = await store.create_run("proj")
        await store.record_cost(run_id, "codex", 10.0, 0.03)
        await store.record_cost(run_id, "claude_code", 20.0, 0.10)
        await store.record_cost(run_id, "stitch", 15.0, 0.06)
        total = await store.get_total_cost(run_id)
        assert abs(total - 0.19) < 0.001

    @pytest.mark.asyncio
    async def test_get_cost_by_specialist(self, store):
        run_id = await store.create_run("proj")
        await store.record_cost(run_id, "codex", 10.0, 0.03)
        await store.record_cost(run_id, "codex", 20.0, 0.06)
        await store.record_cost(run_id, "claude_code", 15.0, 0.10)
        costs = await store.get_cost_by_specialist(run_id)
        assert abs(costs["codex"] - 0.09) < 0.001
        assert abs(costs["claude_code"] - 0.10) < 0.001

    @pytest.mark.asyncio
    async def test_empty_cost(self, store):
        run_id = await store.create_run("proj")
        assert await store.get_total_cost(run_id) == 0.0


# ─── Run Summary ───


class TestRunSummary:
    @pytest.mark.asyncio
    async def test_full_summary(self, store):
        run_id = await store.create_run("proj", max_budget_usd=10.0)
        await store.create_task(run_id, "t1", "codex", phase=1)
        await store.create_task(run_id, "t2", "stitch", phase=2)
        await store.update_task_status("t1", "done", grade_score=90.0)
        await store.record_cost(run_id, "codex", 10.0, 0.03)

        summary = await store.get_run_summary(run_id)
        assert summary["run"]["project_name"] == "proj"
        assert summary["task_counts"]["done"] == 1
        assert summary["task_counts"]["pending"] == 1
        assert abs(summary["total_cost_usd"] - 0.03) < 0.001
        assert "codex" in summary["cost_by_specialist"]


# ─── Context Manager & WAL Mode ───


class TestDatabaseSetup:
    @pytest.mark.asyncio
    async def test_context_manager(self, tmp_path):
        db_path = tmp_path / "test.db"
        async with MaestroStore(db_path) as store:
            run_id = await store.create_run("proj")
            assert run_id is not None

    @pytest.mark.asyncio
    async def test_creates_parent_dirs(self, tmp_path):
        db_path = tmp_path / "deep" / "nested" / "test.db"
        async with MaestroStore(db_path) as store:
            await store.create_run("proj")
        assert db_path.exists()

    @pytest.mark.asyncio
    async def test_cascade_delete(self, store):
        """Deleting a run cascades to tasks and artifacts."""
        run_id = await store.create_run("proj")
        await store.create_task(run_id, "t1", "codex", phase=1)
        await store.create_artifact("t1", run_id, "/a.py", "hash")
        await store.record_feedback("t1", run_id, 1, grade_score=50.0)

        # Delete the run
        await store._db.execute("DELETE FROM runs WHERE run_id = ?", (run_id,))
        await store._db.commit()

        assert await store.get_task("t1") is None
        assert await store.get_artifacts_by_task("t1") == []
        assert await store.get_feedback_history("t1") == []

"""Tests for maestro.checkpoint — Checkpoint manager and JSON migration."""

import json
from pathlib import Path

import pytest

from maestro.db.store import MaestroStore
from maestro.checkpoint import CheckpointManager, migrate_json_state, compute_file_hash
from maestro.state_machine import TaskState


@pytest.fixture
async def store(tmp_path):
    """Create a fresh database store."""
    db_path = tmp_path / "test.db"
    async with MaestroStore(db_path) as s:
        yield s


@pytest.fixture
def manager(store):
    """Create a CheckpointManager instance."""
    return CheckpointManager(store)


@pytest.mark.asyncio
async def test_compute_file_hash(tmp_path):
    f = tmp_path / "file.txt"
    f.write_text("hello world")
    h1 = compute_file_hash(f)
    assert len(h1) == 64  # SHA-256

    f.write_text("hello world!")
    h2 = compute_file_hash(f)
    assert h1 != h2


@pytest.mark.asyncio
async def test_checkpoint_artifact(tmp_path, store, manager):
    run_id = await store.create_run("proj")
    await store.create_task(run_id, "t1", "codex")

    f = tmp_path / "app.py"
    f.write_text("print('hello')")

    aid = await manager.checkpoint_artifact("t1", run_id, f)
    assert len(aid) == 12

    artifacts = await store.get_artifacts_by_task("t1")
    assert len(artifacts) == 1
    assert artifacts[0]["file_path"] == str(f)
    assert artifacts[0]["file_type"] == "py"
    assert artifacts[0]["size_bytes"] == f.stat().st_size


@pytest.mark.asyncio
async def test_workspace_integrity(tmp_path, store, manager):
    run_id = await store.create_run("proj")
    await store.create_task(run_id, "t1", "codex")
    await store.update_task_status("t1", "done")

    f = tmp_path / "app.py"
    f.write_text("print('hello')")
    await manager.checkpoint_artifact("t1", run_id, f)

    # Check initially — should be fine
    modified = await manager.check_workspace_integrity(run_id)
    assert len(modified) == 0

    # Modify the file
    f.write_text("print('hello modified')")
    modified = await manager.check_workspace_integrity(run_id)
    assert len(modified) == 1
    assert modified[0]["issue"] == "modified"
    assert modified[0]["file_path"] == str(f)

    # Delete the file
    f.unlink()
    modified = await manager.check_workspace_integrity(run_id)
    assert len(modified) == 1
    assert modified[0]["issue"] == "deleted"


@pytest.mark.asyncio
async def test_get_resumable_and_completed_tasks(store, manager):
    run_id = await store.create_run("proj")
    await store.create_task(run_id, "t1", "codex")
    await store.create_task(run_id, "t2", "claude_code")
    await store.create_task(run_id, "t3", "stitch")

    await store.update_task_status("t1", "done")
    await store.update_task_status("t2", "running")
    # t3 remains pending

    completed = await manager.get_completed_tasks(run_id)
    assert len(completed) == 1
    assert completed[0]["task_id"] == "t1"

    resumable = await manager.get_resumable_tasks(run_id)
    assert len(resumable) == 2
    resumable_ids = [t["task_id"] for t in resumable]
    assert "t2" in resumable_ids
    assert "t3" in resumable_ids


@pytest.mark.asyncio
async def test_prepare_resume(tmp_path, store, manager):
    run_id = await store.create_run("proj")
    await store.create_task(run_id, "t1", "codex")
    await store.create_task(run_id, "t2", "claude_code")

    f = tmp_path / "app.py"
    f.write_text("original")
    await manager.checkpoint_artifact("t1", run_id, f)
    await store.update_task_status("t1", "done")
    await store.update_task_status("t2", "running")

    # Modify artifact to simulate issue
    f.write_text("modified")

    resume_plan = await manager.prepare_resume(run_id)
    assert resume_plan["run_id"] == run_id
    assert resume_plan["completed_count"] == 1
    assert len(resume_plan["resumable_tasks"]) == 1
    assert resume_plan["resumable_tasks"][0]["task_id"] == "t2"
    assert len(resume_plan["modified_artifacts"]) == 1
    assert resume_plan["modified_artifacts"][0]["file_path"] == str(f)


@pytest.mark.asyncio
async def test_migrate_json_state(tmp_path, store):
    json_path = tmp_path / "state.json"
    state_data = {
        "run_id": "test-run-123",
        "project_name": "migration-project",
        "status": "paused",
        "total_spent_usd": 1.25,
        "phase_results": {
            "1": {"status": "success", "specialist": "codex", "duration_sec": 12.5, "cost_usd": 0.05},
            "2": {"status": "failed", "specialist": "claude_code", "duration_sec": 22.0, "cost_usd": 0.15, "error": "test error"},
        }
    }
    with open(json_path, "w") as f:
        json.dump(state_data, f)

    migrated_id = await migrate_json_state(json_path, store)
    assert migrated_id == "test-run-123"

    # Verify JSON file got renamed
    assert not json_path.exists()
    assert json_path.with_suffix(".json.migrated").exists()

    # Verify SQLite DB
    run = await store.get_run("test-run-123")
    assert run is not None
    assert run["project_name"] == "migration-project"
    assert run["status"] == "paused"
    assert run["total_spent_usd"] == 1.25

    tasks = await store.get_tasks_by_run("test-run-123")
    assert len(tasks) == 2
    t1 = next(t for t in tasks if t["task_id"] == "phase_1")
    assert t1["status"] == "done"
    assert t1["duration_sec"] == 12.5
    assert t1["estimated_cost"] == 0.05

    t2 = next(t for t in tasks if t["task_id"] == "phase_2")
    assert t2["status"] == "failed"
    assert t2["error_message"] == "test error"


@pytest.mark.asyncio
async def test_migrate_json_state_not_found(store):
    assert await migrate_json_state(Path("nonexistent.json"), store) is None

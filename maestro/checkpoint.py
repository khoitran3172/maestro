"""Checkpoint manager — save/restore pipeline state for crash recovery.

Handles:
- Resume after kill -9: skip done tasks, re-queue interrupted ones
- Artifact integrity: detect manual workspace edits between runs via content-hash
- State.json → SQLite migration for Sprint 1 compatibility
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Optional

from maestro.db.store import MaestroStore
from maestro.state_machine import TaskState, get_resumable_states


def compute_file_hash(file_path: Path) -> str:
    """SHA-256 hash of file content for integrity checking."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()


class CheckpointManager:
    """Manages checkpoint/resume for Maestro pipeline runs.

    Usage:
        manager = CheckpointManager(store)

        # On resume:
        resumable = await manager.get_resumable_tasks(run_id)
        modified = await manager.check_workspace_integrity(run_id)

        # After task completion:
        await manager.checkpoint_artifact(task_id, run_id, file_path)
    """

    def __init__(self, store: MaestroStore):
        self.store = store

    async def get_resumable_tasks(self, run_id: str) -> list[dict]:
        """Get tasks that need to be re-queued on resume.

        Returns tasks in running/queued/pending/kicked_back/grading states.
        Done and failed tasks are skipped.
        """
        all_tasks = await self.store.get_tasks_by_run(run_id)
        resumable_states = get_resumable_states()

        resumable = []
        for task in all_tasks:
            if task["status"] in resumable_states:
                resumable.append(task)

        return resumable

    async def get_completed_tasks(self, run_id: str) -> list[dict]:
        """Get all completed (done) tasks — these are skipped on resume."""
        return await self.store.get_tasks_by_status(run_id, TaskState.DONE.value)

    async def checkpoint_artifact(
        self,
        task_id: str,
        run_id: str,
        file_path: Path,
    ) -> str:
        """Record an artifact with its content hash for integrity checking.

        Call this after a task produces output — enables detecting
        manual workspace edits between crash and resume.

        Returns: artifact_id
        """
        content_hash = compute_file_hash(file_path)
        file_type = file_path.suffix.lstrip(".")
        size_bytes = file_path.stat().st_size

        return await self.store.create_artifact(
            task_id=task_id,
            run_id=run_id,
            file_path=str(file_path),
            content_hash=content_hash,
            file_type=file_type,
            size_bytes=size_bytes,
        )

    async def check_workspace_integrity(
        self, run_id: str
    ) -> list[dict]:
        """Check if any completed task artifacts were modified externally.

        Returns list of modified artifacts with details.
        Use this on resume to warn the user about manual edits.
        """
        completed = await self.get_completed_tasks(run_id)
        modified = []

        for task in completed:
            artifacts = await self.store.get_artifacts_by_task(task["task_id"])
            for artifact in artifacts:
                file_path = Path(artifact["file_path"])
                if not file_path.exists():
                    modified.append({
                        "task_id": task["task_id"],
                        "file_path": str(file_path),
                        "issue": "deleted",
                        "recorded_hash": artifact["content_hash"],
                    })
                else:
                    current_hash = compute_file_hash(file_path)
                    if current_hash != artifact["content_hash"]:
                        modified.append({
                            "task_id": task["task_id"],
                            "file_path": str(file_path),
                            "issue": "modified",
                            "recorded_hash": artifact["content_hash"],
                            "current_hash": current_hash,
                        })

        return modified

    async def prepare_resume(self, run_id: str) -> dict:
        """Full resume preparation: check integrity, get resumable tasks.

        Returns a resume plan dict with:
        - completed_count: tasks already done (skipped)
        - resumable_tasks: tasks to re-queue
        - modified_artifacts: workspace integrity issues
        - run_status: current run status
        """
        run = await self.store.get_run(run_id)
        if not run:
            raise ValueError(f"Run not found: {run_id}")

        completed = await self.get_completed_tasks(run_id)
        resumable = await self.get_resumable_tasks(run_id)
        modified = await self.check_workspace_integrity(run_id)

        return {
            "run_id": run_id,
            "run_status": run["status"],
            "project_name": run["project_name"],
            "completed_count": len(completed),
            "resumable_tasks": resumable,
            "modified_artifacts": modified,
            "total_spent_usd": run["total_spent_usd"],
        }


async def migrate_json_state(
    json_state_path: Path,
    store: MaestroStore,
) -> Optional[str]:
    """Migrate Sprint 1 state.json to SQLite.

    One-time migration — reads old JSON state and creates equivalent
    records in SQLite. Returns run_id of the migrated run.
    """
    if not json_state_path.exists():
        return None

    with open(json_state_path) as f:
        state = json.load(f)

    run_id = state.get("run_id", "migrated")
    project_name = state.get("project_name", "unknown")

    # Create run
    await store.create_run(
        project_name=project_name,
        run_id=run_id,
        max_budget_usd=None,
    )

    # Update run status
    status = state.get("status", "running")
    if status != "running":
        await store.update_run_status(run_id, status)

    # Update cost
    total_spent = state.get("total_spent_usd", 0.0)
    if total_spent > 0:
        await store.update_run_cost(run_id, total_spent)

    # Create tasks from phase_results
    for phase_str, result in state.get("phase_results", {}).items():
        phase = int(phase_str)
        task_id = f"phase_{phase}"
        task_status = "done" if result.get("status") == "success" else "failed"

        await store.create_task(
            run_id=run_id,
            task_id=task_id,
            specialist=result.get("specialist", "unknown"),
            phase=phase,
        )
        await store.update_task_status(
            task_id,
            task_status,
            duration_sec=result.get("duration_sec"),
            estimated_cost=result.get("cost_usd"),
            error_message=result.get("error"),
        )

    # Rename old state file to mark as migrated
    migrated_path = json_state_path.with_suffix(".json.migrated")
    json_state_path.rename(migrated_path)

    return run_id

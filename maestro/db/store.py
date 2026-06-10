"""SQLite data access layer for Maestro state persistence.

Provides async CRUD operations for runs, tasks, artifacts, feedback, and cost.
Uses aiosqlite with WAL mode for safe concurrent reads.

Design:
- All methods are async — ready for DAG scheduler (Sprint 6)
- Atomic writes via transactions
- Content-hash tracking for artifact integrity
- Query-friendly: "which specialist costs most?" is a simple SQL query
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

import aiosqlite

# Path to schema.sql relative to this file
_SCHEMA_PATH = Path(__file__).parent / "schema.sql"


def _now_iso() -> str:
    """Current UTC timestamp in ISO-8601 format."""
    return datetime.now(timezone.utc).isoformat()


def _gen_id() -> str:
    """Generate a short unique ID."""
    return uuid.uuid4().hex[:12]


class MaestroStore:
    """Async SQLite data store for Maestro pipeline state.

    Usage:
        store = MaestroStore(Path(".maestro/maestro.db"))
        await store.initialize()

        run_id = await store.create_run("my-project", max_budget=10.0)
        await store.create_task(run_id, "t1", "codex", phase=3, prompt="...")
        await store.update_task_status("t1", "running")

        await store.close()

    Or as context manager:
        async with MaestroStore(db_path) as store:
            ...
    """

    def __init__(self, db_path: Path):
        self.db_path = db_path
        self._db: Optional[aiosqlite.Connection] = None

    async def initialize(self) -> None:
        """Open database and create schema if needed."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = await aiosqlite.connect(str(self.db_path))
        self._db.row_factory = aiosqlite.Row

        # Create tables and run pragmas
        schema = _SCHEMA_PATH.read_text(encoding="utf-8")
        await self._db.executescript(schema)

    async def close(self) -> None:
        """Close the database connection."""
        if self._db:
            await self._db.close()
            self._db = None

    async def __aenter__(self) -> MaestroStore:
        await self.initialize()
        return self

    async def __aexit__(self, *args: Any) -> None:
        await self.close()

    # ─── Runs ───

    async def create_run(
        self,
        project_name: str,
        *,
        run_id: Optional[str] = None,
        max_budget_usd: Optional[float] = None,
        task_graph_json: Optional[str] = None,
    ) -> str:
        """Create a new pipeline run. Returns run_id."""
        rid = run_id or _gen_id()
        now = _now_iso()
        await self._db.execute(
            """INSERT INTO runs (run_id, project_name, status, task_graph_json,
                                 max_budget_usd, created_at, updated_at)
               VALUES (?, ?, 'running', ?, ?, ?, ?)""",
            (rid, project_name, task_graph_json, max_budget_usd, now, now),
        )
        await self._db.commit()
        return rid

    async def get_run(self, run_id: str) -> Optional[dict]:
        """Get run by ID. Returns None if not found."""
        async with self._db.execute(
            "SELECT * FROM runs WHERE run_id = ?", (run_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def update_run_status(self, run_id: str, status: str) -> None:
        """Update run status."""
        await self._db.execute(
            "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
            (status, _now_iso(), run_id),
        )
        await self._db.commit()

    async def update_run_cost(self, run_id: str, total_spent: float) -> None:
        """Update total spent for a run."""
        await self._db.execute(
            "UPDATE runs SET total_spent_usd = ?, updated_at = ? WHERE run_id = ?",
            (total_spent, _now_iso(), run_id),
        )
        await self._db.commit()

    async def get_latest_run(self) -> Optional[dict]:
        """Get the most recent run."""
        async with self._db.execute(
            "SELECT * FROM runs ORDER BY created_at DESC LIMIT 1"
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    # ─── Tasks ───

    async def create_task(
        self,
        run_id: str,
        task_id: str,
        specialist: str,
        *,
        phase: int = 0,
        prompt: Optional[str] = None,
        input_hash: Optional[str] = None,
        max_retries: int = 2,
        branch_name: Optional[str] = None,
    ) -> str:
        """Create a new task in pending state. Returns task_id."""
        now = _now_iso()
        await self._db.execute(
            """INSERT INTO tasks (task_id, run_id, specialist, phase, status,
                                  input_prompt, input_hash, max_retries,
                                  branch_name, created_at, updated_at)
               VALUES (?, ?, ?, ?, 'pending', ?, ?, ?, ?, ?, ?)""",
            (task_id, run_id, specialist, phase, prompt, input_hash,
             max_retries, branch_name, now, now),
        )
        await self._db.commit()
        return task_id

    async def get_task(self, task_id: str) -> Optional[dict]:
        """Get task by ID."""
        async with self._db.execute(
            "SELECT * FROM tasks WHERE task_id = ?", (task_id,)
        ) as cursor:
            row = await cursor.fetchone()
            return dict(row) if row else None

    async def get_tasks_by_run(self, run_id: str) -> list[dict]:
        """Get all tasks for a run, ordered by phase."""
        async with self._db.execute(
            "SELECT * FROM tasks WHERE run_id = ? ORDER BY phase, task_id",
            (run_id,),
        ) as cursor:
            return [dict(row) async for row in cursor]

    async def get_tasks_by_status(
        self, run_id: str, status: str
    ) -> list[dict]:
        """Get tasks with a specific status."""
        async with self._db.execute(
            "SELECT * FROM tasks WHERE run_id = ? AND status = ? ORDER BY phase",
            (run_id, status),
        ) as cursor:
            return [dict(row) async for row in cursor]

    async def update_task_status(
        self,
        task_id: str,
        status: str,
        *,
        error_message: Optional[str] = None,
        session_id: Optional[str] = None,
        grade_score: Optional[float] = None,
        grade_feedback: Optional[str] = None,
        output_artifact: Optional[str] = None,
        duration_sec: Optional[float] = None,
        estimated_cost: Optional[float] = None,
    ) -> None:
        """Update task status and optional fields atomically."""
        updates = ["status = ?", "updated_at = ?"]
        params: list[Any] = [status, _now_iso()]

        if status == "running":
            updates.append("started_at = ?")
            params.append(_now_iso())
        elif status in ("done", "failed"):
            updates.append("completed_at = ?")
            params.append(_now_iso())
        elif status == "kicked_back":
            updates.append("retry_count = retry_count + 1")

        if error_message is not None:
            updates.append("error_message = ?")
            params.append(error_message)
        if session_id is not None:
            updates.append("session_id = ?")
            params.append(session_id)
        if grade_score is not None:
            updates.append("grade_score = ?")
            params.append(grade_score)
        if grade_feedback is not None:
            updates.append("grade_feedback = ?")
            params.append(grade_feedback)
        if output_artifact is not None:
            updates.append("output_artifact = ?")
            params.append(output_artifact)
        if duration_sec is not None:
            updates.append("duration_sec = ?")
            params.append(duration_sec)
        if estimated_cost is not None:
            updates.append("estimated_cost = ?")
            params.append(estimated_cost)

        params.append(task_id)
        sql = f"UPDATE tasks SET {', '.join(updates)} WHERE task_id = ?"
        await self._db.execute(sql, params)
        await self._db.commit()

    async def count_tasks_by_status(self, run_id: str) -> dict[str, int]:
        """Count tasks grouped by status for a run."""
        async with self._db.execute(
            "SELECT status, COUNT(*) as cnt FROM tasks WHERE run_id = ? GROUP BY status",
            (run_id,),
        ) as cursor:
            return {row["status"]: row["cnt"] async for row in cursor}

    # ─── Artifacts ───

    async def create_artifact(
        self,
        task_id: str,
        run_id: str,
        file_path: str,
        content_hash: str,
        *,
        file_type: Optional[str] = None,
        size_bytes: Optional[int] = None,
    ) -> str:
        """Record an artifact with its content hash."""
        aid = _gen_id()
        await self._db.execute(
            """INSERT INTO artifacts (artifact_id, task_id, run_id, file_path,
                                      content_hash, file_type, size_bytes, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (aid, task_id, run_id, file_path, content_hash, file_type,
             size_bytes, _now_iso()),
        )
        await self._db.commit()
        return aid

    async def get_artifacts_by_task(self, task_id: str) -> list[dict]:
        """Get all artifacts for a task."""
        async with self._db.execute(
            "SELECT * FROM artifacts WHERE task_id = ?", (task_id,)
        ) as cursor:
            return [dict(row) async for row in cursor]

    async def check_artifact_integrity(
        self, artifact_id: str, current_hash: str
    ) -> bool:
        """Check if artifact content matches recorded hash.

        Returns True if unchanged, False if modified externally.
        """
        async with self._db.execute(
            "SELECT content_hash FROM artifacts WHERE artifact_id = ?",
            (artifact_id,),
        ) as cursor:
            row = await cursor.fetchone()
            if not row:
                return False
            return row["content_hash"] == current_hash

    # ─── Feedback History ───

    async def record_feedback(
        self,
        task_id: str,
        run_id: str,
        iteration: int,
        *,
        grade_score: Optional[float] = None,
        rubric_failures: Optional[list[str]] = None,
        issues_text: Optional[str] = None,
        prev_artifact_hash: Optional[str] = None,
    ) -> str:
        """Record a feedback entry for a kicked-back task."""
        fid = _gen_id()
        failures_json = json.dumps(rubric_failures) if rubric_failures else None
        await self._db.execute(
            """INSERT INTO feedback_history
               (feedback_id, task_id, run_id, iteration, grade_score,
                rubric_failures, issues_text, prev_artifact_hash, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (fid, task_id, run_id, iteration, grade_score,
             failures_json, issues_text, prev_artifact_hash, _now_iso()),
        )
        await self._db.commit()
        return fid

    async def get_feedback_history(self, task_id: str) -> list[dict]:
        """Get all feedback for a task, ordered by iteration."""
        async with self._db.execute(
            "SELECT * FROM feedback_history WHERE task_id = ? ORDER BY iteration",
            (task_id,),
        ) as cursor:
            rows = [dict(row) async for row in cursor]
            for row in rows:
                if row.get("rubric_failures"):
                    row["rubric_failures"] = json.loads(row["rubric_failures"])
            return rows

    # ─── Cost Log ───

    async def record_cost(
        self,
        run_id: str,
        specialist: str,
        duration_sec: float,
        estimated_cost: float,
        *,
        task_id: Optional[str] = None,
        token_input: Optional[int] = None,
        token_output: Optional[int] = None,
        method: str = "time_based",
    ) -> str:
        """Record a cost entry."""
        cid = _gen_id()
        await self._db.execute(
            """INSERT INTO cost_log
               (cost_id, run_id, task_id, specialist, duration_sec,
                estimated_cost, token_input, token_output, method, created_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (cid, run_id, task_id, specialist, duration_sec,
             estimated_cost, token_input, token_output, method, _now_iso()),
        )
        await self._db.commit()
        return cid

    async def get_total_cost(self, run_id: str) -> float:
        """Get total cost for a run."""
        async with self._db.execute(
            "SELECT COALESCE(SUM(estimated_cost), 0.0) as total FROM cost_log WHERE run_id = ?",
            (run_id,),
        ) as cursor:
            row = await cursor.fetchone()
            return float(row["total"])

    async def get_cost_by_specialist(self, run_id: str) -> dict[str, float]:
        """Get cost breakdown by specialist."""
        async with self._db.execute(
            """SELECT specialist, SUM(estimated_cost) as total
               FROM cost_log WHERE run_id = ? GROUP BY specialist
               ORDER BY total DESC""",
            (run_id,),
        ) as cursor:
            return {row["specialist"]: float(row["total"]) async for row in cursor}

    # ─── Queries for observability ───

    async def get_run_summary(self, run_id: str) -> dict:
        """Complete run summary: status, tasks, cost."""
        run = await self.get_run(run_id)
        if not run:
            return {}

        task_counts = await self.count_tasks_by_status(run_id)
        total_cost = await self.get_total_cost(run_id)
        cost_by_specialist = await self.get_cost_by_specialist(run_id)

        return {
            "run": run,
            "task_counts": task_counts,
            "total_cost_usd": total_cost,
            "cost_by_specialist": cost_by_specialist,
        }

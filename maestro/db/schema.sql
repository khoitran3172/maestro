-- Maestro State Database Schema
-- SQLite with WAL mode for concurrent read + single write.
-- All timestamps are ISO-8601 strings.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

-- ─── Runs ───

CREATE TABLE IF NOT EXISTS runs (
    run_id          TEXT PRIMARY KEY,
    project_name    TEXT NOT NULL,
    status          TEXT NOT NULL DEFAULT 'running'
                    CHECK (status IN ('running', 'completed', 'failed', 'paused')),
    task_graph_json TEXT,               -- JSON serialized TaskGraph (Sprint 6)
    total_spent_usd REAL NOT NULL DEFAULT 0.0,
    max_budget_usd  REAL,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- ─── Tasks ───

CREATE TABLE IF NOT EXISTS tasks (
    task_id         TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    specialist      TEXT NOT NULL,
    phase           INTEGER NOT NULL,
    status          TEXT NOT NULL DEFAULT 'pending'
                    CHECK (status IN (
                        'pending', 'queued', 'running', 'grading',
                        'done', 'failed', 'kicked_back'
                    )),
    input_prompt    TEXT,
    input_hash      TEXT,
    output_artifact TEXT,               -- primary output path
    grade_score     REAL,
    grade_feedback  TEXT,
    session_id      TEXT,               -- for specialist resume
    branch_name     TEXT,               -- for Git isolation
    started_at      TEXT,
    completed_at    TEXT,
    duration_sec    REAL,
    estimated_cost  REAL DEFAULT 0.0,
    retry_count     INTEGER NOT NULL DEFAULT 0,
    max_retries     INTEGER NOT NULL DEFAULT 2,
    error_message   TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_tasks_run_id ON tasks(run_id);
CREATE INDEX IF NOT EXISTS idx_tasks_status ON tasks(status);

-- ─── Artifacts ───

CREATE TABLE IF NOT EXISTS artifacts (
    artifact_id     TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    file_path       TEXT NOT NULL,
    content_hash    TEXT NOT NULL,       -- SHA-256 for integrity check
    file_type       TEXT,               -- extension: md, py, ts, png, jpg...
    size_bytes      INTEGER,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_artifacts_task_id ON artifacts(task_id);

-- ─── Feedback History ───

CREATE TABLE IF NOT EXISTS feedback_history (
    feedback_id     TEXT PRIMARY KEY,
    task_id         TEXT NOT NULL REFERENCES tasks(task_id) ON DELETE CASCADE,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    iteration       INTEGER NOT NULL,
    grade_score     REAL,
    rubric_failures TEXT,               -- JSON array of failure descriptions
    issues_text     TEXT,               -- human-readable feedback
    prev_artifact_hash TEXT,
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_feedback_task_id ON feedback_history(task_id);

-- ─── Cost Log ───

CREATE TABLE IF NOT EXISTS cost_log (
    cost_id         TEXT PRIMARY KEY,
    run_id          TEXT NOT NULL REFERENCES runs(run_id) ON DELETE CASCADE,
    task_id         TEXT,
    specialist      TEXT NOT NULL,
    duration_sec    REAL NOT NULL,
    estimated_cost  REAL NOT NULL,
    token_input     INTEGER,
    token_output    INTEGER,
    method          TEXT NOT NULL DEFAULT 'time_based'
                    CHECK (method IN ('time_based', 'token_based', 'explicit')),
    created_at      TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_cost_run_id ON cost_log(run_id);

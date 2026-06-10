"""Tests for maestro.logger — structured JSONL logging."""

import json
from pathlib import Path

import pytest

from maestro.error_handler import TaskResult, TaskStatus
from maestro.logger import LogEntry, LogLevel, MaestroLogger, hash_input


class TestLogEntry:
    def test_to_json_line_compact(self):
        """JSON line includes only non-None fields."""
        entry = LogEntry(
            timestamp=1700000000.0,
            level=LogLevel.INFO,
            event="specialist_call",
            specialist="codex",
        )
        line = entry.to_json_line()
        data = json.loads(line)
        assert data["event"] == "specialist_call"
        assert data["specialist"] == "codex"
        assert "phase" not in data  # None fields excluded
        assert "msg" not in data

    def test_to_json_line_full(self):
        entry = LogEntry(
            timestamp=1700000000.0,
            level=LogLevel.ERROR,
            event="specialist_call",
            phase=3,
            specialist="claude_code",
            task_id="t4",
            duration_sec=45.678,
            cost_usd=0.123456,
            status="error",
            message="Timeout",
        )
        data = json.loads(entry.to_json_line())
        assert data["duration_sec"] == 45.68  # Rounded
        assert data["cost_usd"] == 0.123456
        assert data["msg"] == "Timeout"


class TestMaestroLogger:
    def test_log_specialist_call(self, tmp_path):
        logger = MaestroLogger(tmp_path)
        result = TaskResult(
            status=TaskStatus.SUCCESS,
            stdout="output",
            exit_code=0,
            duration_sec=12.3,
            command="codex run",
        )
        logger.log_specialist_call(
            phase=3,
            specialist="codex",
            task_id="t4",
            result=result,
            cost_usd=0.05,
        )

        log_file = tmp_path / "log.jsonl"
        assert log_file.exists()
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        data = json.loads(lines[0])
        assert data["event"] == "specialist_call"
        assert data["specialist"] == "codex"
        assert data["status"] == "success"

    def test_log_grade(self, tmp_path):
        logger = MaestroLogger(tmp_path)
        logger.log_grade(
            task_id="t4",
            specialist="codex",
            score=72.5,
            passed=False,
            failures=["Tests not passing", "Missing error handling"],
        )

        data = json.loads((tmp_path / "log.jsonl").read_text().strip())
        assert data["event"] == "grade"
        assert data["status"] == "failed"
        assert data["meta"]["score"] == 72.5

    def test_log_kickback(self, tmp_path):
        logger = MaestroLogger(tmp_path)
        logger.log_kickback(
            task_id="t4",
            specialist="codex",
            attempt=2,
            reason="Score below threshold",
        )

        data = json.loads((tmp_path / "log.jsonl").read_text().strip())
        assert data["event"] == "kickback"
        assert data["meta"]["attempt"] == 2

    def test_level_filtering(self, tmp_path):
        """DEBUG messages filtered when min_level is INFO."""
        logger = MaestroLogger(tmp_path, min_level=LogLevel.WARNING)
        logger.debug("should be filtered")
        logger.info("also filtered")
        logger.warning("should appear")

        log_file = tmp_path / "log.jsonl"
        lines = log_file.read_text().strip().split("\n")
        assert len(lines) == 1
        assert "should appear" in lines[0]

    def test_multiple_entries_appended(self, tmp_path):
        """Multiple log calls append to same file."""
        logger = MaestroLogger(tmp_path)
        logger.info("first")
        logger.info("second")
        logger.warning("third")

        lines = (tmp_path / "log.jsonl").read_text().strip().split("\n")
        assert len(lines) == 3

    def test_creates_directory(self, tmp_path):
        """Logger creates maestro_dir if it doesn't exist."""
        nested = tmp_path / "deep" / "nested" / "dir"
        logger = MaestroLogger(nested)
        logger.info("test")
        assert (nested / "log.jsonl").exists()


class TestHashInput:
    def test_deterministic(self):
        assert hash_input("hello") == hash_input("hello")

    def test_different_inputs(self):
        assert hash_input("hello") != hash_input("world")

    def test_length(self):
        assert len(hash_input("test")) == 16  # Truncated SHA256

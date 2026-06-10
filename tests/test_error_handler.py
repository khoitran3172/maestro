"""Tests for maestro.error_handler — subprocess wrapping and retry logic."""

import subprocess
import time
from pathlib import Path
from unittest.mock import patch, MagicMock

import pytest

from maestro.error_handler import (
    TaskResult,
    TaskStatus,
    RetryPolicy,
    run_specialist_subprocess,
    compute_content_hash,
)


class TestTaskResult:
    def test_success_status(self):
        result = TaskResult(status=TaskStatus.SUCCESS, stdout="ok")
        assert result.succeeded is True

    def test_error_status(self):
        result = TaskResult(status=TaskStatus.ERROR, error_message="bad exit")
        assert result.succeeded is False

    def test_timeout_status(self):
        result = TaskResult(status=TaskStatus.TIMEOUT, error_message="timed out")
        assert result.succeeded is False

    def test_to_dict_caps_output(self):
        long_output = "x" * 5000
        result = TaskResult(status=TaskStatus.SUCCESS, stdout=long_output)
        d = result.to_dict()
        assert len(d["stdout"]) == 2000  # Capped at 2000 chars

    def test_to_dict_structure(self):
        result = TaskResult(
            status=TaskStatus.SUCCESS,
            stdout="hello",
            stderr="",
            exit_code=0,
            duration_sec=1.234,
            command="echo hello",
        )
        d = result.to_dict()
        assert d["status"] == "success"
        assert d["exit_code"] == 0
        assert d["duration_sec"] == 1.23  # Rounded


class TestRunSpecialistSubprocess:
    def test_success(self):
        """Successful command returns SUCCESS status."""
        result = run_specialist_subprocess(
            ["python", "-c", "print('hello')"],
            timeout_sec=10,
        )
        assert result.status == TaskStatus.SUCCESS
        assert "hello" in result.stdout
        assert result.exit_code == 0
        assert result.duration_sec > 0

    def test_nonzero_exit(self):
        """Non-zero exit code returns ERROR status."""
        result = run_specialist_subprocess(
            ["python", "-c", "import sys; sys.exit(42)"],
            timeout_sec=10,
        )
        assert result.status == TaskStatus.ERROR
        assert result.exit_code == 42
        assert "Non-zero exit" in result.error_message

    def test_timeout(self):
        """Timeout returns TIMEOUT status."""
        result = run_specialist_subprocess(
            ["python", "-c", "import time; time.sleep(60)"],
            timeout_sec=1,
        )
        assert result.status == TaskStatus.TIMEOUT
        assert "Timeout" in result.error_message

    def test_command_not_found(self):
        """Missing command returns ERROR with clear message."""
        result = run_specialist_subprocess(
            ["this_command_does_not_exist_xyz"],
            timeout_sec=10,
        )
        assert result.status == TaskStatus.ERROR
        assert "not found" in result.error_message.lower() or "error" in result.error_message.lower()

    def test_captures_stderr(self):
        """Stderr is captured in result."""
        result = run_specialist_subprocess(
            ["python", "-c", "import sys; sys.stderr.write('oops')"],
            timeout_sec=10,
        )
        assert "oops" in result.stderr

    def test_duration_is_measured(self):
        """Duration is measured regardless of outcome."""
        result = run_specialist_subprocess(
            ["python", "-c", "import time; time.sleep(0.1); print('done')"],
            timeout_sec=10,
        )
        assert result.duration_sec >= 0.1


class TestRetryPolicy:
    def test_retry_on_timeout(self):
        policy = RetryPolicy(max_retries=2)
        result = TaskResult(status=TaskStatus.TIMEOUT, error_message="Timeout")
        assert policy.should_retry(result, attempt=0) is True
        assert policy.should_retry(result, attempt=1) is True
        assert policy.should_retry(result, attempt=2) is False  # Max reached

    def test_retry_on_error(self):
        policy = RetryPolicy(max_retries=2)
        result = TaskResult(status=TaskStatus.ERROR, error_message="Some error")
        assert policy.should_retry(result, attempt=0) is True

    def test_no_retry_on_not_found(self):
        policy = RetryPolicy(max_retries=2)
        result = TaskResult(status=TaskStatus.ERROR, error_message="Command not found: xyz")
        assert policy.should_retry(result, attempt=0) is False

    def test_no_retry_on_permission(self):
        policy = RetryPolicy(max_retries=2)
        result = TaskResult(status=TaskStatus.ERROR, error_message="Permission denied: /x")
        assert policy.should_retry(result, attempt=0) is False

    def test_exponential_backoff(self):
        policy = RetryPolicy(base_delay_sec=5.0)
        assert policy.delay_sec(0) == 5.0
        assert policy.delay_sec(1) == 30.0
        assert policy.delay_sec(2) == 180.0


class TestContentHash:
    def test_hash_consistency(self, tmp_path):
        """Same content always produces same hash."""
        f = tmp_path / "test.txt"
        f.write_text("hello world")
        h1 = compute_content_hash(f)
        h2 = compute_content_hash(f)
        assert h1 == h2
        assert len(h1) == 64  # SHA256 hex

    def test_hash_changes_with_content(self, tmp_path):
        """Different content produces different hash."""
        f = tmp_path / "test.txt"
        f.write_text("version 1")
        h1 = compute_content_hash(f)
        f.write_text("version 2")
        h2 = compute_content_hash(f)
        assert h1 != h2

"""Error handling for specialist subprocess calls.

Wraps subprocess execution with structured error capture, timeout handling,
and retry-compatible output format. Every specialist call goes through here.
"""

from __future__ import annotations

import hashlib
import subprocess
import time
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Optional


class TaskStatus(str, Enum):
    """Possible outcomes of a specialist subprocess call."""
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class TaskResult:
    """Structured result from a specialist subprocess call.

    Every specialist call returns this — success or failure.
    Contains enough info for Maestro to decide: retry / route elsewhere / escalate.
    """
    status: TaskStatus
    stdout: str = ""
    stderr: str = ""
    partial_output: str = ""
    exit_code: Optional[int] = None
    duration_sec: float = 0.0
    command: str = ""
    error_message: str = ""
    output_artifacts: list[Path] = field(default_factory=list)

    @property
    def succeeded(self) -> bool:
        return self.status == TaskStatus.SUCCESS

    def to_dict(self) -> dict:
        """Serialize for logging / state persistence."""
        return {
            "status": self.status.value,
            "stdout": self.stdout[:2000],  # cap for logging
            "stderr": self.stderr[:2000],
            "partial_output": self.partial_output[:1000],
            "exit_code": self.exit_code,
            "duration_sec": round(self.duration_sec, 2),
            "command": self.command,
            "error_message": self.error_message,
            "output_artifacts": [str(p) for p in self.output_artifacts],
        }


def run_specialist_subprocess(
    command: list[str],
    *,
    cwd: Optional[Path] = None,
    timeout_sec: int = 600,
    env: Optional[dict[str, str]] = None,
    input_text: Optional[str] = None,
    capture_partial: bool = True,
) -> TaskResult:
    """Execute a specialist CLI command with full error handling.

    This is the ONLY way specialist subprocesses should be invoked.
    Handles: TimeoutExpired, CalledProcessError, non-zero exit, OOM, etc.

    Args:
        command: CLI command as list of tokens, e.g. ["claude", "--prompt", "..."]
        cwd: Working directory for the subprocess
        timeout_sec: Max seconds before killing the process
        env: Additional environment variables (merged with os.environ)
        input_text: Stdin input to send to the process
        capture_partial: If True, capture partial stdout on timeout/error

    Returns:
        TaskResult with structured status, stdout, stderr, and timing info.
    """
    import os

    merged_env = {**os.environ, **(env or {})}
    cmd_str = " ".join(command)
    start_time = time.monotonic()

    try:
        result = subprocess.run(
            command,
            cwd=cwd,
            timeout=timeout_sec,
            env=merged_env,
            input=input_text,
            capture_output=True,
            text=True,
        )
        duration = time.monotonic() - start_time

        if result.returncode == 0:
            return TaskResult(
                status=TaskStatus.SUCCESS,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=0,
                duration_sec=duration,
                command=cmd_str,
            )
        else:
            return TaskResult(
                status=TaskStatus.ERROR,
                stdout=result.stdout,
                stderr=result.stderr,
                exit_code=result.returncode,
                duration_sec=duration,
                command=cmd_str,
                error_message=f"Non-zero exit code: {result.returncode}",
            )

    except subprocess.TimeoutExpired as e:
        duration = time.monotonic() - start_time
        partial = ""
        if capture_partial and e.stdout:
            partial = e.stdout if isinstance(e.stdout, str) else e.stdout.decode("utf-8", errors="replace")
        stderr_out = ""
        if e.stderr:
            stderr_out = e.stderr if isinstance(e.stderr, str) else e.stderr.decode("utf-8", errors="replace")

        return TaskResult(
            status=TaskStatus.TIMEOUT,
            stderr=stderr_out,
            partial_output=partial,
            duration_sec=duration,
            command=cmd_str,
            error_message=f"Timeout after {timeout_sec}s",
        )

    except FileNotFoundError:
        duration = time.monotonic() - start_time
        return TaskResult(
            status=TaskStatus.ERROR,
            duration_sec=duration,
            command=cmd_str,
            error_message=f"Command not found: {command[0]}",
        )

    except PermissionError:
        duration = time.monotonic() - start_time
        return TaskResult(
            status=TaskStatus.ERROR,
            duration_sec=duration,
            command=cmd_str,
            error_message=f"Permission denied: {command[0]}",
        )

    except OSError as e:
        duration = time.monotonic() - start_time
        return TaskResult(
            status=TaskStatus.ERROR,
            duration_sec=duration,
            command=cmd_str,
            error_message=f"OS error: {e}",
        )


class RetryPolicy:
    """Decides whether to retry a failed task and how long to wait.

    Uses exponential backoff: retry 1 → 5s, retry 2 → 30s, retry 3 → 120s.
    """

    def __init__(self, max_retries: int = 2, base_delay_sec: float = 5.0):
        self.max_retries = max_retries
        self.base_delay_sec = base_delay_sec

    def should_retry(self, result: TaskResult, attempt: int) -> bool:
        """Return True if this failure is worth retrying."""
        if attempt >= self.max_retries:
            return False
        # Don't retry: command not found, permission denied
        if "not found" in result.error_message.lower():
            return False
        if "permission denied" in result.error_message.lower():
            return False
        # Retry: timeout, non-zero exit (transient failures)
        return result.status in (TaskStatus.TIMEOUT, TaskStatus.ERROR)

    def delay_sec(self, attempt: int) -> float:
        """Exponential backoff delay for the given attempt (0-indexed)."""
        return self.base_delay_sec * (6 ** attempt)  # 5, 30, 180


def compute_content_hash(file_path: Path) -> str:
    """SHA-256 hash of file content for artifact integrity checking."""
    sha256 = hashlib.sha256()
    with open(file_path, "rb") as f:
        for chunk in iter(lambda: f.read(8192), b""):
            sha256.update(chunk)
    return sha256.hexdigest()

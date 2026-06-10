"""Base adapter interface for all specialist CLIs.

Every specialist (Claude Code, Codex, Stitch, Grok Build, Antigravity)
implements this ABC. The runner/scheduler only talks to this interface —
never to subprocess directly.

Design decisions:
- async def run() — ready for DAG scheduler (Sprint 6)
- TaskInput carries feedback + session_id — ready for feedback loop (Sprint 4)
- TaskOutput carries session_id back — enables resume for stateful specialists
- health_check() — fail fast if CLI not installed
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, Optional


class TaskStatus(str, Enum):
    """Outcome of a specialist task execution."""
    SUCCESS = "success"
    ERROR = "error"
    TIMEOUT = "timeout"


@dataclass
class TaskInput:
    """Everything a specialist needs to execute a task.

    Fields are intentionally broad — not every specialist uses every field.
    Adapters pick what they need and ignore the rest.
    """
    task_id: str
    prompt: str
    artifacts: list[Path] = field(default_factory=list)
    rubric: dict = field(default_factory=dict)

    # Feedback from kick-back (Sprint 4)
    feedback: Optional[str] = None
    feedback_artifacts: list[Path] = field(default_factory=list)

    # Resume support
    session_id: Optional[str] = None

    # Git isolation (Sprint 7)
    branch: Optional[str] = None
    worktree_path: Optional[Path] = None

    # Execution constraints
    timeout_sec: int = 600
    max_retries: int = 2

    # Environment overrides
    env_vars: dict[str, str] = field(default_factory=dict)

    # Specialist-specific config (pass-through)
    extra: dict[str, Any] = field(default_factory=dict)

    @property
    def input_hash(self) -> str:
        """Short hash of prompt + artifact paths for log correlation."""
        content = self.prompt + "".join(str(a) for a in self.artifacts)
        return hashlib.sha256(content.encode()).hexdigest()[:16]


@dataclass
class TaskOutput:
    """Structured result from a specialist execution.

    Every adapter returns this — success or failure.
    Contains enough info for grading, feedback, cost tracking, and resume.
    """
    status: TaskStatus
    artifacts: list[Path] = field(default_factory=list)
    stdout: str = ""
    stderr: str = ""
    duration_sec: float = 0.0
    estimated_cost_usd: float = 0.0

    # Resume support — specialist returns its session ID for next call
    session_id: Optional[str] = None

    # Error info
    error_message: str = ""
    exit_code: Optional[int] = None

    # Token usage (if available from API-based specialists)
    token_usage: Optional[dict[str, int]] = None

    # Specialist-specific metadata (pass-through)
    metadata: dict[str, Any] = field(default_factory=dict)

    # Command executed (for logging / debugging)
    command: str = ""

    @property
    def succeeded(self) -> bool:
        return self.status == TaskStatus.SUCCESS

    def to_dict(self) -> dict:
        """Serialize for logging / state persistence."""
        return {
            "status": self.status.value,
            "artifacts": [str(p) for p in self.artifacts],
            "stdout": self.stdout[:2000],
            "stderr": self.stderr[:2000],
            "duration_sec": round(self.duration_sec, 2),
            "estimated_cost_usd": round(self.estimated_cost_usd, 6),
            "session_id": self.session_id,
            "error_message": self.error_message,
            "exit_code": self.exit_code,
            "token_usage": self.token_usage,
            "command": self.command,
        }


class SpecialistAdapter(ABC):
    """Abstract base class for specialist CLI adapters.

    Contract:
    - run() executes a task and returns structured output
    - health_check() validates the CLI is available
    - supports_resume() indicates if this specialist can resume sessions
    - name property returns a unique identifier

    Implementation guide:
    - Use asyncio.create_subprocess_exec for async subprocess calls
    - Return TaskOutput for EVERY outcome (success, error, timeout)
    - Estimate cost in estimated_cost_usd (time-based if no token info)
    - Set session_id in output if specialist supports resume
    """

    @property
    @abstractmethod
    def name(self) -> str:
        """Unique identifier for this specialist (e.g., 'claude_code')."""
        ...

    @abstractmethod
    async def run(self, task: TaskInput) -> TaskOutput:
        """Execute a task and return structured output.

        Must handle all errors internally — never raise exceptions.
        Return TaskOutput with appropriate status for any failure.
        """
        ...

    @abstractmethod
    async def health_check(self) -> bool:
        """Check if the specialist CLI is installed and accessible.

        Returns True if ready, False otherwise.
        Should be fast (< 5 seconds).
        """
        ...

    @abstractmethod
    def supports_resume(self) -> bool:
        """Whether this specialist supports session resume.

        If True, run() should return session_id in TaskOutput,
        and accept session_id in TaskInput for continuation.
        """
        ...

    @property
    def cost_rate_per_sec(self) -> float:
        """Estimated cost per second for time-based billing.

        Override in subclasses for more accurate estimates.
        Default: $0.003/sec (~$10.8/hr).
        """
        return 0.003

    async def _run_subprocess(
        self,
        command: list[str],
        *,
        cwd: Optional[Path] = None,
        timeout_sec: int = 600,
        env: Optional[dict[str, str]] = None,
        input_text: Optional[str] = None,
    ) -> TaskOutput:
        """Helper: run a subprocess command with full error handling, sandboxing, and credential isolation."""
        from maestro.sandbox.credential_isolator import get_isolated_env
        from maestro.sandbox.container import SandboxContainer
        from maestro.sandbox.network_policy import get_network_policy

        # Credential Isolation: Only allowlist system and specialist environment variables
        isolated_os_env = get_isolated_env(self.name, dict(os.environ))
        merged_env = {**isolated_os_env, **(env or {})}

        target_cwd = cwd or Path(".")

        # Sandbox routing
        sandbox_enabled = os.environ.get("MAESTRO_SANDBOX", "0") == "1"
        if sandbox_enabled:
            net_policy = get_network_policy(self.name, command)
            sandbox = SandboxContainer(target_cwd)
            return await sandbox.run_in_sandbox(
                command,
                cwd=target_cwd,
                env=merged_env,
                timeout_sec=timeout_sec,
                network_policy=net_policy,
                input_text=input_text,
            )

        start_time = time.monotonic()
        cmd_str = " ".join(command)

        try:
            process = await asyncio.create_subprocess_exec(
                *command,
                cwd=target_cwd,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=asyncio.subprocess.PIPE if input_text else None,
                env=merged_env,
            )

            try:
                stdout_bytes, stderr_bytes = await asyncio.wait_for(
                    process.communicate(
                        input=input_text.encode() if input_text else None
                    ),
                    timeout=timeout_sec,
                )
            except asyncio.TimeoutError:
                process.kill()
                await process.wait()
                duration = time.monotonic() - start_time
                return TaskOutput(
                    status=TaskStatus.TIMEOUT,
                    duration_sec=duration,
                    estimated_cost_usd=duration * self.cost_rate_per_sec,
                    error_message=f"Timeout after {timeout_sec}s",
                    command=cmd_str,
                )

            duration = time.monotonic() - start_time
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if process.returncode == 0:
                return TaskOutput(
                    status=TaskStatus.SUCCESS,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=0,
                    duration_sec=duration,
                    estimated_cost_usd=duration * self.cost_rate_per_sec,
                    command=cmd_str,
                )
            else:
                return TaskOutput(
                    status=TaskStatus.ERROR,
                    stdout=stdout,
                    stderr=stderr,
                    exit_code=process.returncode,
                    duration_sec=duration,
                    estimated_cost_usd=duration * self.cost_rate_per_sec,
                    error_message=f"Non-zero exit code: {process.returncode}",
                    command=cmd_str,
                )

        except FileNotFoundError:
            duration = time.monotonic() - start_time
            return TaskOutput(
                status=TaskStatus.ERROR,
                duration_sec=duration,
                error_message=f"Command not found: {command[0]}",
                command=cmd_str,
            )

        except PermissionError:
            duration = time.monotonic() - start_time
            return TaskOutput(
                status=TaskStatus.ERROR,
                duration_sec=duration,
                error_message=f"Permission denied: {command[0]}",
                command=cmd_str,
            )

        except OSError as e:
            duration = time.monotonic() - start_time
            return TaskOutput(
                status=TaskStatus.ERROR,
                duration_sec=duration,
                error_message=f"OS error: {e}",
                command=cmd_str,
            )

    def _build_prompt_with_feedback(self, task: TaskInput) -> str:
        """Merge original prompt with feedback for kick-back iterations.

        Used by stateless adapters that can't resume sessions.
        """
        if not task.feedback:
            return task.prompt

        parts = [
            "# Original Task",
            task.prompt,
            "",
            "# Feedback from Previous Attempt",
            "The previous output did not meet the quality rubric. "
            "Please address the following issues:",
            "",
            task.feedback,
        ]

        if task.feedback_artifacts:
            parts.extend([
                "",
                "# Previous Output Artifacts (for reference)",
                *[f"- {a}" for a in task.feedback_artifacts],
            ])

        return "\n".join(parts)

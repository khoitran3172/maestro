"""Claude Code specialist adapter.

Claude Code has native session resume via `claude --resume <session_id>`.
This makes it the most capable specialist for iterative feedback loops.

CLI reference:
  claude --print --output-format json "prompt"
  claude --resume <session_id> --print "follow-up prompt"
"""

from __future__ import annotations

import json
import os
import shutil
from pathlib import Path
from typing import Optional

from maestro.adapters.base import SpecialistAdapter, TaskInput, TaskOutput, TaskStatus


class ClaudeCodeAdapter(SpecialistAdapter):
    """Adapter for Claude Code CLI.

    Features:
    - Session resume via --resume flag (native stateful conversations)
    - JSON output format for structured parsing
    - Automatic prompt injection of feedback + previous artifacts
    """

    def __init__(
        self,
        cli_path: Optional[str] = None,
        default_model: str = "sonnet",
    ):
        self._cli_path = cli_path or os.environ.get("CLAUDE_CODE_CLI", "claude")
        self._default_model = default_model

    @property
    def name(self) -> str:
        return "claude_code"

    @property
    def cost_rate_per_sec(self) -> float:
        return 0.005  # ~$18/hr (Opus-level usage)

    def supports_resume(self) -> bool:
        return True

    async def health_check(self) -> bool:
        """Check if Claude Code CLI is installed."""
        result = await self._run_subprocess(
            [self._cli_path, "--version"],
            timeout_sec=10,
        )
        return result.succeeded

    async def run(self, task: TaskInput) -> TaskOutput:
        """Execute a task via Claude Code CLI.

        If task.session_id is set, resumes that session instead of starting new.
        Feedback is injected into the prompt for kick-back iterations.
        """
        command = self._build_command(task)
        env = {**task.env_vars}

        # Set working directory to worktree if Git isolation is active
        cwd = task.worktree_path or task.extra.get("cwd")

        result = await self._run_subprocess(
            command,
            cwd=cwd,
            timeout_sec=task.timeout_sec,
            env=env if env else None,
        )

        # Try to extract session_id from JSON output for future resume
        session_id = self._extract_session_id(result.stdout)
        result.session_id = session_id or task.session_id

        # Detect output artifacts
        result.artifacts = self._detect_artifacts(result.stdout, cwd)

        return result

    def _build_command(self, task: TaskInput) -> list[str]:
        """Build the claude CLI command."""
        cmd = [self._cli_path]

        # Resume existing session or start new
        if task.session_id:
            cmd.extend(["--resume", task.session_id])
        
        # Output format
        cmd.extend(["--print", "--output-format", "json"])

        # Build the prompt
        prompt = self._build_prompt_with_feedback(task)

        # Add artifact context
        if task.artifacts:
            artifact_list = "\n".join(f"- {a}" for a in task.artifacts)
            prompt = f"{prompt}\n\n# Input Artifacts\n{artifact_list}"

        # Add rubric
        if task.rubric:
            rubric_text = json.dumps(task.rubric, indent=2)
            prompt = f"{prompt}\n\n# Quality Rubric\n```json\n{rubric_text}\n```"

        cmd.append(prompt)
        return cmd

    def _extract_session_id(self, stdout: str) -> Optional[str]:
        """Try to extract session ID from Claude Code JSON output."""
        try:
            # Claude Code JSON output may contain session info
            for line in stdout.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        # Look for session_id in various possible locations
                        for key in ("session_id", "sessionId", "conversation_id"):
                            if key in data:
                                return str(data[key])
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass
        return None

    def _detect_artifacts(
        self, stdout: str, cwd: Optional[Path]
    ) -> list[Path]:
        """Detect files created/modified by Claude Code from its output."""
        artifacts = []
        if not cwd:
            return artifacts

        try:
            for line in stdout.strip().split("\n"):
                line = line.strip()
                if not line:
                    continue
                try:
                    data = json.loads(line)
                    if isinstance(data, dict):
                        # Look for file paths in output
                        for key in ("file", "path", "created", "modified"):
                            if key in data and isinstance(data[key], str):
                                p = Path(cwd) / data[key]
                                if p.exists():
                                    artifacts.append(p)
                except json.JSONDecodeError:
                    continue
        except Exception:
            pass

        return artifacts

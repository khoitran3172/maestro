"""Codex specialist adapter.

Codex is OpenAI's code generation CLI. It's stateless — feedback is injected
by embedding the previous diff + feedback text into the new prompt.

CLI reference:
  codex exec "prompt"
  codex exec --approval-mode full-auto "prompt"
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from maestro.adapters.base import SpecialistAdapter, TaskInput, TaskOutput, TaskStatus


class CodexAdapter(SpecialistAdapter):
    """Adapter for OpenAI Codex CLI.

    Features:
    - Full-auto mode for non-interactive execution
    - Feedback via prompt injection with previous diff
    - Workspace-aware (respects worktree for Git isolation)
    """

    def __init__(
        self,
        cli_path: Optional[str] = None,
        approval_mode: str = "full-auto",
    ):
        self._cli_path = cli_path or os.environ.get("CODEX_CLI", "codex")
        self._approval_mode = approval_mode

    @property
    def name(self) -> str:
        return "codex"

    @property
    def cost_rate_per_sec(self) -> float:
        return 0.003  # ~$10.8/hr

    def supports_resume(self) -> bool:
        return False  # Stateless — feedback via prompt injection

    async def health_check(self) -> bool:
        """Check if Codex CLI is installed."""
        result = await self._run_subprocess(
            [self._cli_path, "--version"],
            timeout_sec=10,
        )
        return result.succeeded

    async def run(self, task: TaskInput) -> TaskOutput:
        """Execute a task via Codex CLI."""
        command = self._build_command(task)
        cwd = task.worktree_path or task.extra.get("cwd")

        result = await self._run_subprocess(
            command,
            cwd=cwd,
            timeout_sec=task.timeout_sec,
            env=task.env_vars if task.env_vars else None,
        )

        return result

    def _build_command(self, task: TaskInput) -> list[str]:
        """Build the codex CLI command."""
        cmd = [self._cli_path, "exec"]

        # Set approval mode
        cmd.extend(["--approval-mode", self._approval_mode])

        # Build prompt with feedback context
        prompt = self._build_prompt_with_feedback(task)

        # Add artifact context
        if task.artifacts:
            artifact_list = "\n".join(f"- {a}" for a in task.artifacts)
            prompt = f"{prompt}\n\n# Input Files\n{artifact_list}"

        # Add rubric for quality guidance
        if task.rubric:
            rubric_text = json.dumps(task.rubric, indent=2)
            prompt = f"{prompt}\n\n# Quality Requirements\n```json\n{rubric_text}\n```"

        # Inject previous diff if available (for kick-back)
        if task.feedback and task.feedback_artifacts:
            prompt = self._inject_diff_context(prompt, task)

        cmd.append(prompt)
        return cmd

    def _inject_diff_context(self, prompt: str, task: TaskInput) -> str:
        """Add previous attempt's diff to prompt for better feedback context."""
        diff_parts = [prompt, "", "# Previous Attempt Diff"]

        for artifact_path in task.feedback_artifacts:
            if artifact_path.exists() and artifact_path.suffix in (".py", ".ts", ".js", ".md"):
                try:
                    content = artifact_path.read_text(encoding="utf-8")
                    # Cap file content to avoid prompt bloat
                    if len(content) > 3000:
                        content = content[:3000] + "\n... (truncated)"
                    diff_parts.extend([
                        f"\n## {artifact_path.name}",
                        f"```\n{content}\n```",
                    ])
                except (OSError, UnicodeDecodeError):
                    pass

        return "\n".join(diff_parts)

"""Grok Build specialist adapter.

Grok Build is xAI's agentic coding tool. It's branch-aware — each task
runs on its own Git branch, and feedback means selecting a branch + refining.

CLI is still in beta — flags may change. Adapter pattern isolates these changes.

CLI reference (beta):
  grok build "prompt"
  grok build --branch <name> "prompt"
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from maestro.adapters.base import SpecialistAdapter, TaskInput, TaskOutput, TaskStatus


class GrokBuildAdapter(SpecialistAdapter):
    """Adapter for Grok Build CLI (xAI).

    Features:
    - Branch-aware: each task can target a specific Git branch
    - Multi-branch exploration: Grok can generate N branches for comparison
    - Feedback: select best branch + refine prompt
    - Beta CLI: flags may change — changes isolated to this file

    Note: CLI is beta as of 2026. Verify flags with `grok build --help`.
    """

    def __init__(
        self,
        cli_path: Optional[str] = None,
        num_branches: int = 1,
    ):
        self._cli_path = cli_path or os.environ.get("GROK_BUILD_CLI", "grok")
        self._num_branches = num_branches

    @property
    def name(self) -> str:
        return "grok_build"

    @property
    def cost_rate_per_sec(self) -> float:
        return 0.003  # ~$10.8/hr

    def supports_resume(self) -> bool:
        return False  # Branch-based — feedback = new prompt on selected branch

    async def health_check(self) -> bool:
        """Check if Grok Build CLI is installed."""
        result = await self._run_subprocess(
            [self._cli_path, "build", "--help"],
            timeout_sec=10,
        )
        return result.succeeded

    async def run(self, task: TaskInput) -> TaskOutput:
        """Execute a task via Grok Build CLI."""
        command = self._build_command(task)
        cwd = task.worktree_path or task.extra.get("cwd")

        result = await self._run_subprocess(
            command,
            cwd=cwd,
            timeout_sec=task.timeout_sec,
            env=task.env_vars if task.env_vars else None,
        )

        # Record branch info in metadata
        if result.succeeded:
            result.metadata["branch"] = task.branch or f"maestro/grok-{task.task_id}"
            result.metadata["num_branches"] = self._num_branches

        return result

    def _build_command(self, task: TaskInput) -> list[str]:
        """Build the grok build CLI command."""
        cmd = [self._cli_path, "build"]

        # Branch targeting
        branch = task.branch or f"maestro/grok-{task.task_id}"
        cmd.extend(["--branch", branch])

        # Multi-branch exploration
        if self._num_branches > 1:
            cmd.extend(["--num-branches", str(self._num_branches)])

        # Build prompt
        prompt = self._build_prompt_with_feedback(task)

        # Add context files
        if task.artifacts:
            for artifact in task.artifacts:
                if artifact.exists():
                    cmd.extend(["--context", str(artifact)])

        cmd.append(prompt)
        return cmd

"""Antigravity specialist adapter.

Antigravity is Google's agentic coding/deployment tool. It handles
deployment to Cloud Run and other GCP services.

IMPORTANT: Deploy actions require human approval before touching
production infrastructure (Cloud Run, GCS, etc.).

CLI is beta — flags may change frequently.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from maestro.adapters.base import SpecialistAdapter, TaskInput, TaskOutput, TaskStatus


class AntigravityAdapter(SpecialistAdapter):
    """Adapter for Antigravity CLI (Google).

    Features:
    - Deploy capability (Cloud Run, GCS, etc.)
    - Human approval required before deploy actions
    - Beta CLI: flags may change — changes isolated to this file

    Safety:
    - Deploy commands are NEVER auto-approved
    - Adapter returns TaskOutput with status requiring manual confirmation
    """

    def __init__(
        self,
        cli_path: Optional[str] = None,
        auto_approve_deploy: bool = False,  # NEVER True in production
    ):
        self._cli_path = cli_path or os.environ.get("ANTIGRAVITY_CLI", "antigravity")
        self._auto_approve_deploy = auto_approve_deploy

    @property
    def name(self) -> str:
        return "antigravity"

    @property
    def cost_rate_per_sec(self) -> float:
        return 0.002  # ~$7.2/hr

    def supports_resume(self) -> bool:
        return False

    async def health_check(self) -> bool:
        """Check if Antigravity CLI is installed."""
        result = await self._run_subprocess(
            [self._cli_path, "--version"],
            timeout_sec=10,
        )
        return result.succeeded

    async def run(self, task: TaskInput) -> TaskOutput:
        """Execute a task via Antigravity CLI.

        If task involves deployment, requires human approval flag.
        """
        is_deploy = self._is_deploy_task(task)

        # Safety gate: deploy tasks need explicit approval
        if is_deploy and not self._auto_approve_deploy:
            return TaskOutput(
                status=TaskStatus.ERROR,
                error_message=(
                    "Deploy task requires human approval. "
                    "Set 'approve_deploy: true' in task.extra after reviewing the plan."
                ),
                metadata={"requires_approval": True, "task_type": "deploy"},
            )

        command = self._build_command(task)
        cwd = task.worktree_path or task.extra.get("cwd")

        result = await self._run_subprocess(
            command,
            cwd=cwd,
            timeout_sec=task.timeout_sec,
            env=task.env_vars if task.env_vars else None,
        )

        if is_deploy:
            result.metadata["deploy"] = True

        return result

    def _build_command(self, task: TaskInput) -> list[str]:
        """Build the antigravity CLI command."""
        cmd = [self._cli_path]

        # Build prompt
        prompt = self._build_prompt_with_feedback(task)

        # Add context files
        if task.artifacts:
            for artifact in task.artifacts:
                if artifact.exists():
                    cmd.extend(["--context", str(artifact)])

        cmd.append(prompt)
        return cmd

    def _is_deploy_task(self, task: TaskInput) -> bool:
        """Detect if this task involves deployment actions."""
        deploy_keywords = ["deploy", "cloud run", "gcs", "firebase", "publish", "release"]
        prompt_lower = task.prompt.lower()
        return any(kw in prompt_lower for kw in deploy_keywords)

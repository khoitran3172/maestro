"""Stitch specialist adapter.

Stitch generates UI design images from text prompts. It's fully stateless —
feedback is implemented by sending the previous image + feedback text back.

Stitch is the primary reason multi-modal grading (Sprint 5) exists,
since its output is PNG/JPG images that can't be text-graded.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional

from maestro.adapters.base import SpecialistAdapter, TaskInput, TaskOutput, TaskStatus


class StitchAdapter(SpecialistAdapter):
    """Adapter for Stitch (UI design generation).

    Features:
    - Image output (.png/.jpg) — requires vision grading
    - Stateless — feedback via prompt + previous image re-send
    - Output detection from CLI stdout or specified output directory
    """

    def __init__(
        self,
        cli_path: Optional[str] = None,
        output_format: str = "png",
    ):
        self._cli_path = cli_path or os.environ.get("STITCH_CLI", "stitch")
        self._output_format = output_format

    @property
    def name(self) -> str:
        return "stitch"

    @property
    def cost_rate_per_sec(self) -> float:
        return 0.004  # ~$14.4/hr (image generation)

    def supports_resume(self) -> bool:
        return False  # Fully stateless

    async def health_check(self) -> bool:
        """Check if Stitch CLI is installed."""
        result = await self._run_subprocess(
            [self._cli_path, "--version"],
            timeout_sec=10,
        )
        return result.succeeded

    async def run(self, task: TaskInput) -> TaskOutput:
        """Execute a design task via Stitch CLI."""
        command = self._build_command(task)
        cwd = task.worktree_path or task.extra.get("cwd")

        # Determine output directory
        output_dir = self._get_output_dir(task, cwd)
        if output_dir:
            output_dir.mkdir(parents=True, exist_ok=True)

        result = await self._run_subprocess(
            command,
            cwd=cwd,
            timeout_sec=task.timeout_sec,
            env=task.env_vars if task.env_vars else None,
        )

        # Detect image output artifacts
        if result.succeeded and output_dir:
            result.artifacts = self._find_image_artifacts(output_dir)

        return result

    def _build_command(self, task: TaskInput) -> list[str]:
        """Build the stitch CLI command."""
        cmd = [self._cli_path]

        # Build design prompt
        prompt = self._build_design_prompt(task)

        # Add output format
        cmd.extend(["--format", self._output_format])

        # Add output directory if specified
        output_dir = task.extra.get("output_dir")
        if output_dir:
            cmd.extend(["--output", str(output_dir)])

        # Reference images (previous designs for feedback)
        if task.feedback_artifacts:
            for ref_img in task.feedback_artifacts:
                if ref_img.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp"):
                    cmd.extend(["--reference", str(ref_img)])

        cmd.append(prompt)
        return cmd

    def _build_design_prompt(self, task: TaskInput) -> str:
        """Build a design-specific prompt with feedback context."""
        if not task.feedback:
            return task.prompt

        # For Stitch, feedback is about visual quality
        parts = [
            "# Design Task (Revision)",
            "",
            "## Original Brief",
            task.prompt,
            "",
            "## Revision Notes",
            "The previous design needs the following improvements:",
            "",
            task.feedback,
            "",
            "Please generate an improved version addressing all feedback points.",
        ]

        # Add rubric context
        if task.rubric:
            parts.extend([
                "",
                "## Quality Criteria",
                *[f"- {k}: {v}" for k, v in task.rubric.items()],
            ])

        return "\n".join(parts)

    def _get_output_dir(
        self, task: TaskInput, cwd: Optional[Path]
    ) -> Optional[Path]:
        """Determine output directory for design artifacts."""
        if "output_dir" in task.extra:
            return Path(task.extra["output_dir"])
        if cwd:
            output_dir = Path(cwd) / ".maestro" / "designs" / task.task_id
            return output_dir
        return None

    def _find_image_artifacts(self, directory: Path) -> list[Path]:
        """Find image files in the output directory."""
        image_extensions = {".png", ".jpg", ".jpeg", ".webp", ".svg"}
        artifacts = []
        if directory.exists():
            for f in directory.iterdir():
                if f.is_file() and f.suffix.lower() in image_extensions:
                    artifacts.append(f)
        return sorted(artifacts)

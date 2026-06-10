"""Orchestrates multi-modal grading: deterministic first, then routing text/vision LLM checks."""

from __future__ import annotations

from pathlib import Path

from maestro.adapters.base import TaskInput, TaskOutput
from maestro.grader.base import GradeResult, RubricFailure
from maestro.grader.deterministic import DeterministicGrader
from maestro.grader.text_grader import TextGrader
from maestro.grader.vision_grader import VisionGrader
from maestro.grader.composite import CompositeGrader


class GraderPipeline:
    """Grader pipeline executing deterministic local checks first, then routing to LLM graders."""

    def __init__(self):
        self.deterministic = DeterministicGrader()
        self.text_grader = TextGrader()
        self.vision_grader = VisionGrader()

    async def grade(
        self,
        task_input: TaskInput,
        task_output: TaskOutput,
        workspace: Path,
    ) -> GradeResult:
        """Route outputs through local checkers, then vision/text models if successful."""
        # 1. Base execution checks
        if not task_output.succeeded:
            return GradeResult(
                score=0.0,
                passed=False,
                failures=[
                    RubricFailure(
                        item="execution",
                        message=task_output.error_message or "Execution failed.",
                        expected="success",
                        actual=task_output.status.value,
                    )
                ],
                feedback="Specialist run did not succeed. Skipping all other evaluations.",
            )

        # 2. Local/Deterministic checking (Compilations, tests, lint checks)
        deterministic_result = await self.deterministic.grade(task_input, task_output, workspace)
        if not deterministic_result.passed:
            # Short-circuit failure immediately to avoid expensive vision/text API calls!
            print("    ❌ Deterministic checks failed. Short-circuiting LLM grading to save costs.")
            return deterministic_result

        # 3. Detect artifact types to route grading
        has_images = any(
            p.exists() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif")
            for p in task_output.artifacts
        )
        has_text = any(
            p.exists() and p.suffix.lower() in (".py", ".ts", ".js", ".json", ".md", ".txt", ".html", ".css")
            for p in task_output.artifacts
        )

        sub_results: list[GradeResult] = [deterministic_result]

        # Call vision grader if images are present
        if has_images:
            print("    👁️  Visual output detected. Routing to Vision Grader...")
            vision_result = await self.vision_grader.grade(task_input, task_output)
            sub_results.append(vision_result)

        # Call text grader if source code/text is present
        if has_text:
            print("    📝 Code/Text output detected. Routing to Text Grader...")
            text_result = await self.text_grader.grade(task_input, task_output)
            sub_results.append(text_result)

        # 4. Consolidate results
        return CompositeGrader.aggregate(sub_results, fail_fast=True)

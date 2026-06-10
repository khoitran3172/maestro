"""LLM-based visual/image quality grader using Claude Vision."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from maestro.adapters.base import TaskInput, TaskOutput
from maestro.grader.base import GradeResult, RubricFailure
from maestro.grader.llm_client import call_anthropic_api


class VisionGrader:
    """Grades UI designs, images, and layouts using Claude Vision."""

    async def grade(self, task_input: TaskInput, task_output: TaskOutput) -> GradeResult:
        """Evaluate visual outputs (PNG/JPG) against design rubric rules."""
        if not task_output.succeeded:
            return GradeResult(
                score=0.0,
                passed=False,
                failures=[
                    RubricFailure(
                        item="execution",
                        message=task_output.error_message or "Specialist run failed.",
                        expected="success",
                        actual=task_output.status.value,
                    )
                ],
                feedback="Specialist run failed. Vision check skipped.",
            )

        # Detect visual artifacts (.png, .jpg, .jpeg, .webp)
        image_paths = [
            p for p in task_output.artifacts
            if p.exists() and p.suffix.lower() in (".png", ".jpg", ".jpeg", ".webp", ".gif")
        ]

        if not image_paths:
            return GradeResult(
                score=0.0,
                passed=False,
                failures=[
                    RubricFailure(
                        item="images",
                        message="No image artifacts found for visual grading.",
                        expected="image file",
                        actual="none",
                    )
                ],
                feedback="Visual grading was requested but no images were found.",
            )

        prompt = f"""You are a pixel-perfect design QA specialist. Evaluate the attached screenshots/images against the rubric.

# Original Task Prompt:
{task_input.prompt}

# Quality Rubric:
{json.dumps(task_input.rubric, indent=2)}

# Execution stdout context:
{task_output.stdout[:1000]}

Please inspect the attached images. Grade their layout, responsiveness, colors, spacing, and alignment against the rubric requirements.

Return a valid JSON object matching the following structure. Do not output any markdown formatting, wrappers (like ```json), or explanation outside of the JSON block.

JSON Schema:
{{
  "score": float, // 0.0 to 100.0
  "passed": boolean,
  "failures": [
    {{
      "item": "string (name of rubric check item)",
      "message": "string (explanation of failure)",
      "expected": "any (expected value)",
      "actual": "any (actual value)"
    }}
  ],
  "feedback": "string (general feedback comments)"
}}
"""

        # Call Anthropic API with attached images
        response = await call_anthropic_api(
            prompt=prompt,
            image_paths=image_paths,
            model="claude-3-5-sonnet-20241022",
        )

        return self._parse_response(response)

    def _parse_response(self, response_text: str) -> GradeResult:
        """Parse LLM JSON block into a GradeResult object."""
        try:
            # Clean possible markdown code fences
            cleaned = response_text.strip()
            if cleaned.startswith("```json"):
                cleaned = cleaned[7:]
            if cleaned.startswith("```"):
                cleaned = cleaned[3:]
            if cleaned.endswith("```"):
                cleaned = cleaned[:-3]
            cleaned = cleaned.strip()

            data = json.loads(cleaned)
            failures = [
                RubricFailure(
                    item=f.get("item", "rubric_check"),
                    message=f.get("message", "Rubric failure reported"),
                    expected=f.get("expected"),
                    actual=f.get("actual"),
                )
                for f in data.get("failures", [])
            ]
            return GradeResult(
                score=float(data.get("score", 0.0)),
                passed=bool(data.get("passed", False)),
                failures=failures,
                feedback=data.get("feedback"),
            )
        except Exception as e:
            return GradeResult(
                score=0.0,
                passed=False,
                failures=[
                    RubricFailure(
                        item="grader_parsing",
                        message=f"Could not parse LLM grader response: {e}. Raw response: {response_text[:100]}",
                    )
                ],
                feedback=f"LLM Response parsing error: {e}",
            )

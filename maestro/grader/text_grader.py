"""LLM-based text and codebase quality grader using Claude."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional

from maestro.adapters.base import TaskInput, TaskOutput
from maestro.grader.base import GradeResult, RubricFailure
from maestro.grader.llm_client import call_anthropic_api


class TextGrader:
    """Grades source code, configurations, and documentations using Claude Sonnet."""

    async def grade(self, task_input: TaskInput, task_output: TaskOutput) -> GradeResult:
        """Analyze text artifacts and specialist output logs against rubric requirements."""
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
                feedback="Specialist run did not succeed. Skipping text grading.",
            )

        # Gather code artifact contents to provide context to the grader
        artifact_contents = []
        for artifact_path in task_output.artifacts:
            if artifact_path.exists() and artifact_path.suffix in (".py", ".ts", ".js", ".json", ".md", ".txt", ".html", ".css"):
                try:
                    content = artifact_path.read_text(encoding="utf-8")
                    if len(content) > 4000:
                        content = content[:4000] + "\n... (truncated)"
                    artifact_contents.append(
                        f"### File: {artifact_path.name}\n```\n{content}\n```"
                    )
                except Exception:
                    pass

        artifacts_context = "\n\n".join(artifact_contents) if artifact_contents else "No text files generated."

        # System and User Prompt building
        prompt = f"""You are a strict code quality grader. Evaluate the specialist's output against the rubric.

# Original Task Prompt:
{task_input.prompt}

# Rubric:
{json.dumps(task_input.rubric, indent=2)}

# Specialist Execution Log (stdout):
{task_output.stdout[:3000]}

# Specialist Error Log (stderr):
{task_output.stderr[:2000]}

# Code/Text Artifacts Generated:
{artifacts_context}

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

        # Call Anthropic API
        response = await call_anthropic_api(
            prompt=prompt,
            model="claude-3-5-sonnet-20241022",
        )

        return self._parse_response(response)

    def _parse_response(self, response_text: str) -> GradeResult:
        """Parse LLM JSON block into a GradeResult object."""
        try:
            # Clean possible markdown code fences if LLM didn't listen
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

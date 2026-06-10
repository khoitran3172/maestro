"""Constructs structured markdown feedback for specialist kick-back iterations."""

from __future__ import annotations

from maestro.adapters.base import TaskOutput
from maestro.grader import GradeResult


class FeedbackBuilder:
    """Builds markdown feedback prompts from quality check failures."""

    @staticmethod
    def build_feedback(
        grade_result: GradeResult,
        prev_output: TaskOutput,
    ) -> str:
        """Create a structured feedback string to instruct the specialist on retry."""
        parts = [
            "# Quality Rubric Failure",
            "The output from the previous attempt did not meet the quality rubric requirements.",
            f"Overall Score: {grade_result.score:.1f}/100.0",
            "",
            "## Specific Failures to Address:",
        ]

        if grade_result.failures:
            for failure in grade_result.failures:
                expected_part = (
                    f" (expected: {failure.expected}, actual: {failure.actual})"
                    if failure.expected is not None
                    else ""
                )
                parts.append(f"- **{failure.item}**: {failure.message}{expected_part}")
        else:
            parts.append("- No specific check failures listed, but quality verification did not pass.")

        if prev_output.stderr.strip():
            parts.extend([
                "",
                "## Error Output (stderr):",
                "```",
                prev_output.stderr.strip()[:1000],
                "```",
            ])

        if grade_result.feedback:
            parts.extend([
                "",
                "## Grader Feedback:",
                grade_result.feedback,
            ])

        parts.extend([
            "",
            "Please review the issues above, modify the files, and regenerate correct output.",
        ])

        return "\n".join(parts)

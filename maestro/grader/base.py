"""Core grading datatypes and base Grader implementation."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from maestro.adapters.base import TaskInput, TaskOutput


@dataclass
class RubricFailure:
    """Represents a single quality check failure."""
    item: str
    message: str
    expected: Optional[Any] = None
    actual: Optional[Any] = None

    def to_dict(self) -> dict:
        return {
            "item": self.item,
            "message": self.message,
            "expected": self.expected,
            "actual": self.actual,
        }


@dataclass
class GradeResult:
    """Detailed outcome of a quality grading run."""
    score: float  # 0.0 to 100.0
    passed: bool
    failures: list[RubricFailure] = field(default_factory=list)
    feedback: Optional[str] = None

    def to_dict(self) -> dict:
        return {
            "score": self.score,
            "passed": self.passed,
            "failures": [f.to_dict() for f in self.failures],
            "feedback": self.feedback,
        }


class Grader:
    """Grades specialist task outputs against rubrics and criteria."""

    async def grade(self, task_input: TaskInput, task_output: TaskOutput) -> GradeResult:
        """Evaluate task output against the input rubric.

        Ensures execution succeeded and checks all rubric items.
        """
        # 1. Check if execution failed or timed out
        if not task_output.succeeded:
            return GradeResult(
                score=0.0,
                passed=False,
                failures=[
                    RubricFailure(
                        item="execution",
                        message=task_output.error_message or f"Execution failed (status: {task_output.status})",
                        expected="success",
                        actual=task_output.status.value,
                    )
                ],
                feedback="Specialist execution failed. Unable to verify rubric.",
            )

        # 2. Check if output has any artifacts if requested
        failures: list[RubricFailure] = []
        rubric = task_input.rubric or {}

        # 3. Grade standard rubric items deterministically
        stdout_lower = task_output.stdout.lower()
        stderr_lower = task_output.stderr.lower()

        for item, value in rubric.items():
            if item == "builds" and value:
                # Look for typical build failure patterns in logs
                if "build fail" in stdout_lower or "compile error" in stderr_lower or "error:" in stderr_lower:
                    failures.append(
                        RubricFailure(
                            item="builds",
                            message="Compilation or build errors detected in logs.",
                            expected=True,
                            actual=False,
                        )
                    )
            elif item == "tests_pass" and value:
                # Look for test failure patterns
                if "fail" in stdout_lower or "failed" in stdout_lower:
                    failures.append(
                        RubricFailure(
                            item="tests_pass",
                            message="Test failure keywords found in execution logs.",
                            expected=True,
                            actual=False,
                        )
                    )
            elif item == "coverage" and isinstance(value, (int, float)):
                # Try to parse coverage pattern (e.g., "coverage: 75%")
                match = re.search(r"coverage[:\s]+([\d\.]+)%", stdout_lower)
                if match:
                    actual_cov = float(match.group(1)) / 100.0
                    if actual_cov < value:
                        failures.append(
                            RubricFailure(
                                item="coverage",
                                message=f"Coverage ({actual_cov:.1%}) is below required {value:.1%}",
                                expected=value,
                                actual=actual_cov,
                            )
                        )
                else:
                    failures.append(
                        RubricFailure(
                            item="coverage",
                            message="Coverage report could not be parsed from logs.",
                            expected=value,
                            actual=None,
                        )
                    )

        # Calculate score: percentage of passed rubric items
        total_checks = len(rubric)
        if total_checks > 0:
            failed_checks = len(failures)
            score = max(0.0, 100.0 * (1.0 - (failed_checks / total_checks)))
        else:
            score = 100.0

        passed = len(failures) == 0

        # Construct feedback
        if passed:
            feedback = "All rubric items passed successfully."
        else:
            feedback = "The output failed the following rubric checks:\n" + "\n".join(
                f"- {f.item}: {f.message}" for f in failures
            )

        return GradeResult(
            score=score,
            passed=passed,
            failures=failures,
            feedback=feedback,
        )

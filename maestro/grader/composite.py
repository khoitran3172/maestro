"""Aggregates multiple GradeResult instances into a single consensus."""

from __future__ import annotations

from maestro.grader.base import GradeResult, RubricFailure


class CompositeGrader:
    """Aggregates evaluations from deterministic, text, and vision checkers."""

    @staticmethod
    def aggregate(
        results: list[GradeResult],
        *,
        fail_fast: bool = True,
    ) -> GradeResult:
        """Combine scores and failures from multiple graders.

        Args:
            results: List of GradeResult objects to combine.
            fail_fast: If True, any failure in any grader causes overall failure.
                       If False, overall success depends on majority vote.
        """
        if not results:
            return GradeResult(
                score=100.0,
                passed=True,
                feedback="No grading evaluations performed.",
            )

        # Determine overall pass/fail status
        if fail_fast:
            passed = all(r.passed for r in results)
        else:
            # Majority vote
            passed_count = sum(1 for r in results if r.passed)
            passed = passed_count >= (len(results) / 2.0)

        # Average score
        avg_score = sum(r.score for r in results) / len(results)

        # Combine all failures
        combined_failures = []
        for r in results:
            combined_failures.extend(r.failures)

        # Combine feedback notes
        feedback_blocks = []
        for i, r in enumerate(results):
            if r.feedback:
                feedback_blocks.append(f"[Grader {i+1}]: {r.feedback}")
        combined_feedback = "\n".join(feedback_blocks)

        return GradeResult(
            score=avg_score,
            passed=passed,
            failures=combined_failures,
            feedback=combined_feedback,
        )

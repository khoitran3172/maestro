"""Runs local commands (compilation, builds, tests, linters) to verify output."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Optional

from maestro.adapters.base import TaskInput, TaskOutput
from maestro.grader.base import GradeResult, RubricFailure


class DeterministicGrader:
    """Deterministic local code verifier (compiles, builds, runs tests)."""

    async def grade(
        self,
        task_input: TaskInput,
        task_output: TaskOutput,
        workspace: Path,
    ) -> GradeResult:
        """Run local checks based on config or rubric parameters."""
        failures = []
        rubric = task_input.rubric or {}

        # 1. Immediate failure if execution was not successful
        if not task_output.succeeded:
            return GradeResult(
                score=0.0,
                passed=False,
                failures=[
                    RubricFailure(
                        item="execution",
                        message=task_output.error_message or "Specialist execution failed.",
                        expected="success",
                        actual=task_output.status.value,
                    )
                ],
                feedback="Specialist execution failed. Skipping deterministic checks.",
            )

        # 2. Extract build, test, or lint commands if requested in the rubric
        # Supports keys: build_command, test_command, lint_command
        build_cmd = rubric.get("build_command")
        test_cmd = rubric.get("test_command")
        lint_cmd = rubric.get("lint_command")

        if build_cmd:
            print(f"    🔧 Running build command: '{build_cmd}'...")
            build_ok, build_log = await self._run_command(build_cmd, workspace)
            if not build_ok:
                failures.append(
                    RubricFailure(
                        item="builds",
                        message=f"Build verification failed: {build_log}",
                        expected=build_cmd,
                        actual=build_log[:500],
                    )
                )

        if test_cmd:
            print(f"    🧪 Running test command: '{test_cmd}'...")
            test_ok, test_log = await self._run_command(test_cmd, workspace)
            if not test_ok:
                failures.append(
                    RubricFailure(
                        item="tests_pass",
                        message=f"Test execution failed: {test_log}",
                        expected=test_cmd,
                        actual=test_log[:500],
                    )
                )

        if lint_cmd:
            print(f"    🧹 Running lint command: '{lint_cmd}'...")
            lint_ok, lint_log = await self._run_command(lint_cmd, workspace)
            if not lint_ok:
                failures.append(
                    RubricFailure(
                        item="lint",
                        message=f"Lint checks failed: {lint_log}",
                        expected=lint_cmd,
                        actual=lint_log[:500],
                    )
                )

        passed = len(failures) == 0
        score = 100.0 if passed else 0.0
        feedback = "All deterministic checks passed successfully." if passed else "One or more deterministic checks failed."

        return GradeResult(
            score=score,
            passed=passed,
            failures=failures,
            feedback=feedback,
        )

    async def _run_command(self, cmd: str, cwd: Path) -> tuple[bool, str]:
        """Run shell command under cwd path."""
        try:
            process = await asyncio.create_subprocess_shell(
                cmd,
                cwd=str(cwd),
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
            stdout_bytes, stderr_bytes = await process.communicate()
            stdout = stdout_bytes.decode("utf-8", errors="replace")
            stderr = stderr_bytes.decode("utf-8", errors="replace")

            if process.returncode == 0:
                return True, stdout
            else:
                return False, stderr.strip() or stdout.strip() or f"Exit code {process.returncode}"
        except Exception as e:
            return False, str(e)

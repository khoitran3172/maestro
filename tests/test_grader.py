"""Tests for maestro.grader and maestro.feedback modules."""

from __future__ import annotations

from pathlib import Path

import pytest

from maestro.adapters.base import TaskInput, TaskOutput, TaskStatus
from maestro.grader import Grader, RubricFailure, GradeResult
from maestro.feedback import FeedbackBuilder


@pytest.fixture
def grader():
    return Grader()


@pytest.mark.asyncio
async def test_grade_execution_failure(grader):
    task_input = TaskInput(task_id="t1", prompt="Build feature", rubric={"builds": True})
    task_output = TaskOutput(status=TaskStatus.ERROR, error_message="Compile error exit code 1")

    res = await grader.grade(task_input, task_output)
    assert res.passed is False
    assert res.score == 0.0
    assert len(res.failures) == 1
    assert res.failures[0].item == "execution"
    assert "Compile error" in res.failures[0].message


@pytest.mark.asyncio
async def test_grade_no_rubric_success(grader):
    task_input = TaskInput(task_id="t1", prompt="Build feature")
    task_output = TaskOutput(status=TaskStatus.SUCCESS, stdout="build ok")

    res = await grader.grade(task_input, task_output)
    assert res.passed is True
    assert res.score == 100.0
    assert len(res.failures) == 0


@pytest.mark.asyncio
async def test_grade_builds_rubric(grader):
    task_input = TaskInput(task_id="t1", prompt="Build feature", rubric={"builds": True})
    
    # Successful build
    out_ok = TaskOutput(status=TaskStatus.SUCCESS, stdout="Build successful")
    res_ok = await grader.grade(task_input, out_ok)
    assert res_ok.passed is True
    assert res_ok.score == 100.0

    # Failing build
    out_fail = TaskOutput(status=TaskStatus.SUCCESS, stdout="build fail! error in line 4")
    res_fail = await grader.grade(task_input, out_fail)
    assert res_fail.passed is False
    assert res_fail.score == 0.0
    assert len(res_fail.failures) == 1
    assert res_fail.failures[0].item == "builds"


@pytest.mark.asyncio
async def test_grade_tests_pass_rubric(grader):
    task_input = TaskInput(task_id="t1", prompt="Build feature", rubric={"tests_pass": True})
    
    # Successful tests
    out_ok = TaskOutput(status=TaskStatus.SUCCESS, stdout="All 4 tests passed")
    res_ok = await grader.grade(task_input, out_ok)
    assert res_ok.passed is True

    # Failing tests
    out_fail = TaskOutput(status=TaskStatus.SUCCESS, stdout="1 failed, 3 passed")
    res_fail = await grader.grade(task_input, out_fail)
    assert res_fail.passed is False
    assert res_fail.failures[0].item == "tests_pass"


@pytest.mark.asyncio
async def test_grade_coverage_rubric(grader):
    task_input = TaskInput(task_id="t1", prompt="Build feature", rubric={"coverage": 0.80})
    
    # Sufficient coverage
    out_ok = TaskOutput(status=TaskStatus.SUCCESS, stdout="Coverage: 85.5%")
    res_ok = await grader.grade(task_input, out_ok)
    assert res_ok.passed is True

    # Insufficient coverage
    out_low = TaskOutput(status=TaskStatus.SUCCESS, stdout="coverage: 72%")
    res_low = await grader.grade(task_input, out_low)
    assert res_low.passed is False
    assert res_low.failures[0].item == "coverage"

    # Missing coverage output
    out_missing = TaskOutput(status=TaskStatus.SUCCESS, stdout="tests passed")
    res_missing = await grader.grade(task_input, out_missing)
    assert res_missing.passed is False
    assert "could not be parsed" in res_missing.failures[0].message


@pytest.mark.asyncio
async def test_grade_composite_score(grader):
    # Rubric has 3 checks
    task_input = TaskInput(
        task_id="t1",
        prompt="Build feature",
        rubric={"builds": True, "tests_pass": True, "coverage": 0.80}
    )
    
    # 2 out of 3 checks pass (coverage fails)
    task_output = TaskOutput(status=TaskStatus.SUCCESS, stdout="Build ok. tests ok. coverage: 50%")
    res = await grader.grade(task_input, task_output)
    assert res.passed is False
    assert res.score == pytest.approx(66.66, abs=0.1)
    assert len(res.failures) == 1
    assert res.failures[0].item == "coverage"


def test_feedback_builder():
    grade_result = GradeResult(
        score=50.0,
        passed=False,
        failures=[
            RubricFailure(item="builds", message="Build errors", expected=True, actual=False),
            RubricFailure(item="coverage", message="Coverage is 50%", expected=0.8, actual=0.5)
        ],
        feedback="Additional notes"
    )
    prev_output = TaskOutput(status=TaskStatus.SUCCESS, stderr="Compile error in main.py")

    feedback_text = FeedbackBuilder.build_feedback(grade_result, prev_output)
    
    assert "# Quality Rubric Failure" in feedback_text
    assert "Score: 50.0/100.0" in feedback_text
    assert "builds" in feedback_text
    assert "coverage" in feedback_text
    assert "Compile error in main.py" in feedback_text
    assert "Additional notes" in feedback_text

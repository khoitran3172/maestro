"""Tests for maestro.grader.pipeline (GraderPipeline)."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from maestro.adapters.base import TaskInput, TaskOutput, TaskStatus
from maestro.grader import GradeResult, RubricFailure
from maestro.grader.pipeline import GraderPipeline


@pytest.fixture
def pipeline():
    p = GraderPipeline()
    p.deterministic.grade = AsyncMock()
    p.text_grader.grade = AsyncMock()
    p.vision_grader.grade = AsyncMock()
    return p


@pytest.mark.asyncio
async def test_pipeline_execution_failure(pipeline, tmp_path):
    task_input = TaskInput(task_id="t1", prompt="Build UI")
    task_output = TaskOutput(status=TaskStatus.TIMEOUT, error_message="Timeout")

    res = await pipeline.grade(task_input, task_output, tmp_path)
    assert res.passed is False
    assert res.score == 0.0
    assert len(res.failures) == 1
    assert res.failures[0].item == "execution"
    
    # Assert other graders were not called
    pipeline.deterministic.grade.assert_not_called()
    pipeline.text_grader.grade.assert_not_called()
    pipeline.vision_grader.grade.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_deterministic_failure_short_circuits(pipeline, tmp_path):
    task_input = TaskInput(task_id="t1", prompt="Build UI")
    task_output = TaskOutput(status=TaskStatus.SUCCESS, stdout="Build logs")

    # Mock deterministic grader returning failure
    det_failure = GradeResult(
        score=0.0,
        passed=False,
        failures=[RubricFailure(item="builds", message="Build command failed")]
    )
    pipeline.deterministic.grade.return_return_value = det_failure  # Wait, mock return value
    pipeline.deterministic.grade.return_value = det_failure

    res = await pipeline.grade(task_input, task_output, tmp_path)
    assert res.passed is False
    assert len(res.failures) == 1
    assert res.failures[0].item == "builds"

    # Verify LLM graders were NOT called to save costs
    pipeline.text_grader.grade.assert_not_called()
    pipeline.vision_grader.grade.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_routes_only_text(pipeline, tmp_path):
    task_input = TaskInput(task_id="t1", prompt="Build UI")
    
    # Create a text file artifact
    text_file = tmp_path / "app.py"
    text_file.write_text("print('hello')")
    task_output = TaskOutput(status=TaskStatus.SUCCESS, artifacts=[text_file])

    # Mock return values
    pipeline.deterministic.grade.return_value = GradeResult(score=100.0, passed=True)
    pipeline.text_grader.grade.return_value = GradeResult(score=90.0, passed=True, feedback="Text grade ok")

    res = await pipeline.grade(task_input, task_output, tmp_path)
    assert res.passed is True
    # Composite score should be average of deterministic (100) and text (90) = 95
    assert res.score == 95.0

    pipeline.deterministic.grade.assert_called_once()
    pipeline.text_grader.grade.assert_called_once()
    pipeline.vision_grader.grade.assert_not_called()


@pytest.mark.asyncio
async def test_pipeline_routes_only_vision(pipeline, tmp_path):
    task_input = TaskInput(task_id="t1", prompt="Build UI")
    
    # Create an image file artifact
    img_file = tmp_path / "screen.png"
    img_file.write_bytes(b"PNG_DATA")
    task_output = TaskOutput(status=TaskStatus.SUCCESS, artifacts=[img_file])

    # Mock return values
    pipeline.deterministic.grade.return_value = GradeResult(score=100.0, passed=True)
    pipeline.vision_grader.grade.return_value = GradeResult(score=80.0, passed=True, feedback="Vision grade ok")

    res = await pipeline.grade(task_input, task_output, tmp_path)
    assert res.passed is True
    # Average of 100 and 80 = 90
    assert res.score == 90.0

    pipeline.deterministic.grade.assert_called_once()
    pipeline.text_grader.grade.assert_not_called()
    pipeline.vision_grader.grade.assert_called_once()


@pytest.mark.asyncio
async def test_pipeline_routes_both_text_and_vision(pipeline, tmp_path):
    task_input = TaskInput(task_id="t1", prompt="Build UI")
    
    # Create both text and image file artifacts
    text_file = tmp_path / "app.py"
    text_file.write_text("print('hello')")
    img_file = tmp_path / "screen.png"
    img_file.write_bytes(b"PNG_DATA")
    task_output = TaskOutput(status=TaskStatus.SUCCESS, artifacts=[text_file, img_file])

    # Mock return values
    pipeline.deterministic.grade.return_value = GradeResult(score=100.0, passed=True)
    pipeline.text_grader.grade.return_value = GradeResult(score=90.0, passed=True)
    pipeline.vision_grader.grade.return_value = GradeResult(score=80.0, passed=True)

    res = await pipeline.grade(task_input, task_output, tmp_path)
    assert res.passed is True
    # Average of 100, 90, 80 = 90
    assert res.score == 90.0

    pipeline.deterministic.grade.assert_called_once()
    pipeline.text_grader.grade.assert_called_once()
    pipeline.vision_grader.grade.assert_called_once()

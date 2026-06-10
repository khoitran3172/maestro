"""Tests for specialist adapter base classes and interface contract."""

import asyncio
from pathlib import Path
from typing import Optional

import pytest

from maestro.adapters.base import (
    SpecialistAdapter,
    TaskInput,
    TaskOutput,
    TaskStatus,
)


# ── Test doubles ──


class MockSuccessAdapter(SpecialistAdapter):
    """Adapter that always succeeds — for testing interface contract."""

    @property
    def name(self) -> str:
        return "mock_success"

    async def run(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(
            status=TaskStatus.SUCCESS,
            stdout=f"Completed: {task.task_id}",
            duration_sec=1.0,
            estimated_cost_usd=0.003,
            session_id="session-123" if task.session_id else None,
        )

    async def health_check(self) -> bool:
        return True

    def supports_resume(self) -> bool:
        return True


class MockFailAdapter(SpecialistAdapter):
    """Adapter that always fails — for testing error paths."""

    @property
    def name(self) -> str:
        return "mock_fail"

    async def run(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(
            status=TaskStatus.ERROR,
            error_message="Simulated failure",
            duration_sec=0.5,
        )

    async def health_check(self) -> bool:
        return False

    def supports_resume(self) -> bool:
        return False


class MockTimeoutAdapter(SpecialistAdapter):
    """Adapter that always times out."""

    @property
    def name(self) -> str:
        return "mock_timeout"

    async def run(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(
            status=TaskStatus.TIMEOUT,
            error_message=f"Timeout after {task.timeout_sec}s",
            duration_sec=float(task.timeout_sec),
        )

    async def health_check(self) -> bool:
        return True

    def supports_resume(self) -> bool:
        return False


# ── TaskInput tests ──


class TestTaskInput:
    def test_defaults(self):
        task = TaskInput(task_id="t1", prompt="Build a todo app")
        assert task.task_id == "t1"
        assert task.prompt == "Build a todo app"
        assert task.artifacts == []
        assert task.feedback is None
        assert task.session_id is None
        assert task.timeout_sec == 600
        assert task.max_retries == 2

    def test_input_hash_deterministic(self):
        t1 = TaskInput(task_id="t1", prompt="hello")
        t2 = TaskInput(task_id="t1", prompt="hello")
        assert t1.input_hash == t2.input_hash

    def test_input_hash_changes(self):
        t1 = TaskInput(task_id="t1", prompt="hello")
        t2 = TaskInput(task_id="t1", prompt="world")
        assert t1.input_hash != t2.input_hash

    def test_with_all_fields(self):
        task = TaskInput(
            task_id="t5",
            prompt="Implement auth",
            artifacts=[Path("/a/b.py")],
            rubric={"tests_pass": True},
            feedback="Add input validation",
            feedback_artifacts=[Path("/old/output.py")],
            session_id="sess-abc",
            branch="maestro/codex-t5",
            timeout_sec=300,
            max_retries=3,
            env_vars={"API_KEY": "xxx"},
            extra={"cwd": "/workspace"},
        )
        assert task.session_id == "sess-abc"
        assert task.branch == "maestro/codex-t5"
        assert len(task.artifacts) == 1


# ── TaskOutput tests ──


class TestTaskOutput:
    def test_success(self):
        out = TaskOutput(status=TaskStatus.SUCCESS, stdout="ok")
        assert out.succeeded is True

    def test_error(self):
        out = TaskOutput(status=TaskStatus.ERROR, error_message="fail")
        assert out.succeeded is False

    def test_to_dict(self):
        out = TaskOutput(
            status=TaskStatus.SUCCESS,
            stdout="hello",
            duration_sec=2.567,
            estimated_cost_usd=0.0123456,
            session_id="s1",
        )
        d = out.to_dict()
        assert d["status"] == "success"
        assert d["duration_sec"] == 2.57
        assert d["estimated_cost_usd"] == 0.012346
        assert d["session_id"] == "s1"

    def test_to_dict_caps_output(self):
        out = TaskOutput(status=TaskStatus.SUCCESS, stdout="x" * 5000)
        d = out.to_dict()
        assert len(d["stdout"]) == 2000


# ── SpecialistAdapter contract tests ──


class TestSpecialistAdapterContract:
    """Verify the adapter contract using mock implementations."""

    @pytest.fixture
    def success_adapter(self):
        return MockSuccessAdapter()

    @pytest.fixture
    def fail_adapter(self):
        return MockFailAdapter()

    @pytest.fixture
    def timeout_adapter(self):
        return MockTimeoutAdapter()

    @pytest.mark.asyncio
    async def test_success_run(self, success_adapter):
        task = TaskInput(task_id="t1", prompt="test")
        result = await success_adapter.run(task)
        assert result.succeeded
        assert result.status == TaskStatus.SUCCESS
        assert "t1" in result.stdout

    @pytest.mark.asyncio
    async def test_fail_run(self, fail_adapter):
        task = TaskInput(task_id="t1", prompt="test")
        result = await fail_adapter.run(task)
        assert not result.succeeded
        assert result.error_message == "Simulated failure"

    @pytest.mark.asyncio
    async def test_timeout_run(self, timeout_adapter):
        task = TaskInput(task_id="t1", prompt="test", timeout_sec=30)
        result = await timeout_adapter.run(task)
        assert result.status == TaskStatus.TIMEOUT

    @pytest.mark.asyncio
    async def test_health_check_healthy(self, success_adapter):
        assert await success_adapter.health_check() is True

    @pytest.mark.asyncio
    async def test_health_check_unhealthy(self, fail_adapter):
        assert await fail_adapter.health_check() is False

    def test_name_property(self, success_adapter, fail_adapter):
        assert success_adapter.name == "mock_success"
        assert fail_adapter.name == "mock_fail"

    def test_supports_resume(self, success_adapter, fail_adapter):
        assert success_adapter.supports_resume() is True
        assert fail_adapter.supports_resume() is False

    def test_cost_rate_default(self, success_adapter):
        assert success_adapter.cost_rate_per_sec == 0.003  # Default

    @pytest.mark.asyncio
    async def test_session_passthrough(self, success_adapter):
        """Session ID flows through input → output for resume."""
        task = TaskInput(task_id="t1", prompt="test", session_id="prev-session")
        result = await success_adapter.run(task)
        assert result.session_id == "session-123"


# ── _run_subprocess helper tests ──


class TestRunSubprocess:
    """Test the shared _run_subprocess helper in base adapter."""

    @pytest.fixture
    def adapter(self):
        return MockSuccessAdapter()

    @pytest.mark.asyncio
    async def test_subprocess_success(self, adapter):
        result = await adapter._run_subprocess(
            ["python", "-c", "print('hello from subprocess')"],
            timeout_sec=10,
        )
        assert result.succeeded
        assert "hello from subprocess" in result.stdout

    @pytest.mark.asyncio
    async def test_subprocess_error(self, adapter):
        result = await adapter._run_subprocess(
            ["python", "-c", "import sys; sys.exit(42)"],
            timeout_sec=10,
        )
        assert result.status == TaskStatus.ERROR
        assert result.exit_code == 42

    @pytest.mark.asyncio
    async def test_subprocess_timeout(self, adapter):
        result = await adapter._run_subprocess(
            ["python", "-c", "import time; time.sleep(60)"],
            timeout_sec=1,
        )
        assert result.status == TaskStatus.TIMEOUT
        assert "Timeout" in result.error_message

    @pytest.mark.asyncio
    async def test_subprocess_not_found(self, adapter):
        result = await adapter._run_subprocess(
            ["nonexistent_command_xyz"],
            timeout_sec=10,
        )
        assert result.status == TaskStatus.ERROR
        assert "not found" in result.error_message.lower() or "error" in result.error_message.lower()

    @pytest.mark.asyncio
    async def test_subprocess_cost_estimation(self, adapter):
        """Duration-based cost is calculated automatically."""
        result = await adapter._run_subprocess(
            ["python", "-c", "import time; time.sleep(0.1); print('done')"],
            timeout_sec=10,
        )
        assert result.estimated_cost_usd > 0
        assert result.duration_sec >= 0.1


# ── Feedback prompt builder tests ──


class TestFeedbackPromptBuilder:
    @pytest.fixture
    def adapter(self):
        return MockSuccessAdapter()

    def test_no_feedback(self, adapter):
        task = TaskInput(task_id="t1", prompt="Build a todo app")
        result = adapter._build_prompt_with_feedback(task)
        assert result == "Build a todo app"

    def test_with_feedback(self, adapter):
        task = TaskInput(
            task_id="t1",
            prompt="Build a todo app",
            feedback="Missing delete functionality",
        )
        result = adapter._build_prompt_with_feedback(task)
        assert "Original Task" in result
        assert "Build a todo app" in result
        assert "Feedback from Previous Attempt" in result
        assert "Missing delete functionality" in result

    def test_with_feedback_artifacts(self, adapter, tmp_path):
        artifact = tmp_path / "old_output.py"
        artifact.write_text("# old code")
        task = TaskInput(
            task_id="t1",
            prompt="Build a todo app",
            feedback="Fix the code",
            feedback_artifacts=[artifact],
        )
        result = adapter._build_prompt_with_feedback(task)
        assert "Previous Output Artifacts" in result
        assert "old_output.py" in result

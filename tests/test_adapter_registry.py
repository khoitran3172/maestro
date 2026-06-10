"""Tests for adapter registry — discovery, loading, health checks."""

import asyncio
from pathlib import Path

import pytest

from maestro.adapter_registry import AdapterRegistry
from maestro.adapters.base import SpecialistAdapter, TaskInput, TaskOutput, TaskStatus
from maestro.adapters.claude_code import ClaudeCodeAdapter
from maestro.adapters.codex import CodexAdapter
from maestro.adapters.stitch import StitchAdapter
from maestro.adapters.grok_build import GrokBuildAdapter
from maestro.adapters.antigravity import AntigravityAdapter


# ── Mock adapter for testing ──

class MockAdapter(SpecialistAdapter):
    def __init__(self, adapter_name: str = "mock", healthy: bool = True):
        self._name = adapter_name
        self._healthy = healthy

    @property
    def name(self) -> str:
        return self._name

    async def run(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(status=TaskStatus.SUCCESS, stdout="mock output")

    async def health_check(self) -> bool:
        return self._healthy

    def supports_resume(self) -> bool:
        return False


# ── Registry tests ──


class TestAdapterRegistry:
    def test_register_and_get(self):
        registry = AdapterRegistry()
        adapter = MockAdapter("test_adapter")
        registry.register("test_adapter", adapter)
        assert registry.get("test_adapter") is adapter

    def test_get_unknown_raises(self):
        registry = AdapterRegistry()
        with pytest.raises(KeyError, match="Unknown specialist 'nonexistent'"):
            registry.get("nonexistent")

    def test_has(self):
        registry = AdapterRegistry()
        registry.register("mock", MockAdapter())
        assert registry.has("mock") is True
        assert registry.has("nonexistent") is False

    def test_available_list(self):
        registry = AdapterRegistry()
        registry.register("beta", MockAdapter("beta"))
        registry.register("alpha", MockAdapter("alpha"))
        assert registry.available == ["alpha", "beta"]  # Sorted

    def test_register_defaults_all(self):
        """All 5 default adapters are registered."""
        registry = AdapterRegistry()
        registry.register_defaults()
        assert len(registry.available) == 5
        assert "claude_code" in registry.available
        assert "codex" in registry.available
        assert "stitch" in registry.available
        assert "grok_build" in registry.available
        assert "antigravity" in registry.available

    def test_register_defaults_only(self):
        """Only selected adapters are registered."""
        registry = AdapterRegistry()
        registry.register_defaults(only=["claude_code", "codex"])
        assert registry.available == ["claude_code", "codex"]

    def test_register_defaults_exclude(self):
        """Excluded adapters are skipped."""
        registry = AdapterRegistry()
        registry.register_defaults(exclude=["grok_build", "antigravity"])
        assert "grok_build" not in registry.available
        assert "antigravity" not in registry.available
        assert "claude_code" in registry.available

    def test_summary(self):
        registry = AdapterRegistry()
        registry.register("mock", MockAdapter())
        summary = registry.summary()
        assert "mock" in summary
        assert "supports_resume" in summary["mock"]
        assert "cost_rate_per_sec" in summary["mock"]


class TestAdapterRegistryHealthChecks:
    @pytest.mark.asyncio
    async def test_health_check_all(self):
        registry = AdapterRegistry()
        registry.register("healthy", MockAdapter("healthy", healthy=True))
        registry.register("unhealthy", MockAdapter("unhealthy", healthy=False))

        results = await registry.health_check_all()
        assert results["healthy"] is True
        assert results["unhealthy"] is False

    @pytest.mark.asyncio
    async def test_health_check_single(self):
        registry = AdapterRegistry()
        registry.register("mock", MockAdapter("mock", healthy=True))
        assert await registry.health_check("mock") is True

    @pytest.mark.asyncio
    async def test_health_check_exception_returns_false(self):
        """Adapter that throws during health check returns False."""
        class BrokenAdapter(MockAdapter):
            async def health_check(self) -> bool:
                raise RuntimeError("CLI crashed")

        registry = AdapterRegistry()
        registry.register("broken", BrokenAdapter())
        assert await registry.health_check("broken") is False


# ── Individual adapter type tests ──


class TestAdapterTypes:
    """Verify each adapter class is correctly typed and configured."""

    def test_claude_code_properties(self):
        adapter = ClaudeCodeAdapter()
        assert adapter.name == "claude_code"
        assert adapter.supports_resume() is True
        assert adapter.cost_rate_per_sec == 0.005

    def test_codex_properties(self):
        adapter = CodexAdapter()
        assert adapter.name == "codex"
        assert adapter.supports_resume() is False
        assert adapter.cost_rate_per_sec == 0.003

    def test_stitch_properties(self):
        adapter = StitchAdapter()
        assert adapter.name == "stitch"
        assert adapter.supports_resume() is False
        assert adapter.cost_rate_per_sec == 0.004

    def test_grok_build_properties(self):
        adapter = GrokBuildAdapter()
        assert adapter.name == "grok_build"
        assert adapter.supports_resume() is False
        assert adapter.cost_rate_per_sec == 0.003

    def test_antigravity_properties(self):
        adapter = AntigravityAdapter()
        assert adapter.name == "antigravity"
        assert adapter.supports_resume() is False
        assert adapter.cost_rate_per_sec == 0.002


class TestAntigravityDeploySafety:
    """Verify deploy tasks require human approval."""

    @pytest.mark.asyncio
    async def test_deploy_blocked_by_default(self):
        adapter = AntigravityAdapter()
        task = TaskInput(
            task_id="t1",
            prompt="Deploy the app to Cloud Run",
        )
        result = await adapter.run(task)
        assert result.status == TaskStatus.ERROR
        assert "approval" in result.error_message.lower()
        assert result.metadata.get("requires_approval") is True

    @pytest.mark.asyncio
    async def test_non_deploy_task_proceeds(self):
        """Non-deploy tasks don't trigger approval gate."""
        adapter = AntigravityAdapter()
        task = TaskInput(
            task_id="t1",
            prompt="Generate a configuration file",
        )
        # Will fail because CLI isn't installed, but won't hit approval gate
        result = await adapter.run(task)
        assert result.metadata.get("requires_approval") is not True


class TestClaudeCodeAdapter:
    def test_command_with_resume(self):
        adapter = ClaudeCodeAdapter()
        task = TaskInput(
            task_id="t1",
            prompt="Fix the bug",
            session_id="session-abc",
        )
        cmd = adapter._build_command(task)
        assert "--resume" in cmd
        assert "session-abc" in cmd

    def test_command_without_resume(self):
        adapter = ClaudeCodeAdapter()
        task = TaskInput(task_id="t1", prompt="Build feature")
        cmd = adapter._build_command(task)
        assert "--resume" not in cmd
        assert "--print" in cmd
        assert "--output-format" in cmd

    def test_command_with_rubric(self):
        adapter = ClaudeCodeAdapter()
        task = TaskInput(
            task_id="t1",
            prompt="Build feature",
            rubric={"tests_pass": True, "coverage": 0.8},
        )
        cmd = adapter._build_command(task)
        prompt = cmd[-1]
        assert "Quality Rubric" in prompt
        assert "tests_pass" in prompt


class TestCodexAdapter:
    def test_command_structure(self):
        adapter = CodexAdapter()
        task = TaskInput(task_id="t1", prompt="Implement API")
        cmd = adapter._build_command(task)
        assert cmd[0] == "codex"
        assert "exec" in cmd
        assert "--approval-mode" in cmd
        assert "full-auto" in cmd

    def test_feedback_injects_diff(self, tmp_path):
        adapter = CodexAdapter()
        old_file = tmp_path / "old.py"
        old_file.write_text("def old(): pass")

        task = TaskInput(
            task_id="t1",
            prompt="Implement API",
            feedback="Add error handling",
            feedback_artifacts=[old_file],
        )
        cmd = adapter._build_command(task)
        prompt = cmd[-1]
        assert "Previous Attempt Diff" in prompt
        assert "def old(): pass" in prompt


class TestStitchAdapter:
    def test_command_has_format(self):
        adapter = StitchAdapter(output_format="png")
        task = TaskInput(task_id="t1", prompt="Design login page")
        cmd = adapter._build_command(task)
        assert "--format" in cmd
        assert "png" in cmd

    def test_finds_image_artifacts(self, tmp_path):
        adapter = StitchAdapter()
        # Create some test images
        (tmp_path / "screen_a.png").write_bytes(b"PNG")
        (tmp_path / "screen_b.jpg").write_bytes(b"JPG")
        (tmp_path / "readme.md").write_text("not an image")

        artifacts = adapter._find_image_artifacts(tmp_path)
        assert len(artifacts) == 2
        names = [a.name for a in artifacts]
        assert "screen_a.png" in names
        assert "screen_b.jpg" in names
        assert "readme.md" not in names

    def test_design_prompt_with_feedback(self):
        adapter = StitchAdapter()
        task = TaskInput(
            task_id="t1",
            prompt="Design a dashboard",
            feedback="Colors are too muted, make them vibrant",
            rubric={"layout_match": 0.8},
        )
        prompt = adapter._build_design_prompt(task)
        assert "Revision" in prompt
        assert "Colors are too muted" in prompt
        assert "layout_match" in prompt


class TestGrokBuildAdapter:
    def test_command_has_branch(self):
        adapter = GrokBuildAdapter()
        task = TaskInput(task_id="t5", prompt="Build feature")
        cmd = adapter._build_command(task)
        assert "--branch" in cmd
        assert "maestro/grok-t5" in cmd

    def test_custom_branch(self):
        adapter = GrokBuildAdapter()
        task = TaskInput(
            task_id="t5",
            prompt="Build feature",
            branch="custom/branch",
        )
        cmd = adapter._build_command(task)
        assert "custom/branch" in cmd

    def test_multi_branch(self):
        adapter = GrokBuildAdapter(num_branches=3)
        task = TaskInput(task_id="t5", prompt="Build feature")
        cmd = adapter._build_command(task)
        assert "--num-branches" in cmd
        assert "3" in cmd

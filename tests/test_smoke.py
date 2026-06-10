"""Smoke test — todo-app pipeline end-to-end.

Runs a simplified pipeline using echo commands as mock specialists.
Validates: error handling, cost tracking, logging, state persistence.
"""

import json
from pathlib import Path

import pytest

from maestro.runner import PipelineConfig, PipelineRunner, PhaseConfig
from maestro.adapters.base import SpecialistAdapter, TaskInput, TaskOutput, TaskStatus
from maestro.adapter_registry import AdapterRegistry
from maestro.db.store import MaestroStore


class SmokeTestAdapter(SpecialistAdapter):
    """A specialist adapter for smoke testing that runs the command_template."""
    def __init__(self, name: str):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def run(self, task: TaskInput) -> TaskOutput:
        command = task.extra.get("command_template")
        cwd = task.extra.get("cwd")
        if not command:
            return TaskOutput(status=TaskStatus.SUCCESS)
        return await self._run_subprocess(command, cwd=cwd, timeout_sec=task.timeout_sec)

    async def health_check(self) -> bool:
        return True

    def supports_resume(self) -> bool:
        return True


@pytest.fixture
def test_registry():
    """Create a registry containing mock adapters for testing."""
    registry = AdapterRegistry()
    registry.register("claude_code", SmokeTestAdapter("claude_code"))
    registry.register("stitch", SmokeTestAdapter("stitch"))
    registry.register("codex", SmokeTestAdapter("codex"))
    return registry


@pytest.fixture
def todo_pipeline_config(tmp_path):
    """Create a mock todo-app pipeline config."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = PipelineConfig(
        project_name="todo-app-smoke-test",
        workspace=workspace,
        phases=[
            PhaseConfig(
                phase=1,
                name="Spec Generation",
                specialist="claude_code",
                command_template=["python", "-c", "print('Phase 1: spec generated')"],
                timeout_sec=30,
                max_retries=1,
            ),
            PhaseConfig(
                phase=2,
                name="Design",
                specialist="stitch",
                command_template=["python", "-c", "print('Phase 2: design created')"],
                timeout_sec=30,
                max_retries=1,
            ),
            PhaseConfig(
                phase=3,
                name="Implementation",
                specialist="codex",
                command_template=["python", "-c", "print('Phase 3: code written')"],
                timeout_sec=30,
                max_retries=1,
            ),
            PhaseConfig(
                phase=4,
                name="Review",
                specialist="claude_code",
                command_template=["python", "-c", "print('Phase 4: review passed')"],
                timeout_sec=30,
                max_retries=1,
            ),
        ],
        max_budget_usd=5.0,
    )
    return config


class TestSmokePipeline:
    @pytest.mark.asyncio
    async def test_end_to_end_success(self, todo_pipeline_config, test_registry):
        """Full pipeline runs without crashing."""
        runner = PipelineRunner(todo_pipeline_config, registry=test_registry)
        success = await runner.run()
        assert success is True
        assert runner.state.status == "completed"

    @pytest.mark.asyncio
    async def test_state_persisted(self, todo_pipeline_config, test_registry):
        """State database exists after run."""
        runner = PipelineRunner(todo_pipeline_config, registry=test_registry)
        await runner.run()

        db_path = todo_pipeline_config.workspace / ".maestro" / "maestro.db"
        assert db_path.exists()

        async with MaestroStore(db_path) as store:
            run = await store.get_run(runner.state.run_id)
            assert run is not None
            assert run["status"] == "completed"
            assert run["project_name"] == "todo-app-smoke-test"

            tasks = await store.get_tasks_by_run(runner.state.run_id)
            assert len(tasks) == 4

    @pytest.mark.asyncio
    async def test_log_file_created(self, todo_pipeline_config, test_registry):
        """JSONL log file has entries for each phase."""
        runner = PipelineRunner(todo_pipeline_config, registry=test_registry)
        await runner.run()

        log_path = todo_pipeline_config.workspace / ".maestro" / "log.jsonl"
        assert log_path.exists()
        lines = log_path.read_text().strip().split("\n")
        # At minimum: 1 start + 4 specialist calls + 1 completion
        assert len(lines) >= 6

    @pytest.mark.asyncio
    async def test_cost_tracked(self, todo_pipeline_config, test_registry):
        """Cost state file has accumulated costs."""
        runner = PipelineRunner(todo_pipeline_config, registry=test_registry)
        await runner.run()

        cost_path = todo_pipeline_config.workspace / ".maestro" / "cost_state.json"
        assert cost_path.exists()

        with open(cost_path) as f:
            cost_data = json.load(f)
        assert cost_data["total_spent_usd"] > 0
        assert len(cost_data["entries"]) == 4  # 4 phases

    @pytest.mark.asyncio
    async def test_handles_failing_phase(self, tmp_path, test_registry):
        """Pipeline stops and reports failure on non-zero exit."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = PipelineConfig(
            project_name="failing-test",
            workspace=workspace,
            phases=[
                PhaseConfig(
                    phase=1,
                    name="Good Phase",
                    specialist="codex",
                    command_template=["python", "-c", "print('ok')"],
                    timeout_sec=10,
                    max_retries=0,
                ),
                PhaseConfig(
                    phase=2,
                    name="Bad Phase",
                    specialist="codex",
                    command_template=["python", "-c", "import sys; sys.exit(1)"],
                    timeout_sec=10,
                    max_retries=0,
                ),
                PhaseConfig(
                    phase=3,
                    name="Never Reached",
                    specialist="codex",
                    command_template=["python", "-c", "print('should not run')"],
                    timeout_sec=10,
                    max_retries=0,
                ),
            ],
            max_budget_usd=5.0,
        )

        runner = PipelineRunner(config, registry=test_registry)
        success = await runner.run()
        assert success is False
        assert runner.state.status == "failed"
        assert 1 in runner.state.phase_results  # Phase 1 ran
        assert 2 in runner.state.phase_results  # Phase 2 ran (failed)
        assert 3 not in runner.state.phase_results  # Phase 3 never started

    @pytest.mark.asyncio
    async def test_retry_on_transient_failure(self, tmp_path, test_registry):
        """Transient failure triggers retry with backoff."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        # Create a script that fails first time, succeeds second
        counter_file = workspace / "counter.txt"
        counter_file.write_text("0")

        script = (
            "import sys; "
            f"f = open(r'{counter_file}'); c = int(f.read()); f.close(); "
            f"f = open(r'{counter_file}', 'w'); f.write(str(c+1)); f.close(); "
            "sys.exit(0 if c > 0 else 1)"
        )

        config = PipelineConfig(
            project_name="retry-test",
            workspace=workspace,
            phases=[
                PhaseConfig(
                    phase=1,
                    name="Flaky Phase",
                    specialist="codex",
                    command_template=["python", "-c", script],
                    timeout_sec=10,
                    max_retries=2,
                ),
            ],
            max_budget_usd=5.0,
        )

        runner = PipelineRunner(config, registry=test_registry)
        # Override retry delay for faster test
        runner.retry_policy.base_delay_sec = 0.01
        success = await runner.run()
        assert success is True
        assert runner.state.phase_results[1]["attempt"] == 2  # Succeeded on 2nd attempt

    @pytest.mark.asyncio
    async def test_three_consecutive_runs(self, todo_pipeline_config, test_registry):
        """Pipeline completes 3 times in a row (stability check)."""
        for i in range(3):
            # Fresh workspace each run
            workspace = todo_pipeline_config.workspace.parent / f"run_{i}"
            workspace.mkdir()
            todo_pipeline_config.workspace = workspace
            runner = PipelineRunner(todo_pipeline_config, registry=test_registry)
            success = await runner.run()
            assert success is True, f"Run {i+1} failed"

    @pytest.mark.asyncio
    async def test_budget_enforcement(self, tmp_path, test_registry):
        """Pipeline halts when budget exceeded."""
        workspace = tmp_path / "workspace"
        workspace.mkdir()

        config = PipelineConfig(
            project_name="budget-test",
            workspace=workspace,
            phases=[
                PhaseConfig(
                    phase=1,
                    name="Expensive Phase",
                    specialist="claude_code",
                    # Sleep 10s at $0.005/s = $0.05 (already exceeds $0.001 budget)
                    command_template=["python", "-c", "import time; time.sleep(0.5); print('done')"],
                    timeout_sec=30,
                    max_retries=0,
                ),
                PhaseConfig(
                    phase=2,
                    name="Should Not Run",
                    specialist="codex",
                    command_template=["python", "-c", "print('unreachable')"],
                    timeout_sec=10,
                    max_retries=0,
                ),
            ],
            max_budget_usd=0.001,  # $0.001 — will exceed after phase 1
        )

        runner = PipelineRunner(config, registry=test_registry)
        success = await runner.run()
        assert success is False
        assert runner.state.status in ("paused", "failed")

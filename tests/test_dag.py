"""Unit tests for Maestro DAG scheduler and TaskGraph execution."""

import asyncio
import json
import time
from pathlib import Path
import pytest

from maestro.dag.task_graph import TaskGraph, TaskNode, CycleError
from maestro.dag.scheduler import DAGScheduler
from maestro.adapters.base import SpecialistAdapter, TaskInput, TaskOutput, TaskStatus
from maestro.adapter_registry import AdapterRegistry
from maestro.runner import PipelineConfig, PipelineRunner, PhaseConfig, RunState


@pytest.fixture(autouse=True)
def disable_git_isolation(monkeypatch):
    """Disable Git isolation for DAG scheduling tests to prevent workspace lock/CLI overhead."""
    monkeypatch.setenv("MAESTRO_GIT_ISOLATION", "0")


class SleepyAdapter(SpecialistAdapter):
    """Adapter that sleeps to test concurrent execution."""
    def __init__(self, name: str, sleep_sec: float = 0.2):
        self._name = name
        self.sleep_sec = sleep_sec
        self.executions = []

    @property
    def name(self) -> str:
        return self._name

    async def run(self, task: TaskInput) -> TaskOutput:
        start_time = time.time()
        await asyncio.sleep(self.sleep_sec)
        end_time = time.time()
        self.executions.append((start_time, end_time))
        return TaskOutput(
            status=TaskStatus.SUCCESS,
            artifacts=[],
            stdout=f"{self._name} complete",
            stderr="",
            duration_sec=self.sleep_sec,
            estimated_cost_usd=0.005,
        )

    async def health_check(self) -> bool:
        return True

    def supports_resume(self) -> bool:
        return True


class FlakyAdapter(SpecialistAdapter):
    """Adapter that fails to test error propagation."""
    def __init__(self, name: str):
        self._name = name

    @property
    def name(self) -> str:
        return self._name

    async def run(self, task: TaskInput) -> TaskOutput:
        return TaskOutput(
            status=TaskStatus.ERROR,
            artifacts=[],
            stdout="",
            stderr="Failed execution",
            duration_sec=0.01,
            estimated_cost_usd=0.001,
            error_message="Simulated specialist error",
        )

    async def health_check(self) -> bool:
        return True

    def supports_resume(self) -> bool:
        return False


def test_task_graph_basic():
    """Test basic graph manipulation and topological sort."""
    graph = TaskGraph()
    n1 = TaskNode(task_id="t1", specialist="codex", command_template=["echo", "1"])
    n2 = TaskNode(task_id="t2", specialist="stitch", command_template=["echo", "2"])
    n3 = TaskNode(task_id="t3", specialist="antigravity", command_template=["echo", "3"])

    graph.add_node(n1)
    graph.add_node(n2)
    graph.add_node(n3)

    # Setup dependencies: t1 -> t2, t1 -> t3
    graph.add_edge("t1", "t2")
    graph.add_edge("t1", "t3")

    assert graph.get_dependencies("t2") == ["t1"]
    assert sorted(graph.get_dependents("t1")) == ["t2", "t3"]

    order = graph.topological_sort()
    assert order[0] == "t1"
    assert set(order[1:]) == {"t2", "t3"}


def test_task_graph_cycle_detection():
    """Test that a circular dependency raises CycleError."""
    graph = TaskGraph()
    n1 = TaskNode(task_id="t1", specialist="codex", command_template=["echo", "1"])
    n2 = TaskNode(task_id="t2", specialist="stitch", command_template=["echo", "2"])

    graph.add_node(n1)
    graph.add_node(n2)

    graph.add_edge("t1", "t2")
    graph.add_edge("t2", "t1")

    with pytest.raises(CycleError):
        graph.validate()

    with pytest.raises(CycleError):
        graph.topological_sort()


def test_task_graph_json_serialization():
    """Test serializing and deserializing graphs."""
    graph = TaskGraph()
    n1 = TaskNode(task_id="t1", specialist="codex", command_template=["echo", "1"], prompt="Custom Prompt")
    n2 = TaskNode(task_id="t2", specialist="stitch", command_template=["echo", "2"])
    graph.add_node(n1)
    graph.add_node(n2)
    graph.add_edge("t1", "t2")

    json_str = graph.to_json()
    parsed_graph = TaskGraph.from_json(json_str)

    assert "t1" in parsed_graph.nodes
    assert "t2" in parsed_graph.nodes
    assert parsed_graph.nodes["t1"].prompt == "Custom Prompt"
    assert parsed_graph.get_dependencies("t2") == ["t1"]


@pytest.mark.asyncio
async def test_scheduler_linear_execution(tmp_path):
    """Test scheduler executing a linear path of tasks."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    
    config = PipelineConfig(
        project_name="linear-dag-test",
        workspace=workspace,
        phases=[
            PhaseConfig(phase=1, name="P1", specialist="codex", command_template=["echo"]),
            PhaseConfig(phase=2, name="P2", specialist="stitch", command_template=["echo"]),
        ],
    )
    
    registry = AdapterRegistry()
    a1 = SleepyAdapter("codex", sleep_sec=0.05)
    a2 = SleepyAdapter("stitch", sleep_sec=0.05)
    registry.register("codex", a1)
    registry.register("stitch", a2)

    runner = PipelineRunner(config, registry=registry)
    success = await runner.run()
    assert success is True
    assert runner.state.status == "completed"

    # Check database status
    async with runner.store:
        t1 = await runner.store.get_task("phase_1")
        t2 = await runner.store.get_task("phase_2")
        assert t1["status"] == "done"
        assert t2["status"] == "done"


@pytest.mark.asyncio
async def test_scheduler_parallel_execution(tmp_path):
    """Test that independent tasks run concurrently."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = PipelineConfig(
        project_name="parallel-dag-test",
        workspace=workspace,
        phases=[],
    )

    registry = AdapterRegistry()
    a1 = SleepyAdapter("codex", sleep_sec=0.2)
    a2 = SleepyAdapter("stitch", sleep_sec=0.2)
    registry.register("codex", a1)
    registry.register("stitch", a2)

    runner = PipelineRunner(config, registry=registry)
    
    # We construct a custom DAG manually for parallel tasks
    graph = TaskGraph()
    n1 = TaskNode(task_id="t1", specialist="codex", command_template=["echo"], phase=1, prompt="T1")
    n2 = TaskNode(task_id="t2", specialist="stitch", command_template=["echo"], phase=1, prompt="T2")
    graph.add_node(n1)
    graph.add_node(n2)

    # Run DB connection and create tasks
    async with runner.store:
        run_id = await runner.store.create_run(
            project_name=config.project_name,
            task_graph_json=graph.to_json()
        )
        runner.state = RunState(run_id=run_id, project_name=config.project_name)

        await runner.store.create_task(run_id, "t1", "codex", phase=1, prompt="T1")
        await runner.store.create_task(run_id, "t2", "stitch", phase=1, prompt="T2")

        start_time = time.time()
        scheduler = DAGScheduler(max_concurrency=4)
        success = await scheduler.run(graph, runner, run_id)
        end_time = time.time()

        assert success is True
        total_time = end_time - start_time

        # If run sequentially, total time is >= 0.4s. If parallel, it should be around 0.2s - 0.35s
        assert total_time < 0.35

        # Check execution times overlap
        s1, e1 = a1.executions[0]
        s2, e2 = a2.executions[0]
        overlap_start = max(s1, s2)
        overlap_end = min(e1, e2)
        assert overlap_start < overlap_end


@pytest.mark.asyncio
async def test_scheduler_concurrency_limit(tmp_path):
    """Test that max concurrency limit (semaphore) restricts parallel tasks."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = PipelineConfig(
        project_name="limit-dag-test",
        workspace=workspace,
        phases=[],
    )

    registry = AdapterRegistry()
    a1 = SleepyAdapter("codex", sleep_sec=0.1)
    registry.register("codex", a1)

    runner = PipelineRunner(config, registry=registry)

    # 3 parallel nodes, but max_concurrency = 2
    graph = TaskGraph()
    graph.add_node(TaskNode(task_id="t1", specialist="codex", command_template=[]))
    graph.add_node(TaskNode(task_id="t2", specialist="codex", command_template=[]))
    graph.add_node(TaskNode(task_id="t3", specialist="codex", command_template=[]))

    async with runner.store:
        run_id = await runner.store.create_run(config.project_name, task_graph_json=graph.to_json())
        runner.state = RunState(run_id=run_id, project_name=config.project_name)
        for node_id in graph.nodes:
            await runner.store.create_task(run_id, node_id, "codex")

        start_time = time.time()
        scheduler = DAGScheduler(max_concurrency=2)
        success = await scheduler.run(graph, runner, run_id)
        end_time = time.time()

        assert success is True
        total_time = end_time - start_time
        # With concurrency=2, 2 tasks run in parallel (0.1s), then the 3rd task runs (0.1s).
        # Total time should be >= 0.2s
        assert total_time >= 0.18


@pytest.mark.asyncio
async def test_scheduler_cascade_failure(tmp_path):
    """Test that failure propagates downstream without running dependent tasks."""
    workspace = tmp_path / "workspace"
    workspace.mkdir()

    config = PipelineConfig(
        project_name="fail-dag-test",
        workspace=workspace,
        phases=[],
    )

    registry = AdapterRegistry()
    a1 = FlakyAdapter("codex")
    a2 = SleepyAdapter("stitch", sleep_sec=0.01)
    registry.register("codex", a1)
    registry.register("stitch", a2)

    runner = PipelineRunner(config, registry=registry)

    # t1 (flaky) -> t2 (sleepy)
    graph = TaskGraph()
    graph.add_node(TaskNode(task_id="t1", specialist="codex", command_template=[]))
    graph.add_node(TaskNode(task_id="t2", specialist="stitch", command_template=[]))
    graph.add_edge("t1", "t2")

    async with runner.store:
        run_id = await runner.store.create_run(config.project_name, task_graph_json=graph.to_json())
        runner.state = RunState(run_id=run_id, project_name=config.project_name)
        await runner.store.create_task(run_id, "t1", "codex")
        await runner.store.create_task(run_id, "t2", "stitch")

        scheduler = DAGScheduler(max_concurrency=4)
        success = await scheduler.run(graph, runner, run_id)

        assert success is False

        # Verify t1 failed, and t2 was marked failed due to dependency check without running
        t1_status = await runner.store.get_task("t1")
        t2_status = await runner.store.get_task("t2")

        assert t1_status["status"] == "failed"
        assert t2_status["status"] == "failed"
        assert "Dependency 't1' failed" in t2_status["error_message"]
        # Make sure t2 SleepyAdapter was never called
        assert len(a2.executions) == 0

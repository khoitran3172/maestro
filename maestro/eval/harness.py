"""Evaluation benchmark harness for Maestro.

Defines and executes automated mock benchmark projects, printing
performance statistics (time, cost, grading quality).
"""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Dict, List, Any
from unittest.mock import patch

from maestro.adapters.base import TaskStatus, TaskOutput, SpecialistAdapter
from maestro.grader.base import GradeResult, RubricFailure
from maestro.grader.pipeline import GraderPipeline
from maestro.runner import PipelineConfig, PipelineRunner


class MockBenchmarkContext:
    """Provides controlled execution outcomes for specialist adapters and graders."""
    def __init__(self, name: str):
        self.name = name
        self.attempt_counts: Dict[str, int] = {}

    def get_output(self, specialist: str, task_id: str) -> TaskOutput:
        # Todo App
        if self.name == "Todo App":
            if specialist == "claude_code":
                return TaskOutput(
                    status=TaskStatus.SUCCESS,
                    duration_sec=5.0,
                    estimated_cost_usd=0.0150,
                    stdout="[Mock] Todo list frontend UI created.",
                    stderr="",
                )
            elif specialist == "codex":
                return TaskOutput(
                    status=TaskStatus.SUCCESS,
                    duration_sec=3.0,
                    estimated_cost_usd=0.0090,
                    stdout="[Mock] Todo list unit tests compiled and passed.",
                    stderr="",
                )
        # Blog Engine
        elif self.name == "Blog Engine":
            if specialist == "claude_code":
                return TaskOutput(
                    status=TaskStatus.SUCCESS,
                    duration_sec=6.0,
                    estimated_cost_usd=0.0180,
                    stdout="[Mock] SQLite database schema defined.",
                    stderr="",
                )
            elif specialist == "codex":
                attempt = self.attempt_counts.get(task_id, 0)
                self.attempt_counts[task_id] = attempt + 1
                if attempt == 0:
                    return TaskOutput(
                        status=TaskStatus.SUCCESS,
                        duration_sec=8.0,
                        estimated_cost_usd=0.0240,
                        stdout="[Mock] Backend endpoints created but has constraint bug.",
                        stderr="",
                    )
                else:
                    return TaskOutput(
                        status=TaskStatus.SUCCESS,
                        duration_sec=5.0,
                        estimated_cost_usd=0.0150,
                        stdout="[Mock] Backend constraints fixed and tests pass.",
                        stderr="",
                    )
            elif specialist == "stitch":
                return TaskOutput(
                    status=TaskStatus.SUCCESS,
                    duration_sec=4.0,
                    estimated_cost_usd=0.0120,
                    stdout="[Mock] Visual verification done.",
                    stderr="",
                )
        # Landing Page
        elif self.name == "Landing Page":
            if specialist == "grok_build":
                return TaskOutput(
                    status=TaskStatus.SUCCESS,
                    duration_sec=4.0,
                    estimated_cost_usd=0.0120,
                    stdout="[Mock] HTML page shell created.",
                    stderr="",
                )
            elif specialist == "antigravity":
                return TaskOutput(
                    status=TaskStatus.SUCCESS,
                    duration_sec=4.0,
                    estimated_cost_usd=0.0100,
                    stdout="[Mock] CSS styling injected and page deployed.",
                    stderr="",
                )

        return TaskOutput(
            status=TaskStatus.SUCCESS,
            duration_sec=2.0,
            estimated_cost_usd=0.0050,
            stdout="[Mock] Default specialist execution finished.",
            stderr="",
        )

    def get_grade(self, specialist: str, task_id: str) -> GradeResult:
        if self.name == "Blog Engine" and specialist == "codex":
            attempt = self.attempt_counts.get(task_id, 0)
            # The output generator increments count BEFORE grading runs
            if attempt <= 1:
                return GradeResult(
                    score=50.0,
                    passed=False,
                    failures=[RubricFailure(item="tests_pass", message="Database constraint check failed.")],
                    feedback="Failed. Please add database constraints for post tags."
                )
            else:
                return GradeResult(
                    score=88.0,
                    passed=True,
                    failures=[],
                    feedback="Database checks passed. Constraints are correct."
                )

        scores = {
            "Todo App": 95.0,
            "Blog Engine": 88.0,
            "Landing Page": 98.0
        }
        score = scores.get(self.name, 100.0)
        return GradeResult(
            score=score,
            passed=True,
            failures=[],
            feedback="All checks passed."
        )


async def run_benchmark(name: str, config_data: Dict[str, Any], workspace: Path) -> Dict[str, Any]:
    """Execute a single benchmark under mock parameters."""
    bench_dir = workspace / ".maestro_eval" / name.replace(" ", "_").lower()
    if bench_dir.exists():
        import shutil
        shutil.rmtree(bench_dir, ignore_errors=True)
    bench_dir.mkdir(parents=True, exist_ok=True)
    
    config_file = bench_dir / "pipeline_config.json"
    with open(config_file, "w", encoding="utf-8") as f:
        json.dump(config_data, f, indent=2)

    config = PipelineConfig.from_file(config_file)
    config.workspace = bench_dir

    runner = PipelineRunner(config)
    ctx = MockBenchmarkContext(name)

    # Bind mocks directly to the adapter instances in the runner's registry
    for adapter_name in runner.registry.available:
        adapter = runner.registry.get(adapter_name)
        
        def bind_mock(target_adapter):
            async def mock_run_method(task_input):
                await asyncio.sleep(0.01)
                return ctx.get_output(target_adapter.name, task_input.task_id)
            target_adapter.run = mock_run_method

            async def mock_health_check_method():
                return True
            target_adapter.health_check = mock_health_check_method
            
        bind_mock(adapter)

    async def mock_grade(self, task_input, task_output, workspace_path):
        await asyncio.sleep(0.01)
        specialist = None
        for p in config.phases:
            if f"phase_{p.phase}" == task_input.task_id:
                specialist = p.specialist
                break
        if not specialist:
            specialist = "generic"
        return ctx.get_grade(specialist, task_input.task_id)

    # Run execution with mocked components
    start_time = time.time()
    with patch.object(GraderPipeline, "grade", mock_grade):
        success = await runner.run()

    end_time = time.time()
    elapsed = end_time - start_time

    # Collect stats from SQLite
    async with runner.store as store:
        run_data = await store.get_run(runner.state.run_id) if runner.state else None
        tasks = await store.get_tasks_by_run(runner.state.run_id) if runner.state else []

    total_spent = run_data["total_spent_usd"] if run_data else 0.0
    status = run_data["status"] if run_data else "failed"

    task_summaries = []
    for t in tasks:
        task_summaries.append({
            "task_id": t["task_id"],
            "specialist": t["specialist"],
            "status": t["status"],
            "retries": t["retry_count"],
            "score": t["grade_score"],
            "duration": t["duration_sec"],
            "cost": t["estimated_cost"]
        })

    return {
        "name": name,
        "status": status,
        "duration_sec": elapsed,
        "cost_usd": total_spent,
        "tasks": task_summaries
    }


async def run_all_benchmarks(workspace: Path) -> List[Dict[str, Any]]:
    """Execute all packaged benchmarks and return their result structures."""
    benchmarks = [
        {
            "name": "Todo App",
            "config": {
                "project_name": "Todo App",
                "max_budget_usd": 1.0,
                "phases": [
                    {
                        "phase": 1,
                        "name": "Create Frontend Layout",
                        "specialist": "claude_code",
                        "command_template": ["echo", "frontend"]
                    },
                    {
                        "phase": 2,
                        "name": "Write Test Suite",
                        "specialist": "codex",
                        "command_template": ["echo", "tests"]
                    }
                ]
            }
        },
        {
            "name": "Blog Engine",
            "config": {
                "project_name": "Blog Engine",
                "max_budget_usd": 2.0,
                "phases": [
                    {
                        "phase": 1,
                        "name": "Design DB Schema",
                        "specialist": "claude_code",
                        "command_template": ["echo", "db"]
                    },
                    {
                        "phase": 2,
                        "name": "Build CRUD Endpoints",
                        "specialist": "codex",
                        "command_template": ["echo", "crud"]
                    },
                    {
                        "phase": 3,
                        "name": "Verify Visuals",
                        "specialist": "stitch",
                        "command_template": ["echo", "ui"]
                    }
                ]
            }
        },
        {
            "name": "Landing Page",
            "config": {
                "project_name": "Landing Page",
                "max_budget_usd": 1.5,
                "phases": [
                    {
                        "phase": 1,
                        "name": "Structure HTML markup",
                        "specialist": "grok_build",
                        "command_template": ["echo", "html"]
                    },
                    {
                        "phase": 2,
                        "name": "Inject Tailwind Styles",
                        "specialist": "antigravity",
                        "command_template": ["echo", "css"]
                    }
                ]
            }
        }
    ]

    results = []
    print("\n[EVAL] Starting Automated Evaluation Harness...")
    for b in benchmarks:
        print(f"  -> Running benchmark: {b['name']}...")
        res = await run_benchmark(b["name"], b["config"], workspace)
        results.append(res)

    print("\n========================================================")
    print(" EVALUATION HARNESS BENCHMARK RESULTS ")
    print("========================================================")
    print(f"| {'Benchmark Name':<15} | {'Status':<10} | {'Duration (s)':<12} | {'Cost (USD)':<10} |")
    print(f"| :--- | :--- | :--- | :--- |")
    for r in results:
        print(f"| {r['name']:<15} | {r['status'].upper():<10} | {r['duration_sec']:<12.3f} | ${r['cost_usd']:<9.4f} |")
    print("========================================================\n")
    return results


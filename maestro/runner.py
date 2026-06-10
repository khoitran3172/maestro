"""Maestro pipeline runner — Sprint 4 Feedback Loop integration.

Orchestrates specialist calls through a phase pipeline.
Every call goes through adapters → error_handler → cost_tracker → logger → db.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from maestro.cost_tracker import BudgetExceededError, BudgetStatus, CostTracker
from maestro.error_handler import RetryPolicy
from maestro.logger import MaestroLogger, hash_input
from maestro.db.store import MaestroStore
from maestro.checkpoint import CheckpointManager, migrate_json_state
from maestro.state_machine import validate_task_transition, TaskState
from maestro.adapter_registry import AdapterRegistry
from maestro.adapters.base import TaskInput, TaskOutput, TaskStatus
from maestro.grader import GraderPipeline
from maestro.feedback import FeedbackBuilder
from maestro.dag.task_graph import TaskGraph, TaskNode


@dataclass
class PhaseConfig:
    """Configuration for one pipeline phase."""
    phase: int
    name: str
    specialist: str
    command_template: list[str]   # Template with {placeholders}
    timeout_sec: int = 600
    rubric: Optional[dict] = None
    max_retries: int = 2


@dataclass
class PipelineConfig:
    """Full pipeline configuration."""
    project_name: str
    workspace: Path
    phases: list[PhaseConfig]
    max_budget_usd: Optional[float] = None

    @classmethod
    def from_file(cls, config_path: Path) -> PipelineConfig:
        """Load pipeline config from JSON file."""
        with open(config_path) as f:
            data = json.load(f)

        phases = [
            PhaseConfig(
                phase=p["phase"],
                name=p["name"],
                specialist=p["specialist"],
                command_template=p["command_template"],
                timeout_sec=p.get("timeout_sec", 600),
                rubric=p.get("rubric"),
                max_retries=p.get("max_retries", 2),
            )
            for p in data["phases"]
        ]

        return cls(
            project_name=data["project_name"],
            workspace=Path(data.get("workspace", ".")),
            phases=phases,
            max_budget_usd=data.get("max_budget_usd"),
        )


@dataclass
class RunState:
    """Mutable state for a single pipeline run in memory (for compatibility)."""
    run_id: str
    project_name: str
    status: str = "running"  # running, completed, failed, paused
    current_phase: int = 0
    total_spent_usd: float = 0.0
    phase_results: dict[int, dict[str, Any]] = field(default_factory=dict)
    started_at: float = field(default_factory=time.time)
    completed_at: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "run_id": self.run_id,
            "project_name": self.project_name,
            "status": self.status,
            "current_phase": self.current_phase,
            "total_spent_usd": round(self.total_spent_usd, 4),
            "phase_results": self.phase_results,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> RunState:
        return cls(
            run_id=data["run_id"],
            project_name=data["project_name"],
            status=data.get("status", "running"),
            current_phase=data.get("current_phase", 0),
            total_spent_usd=data.get("total_spent_usd", 0.0),
            phase_results={int(k): v for k, v in data.get("phase_results", {}).items()},
            started_at=data.get("started_at", time.time()),
            completed_at=data.get("completed_at"),
        )


class PipelineRunner:
    """Sequential pipeline runner for Maestro.

    Runs phases in order, with error handling, cost tracking, and logging.
    """

    def __init__(self, config: PipelineConfig, registry: Optional[AdapterRegistry] = None):
        self.config = config
        self.maestro_dir = config.workspace / ".maestro"
        self.maestro_dir.mkdir(parents=True, exist_ok=True)

        # Initialize subsystems
        self.logger = MaestroLogger(self.maestro_dir)
        self.cost_tracker = CostTracker.from_env(persist_dir=self.maestro_dir)
        self.retry_policy = RetryPolicy(max_retries=2)

        # Override budget if specified in config
        if config.max_budget_usd is not None:
            self.cost_tracker.max_budget_usd = config.max_budget_usd

        # Use passed registry or default
        if registry:
            self.registry = registry
        else:
            self.registry = AdapterRegistry()
            self.registry.register_defaults()

        # Database store and checkpoint manager
        self.store = MaestroStore(self.maestro_dir / "maestro.db")
        self.checkpoint = CheckpointManager(self.store)
        
        # State in memory (will be initialized/synced during run)
        self.state: Optional[RunState] = None

    async def _transition_task(self, task_id: str, target_status: str, **kwargs) -> None:
        """Helper to transition task status with state machine validation."""
        task = await self.store.get_task(task_id)
        if not task:
            return
        
        current_status = task["status"]
        if current_status == "grading" and target_status == "queued":
            # intermediate transition for crash recovery
            await self.store.update_task_status(task_id, "kicked_back")
            current_status = "kicked_back"
            
        validate_task_transition(current_status, target_status)
        await self.store.update_task_status(task_id, target_status, **kwargs)

    def _build_graph_from_phases(self) -> TaskGraph:
        """Convert linear PhaseConfig list to a TaskGraph."""
        graph = TaskGraph()
        prev_node_id = None
        for p in self.config.phases:
            node_id = f"phase_{p.phase}"
            node = TaskNode(
                task_id=node_id,
                specialist=p.specialist,
                command_template=p.command_template,
                phase=p.phase,
                input_artifacts=[],
                rubric=p.rubric or {},
                timeout_sec=p.timeout_sec,
                max_retries=p.max_retries,
                prompt=p.name,
            )
            graph.add_node(node)
            if prev_node_id:
                graph.add_edge(prev_node_id, node_id)
            prev_node_id = node_id
        return graph

    async def run(self, resume_run_id: Optional[str] = None) -> bool:
        """Execute the full pipeline asynchronously using DAGScheduler."""
        async with self.store:
            # One-time migration from state.json if exists
            json_state_path = self.maestro_dir / "state.json"
            if json_state_path.exists():
                try:
                    migrated_id = await migrate_json_state(json_state_path, self.store)
                    if migrated_id and not resume_run_id:
                        resume_run_id = migrated_id
                except Exception as e:
                    self.logger.error(f"Migration from state.json failed: {e}")

            if resume_run_id:
                # Resume existing run
                resume_plan = await self.checkpoint.prepare_resume(resume_run_id)
                run_id = resume_run_id
                
                # Setup in-memory state compatibility
                self.state = RunState(
                    run_id=run_id,
                    project_name=resume_plan["project_name"],
                    status="running",
                    total_spent_usd=resume_plan["total_spent_usd"],
                )
                
                # Warn if workspace was modified
                if resume_plan["modified_artifacts"]:
                    print("\n⚠️  WARNING: Workspace modifications detected since last run:")
                    for mod in resume_plan["modified_artifacts"]:
                        print(f"  - {mod['file_path']} ({mod['issue']})")
                    self.logger.warning(
                        "Workspace modifications detected on resume",
                        modified_count=len(resume_plan["modified_artifacts"]),
                    )

                if resume_plan["run_status"] == "completed":
                    self.state.status = "completed"
                    return True

                await self.store.update_run_status(run_id, "running")

                # Fetch task graph from run database
                run_data = await self.store.get_run(run_id)
                if run_data and run_data.get("task_graph_json"):
                    graph = TaskGraph.from_json(run_data["task_graph_json"])
                else:
                    graph = self._build_graph_from_phases()
            else:
                # Create fresh run
                graph = self._build_graph_from_phases()
                run_id = await self.store.create_run(
                    project_name=self.config.project_name,
                    max_budget_usd=self.cost_tracker.max_budget_usd,
                    task_graph_json=graph.to_json(),
                )
                self.state = RunState(
                    run_id=run_id,
                    project_name=self.config.project_name,
                    status="running",
                )

                # Create all tasks in DB
                for node in graph.nodes.values():
                    await self.store.create_task(
                        run_id=run_id,
                        task_id=node.task_id,
                        specialist=node.specialist,
                        phase=node.phase,
                        prompt=node.prompt or node.task_id,
                        max_retries=node.max_retries,
                    )

            # Sync in-memory state with existing DB task results for completed tasks
            db_tasks = await self.store.get_tasks_by_run(run_id)
            for t in db_tasks:
                if t["status"] == "done":
                    self.state.phase_results[t["phase"]] = {
                        "status": "success",
                        "duration_sec": t["duration_sec"],
                        "cost_usd": t["estimated_cost"],
                        "attempt": t["retry_count"] + 1,
                    }

            self.logger.info(
                f"Pipeline started: {self.config.project_name}",
                run_id=run_id,
                total_phases=len(self.config.phases),
            )

            try:
                from maestro.dag.scheduler import DAGScheduler
                scheduler = DAGScheduler(max_concurrency=4)
                success = await scheduler.run(graph, self, run_id)

                if not success:
                    self.state.status = "failed"
                    await self.store.update_run_status(run_id, "failed")
                    self.logger.error(
                        "Pipeline execution failed",
                        run_id=run_id,
                    )
                    try:
                        from maestro.learning import generate_lessons_learned
                        generate_lessons_learned(self.store.db_path, run_id)
                    except Exception as e:
                        self.logger.error(f"Learning layer failure: {e}")
                    return False

                self.state.status = "completed"
                self.state.completed_at = time.time()
                await self.store.update_run_status(run_id, "completed")

                total_duration = self.state.completed_at - self.state.started_at
                self.logger.info(
                    "Pipeline completed successfully",
                    run_id=run_id,
                    total_duration_sec=round(total_duration, 1),
                    **self.cost_tracker.summary(),
                )
                self._print_summary()
                try:
                    from maestro.learning import generate_lessons_learned
                    generate_lessons_learned(self.store.db_path, run_id)
                except Exception as e:
                    self.logger.error(f"Learning layer failure: {e}")
                return True

            except BudgetExceededError as e:
                self.state.status = "paused"
                await self.store.update_run_status(run_id, "paused")
                self.logger.log_budget_exceeded(
                    self.cost_tracker.total_spent_usd,
                    self.cost_tracker.max_budget_usd,
                )
                print(f"\n[ERROR] {e}")
                print("Run `maestro resume` after increasing MAX_USD to continue.")
                return False

            except KeyboardInterrupt:
                self.state.status = "paused"
                await self.store.update_run_status(run_id, "paused")
                self.logger.warning("Pipeline interrupted by user (Ctrl+C)")
                print("\n[PAUSED] Pipeline paused. Run `maestro resume` to continue.")
                return False

    def _print_summary(self) -> None:
        """Print human-readable pipeline summary."""
        summary = self.cost_tracker.summary()
        total_time = (self.state.completed_at or time.time()) - self.state.started_at

        print("\n" + "=" * 60)
        print("MAESTRO PIPELINE SUMMARY")
        print("=" * 60)
        print(f"  Project:   {self.config.project_name}")
        print(f"  Run ID:    {self.state.run_id}")
        print(f"  Status:    {self.state.status}")
        print(f"  Duration:  {total_time:.1f}s")
        print(f"  Cost:      ${summary['total_spent_usd']:.4f} / ${summary['max_budget_usd']:.2f} "
              f"({summary['utilization_pct']:.1f}%)")
        print(f"  Phases:    {len(self.state.phase_results)} / {len(self.config.phases)}")

        if summary["by_specialist"]:
            print(f"\n  Cost by specialist:")
            for specialist, cost in summary["by_specialist"].items():
                print(f"    {specialist}: ${cost:.4f}")

        print("=" * 60)

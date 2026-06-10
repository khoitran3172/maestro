"""Async DAG scheduler and executor for Maestro tasks."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Optional, TYPE_CHECKING

from maestro.adapters.base import TaskInput, TaskOutput, TaskStatus
from maestro.cost_tracker import BudgetStatus, BudgetExceededError
from maestro.grader import GraderPipeline
from maestro.feedback import FeedbackBuilder
from maestro.dag.task_graph import TaskGraph, TaskNode

if TYPE_CHECKING:
    from maestro.runner import PipelineRunner


class DAGScheduler:
    """Orchestrates parallel execution of tasks in a TaskGraph respecting dependencies."""

    def __init__(self, max_concurrency: int = 4):
        self.max_concurrency = max_concurrency
        self.semaphore = asyncio.Semaphore(max_concurrency)

    async def run(self, graph: TaskGraph, runner: PipelineRunner, run_id: str) -> bool:
        """Run the DAG graph asynchronously.

        Returns True if all nodes complete successfully, False otherwise.
        """
        # Validate graph before execution
        graph.validate()

        # Initialize event and failure tracking for each node
        node_events = {node_id: asyncio.Event() for node_id in graph.nodes}
        node_failures: dict[str, bool] = {}

        # Set events for already completed nodes (support resume)
        for node_id in graph.nodes:
            task_data = await runner.store.get_task(node_id)
            if task_data and task_data["status"] == "done":
                node_events[node_id].set()

        async def execute_node(node_id: str) -> None:
            node = graph.nodes[node_id]
            deps = graph.get_dependencies(node_id)

            # 1. Wait for all parent dependency tasks to complete
            if deps:
                await asyncio.gather(*(node_events[dep].wait() for dep in deps))

            # 2. Check if any parent task failed or is not done
            for dep in deps:
                dep_task = await runner.store.get_task(dep)
                if not dep_task or dep_task["status"] != "done":
                    # Mark current task as failed because parent failed (cascade failure)
                    print(f"    [BLOCKED] Task '{node_id}' blocked because dependency '{dep}' failed.")
                    await runner.store.update_task_status(
                        node_id, "failed",
                        error_message=f"Dependency '{dep}' failed or was not completed."
                    )
                    node_failures[node_id] = True
                    node_events[node_id].set()
                    return

            # Skip execution if this node is already done
            task_data = await runner.store.get_task(node_id)
            if task_data and task_data["status"] == "done":
                return

            # 3. Execute with semaphore concurrency limit
            async with self.semaphore:
                success = await self._run_node_execution(node, runner, run_id)
                if not success:
                    node_failures[node_id] = True

            # Trigger downstream tasks
            node_events[node_id].set()

        # Launch execution of all nodes concurrently
        tasks = [asyncio.create_task(execute_node(node_id)) for node_id in graph.nodes]
        await asyncio.gather(*tasks)

        # Return True if no node failed
        return len(node_failures) == 0

    async def _run_node_execution(self, node: TaskNode, runner: PipelineRunner, run_id: str) -> bool:
        """Run a single task node's execution retry loop and quality check with Git worktree and prompt safety isolation."""
        task_id = node.task_id

        # Load session_id if any (for resume)
        task_data = await runner.store.get_task(task_id)
        session_id = task_data["session_id"] if task_data else None

        # Transition state: pending -> queued -> running
        await runner._transition_task(task_id, "queued")
        await runner._transition_task(task_id, "running")

        runner.logger.info(
            f"Starting task {task_id} [{node.specialist}]: {node.specialist}",
            specialist=node.specialist,
            task_id=task_id,
        )

        from maestro.security.prompt_guard import is_safe_prompt
        import os
        from maestro.git.branch_manager import GitBranchManager, GitError

        # Validate prompt safety
        prompt_text = node.prompt or node.task_id
        if not is_safe_prompt(prompt_text):
            print(f"  [SAFETY ALERT] task '{task_id}' prompt flagged as unsafe!")
            await runner._transition_task(
                task_id, "failed",
                error_message="Prompt injection attempt detected."
            )
            if runner.state:
                runner.state.phase_results[node.phase] = {
                    "status": "failed",
                    "error": "Prompt injection attempt detected.",
                    "attempts": 1,
                }
            return False

        # Create Git worktree
        branch_manager = GitBranchManager(runner.config.workspace)
        worktree_cwd = None
        use_git = os.environ.get("MAESTRO_GIT_ISOLATION", "1") == "1"
        
        if use_git:
            try:
                worktree_cwd = await branch_manager.create_isolated_worktree(task_id, node.specialist)
            except GitError as e:
                print(f"  [WARNING] Failed to create isolated Git worktree: {e}")
                worktree_cwd = None

        feedback: Optional[str] = None
        feedback_artifacts: list[Path] = []
        execution_success = False

        try:
            for attempt in range(node.max_retries + 1):
                # Check budget
                budget_status = runner.cost_tracker.check_budget()
                if budget_status == BudgetStatus.EXCEEDED:
                    runner.cost_tracker.enforce_budget()
                elif budget_status == BudgetStatus.WARNING:
                    runner.logger.log_budget_warning(
                        runner.cost_tracker.total_spent_usd,
                        runner.cost_tracker.max_budget_usd,
                    )
                    print(f"[BUDGET WARNING] Budget at {runner.cost_tracker.utilization_pct:.0f}% "
                          f"(${runner.cost_tracker.total_spent_usd:.2f} / "
                          f"${runner.cost_tracker.max_budget_usd:.2f})")

                print(f"  [RUN] Task {task_id} [{node.specialist}] attempt {attempt + 1}/{node.max_retries + 1}...")

                # Build TaskInput
                task_input = TaskInput(
                    task_id=task_id,
                    prompt=prompt_text,
                    timeout_sec=node.timeout_sec,
                    max_retries=node.max_retries,
                    rubric=node.rubric or {},
                    session_id=session_id,
                    feedback=feedback,
                    feedback_artifacts=feedback_artifacts,
                    worktree_path=worktree_cwd,
                    extra={
                        "command_template": node.command_template,
                        "cwd": worktree_cwd or runner.config.workspace,
                    }
                )

                # Get adapter and execute
                adapter = runner.registry.get(node.specialist)
                result = await adapter.run(task_input)

                # Record cost
                cost_entry = runner.cost_tracker.record(
                    specialist=node.specialist,
                    task_id=task_id,
                    duration_sec=result.duration_sec,
                )
                await runner.store.record_cost(
                    run_id=run_id,
                    specialist=node.specialist,
                    duration_sec=result.duration_sec,
                    estimated_cost=cost_entry.estimated_cost_usd,
                    task_id=task_id,
                    method=cost_entry.method,
                )

                # Update runner state cost (thread-safe, memory status only)
                if runner.state:
                    runner.state.total_spent_usd = runner.cost_tracker.total_spent_usd
                await runner.store.update_run_cost(run_id, runner.cost_tracker.total_spent_usd)

                # Log specialist call
                runner.logger.log_specialist_call(
                    phase=node.phase,
                    specialist=node.specialist,
                    task_id=task_id,
                    result=result,
                    cost_usd=cost_entry.estimated_cost_usd,
                )

                # Transition task to grading
                await runner._transition_task(task_id, "grading")

                # Run multi-modal grading
                grader = GraderPipeline()
                grade_result = await grader.grade(task_input, result, worktree_cwd or runner.config.workspace)

                # Log grade
                runner.logger.log_grade(
                    task_id=task_id,
                    specialist=node.specialist,
                    score=grade_result.score,
                    passed=grade_result.passed,
                    failures=[f.item for f in grade_result.failures],
                )

                if grade_result.passed:
                    # Save outputs in checkpoints
                    for artifact_path in result.artifacts:
                        if artifact_path.exists():
                            await runner.checkpoint.checkpoint_artifact(task_id, run_id, artifact_path)

                    # Transition task to done
                    await runner._transition_task(
                        task_id, "done",
                        grade_score=grade_result.score,
                        grade_feedback=grade_result.feedback,
                        duration_sec=result.duration_sec,
                        estimated_cost=cost_entry.estimated_cost_usd,
                        session_id=result.session_id,
                    )

                    if runner.state:
                        runner.state.phase_results[node.phase] = {
                            "status": "success",
                            "duration_sec": result.duration_sec,
                            "cost_usd": cost_entry.estimated_cost_usd,
                            "attempt": attempt + 1,
                        }

                    print(f"  [OK] Task {task_id} completed ({result.duration_sec:.1f}s, ${cost_entry.estimated_cost_usd:.4f})")
                    execution_success = True
                    break

                # Grade failed
                print(f"  [FAIL] Task {task_id} failed quality checks: {grade_result.feedback}")

                if runner.retry_policy.should_retry(result, attempt) or (not grade_result.passed and attempt < node.max_retries):
                    delay = runner.retry_policy.delay_sec(attempt)
                    feedback = FeedbackBuilder.build_feedback(grade_result, result)
                    feedback_artifacts = list(result.artifacts)
                    session_id = result.session_id

                    runner.logger.log_kickback(
                        task_id=task_id,
                        specialist=node.specialist,
                        attempt=attempt + 1,
                        reason=grade_result.feedback or result.error_message,
                    )

                    # Record feedback history
                    prev_hash = None
                    if result.artifacts:
                        from maestro.checkpoint import compute_file_hash
                        try:
                            prev_hash = compute_file_hash(result.artifacts[0])
                        except Exception:
                            pass
                    await runner.store.record_feedback(
                        task_id=task_id,
                        run_id=run_id,
                        iteration=attempt + 1,
                        grade_score=grade_result.score,
                        rubric_failures=[f.item for f in grade_result.failures],
                        issues_text=feedback,
                        prev_artifact_hash=prev_hash,
                    )

                    await runner._transition_task(
                        task_id, "kicked_back",
                        error_message=grade_result.feedback,
                    )
                    print(f"  [RETRY] Retrying in {delay:.0f}s...")
                    await asyncio.sleep(delay)
                    # Re-transition to queued and running
                    await runner._transition_task(task_id, "queued")
                    await runner._transition_task(task_id, "running")
                else:
                    runner.logger.error(
                        f"Task {task_id} failed after {attempt + 1} attempts",
                        specialist=node.specialist,
                        last_error=grade_result.feedback or result.error_message,
                    )
                    # Record final feedback in DB
                    prev_hash = None
                    if result.artifacts:
                        from maestro.checkpoint import compute_file_hash
                        try:
                            prev_hash = compute_file_hash(result.artifacts[0])
                        except Exception:
                            pass
                    await runner.store.record_feedback(
                        task_id=task_id,
                        run_id=run_id,
                        iteration=attempt + 1,
                        grade_score=grade_result.score,
                        rubric_failures=[f.item for f in grade_result.failures],
                        issues_text=grade_result.feedback or result.error_message,
                        prev_artifact_hash=prev_hash,
                    )
                    break
        finally:
            if use_git and worktree_cwd:
                if execution_success:
                    try:
                        await branch_manager.merge_and_cleanup(task_id, node.specialist)
                    except GitError as e:
                        print(f"  ❌ Failed to merge Git branch back: {e}")
                        await runner._transition_task(
                            task_id, "failed",
                            error_message=f"Git merge failed: {e}"
                        )
                        execution_success = False
                else:
                    await branch_manager.cleanup_failed(task_id, node.specialist)

        if not execution_success:
            task_status_data = await runner.store.get_task(task_id)
            if task_status_data and task_status_data["status"] != "failed":
                await runner._transition_task(
                    task_id, "failed",
                    error_message=grade_result.feedback or result.error_message if 'grade_result' in locals() and 'result' in locals() else "Execution failed."
                )
            if runner.state:
                runner.state.phase_results[node.phase] = {
                    "status": "failed",
                    "error": grade_result.feedback or result.error_message if 'grade_result' in locals() and 'result' in locals() else "Execution failed.",
                    "attempts": node.max_retries + 1,
                }
            return False

        return True

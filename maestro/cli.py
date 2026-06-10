"""Maestro CLI entry point.

Usage:
    python -m maestro run pipeline.json     # Run pipeline
    python -m maestro resume                # Resume latest paused/crashed pipeline
    python -m maestro resume --run-id <id>  # Resume specific pipeline
    python -m maestro status                # Show latest run status
    python -m maestro status --run-id <id>  # Show specific run status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import sys
from pathlib import Path
from typing import Optional

from maestro.runner import PipelineConfig, PipelineRunner
from maestro.db.store import MaestroStore


async def run_pipeline(config_path: Path, workspace: Path) -> None:
    """Run a new pipeline execution."""
    config = PipelineConfig.from_file(config_path)
    config.workspace = workspace.resolve()
    
    # Ensure .maestro directory exists
    maestro_dir = config.workspace / ".maestro"
    maestro_dir.mkdir(parents=True, exist_ok=True)
    
    # Copy configuration file for resume support
    shutil.copy(config_path, maestro_dir / "pipeline_config.json")
    
    runner = PipelineRunner(config)
    success = await runner.run()
    sys.exit(0 if success else 1)


async def resume_pipeline(workspace: Path, run_id: Optional[str] = None) -> None:
    """Resume an existing pipeline execution."""
    workspace = workspace.resolve()
    db_path = workspace / ".maestro" / "maestro.db"
    if not db_path.exists():
        # Check if legacy state.json exists for one-time migration
        legacy_state = workspace / ".maestro" / "state.json"
        if not legacy_state.exists():
            print("[ERROR] No active runs found. Run `maestro run <config>` first.")
            sys.exit(1)

    # Load config from .maestro/pipeline_config.json
    config_path = workspace / ".maestro" / "pipeline_config.json"
    if not config_path.exists():
        print("[ERROR] Pipeline config not found at .maestro/pipeline_config.json")
        sys.exit(1)

    config = PipelineConfig.from_file(config_path)
    config.workspace = workspace
    runner = PipelineRunner(config)

    # If run_id not provided, find the latest run from the database
    target_run_id = run_id
    if not target_run_id:
        async with runner.store as store:
            latest = await store.get_latest_run()
            if latest:
                target_run_id = latest["run_id"]
            else:
                # Might need migration
                target_run_id = None

    success = await runner.run(resume_run_id=target_run_id)
    sys.exit(0 if success else 1)


async def show_status(workspace: Path, run_id: Optional[str] = None) -> None:
    """Print run status details."""
    workspace = workspace.resolve()
    db_path = workspace / ".maestro" / "maestro.db"
    
    # Try one-time migration if db doesn't exist but JSON state does
    if not db_path.exists():
        legacy_state = workspace / ".maestro" / "state.json"
        if legacy_state.exists():
            print("[MIGRATE] Migrating legacy state.json to SQLite database...")
            store = MaestroStore(db_path)
            from maestro.checkpoint import migrate_json_state
            async with store:
                await migrate_json_state(legacy_state, store)
        else:
            print("No active run found.")
            sys.exit(0)

    store = MaestroStore(db_path)
    async with store:
        target_run_id = run_id
        if not target_run_id:
            latest = await store.get_latest_run()
            if latest:
                target_run_id = latest["run_id"]
            else:
                print("No runs found in database.")
                sys.exit(0)

        summary = await store.get_run_summary(target_run_id)
        if not summary:
            print(f"Run ID '{target_run_id}' not found.")
            sys.exit(1)

        run = summary["run"]
        task_counts = summary["task_counts"]
        total_cost = summary["total_cost_usd"]
        cost_by_specialist = summary["cost_by_specialist"]

        print(f"Run ID:     {run['run_id']}")
        print(f"Project:    {run['project_name']}")
        print(f"Status:     {run['status'].upper()}")
        print(f"Cost:       ${total_cost:.4f} / ${run['max_budget_usd'] or 0.0:.2f}")
        print(f"Created At: {run['created_at']}")
        
        # Show tasks
        tasks = await store.get_tasks_by_run(target_run_id)
        if tasks:
            print("\nTasks:")
            for t in tasks:
                status_icon = "[OK]" if t["status"] == "done" else "[FAIL]" if t["status"] == "failed" else "[RUNNING]" if t["status"] == "running" else "[PAUSED]"
                cost_str = f" (${t['estimated_cost']:.4f})" if t["estimated_cost"] else ""
                err_str = f" - Error: {t['error_message']}" if t["error_message"] else ""
                print(f"  Phase {t['phase']}: {status_icon} {t['specialist']} - {t['status'].upper()}{cost_str}{err_str}")

        if cost_by_specialist:
            print("\nCost Breakdown:")
            for specialist, cost in cost_by_specialist.items():
                print(f"  {specialist}: ${cost:.4f}")


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="maestro",
        description="Maestro — AI Orchestrator for multi-specialist pipelines",
    )
    subparsers = parser.add_subparsers(dest="command", help="Available commands")

    # Run command
    run_parser = subparsers.add_parser("run", help="Run a pipeline from config file")
    run_parser.add_argument("config", type=Path, help="Path to pipeline config JSON")
    run_parser.add_argument("--workspace", type=Path, default=Path("."),
                           help="Workspace directory (default: current dir)")

    # Resume command
    resume_parser = subparsers.add_parser("resume", help="Resume a paused/crashed pipeline")
    resume_parser.add_argument("--workspace", type=Path, default=Path("."),
                              help="Workspace directory containing .maestro/")
    resume_parser.add_argument("--run-id", type=str, help="Optional specific run ID to resume")

    # Status command
    status_parser = subparsers.add_parser("status", help="Show current run status")
    status_parser.add_argument("--workspace", type=Path, default=Path("."),
                              help="Workspace directory containing .maestro/")
    status_parser.add_argument("--run-id", type=str, help="Optional specific run ID")

    # Dashboard command
    dashboard_parser = subparsers.add_parser("dashboard", help="Start the observability web dashboard")
    dashboard_parser.add_argument("--workspace", type=Path, default=Path("."),
                                  help="Workspace directory containing .maestro/")
    dashboard_parser.add_argument("--port", type=int, default=8000,
                                  help="Port to run dashboard server on (default: 8000)")

    # Eval command
    eval_parser = subparsers.add_parser("eval", help="Run automated evaluation benchmarks")
    eval_parser.add_argument("--workspace", type=Path, default=Path("."),
                             help="Workspace directory for benchmarks (default: current dir)")

    args = parser.parse_args()

    if args.command == "run":
        asyncio.run(run_pipeline(args.config, args.workspace))
    elif args.command == "resume":
        asyncio.run(resume_pipeline(args.workspace, args.run_id))
    elif args.command == "status":
        asyncio.run(show_status(args.workspace, args.run_id))
    elif args.command == "dashboard":
        from maestro.dashboard.server import start_server
        workspace = args.workspace.resolve()
        port = args.port
        print(f"[START] Starting Maestro Observability Dashboard on http://localhost:{port}")
        print(f"[DIR] Workspace: {workspace}")
        print("Press Ctrl+C to stop the server.")
        server = start_server(workspace, port)
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print("\n[STOP] Dashboard server stopped.")
            sys.exit(0)
    elif args.command == "eval":
        from maestro.eval.harness import run_all_benchmarks
        workspace = args.workspace.resolve()
        asyncio.run(run_all_benchmarks(workspace))
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()


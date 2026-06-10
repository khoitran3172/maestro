"""Tests for the Maestro Observability Dashboard server."""

from __future__ import annotations

import json
import socket
import threading
import time
from pathlib import Path
import pytest
import urllib.request

from maestro.dashboard.server import start_server
from maestro.db.store import MaestroStore


def get_free_port() -> int:
    s = socket.socket()
    s.bind(("", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def fetch_url(url: str) -> tuple[int, str]:
    """Helper to fetch a URL synchronously and return status + text."""
    try:
        req = urllib.request.Request(url)
        with urllib.request.urlopen(req) as response:
            return response.status, response.read().decode("utf-8")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8")
    except Exception as e:
        return 500, str(e)


@pytest.mark.asyncio
async def test_dashboard_server(tmp_path: Path) -> None:
    # Setup SQLite test DB structure under tmp_path
    db_path = tmp_path / ".maestro" / "maestro.db"
    store = MaestroStore(db_path)
    await store.initialize()
    
    run_id = await store.create_run("Test Observability Project", max_budget_usd=15.0)
    await store.create_task(run_id, "task_obs_1", "claude_code", phase=1)
    await store.update_task_status("task_obs_1", "done", grade_score=95.0, estimated_cost=0.03)
    await store.record_cost(run_id, "claude_code", 12.0, 0.03, task_id="task_obs_1")
    await store.close()
    
    # Create mock log file
    log_file = tmp_path / ".maestro" / "log.jsonl"
    with open(log_file, "w", encoding="utf-8") as f:
        f.write(json.dumps({"ts": time.time(), "level": "INFO", "event": "pipeline_start", "msg": "Started testing"}) + "\n")
        
    # Create mock artifact
    art_file = tmp_path / "obs_artifact.txt"
    art_file.write_text("Artifact preview content 123", encoding="utf-8")
    
    # Start server
    port = get_free_port()
    server = start_server(tmp_path, port)
    
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    
    # Allow the thread to start serving
    time.sleep(0.2)
    
    base_url = f"http://localhost:{port}"
    try:
        # 1. Test index.html endpoint
        code, text = fetch_url(f"{base_url}/")
        assert code == 200
        assert "Maestro Observability" in text
        
        # 2. Test runs list endpoint
        code, text = fetch_url(f"{base_url}/api/runs")
        assert code == 200
        runs = json.loads(text)
        assert len(runs) == 1
        assert runs[0]["project_name"] == "Test Observability Project"
        
        # 3. Test run details endpoint
        code, text = fetch_url(f"{base_url}/api/run/{run_id}")
        assert code == 200
        run_details = json.loads(text)
        assert run_details["run"]["project_name"] == "Test Observability Project"
        assert len(run_details["tasks"]) == 1
        assert run_details["tasks"][0]["task_id"] == "task_obs_1"
        assert len(run_details["cost_log"]) == 1
        
        # 4. Test logs endpoint
        code, text = fetch_url(f"{base_url}/api/logs")
        assert code == 200
        logs = json.loads(text)
        assert len(logs) == 1
        assert logs[0]["event"] == "pipeline_start"
        
        # 5. Test artifact reader endpoint
        escaped_path = urllib.parse.quote(str(art_file))
        code, text = fetch_url(f"{base_url}/api/artifact?path={escaped_path}")
        assert code == 200
        assert json.loads(text)["content"] == "Artifact preview content 123"
        
        # 6. Test artifact traversal security check (must return 403 or 404 or raise)
        escaped_bad_path = urllib.parse.quote(str(tmp_path / "../../../bad_file.txt"))
        code, text = fetch_url(f"{base_url}/api/artifact?path={escaped_bad_path}")
        assert code in (403, 404)
        
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=1.0)

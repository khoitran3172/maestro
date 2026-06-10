"""Lightweight HTTP server serving the Maestro Observability Dashboard.

Connects directly to the local SQLite database to fetch runs, tasks, costs, and logs,
serving the glassmorphism frontend index.html page.
"""

from __future__ import annotations

import json
import urllib.parse
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
import sqlite3
import sys
from typing import Any


def make_handler(workspace_dir: Path):
    class DashboardHTTPRequestHandler(BaseHTTPRequestHandler):
        def log_message(self, format: str, *args: Any) -> None:
            # Silence standard server logs to prevent flooding the CLI
            pass

        def _send_response(self, status_code: int, content: bytes, content_type: str) -> None:
            self.send_response(status_code)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(content)))
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(content)

        def do_OPTIONS(self) -> None:
            self.send_response(200)
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Methods", "GET, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()

        def do_GET(self) -> None:
            parsed_url = urllib.parse.urlparse(self.path)
            path = parsed_url.path
            query_params = urllib.parse.parse_qs(parsed_url.query)

            db_path = workspace_dir / ".maestro" / "maestro.db"

            if path in ("/", "/index.html"):
                html_file = Path(__file__).parent / "index.html"
                if html_file.exists():
                    with open(html_file, "rb") as f:
                        self._send_response(200, f.read(), "text/html")
                else:
                    self._send_response(404, b"index.html not found", "text/plain")

            elif path == "/api/runs":
                if not db_path.exists():
                    self._send_response(200, json.dumps([]).encode("utf-8"), "application/json")
                    return
                try:
                    conn = sqlite3.connect(str(db_path))
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute("SELECT * FROM runs ORDER BY created_at DESC")
                    rows = cursor.fetchall()
                    runs = [dict(row) for row in rows]
                    conn.close()
                    self._send_response(200, json.dumps(runs).encode("utf-8"), "application/json")
                except Exception as e:
                    self._send_response(500, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")

            elif path.startswith("/api/run/"):
                run_id = path[len("/api/run/"):]
                if not db_path.exists():
                    self._send_response(404, json.dumps({"error": "No database found"}).encode("utf-8"), "application/json")
                    return
                try:
                    conn = sqlite3.connect(str(db_path))
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    
                    cursor.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,))
                    run_row = cursor.fetchone()
                    if not run_row:
                        conn.close()
                        self._send_response(404, json.dumps({"error": f"Run {run_id} not found"}).encode("utf-8"), "application/json")
                        return
                    
                    # Tasks
                    cursor.execute("SELECT * FROM tasks WHERE run_id = ? ORDER BY phase, task_id", (run_id,))
                    tasks = [dict(row) for row in cursor.fetchall()]
                    
                    # Cost Log
                    cursor.execute("SELECT * FROM cost_log WHERE run_id = ? ORDER BY created_at", (run_id,))
                    costs = [dict(row) for row in cursor.fetchall()]

                    # Feedback History
                    cursor.execute("SELECT * FROM feedback_history WHERE run_id = ? ORDER BY created_at", (run_id,))
                    feedbacks = [dict(row) for row in cursor.fetchall()]
                    for fb in feedbacks:
                        if fb.get("rubric_failures"):
                            try:
                                fb["rubric_failures"] = json.loads(fb["rubric_failures"])
                            except Exception:
                                pass

                    # Artifacts
                    cursor.execute("SELECT * FROM artifacts WHERE run_id = ? ORDER BY created_at", (run_id,))
                    artifacts = [dict(row) for row in cursor.fetchall()]

                    conn.close()

                    response_data = {
                        "run": dict(run_row),
                        "tasks": tasks,
                        "cost_log": costs,
                        "feedback_history": feedbacks,
                        "artifacts": artifacts
                    }
                    self._send_response(200, json.dumps(response_data).encode("utf-8"), "application/json")
                except Exception as e:
                    self._send_response(500, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")

            elif path == "/api/logs":
                log_path = workspace_dir / ".maestro" / "log.jsonl"
                logs = []
                if log_path.exists():
                    try:
                        with open(log_path, "r", encoding="utf-8") as f:
                            for line in f:
                                line = line.strip()
                                if line:
                                    try:
                                        logs.append(json.loads(line))
                                    except Exception:
                                        pass
                    except Exception as e:
                        self._send_response(500, json.dumps({"error": f"Failed to read logs: {e}"}).encode("utf-8"), "application/json")
                        return
                
                limit_val = query_params.get("limit", [None])[0]
                if limit_val:
                    try:
                        limit = int(limit_val)
                        logs = logs[-limit:]
                    except ValueError:
                        pass
                else:
                    logs = logs[-1000:]

                self._send_response(200, json.dumps(logs).encode("utf-8"), "application/json")

            elif path == "/api/artifact":
                artifact_path_str = query_params.get("path", [None])[0]
                if not artifact_path_str:
                    self._send_response(400, json.dumps({"error": "Path parameter is required"}).encode("utf-8"), "application/json")
                    return
                
                try:
                    path_to_read = Path(artifact_path_str)
                    if not path_to_read.is_absolute():
                        path_to_read = workspace_dir / path_to_read
                    
                    path_to_read = path_to_read.resolve()
                    resolved_workspace = workspace_dir.resolve()
                    
                    # Ensure path is within workspace to prevent directory traversal
                    if not str(path_to_read).startswith(str(resolved_workspace)):
                        self._send_response(403, json.dumps({"error": "Access denied: outside workspace"}).encode("utf-8"), "application/json")
                        return

                    if not path_to_read.exists() or not path_to_read.is_file():
                        self._send_response(404, json.dumps({"error": f"Artifact not found or not a file: {artifact_path_str}"}).encode("utf-8"), "application/json")
                        return
                    
                    content = ""
                    try:
                        with open(path_to_read, "r", encoding="utf-8", errors="replace") as f:
                            content = f.read(1024 * 1024)
                    except Exception as e:
                        content = f"[Binary or unreadable file content: {e}]"
                    
                    self._send_response(200, json.dumps({"content": content}).encode("utf-8"), "application/json")
                except Exception as e:
                    self._send_response(500, json.dumps({"error": str(e)}).encode("utf-8"), "application/json")
            else:
                self._send_response(404, b"Not found", "text/plain")

    return DashboardHTTPRequestHandler


def start_server(workspace_dir: Path, port: int = 8000) -> HTTPServer:
    """Start the HTTP server on localhost."""
    handler = make_handler(workspace_dir)
    server = HTTPServer(("localhost", port), handler)
    return server

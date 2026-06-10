"""Structured JSONL logger for Maestro.

Every specialist call is logged with: timestamp, phase, specialist,
input hash, output path, duration, cost, status.

Log file: .maestro/log.jsonl — used for replay, debug, and observability.
"""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import Any, Optional

from maestro.error_handler import TaskResult


class LogLevel(str, Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"


@dataclass
class LogEntry:
    """A single structured log record."""
    timestamp: float
    level: LogLevel
    event: str
    phase: Optional[int] = None
    specialist: Optional[str] = None
    task_id: Optional[str] = None
    input_hash: Optional[str] = None
    output_path: Optional[str] = None
    duration_sec: Optional[float] = None
    cost_usd: Optional[float] = None
    status: Optional[str] = None
    message: Optional[str] = None
    metadata: Optional[dict[str, Any]] = None

    def to_json_line(self) -> str:
        """Serialize to compact JSON line (no pretty-print)."""
        data: dict[str, Any] = {
            "ts": self.timestamp,
            "level": self.level.value,
            "event": self.event,
        }
        # Only include non-None fields to keep log lines compact
        if self.phase is not None:
            data["phase"] = self.phase
        if self.specialist:
            data["specialist"] = self.specialist
        if self.task_id:
            data["task_id"] = self.task_id
        if self.input_hash:
            data["input_hash"] = self.input_hash
        if self.output_path:
            data["output_path"] = self.output_path
        if self.duration_sec is not None:
            data["duration_sec"] = round(self.duration_sec, 2)
        if self.cost_usd is not None:
            data["cost_usd"] = round(self.cost_usd, 6)
        if self.status:
            data["status"] = self.status
        if self.message:
            data["msg"] = self.message
        if self.metadata:
            data["meta"] = self.metadata

        return json.dumps(data, ensure_ascii=False)


class MaestroLogger:
    """Append-only JSONL logger for Maestro pipeline execution.

    Usage:
        logger = MaestroLogger(workspace / ".maestro")
        logger.log_specialist_call(phase=3, specialist="codex", task_id="t4", ...)
        logger.info("Pipeline completed", metadata={"total_cost": 4.20})
    """

    def __init__(self, maestro_dir: Path, min_level: LogLevel = LogLevel.INFO):
        self.log_file = maestro_dir / "log.jsonl"
        self.min_level = min_level
        self.maestro_dir = maestro_dir
        self.maestro_dir.mkdir(parents=True, exist_ok=True)

    def _write(self, entry: LogEntry) -> None:
        """Append a log entry to the JSONL file."""
        level_order = [LogLevel.DEBUG, LogLevel.INFO, LogLevel.WARNING, LogLevel.ERROR]
        if level_order.index(entry.level) < level_order.index(self.min_level):
            return
        with open(self.log_file, "a", encoding="utf-8") as f:
            f.write(entry.to_json_line() + "\n")

    def log_specialist_call(
        self,
        *,
        phase: int,
        specialist: str,
        task_id: str,
        result: TaskResult,
        cost_usd: float = 0.0,
        input_hash: Optional[str] = None,
        output_path: Optional[str] = None,
    ) -> None:
        """Log a complete specialist invocation (success or failure).

        This is the primary logging method — called after every specialist subprocess.
        """
        entry = LogEntry(
            timestamp=time.time(),
            level=LogLevel.ERROR if not result.succeeded else LogLevel.INFO,
            event="specialist_call",
            phase=phase,
            specialist=specialist,
            task_id=task_id,
            input_hash=input_hash,
            output_path=output_path,
            duration_sec=result.duration_sec,
            cost_usd=cost_usd,
            status=result.status.value,
            message=result.error_message if result.error_message else None,
            metadata={
                "exit_code": result.exit_code,
                "command": result.command,
            },
        )
        self._write(entry)

    def log_grade(
        self,
        *,
        task_id: str,
        specialist: str,
        score: float,
        passed: bool,
        failures: Optional[list[str]] = None,
    ) -> None:
        """Log a grading result."""
        self._write(LogEntry(
            timestamp=time.time(),
            level=LogLevel.INFO if passed else LogLevel.WARNING,
            event="grade",
            task_id=task_id,
            specialist=specialist,
            status="passed" if passed else "failed",
            metadata={"score": score, "failures": failures or []},
        ))

    def log_kickback(
        self,
        *,
        task_id: str,
        specialist: str,
        attempt: int,
        reason: str,
    ) -> None:
        """Log a kick-back (retry with feedback)."""
        self._write(LogEntry(
            timestamp=time.time(),
            level=LogLevel.WARNING,
            event="kickback",
            task_id=task_id,
            specialist=specialist,
            message=reason,
            metadata={"attempt": attempt},
        ))

    def log_budget_warning(self, spent: float, max_budget: float) -> None:
        """Log budget warning at 80% threshold."""
        self._write(LogEntry(
            timestamp=time.time(),
            level=LogLevel.WARNING,
            event="budget_warning",
            message=f"Budget at {(spent/max_budget)*100:.1f}%: ${spent:.2f} / ${max_budget:.2f}",
            metadata={"spent_usd": spent, "max_budget_usd": max_budget},
        ))

    def log_budget_exceeded(self, spent: float, max_budget: float) -> None:
        """Log budget exceeded — pipeline halted."""
        self._write(LogEntry(
            timestamp=time.time(),
            level=LogLevel.ERROR,
            event="budget_exceeded",
            message=f"Budget exceeded: ${spent:.2f} / ${max_budget:.2f}. Pipeline halted.",
            metadata={"spent_usd": spent, "max_budget_usd": max_budget},
        ))

    # ── Convenience methods ──

    def debug(self, message: str, **kwargs: Any) -> None:
        self._write(LogEntry(
            timestamp=time.time(), level=LogLevel.DEBUG,
            event="debug", message=message, metadata=kwargs or None,
        ))

    def info(self, message: str, **kwargs: Any) -> None:
        self._write(LogEntry(
            timestamp=time.time(), level=LogLevel.INFO,
            event="info", message=message, metadata=kwargs or None,
        ))

    def warning(self, message: str, **kwargs: Any) -> None:
        self._write(LogEntry(
            timestamp=time.time(), level=LogLevel.WARNING,
            event="warning", message=message, metadata=kwargs or None,
        ))

    def error(self, message: str, **kwargs: Any) -> None:
        self._write(LogEntry(
            timestamp=time.time(), level=LogLevel.ERROR,
            event="error", message=message, metadata=kwargs or None,
        ))


def hash_input(content: str) -> str:
    """Quick hash of input content for deduplication / log correlation."""
    return hashlib.sha256(content.encode()).hexdigest()[:16]

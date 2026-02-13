"""Per-task event stream — captures every MCP tool call for a task.

Each task gets an ``events.jsonl`` file in its working directory.
Every MCP tool call from the task's worker or supervisor is logged here,
providing a complete audit trail that supervisors and users can read.

This replaces the broken stdout-based logging (Copilot CLI produces no
stdout when working via MCP tool calls).
"""
from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

from copenclaw.core.logging_config import append_to_file, get_worker_log_dir

logger = logging.getLogger("copenclaw.task_events")


@dataclass
class TaskEvent:
    """A single MCP tool call event for a task."""
    timestamp: str
    role: str           # "worker" | "supervisor" | "orchestrator"
    tool: str           # e.g. "files_read", "task_report"
    args_summary: str   # truncated args for readability
    result_summary: str # truncated result
    is_error: bool = False
    task_id: str = ""

    def to_dict(self) -> dict:
        return {
            "ts": self.timestamp,
            "role": self.role,
            "tool": self.tool,
            "args": self.args_summary,
            "result": self.result_summary,
            "error": self.is_error,
            "task_id": self.task_id,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "TaskEvent":
        return cls(
            timestamp=d.get("ts", ""),
            role=d.get("role", ""),
            tool=d.get("tool", ""),
            args_summary=d.get("args", ""),
            result_summary=d.get("result", ""),
            is_error=d.get("error", False),
            task_id=d.get("task_id", ""),
        )

    def format_line(self) -> str:
        """Human-readable one-line summary."""
        status = "❌" if self.is_error else "✓"
        return f"[{self.timestamp}] {self.role} {status} {self.tool}: {self.args_summary[:100]} → {self.result_summary[:100]}"


class TaskEventLog:
    """Append-only event stream for a task, backed by a JSONL file.

    Thread-safe: uses file-level append (each write is a single line).
    """

    def __init__(self, task_dir: str, task_id: str = "") -> None:
        self.task_dir = task_dir
        self.task_id = task_id
        self._path = os.path.join(task_dir, "events.jsonl")

    @property
    def path(self) -> str:
        return self._path

    def append(
        self,
        role: str,
        tool: str,
        args_summary: str,
        result_summary: str,
        is_error: bool = False,
    ) -> TaskEvent:
        """Log a tool call event. Returns the created event."""
        event = TaskEvent(
            timestamp=time.strftime("%Y-%m-%dT%H:%M:%S"),
            role=role,
            tool=tool,
            args_summary=args_summary[:500],
            result_summary=result_summary[:500],
            is_error=is_error,
            task_id=self.task_id,
        )
        line = json.dumps(event.to_dict(), default=str)
        try:
            os.makedirs(os.path.dirname(self._path) or ".", exist_ok=True)
            with open(self._path, "a", encoding="utf-8") as f:
                f.write(line + "\n")
                f.flush()
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write task event: %s", exc)
        # Mirror to centralized per-task log dir
        if self.task_id:
            try:
                central_path = os.path.join(get_worker_log_dir(self.task_id), "events.jsonl")
                if central_path != self._path:
                    append_to_file(central_path, line)
            except Exception:  # noqa: BLE001
                pass
        return event

    def tail(self, n: int = 50) -> List[TaskEvent]:
        """Read the last N events."""
        if not os.path.exists(self._path):
            return []
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                lines = f.readlines()
            events = []
            for line in lines[-n:]:
                line = line.strip()
                if line:
                    events.append(TaskEvent.from_dict(json.loads(line)))
            return events
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to read task events: %s", exc)
            return []

    def all_events(self) -> List[TaskEvent]:
        """Read all events."""
        return self.tail(n=999999)

    def formatted_tail(self, n: int = 50) -> str:
        """Return a human-readable string of the last N events."""
        events = self.tail(n)
        if not events:
            return "(no events yet)"
        return "\n".join(e.format_line() for e in events)

    def count(self) -> int:
        """Count total events."""
        if not os.path.exists(self._path):
            return 0
        try:
            with open(self._path, "r", encoding="utf-8") as f:
                return sum(1 for line in f if line.strip())
        except Exception:  # noqa: BLE001
            return 0


class TaskEventRegistry:
    """Registry of TaskEventLog instances, keyed by task_id.

    Used by the protocol handler to look up the right event log
    for incoming MCP tool calls.
    """

    def __init__(self) -> None:
        self._logs: Dict[str, TaskEventLog] = {}

    def register(self, task_id: str, task_dir: str) -> TaskEventLog:
        """Register a task's event log. Creates the log if not exists."""
        if task_id not in self._logs:
            self._logs[task_id] = TaskEventLog(task_dir, task_id)
        return self._logs[task_id]

    def get(self, task_id: str) -> Optional[TaskEventLog]:
        """Get a task's event log, or None if not registered."""
        return self._logs.get(task_id)

    def get_or_create(self, task_id: str, task_dir: str) -> TaskEventLog:
        """Get or create a task's event log."""
        return self.register(task_id, task_dir)

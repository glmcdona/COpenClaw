"""Tests for the per-task event stream (task_events.py)."""
from __future__ import annotations

import json
import os
import tempfile

import pytest

from copenclaw.core.task_events import TaskEvent, TaskEventLog, TaskEventRegistry


class TestTaskEvent:
    def test_to_dict_roundtrip(self):
        event = TaskEvent(
            timestamp="2026-01-01T00:00:00",
            role="worker",
            tool="exec_run",
            args_summary="ls -la",
            result_summary="file1.txt file2.txt",
            is_error=False,
            task_id="task-123",
        )
        d = event.to_dict()
        assert d["role"] == "worker"
        assert d["tool"] == "exec_run"
        restored = TaskEvent.from_dict(d)
        assert restored.role == "worker"
        assert restored.tool == "exec_run"
        assert restored.task_id == "task-123"

    def test_format_line_success(self):
        event = TaskEvent(
            timestamp="2026-01-01T12:00:00",
            role="worker",
            tool="exec_run",
            args_summary="node -v",
            result_summary="v20.0.0",
        )
        line = event.format_line()
        assert "✓" in line
        assert "exec_run" in line
        assert "node -v" in line

    def test_format_line_error(self):
        event = TaskEvent(
            timestamp="2026-01-01T12:00:00",
            role="worker",
            tool="exec_run",
            args_summary="bad_cmd",
            result_summary="command not found",
            is_error=True,
        )
        line = event.format_line()
        assert "❌" in line


class TestTaskEventLog:
    def test_append_and_tail(self, tmp_path):
        log = TaskEventLog(str(tmp_path), task_id="task-abc")
        log.append("worker", "exec_run", "ls", "file1 file2")
        log.append("worker", "task_report", "progress", "50% done")
        log.append("supervisor", "task_read_peer", "task-abc", "worker active")

        events = log.tail(10)
        assert len(events) == 3
        assert events[0].tool == "exec_run"
        assert events[1].tool == "task_report"
        assert events[2].role == "supervisor"

    def test_tail_limit(self, tmp_path):
        log = TaskEventLog(str(tmp_path), task_id="task-abc")
        for i in range(10):
            log.append("worker", f"tool_{i}", f"arg_{i}", f"result_{i}")

        events = log.tail(3)
        assert len(events) == 3
        assert events[0].tool == "tool_7"
        assert events[2].tool == "tool_9"

    def test_count(self, tmp_path):
        log = TaskEventLog(str(tmp_path), task_id="task-abc")
        assert log.count() == 0
        log.append("worker", "exec_run", "ls", "ok")
        assert log.count() == 1
        log.append("worker", "exec_run", "pwd", "/home")
        assert log.count() == 2

    def test_empty_tail(self, tmp_path):
        log = TaskEventLog(str(tmp_path), task_id="task-abc")
        assert log.tail() == []

    def test_formatted_tail_empty(self, tmp_path):
        log = TaskEventLog(str(tmp_path), task_id="task-abc")
        assert log.formatted_tail() == "(no events yet)"

    def test_formatted_tail_with_events(self, tmp_path):
        log = TaskEventLog(str(tmp_path), task_id="task-abc")
        log.append("worker", "exec_run", "node -v", "v20.0.0")
        text = log.formatted_tail()
        assert "exec_run" in text
        assert "node -v" in text

    def test_error_event(self, tmp_path):
        log = TaskEventLog(str(tmp_path), task_id="task-abc")
        event = log.append("worker", "exec_run", "bad_cmd", "not found", is_error=True)
        assert event.is_error is True
        events = log.tail()
        assert events[0].is_error is True

    def test_truncates_long_args(self, tmp_path):
        log = TaskEventLog(str(tmp_path), task_id="task-abc")
        long_arg = "x" * 1000
        event = log.append("worker", "exec_run", long_arg, "ok")
        assert len(event.args_summary) == 500

    def test_jsonl_format(self, tmp_path):
        log = TaskEventLog(str(tmp_path), task_id="task-abc")
        log.append("worker", "exec_run", "ls", "ok")
        with open(log.path, "r") as f:
            line = f.readline()
        data = json.loads(line)
        assert data["tool"] == "exec_run"
        assert data["role"] == "worker"


class TestTaskEventRegistry:
    def test_register_and_get(self, tmp_path):
        registry = TaskEventRegistry()
        log = registry.register("task-1", str(tmp_path / "task-1"))
        assert log is not None
        assert registry.get("task-1") is log

    def test_get_missing(self):
        registry = TaskEventRegistry()
        assert registry.get("task-nonexistent") is None

    def test_get_or_create(self, tmp_path):
        registry = TaskEventRegistry()
        log1 = registry.get_or_create("task-1", str(tmp_path / "task-1"))
        log2 = registry.get_or_create("task-1", str(tmp_path / "task-1"))
        assert log1 is log2  # Same instance

    def test_multiple_tasks(self, tmp_path):
        registry = TaskEventRegistry()
        log1 = registry.register("task-1", str(tmp_path / "task-1"))
        log2 = registry.register("task-2", str(tmp_path / "task-2"))
        assert log1 is not log2
        assert registry.get("task-1") is log1
        assert registry.get("task-2") is log2
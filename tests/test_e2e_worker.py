"""End-to-end tests for worker, supervisor, and inter-session messaging.

Uses mocked subprocess.Popen to simulate Copilot CLI behavior without
needing the actual CLI installed. Tests cover:
- Worker lifecycle (start, output streaming, completion, errors)
- Supervisor lifecycle (start, periodic checks, completion detection)
- Full task flow via MCPProtocolHandler (propose → approve → worker → supervisor)
- Inter-session messaging (inbox/outbox, task_check_inbox, task_send_input)
- Deferred completion (worker says done but supervisor verifies first)
- Task cancellation
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import os
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

from copenclaw.core.tasks import TaskManager
from copenclaw.core.worker import (
    WorkerThread,
    WorkerPool,
    SupervisorThread,
    _write_instructions_file,
)
from copenclaw.core.templates import worker_template, supervisor_template
from copenclaw.core.scheduler import Scheduler
from copenclaw.core.policy import ExecutionPolicy
from copenclaw.mcp.protocol import MCPProtocolHandler


# ── Helpers ──────────────────────────────────────────────────

class FakeProcess:
    """Simulates subprocess.Popen with controlled stdout lines and exit code."""

    def __init__(self, lines: list[str], exit_code: int = 0, delay: float = 0.0):
        self._lines = lines
        self.returncode = exit_code
        self._delay = delay
        self.pid = 99999
        self._killed = False
        self._terminated = False
        # Create a StringIO that acts like a line-by-line iterator
        self.stdout = io.StringIO("\n".join(lines) + "\n" if lines else "")
        self.stderr = None

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self._terminated = True

    def kill(self):
        self._killed = True


class SlowFakeProcess(FakeProcess):
    """FakeProcess that yields lines with a delay (for testing streaming)."""

    def __init__(self, lines: list[str], exit_code: int = 0, delay: float = 0.01):
        super().__init__(lines, exit_code, delay)
        # Override stdout with a generator-based iterator
        self.stdout = self._line_generator()

    def _line_generator(self):
        for line in self._lines:
            time.sleep(self._delay)
            yield line + "\n"


def make_fake_popen(process: FakeProcess):
    """Create a mock Popen constructor that returns the given process."""
    def fake_popen(*args, **kwargs):
        return process
    return fake_popen


@pytest.fixture
def tmp_data_dir(tmp_path):
    """Create a temporary data directory with tasks subdirectory."""
    data_dir = str(tmp_path / "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


@pytest.fixture
def task_manager(tmp_data_dir):
    return TaskManager(data_dir=tmp_data_dir)


@pytest.fixture
def worker_pool():
    return WorkerPool(
        mcp_server_url="http://127.0.0.1:18790/mcp",
        mcp_token="test-token",
        supervisor_timeout=30,
    )


# ── Test: Instructions file writing ─────────────────────────

class TestInstructionsFile:

    def test_write_instructions_creates_file(self, tmp_path):
        working_dir = str(tmp_path / "workdir")
        os.makedirs(working_dir)
        path = _write_instructions_file(working_dir, "Test content here")

        assert os.path.exists(path)
        assert path.endswith("copilot-instructions.md")
        with open(path, "r") as f:
            assert f.read() == "Test content here"

    def test_write_instructions_creates_github_dir(self, tmp_path):
        working_dir = str(tmp_path / "workdir")
        os.makedirs(working_dir)
        _write_instructions_file(working_dir, "content")

        github_dir = os.path.join(working_dir, ".github")
        assert os.path.isdir(github_dir)

    def test_worker_template_formats(self):
        result = worker_template(
            task_id="task-abc123",
            prompt="Build a hello world app",
            workspace_root="/tmp/workspace",
        )
        assert "task-abc123" in result
        assert "Build a hello world app" in result
        assert "files_read" in result
        assert "task_report" in result

    def test_supervisor_template_formats(self):
        result = supervisor_template(
            task_id="task-abc123",
            prompt="Build a hello world app",
            worker_session_id="session-xyz",
            supervisor_instructions="Check that hello.py exists",
            workspace_root="/tmp/workspace",
        )
        assert "task-abc123" in result
        assert "session-xyz" in result
        assert "Build a hello world app" in result
        assert "Check that hello.py exists" in result


# ── Test: WorkerThread lifecycle ─────────────────────────────

class TestWorkerLifecycle:

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_worker_starts_and_streams_output(self, mock_which, mock_popen, tmp_path):
        """Worker should stream lines from subprocess stdout to on_output callback."""
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)

        lines = ["Starting task...", "Working on step 1", "Step 1 complete", "All done!"]
        mock_popen.return_value = FakeProcess(lines, exit_code=0)

        output_lines = []
        complete_results = []

        worker = WorkerThread(
            task_id="task-test1",
            prompt="Do something simple",
            working_dir=working_dir,
            mcp_server_url="http://127.0.0.1:18790/mcp",
            on_output=lambda tid, text: output_lines.append((tid, text)),
            on_complete=lambda tid, text: complete_results.append((tid, text)),
        )
        worker.start()

        # Wait for thread to finish
        worker._thread.join(timeout=5)

        assert not worker.is_running
        assert len(output_lines) == 4
        assert output_lines[0] == ("task-test1", "Starting task...")
        assert output_lines[3] == ("task-test1", "All done!")
        assert len(complete_results) == 1
        assert complete_results[0][0] == "task-test1"
        assert "All done!" in complete_results[0][1]

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_worker_writes_instructions_file(self, mock_which, mock_popen, tmp_path):
        """Worker should write .github/copilot-instructions.md before launching."""
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)

        mock_popen.return_value = FakeProcess(["ok"], exit_code=0)

        worker = WorkerThread(
            task_id="task-instr",
            prompt="Build a widget",
            working_dir=working_dir,
            mcp_server_url="http://127.0.0.1:18790/mcp",
        )
        worker.start()
        worker._thread.join(timeout=5)

        # Instructions now go into workspace/ subdirectory
        instructions_path = os.path.join(working_dir, "workspace", ".github", "copilot-instructions.md")
        assert os.path.exists(instructions_path)
        with open(instructions_path, "r", encoding="utf-8") as f:
            content = f.read()
        assert "task-instr" in content
        assert "Build a widget" in content

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_worker_error_exit_code(self, mock_which, mock_popen, tmp_path):
        """Worker should report error when process exits with non-zero code."""
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)

        mock_popen.return_value = FakeProcess(["crash!"], exit_code=1)

        complete_results = []
        worker = WorkerThread(
            task_id="task-err",
            prompt="This will fail",
            working_dir=working_dir,
            mcp_server_url="http://127.0.0.1:18790/mcp",
            on_complete=lambda tid, text: complete_results.append((tid, text)),
        )
        worker.start()
        worker._thread.join(timeout=5)

        assert len(complete_results) == 1
        assert "ERROR (exit 1)" in complete_results[0][1]

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_worker_empty_output(self, mock_which, mock_popen, tmp_path):
        """Worker should handle empty output gracefully."""
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)

        mock_popen.return_value = FakeProcess([], exit_code=0)

        complete_results = []
        worker = WorkerThread(
            task_id="task-empty",
            prompt="Silent task",
            working_dir=working_dir,
            mcp_server_url="http://127.0.0.1:18790/mcp",
            on_complete=lambda tid, text: complete_results.append((tid, text)),
        )
        worker.start()
        worker._thread.join(timeout=5)

        assert len(complete_results) == 1
        # Should complete without error even with empty output
        assert complete_results[0][0] == "task-empty"

    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value=None)
    def test_worker_cli_not_found(self, mock_which, tmp_path):
        """Worker should handle missing copilot CLI executable."""
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)

        complete_results = []
        worker = WorkerThread(
            task_id="task-noclid",
            prompt="Can't find CLI",
            working_dir=working_dir,
            mcp_server_url="http://127.0.0.1:18790/mcp",
            on_complete=lambda tid, text: complete_results.append((tid, text)),
        )
        worker.start()
        worker._thread.join(timeout=5)

        assert len(complete_results) == 1
        assert "UNEXPECTED ERROR" in complete_results[0][1]

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_worker_stop_terminates_process(self, mock_which, mock_popen, tmp_path):
        """Worker.stop() should terminate the subprocess."""
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)

        process = SlowFakeProcess(["line1", "line2", "line3"] * 100, delay=0.1)
        mock_popen.return_value = process

        worker = WorkerThread(
            task_id="task-stop",
            prompt="Long running task",
            working_dir=working_dir,
            mcp_server_url="http://127.0.0.1:18790/mcp",
        )
        worker.start()
        time.sleep(0.2)  # Let it start

        assert worker.is_running
        worker.stop()
        worker._thread.join(timeout=5)

        assert not worker.is_running

    def test_worker_process_snapshot_tracks_pid_and_children(self, tmp_path):
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)
        worker = WorkerThread(
            task_id="task-proc",
            prompt="Track process tree",
            working_dir=working_dir,
            mcp_server_url="http://127.0.0.1:18790/mcp",
        )
        fake_process = MagicMock()
        fake_process.pid = 43210
        fake_process.poll.return_value = None
        worker._process = fake_process
        worker._last_pid = 43210

        with patch("copenclaw.core.worker._collect_child_processes", return_value=[43211, 43212]):
            snapshot = worker.process_snapshot()

        assert snapshot["pid"] == 43210
        assert snapshot["child_pids"] == [43211, 43212]
        assert snapshot["running"] is True
        assert 43210 in snapshot["active_pids"]


# ── Test: SupervisorThread lifecycle ─────────────────────────

class TestSupervisorLifecycle:

    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_supervisor_writes_instructions(self, mock_which, tmp_path):
        """Supervisor should write instructions to its own directory."""
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)

        supervisor = SupervisorThread(
            task_id="task-sup1",
            prompt="Watch the worker",
            worker_session_id="session-w1",
            mcp_server_url="http://127.0.0.1:18790/mcp",
            check_interval=1,
            timeout=5,
            supervisor_instructions="Verify output exists",
            working_dir=working_dir,
        )

        # Get the supervisor dir and write instructions manually to test
        sup_dir = supervisor._get_supervisor_dir()
        assert "supervisor" in sup_dir

        instructions = supervisor_template(
            task_id="task-sup1",
            prompt="Watch the worker",
            worker_session_id="session-w1",
            supervisor_instructions="Verify output exists",
            workspace_root="/tmp/workspace",
        )
        _write_instructions_file(sup_dir, instructions)

        path = os.path.join(sup_dir, ".github", "copilot-instructions.md")
        assert os.path.exists(path)
        with open(path, encoding="utf-8") as f:
            content = f.read()
        assert "task-sup1" in content or "supervisor" in content.lower()
        assert "Verify output exists" in content

    @patch("copenclaw.integrations.copilot_cli.CopilotCli.run_prompt")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_supervisor_stops_when_stopped(self, mock_which, mock_run, tmp_path):
        """Supervisor should stop looping when asked to stop."""
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)

        mock_run.return_value = "All verified, task done."

        output_lines = []
        supervisor = SupervisorThread(
            task_id="task-sup2",
            prompt="Check task",
            worker_session_id=None,
            mcp_server_url="http://127.0.0.1:18790/mcp",
            check_interval=1,
            on_output=lambda tid, text: output_lines.append((tid, text)),
            timeout=5,
            working_dir=working_dir,
        )
        supervisor.start()

        # Supervisor uses event-driven checks — kick it to trigger one
        time.sleep(0.5)
        supervisor.request_check()
        time.sleep(1.5)
        assert supervisor.is_running
        supervisor.stop()
        supervisor._thread.join(timeout=10)

        assert len(output_lines) >= 1
        assert not supervisor.is_running

    @patch("copenclaw.integrations.copilot_cli.CopilotCli.run_prompt")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_supervisor_handles_multiple_triggers_without_autopilot(self, mock_which, mock_run, tmp_path):
        """Supervisor should run one bounded check per trigger (non-autopilot)."""
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)

        first_check = threading.Event()
        second_check = threading.Event()
        call_count = {"value": 0}

        def _fake_run(*args, **kwargs):
            call_count["value"] += 1
            if call_count["value"] == 1:
                first_check.set()
            elif call_count["value"] == 2:
                second_check.set()
            return "check complete"

        mock_run.side_effect = _fake_run

        supervisor = SupervisorThread(
            task_id="task-sup3",
            prompt="Check task",
            worker_session_id=None,
            mcp_server_url="http://127.0.0.1:18790/mcp",
            check_interval=300,
            timeout=5,
            working_dir=working_dir,
        )
        supervisor.start()
        try:
            supervisor.request_check()
            assert first_check.wait(timeout=3)

            supervisor.request_check()
            assert second_check.wait(timeout=3)
        finally:
            supervisor.stop()
            supervisor._thread.join(timeout=10)

        assert mock_run.call_count >= 2
        for call in mock_run.call_args_list:
            assert call.kwargs.get("autopilot") is False

    def test_supervisor_prompt_stays_passive_with_active_process_tree(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        tm = TaskManager(data_dir=data_dir)
        task = tm.create_task(name="Passive", prompt="test", auto_supervise=True, check_interval=300)
        tm.update_status(task.task_id, "running")
        task.last_worker_activity_at = datetime.now(timezone.utc) - timedelta(seconds=420)
        tm._save()

        worker = MagicMock()
        worker.is_running = True
        worker.process_snapshot.return_value = {
            "pid": 1001,
            "child_pids": [1002, 1003],
            "active_pids": [1001, 1002, 1003],
            "running": True,
        }
        pool = MagicMock()
        pool.get_worker.return_value = worker

        supervisor = SupervisorThread(
            task_id=task.task_id,
            prompt="Check task",
            worker_session_id=None,
            mcp_server_url="http://127.0.0.1:18790/mcp",
            check_interval=300,
            timeout=5,
            task_manager=tm,
            worker_pool=pool,
        )

        trigger = supervisor._build_trigger_prompt(check_count=1)
        assert "Monitor passively" in trigger
        assert "do NOT send assessment/intervention" in trigger

    def test_supervisor_prompt_escalates_after_stall_threshold(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        tm = TaskManager(data_dir=data_dir)
        task = tm.create_task(name="Stalled", prompt="test", auto_supervise=True, check_interval=300)
        tm.update_status(task.task_id, "running")
        task.last_worker_activity_at = datetime.now(timezone.utc) - timedelta(seconds=1800)
        tm._save()

        worker = MagicMock()
        worker.is_running = True
        worker.process_snapshot.return_value = {
            "pid": 2001,
            "child_pids": [],
            "active_pids": [2001],
            "running": True,
        }
        pool = MagicMock()
        pool.get_worker.return_value = worker

        supervisor = SupervisorThread(
            task_id=task.task_id,
            prompt="Check task",
            worker_session_id=None,
            mcp_server_url="http://127.0.0.1:18790/mcp",
            check_interval=300,
            timeout=5,
            task_manager=tm,
            worker_pool=pool,
        )

        trigger = supervisor._build_trigger_prompt(check_count=1)
        assert "STUCK" in trigger
        assert "type='intervention'" in trigger


# ── Test: WorkerPool ─────────────────────────────────────────

class TestWorkerPool:

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_pool_start_worker(self, mock_which, mock_popen, tmp_path):
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)
        mock_popen.return_value = FakeProcess(["working..."], exit_code=0)

        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        worker = pool.start_worker(
            task_id="task-pool1",
            prompt="Pool task",
            working_dir=working_dir,
        )

        worker._thread.join(timeout=5)
        assert pool.get_worker("task-pool1") is worker

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_pool_duplicate_worker_raises(self, mock_which, mock_popen, tmp_path):
        """Starting a worker for a task that already has one running should raise."""
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)

        # Use a slow process so it's still "running" when we try to start another
        mock_popen.return_value = SlowFakeProcess(["line"] * 100, delay=0.5)

        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        pool.start_worker(task_id="task-dup", prompt="Task", working_dir=working_dir)

        time.sleep(0.1)  # Let it start

        with pytest.raises(RuntimeError, match="already running"):
            pool.start_worker(task_id="task-dup", prompt="Task", working_dir=working_dir)

        pool.stop_all()

    def test_pool_status(self):
        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        status = pool.status()
        assert "workers" in status
        assert "supervisors" in status

    @patch.object(SupervisorThread, "start", autospec=True)
    def test_pool_start_supervisor_caps_timeout_to_check_interval(self, mock_start, tmp_path):
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)
        os.makedirs(os.path.join(working_dir, "workspace"), exist_ok=True)

        pool = WorkerPool(
            mcp_server_url="http://127.0.0.1:18790/mcp",
            supervisor_timeout=7200,
        )
        supervisor = pool.start_supervisor(
            task_id="task-sup-timeout",
            prompt="Monitor progress",
            check_interval=300,
            working_dir=working_dir,
        )

        assert supervisor.timeout == 300
        assert pool.get_supervisor("task-sup-timeout") is supervisor

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_pool_stop_task(self, mock_which, mock_popen, tmp_path):
        working_dir = str(tmp_path / "work")
        os.makedirs(working_dir)
        mock_popen.return_value = SlowFakeProcess(["line"] * 100, delay=0.5)

        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        pool.start_worker(task_id="task-stop", prompt="Stop me", working_dir=working_dir)
        time.sleep(0.1)

        pool.stop_task("task-stop")
        time.sleep(0.5)

        worker = pool.get_worker("task-stop")
        assert worker is not None
        # Worker should have stopped (thread no longer alive after a bit)


# ── Test: Full E2E task flow via protocol ────────────────────

class TestE2ETaskFlow:
    """End-to-end tests using MCPProtocolHandler with mocked subprocess."""

    def _make_handler(self, tmp_data_dir: str, telegram_token: str | None = None) -> MCPProtocolHandler:
        """Create an MCPProtocolHandler with real TaskManager and WorkerPool."""
        tm = TaskManager(data_dir=tmp_data_dir)
        pool = WorkerPool(
            mcp_server_url="http://127.0.0.1:18790/mcp",
            mcp_token="test-token",
        )
        scheduler = Scheduler()
        policy = ExecutionPolicy(allow_all=True)

        return MCPProtocolHandler(
            scheduler=scheduler,
            data_dir=tmp_data_dir,
            task_manager=tm,
            worker_pool=pool,
            owner_chat_id="12345",
            execution_policy=policy,
            telegram_token=telegram_token,
        )

    def _call_tool(self, handler: MCPProtocolHandler, tool_name: str, args: dict) -> dict:
        """Helper to call a tool via JSON-RPC and return the parsed result."""
        response = handler.handle_request({
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        })
        content = response["result"]["content"][0]["text"]
        import json
        return json.loads(content)

    def test_completion_hook_fires_without_on_complete(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)
        tm = handler.task_manager
        task = tm.create_task(
            name="demo-task",
            prompt="Do the demo work",
            channel="telegram",
            target="12345",
        )

        event = threading.Event()
        captured = {}

        def _hook(prompt: str, channel: str, target: str, service_url: str, source_task_name: str) -> None:
            captured["prompt"] = prompt
            captured["channel"] = channel
            captured["target"] = target
            captured["source_task_name"] = source_task_name
            event.set()

        handler.on_complete_callback = _hook

        handler._tool_task_report({
            "task_id": task.task_id,
            "type": "completed",
            "summary": "All done",
            "detail": "Everything finished",
            "from_tier": "worker",
        })

        assert event.wait(1)
        assert "task_id=" in captured["prompt"]
        assert "Completion summary: All done" in captured["prompt"]
        assert "No on_complete hook was provided" in captured["prompt"]

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    @patch("copenclaw.mcp.protocol.TelegramAdapter")
    def test_propose_approve_worker_runs(self, mock_telegram, mock_which, mock_popen, tmp_path):
        """Full flow: propose → approve → worker runs → reports progress → completes."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        # Mock subprocess for worker
        mock_popen.return_value = FakeProcess(
            ["Starting work", "Step 1 done", "All complete"],
            exit_code=0,
        )

        # Step 1: Propose
        result = self._call_tool(handler, "tasks_propose", {
            "prompt": "Create a hello.py file",
            "plan": "- Create hello.py\n- Test it",
            "supervisor_instructions": "Verify hello.py exists",
        })
        assert result["status"] == "proposed"
        task_id = result["task_id"]

        # Step 2: Approve
        result = self._call_tool(handler, "tasks_approve", {"task_id": task_id})
        assert result["status"] == "running"

        # Wait for worker to complete
        worker = handler.worker_pool.get_worker(task_id)
        assert worker is not None
        worker._thread.join(timeout=5)

        # Step 3: Check task status
        result = self._call_tool(handler, "tasks_status", {"task_id": task_id})
        assert result["task_id"] == task_id
        # Worker should have appended logs
        logs = self._call_tool(handler, "tasks_logs", {"task_id": task_id})
        assert "Starting work" in logs["logs"]

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    @patch("copenclaw.mcp.protocol.TelegramAdapter")
    def test_create_immediate_dispatch(self, mock_telegram, mock_which, mock_popen, tmp_path):
        """tasks_create should immediately dispatch without approval."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        mock_popen.return_value = FakeProcess(["Done!"], exit_code=0)

        result = self._call_tool(handler, "tasks_create", {
            "prompt": "Quick task",
            "auto_supervise": False,
        })
        assert result["status"] == "running"
        task_id = result["task_id"]

        worker = handler.worker_pool.get_worker(task_id)
        worker._thread.join(timeout=5)

        logs = self._call_tool(handler, "tasks_logs", {"task_id": task_id})
        assert "Done!" in logs["logs"]

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    @patch("copenclaw.mcp.protocol.TelegramAdapter")
    def test_telegram_ls_task_completes_and_notifies(self, mock_telegram, mock_which, mock_popen, tmp_path):
        """Telegram 'ls' request leads to task completion and notification."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir, telegram_token="token")

        mock_popen.return_value = FakeProcess(["listing...", "file-a", "file-b"], exit_code=0)

        start = time.monotonic()
        result = self._call_tool(handler, "tasks_create", {
            "prompt": "ls",
            "channel": "telegram",
            "target": "999",
            "auto_supervise": False,
        })
        assert result["status"] == "running"
        task_id = result["task_id"]

        worker = handler.worker_pool.get_worker(task_id)
        assert worker is not None
        worker._thread.join(timeout=5)

        # Simulate worker reporting completion with results
        self._call_tool(handler, "task_report", {
            "task_id": task_id,
            "type": "completed",
            "summary": "Listed files",
            "detail": "file-a\nfile-b",
            "from_tier": "worker",
        })

        status = self._call_tool(handler, "tasks_status", {"task_id": task_id})
        assert status["status"] == "completed"

        elapsed = time.monotonic() - start
        assert elapsed < 120

        mock_telegram.return_value.send_message.assert_called()
        sent_args = mock_telegram.return_value.send_message.call_args.kwargs
        assert sent_args.get("chat_id") == 999
        sent_text = sent_args.get("text", "")
        assert "Listed files" in sent_text

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    @patch("copenclaw.mcp.protocol.TelegramAdapter")
    def test_worker_error_reports_failure(self, mock_telegram, mock_which, mock_popen, tmp_path):
        """Worker process exit code != 0 should trigger error in on_complete."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        mock_popen.return_value = FakeProcess(["crash!"], exit_code=1)

        result = self._call_tool(handler, "tasks_create", {
            "prompt": "Broken task",
            "auto_supervise": False,
        })
        task_id = result["task_id"]

        worker = handler.worker_pool.get_worker(task_id)
        worker._thread.join(timeout=5)

        # The on_complete callback should have reported failure
        logs = self._call_tool(handler, "tasks_logs", {"task_id": task_id})
        assert "ERROR" in logs["logs"]

    @patch("copenclaw.core.worker.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    @patch("copenclaw.mcp.protocol.TelegramAdapter")
    def test_e2e_ls_cwd_with_supervisor_reports_to_telegram(self, mock_telegram, mock_which, mock_popen, tmp_path):
        """Full E2E: dispatch 'ls and cwd' with supervisor → worker runs → supervisor verifies → Telegram notified."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir, telegram_token="fake-token")

        # Worker subprocess simulates running ls and cwd
        mock_popen.return_value = FakeProcess(
            [
                "Running ls and cwd task...",
                "Executing Get-ChildItem...",
                "file1.txt  file2.py  README.md",
                "Executing Get-Location...",
                "D:\\Projects\\my-workspace",
                "Task complete.",
            ],
            exit_code=0,
        )

        # Step 1: Create task with supervisor and Telegram channel
        result = self._call_tool(handler, "tasks_create", {
            "prompt": "Run ls and cwd: list the current directory contents and print the working directory",
            "channel": "telegram",
            "target": "999",
            "auto_supervise": True,
            "check_interval": 60,
        })
        assert result["status"] == "running"
        task_id = result["task_id"]
        assert result["auto_supervise"] is True

        # Step 2: Wait for worker subprocess to finish
        worker = handler.worker_pool.get_worker(task_id)
        assert worker is not None
        worker._thread.join(timeout=5)

        # Verify worker output was logged
        logs = self._call_tool(handler, "tasks_logs", {"task_id": task_id})
        assert "file1.txt" in logs["logs"]
        assert "my-workspace" in logs["logs"]

        # Step 3: Supervisor should be running
        supervisor = handler.worker_pool.get_supervisor(task_id)
        assert supervisor is not None
        assert supervisor.is_running

        # Step 4: Worker reports completion — should be DEFERRED because supervisor is active
        result = self._call_tool(handler, "task_report", {
            "task_id": task_id,
            "type": "completed",
            "summary": "Listed directory and printed cwd",
            "detail": "Directory listing:\nfile1.txt  file2.py  README.md\n\nCurrent directory:\nD:\\Projects\\my-workspace",
            "from_tier": "worker",
        })
        assert result["status"] == "deferred"

        # Task should NOT be completed yet (awaiting supervisor verification)
        status = self._call_tool(handler, "tasks_status", {"task_id": task_id})
        assert status["status"] != "completed"

        # Step 5: Supervisor verifies and reports positive assessment
        result = self._call_tool(handler, "task_report", {
            "task_id": task_id,
            "type": "assessment",
            "summary": "Verified: directory listing and cwd output look correct",
            "detail": "Worker successfully ran ls and cwd. Output contains file listing and path.",
            "from_tier": "supervisor",
        })
        assert result["status"] == "reported"

        # Step 6: Task should now be completed
        task = handler.task_manager.get(task_id)
        assert task.status == "completed"
        assert task.completed_at is not None

        # Step 7: Verify Telegram notifications were sent
        mock_telegram.return_value.send_message.assert_called()
        # Collect all Telegram send_message calls
        all_calls = mock_telegram.return_value.send_message.call_args_list
        assert len(all_calls) >= 1

        # Find the call(s) targeting chat_id 999
        telegram_texts = []
        for call in all_calls:
            kwargs = call.kwargs
            if kwargs.get("chat_id") == 999:
                telegram_texts.append(kwargs.get("text", ""))

        assert len(telegram_texts) >= 1, "Expected at least one Telegram message to chat 999"

        # At least one message should reference the task completion or results
        combined_texts = " ".join(telegram_texts)
        assert any(
            keyword in combined_texts
            for keyword in ["completed", "Verified", "directory", "Listed", "cwd"]
        ), f"Expected completion-related text in Telegram messages, got: {combined_texts[:500]}"

        # Step 8: Verify timeline has the full lifecycle
        status = self._call_tool(handler, "tasks_status", {"task_id": task_id})
        timeline = status["timeline"]
        assert "completed" in timeline.lower() or "verified" in timeline.lower()

        # Clean up supervisor
        handler.worker_pool.stop_all()

    @patch("copenclaw.mcp.protocol.TelegramAdapter")
    def test_task_cancel(self, mock_telegram, tmp_path):
        """Cancelling a task should update status and stop workers."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        # Create a task manually without starting worker
        tm = handler.task_manager
        task = tm.create_task(name="Cancel me", prompt="test")

        result = self._call_tool(handler, "tasks_cancel", {"task_id": task.task_id})
        assert result["status"] == "cancelled"

        task = tm.get(task.task_id)
        assert task.status == "cancelled"


# ── Test: Inter-session messaging ────────────────────────────

class TestInterSessionMessaging:
    """Tests for the bidirectional messaging between tiers via MCP tools."""

    def _make_handler(self, data_dir: str, telegram_token: str | None = None) -> MCPProtocolHandler:
        tm = TaskManager(data_dir=data_dir)
        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        scheduler = Scheduler()
        policy = ExecutionPolicy(allow_all=True)
        return MCPProtocolHandler(
            scheduler=scheduler,
            data_dir=data_dir,
            telegram_token=telegram_token,
            task_manager=tm,
            worker_pool=pool,
            owner_chat_id="12345",
            execution_policy=policy,
        )

    def _call_tool(self, handler: MCPProtocolHandler, tool_name: str, args: dict) -> dict:
        response = handler.handle_request({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        })
        content = response["result"]["content"][0]["text"]
        import json
        return json.loads(content)

    def test_worker_reports_progress(self, tmp_path):
        """Worker calling task_report(progress) should update timeline."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        # Create task
        task = handler.task_manager.create_task(name="Msg test", prompt="test")
        handler.task_manager.update_status(task.task_id, "running")

        # Worker reports progress
        result = self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "progress",
            "summary": "Step 1 of 3 done",
            "detail": "Created the file structure",
            "from_tier": "worker",
        })
        assert result["status"] == "reported"

        # Check timeline
        status = self._call_tool(handler, "tasks_status", {"task_id": task.task_id})
        assert "Step 1 of 3 done" in status["timeline"]

    def test_worker_reports_completed(self, tmp_path):
        """Worker calling task_report(completed) should finalize the task."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(name="Complete test", prompt="test")
        handler.task_manager.update_status(task.task_id, "running")

        result = self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "completed",
            "summary": "All done!",
            "from_tier": "worker",
        })
        assert result["status"] == "reported"

        task = handler.task_manager.get(task.task_id)
        assert task.status == "completed"

    def test_worker_checks_inbox_empty(self, tmp_path):
        """Worker checking inbox with no messages should get empty list."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(name="Inbox test", prompt="test")

        result = self._call_tool(handler, "task_check_inbox", {"task_id": task.task_id})
        assert result["messages"] == []

    def test_supervisor_sends_input_to_worker(self, tmp_path):
        """Supervisor sends input → worker reads it from inbox."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(name="ITC test", prompt="test")
        handler.task_manager.update_status(task.task_id, "running")

        # Supervisor sends input to worker
        send_result = self._call_tool(handler, "task_send_input", {
            "task_id": task.task_id,
            "content": "You need to add error handling to the main function",
        })
        assert send_result["status"] == "sent"

        # Worker checks inbox
        inbox_result = self._call_tool(handler, "task_check_inbox", {"task_id": task.task_id})
        assert len(inbox_result["messages"]) == 1
        msg = inbox_result["messages"][0]
        assert msg["type"] == "instruction"
        assert msg["from"] == "supervisor"
        assert "error handling" in msg["content"]

        # Second check should be empty (messages acknowledged)
        inbox_result2 = self._call_tool(handler, "task_check_inbox", {"task_id": task.task_id})
        assert len(inbox_result2["messages"]) == 0

    def test_orchestrator_sends_instruction_to_worker(self, tmp_path):
        """Orchestrator sends instruction → worker reads it."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(name="Orch msg test", prompt="test")
        handler.task_manager.update_status(task.task_id, "running")

        # Orchestrator sends instruction
        send_result = self._call_tool(handler, "tasks_send", {
            "task_id": task.task_id,
            "msg_type": "instruction",
            "content": "Please also add a README",
        })
        assert send_result["status"] == "sent"

        # Worker checks inbox
        inbox_result = self._call_tool(handler, "task_check_inbox", {"task_id": task.task_id})
        assert len(inbox_result["messages"]) == 1
        assert "README" in inbox_result["messages"][0]["content"]

    def test_worker_gets_context(self, tmp_path):
        """Worker calling task_get_context should get prompt and recent messages."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(name="Context test", prompt="Build a snake game")
        handler.task_manager.update_status(task.task_id, "running")

        # Add some messages
        self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "progress",
            "summary": "Started",
            "from_tier": "worker",
        })

        result = self._call_tool(handler, "task_get_context", {"task_id": task.task_id})
        assert result["prompt"] == "Build a snake game"
        assert result["status"] == "running"
        assert len(result["recent_messages"]) >= 1

    def test_supervisor_reads_peer_logs(self, tmp_path):
        """Supervisor calling task_read_peer should see worker's logs."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(name="Peer test", prompt="test")
        handler.task_manager.update_status(task.task_id, "running")

        # Write worker.log directly (simulating real WorkerThread output)
        worker_log_path = os.path.join(task.working_dir, "worker.log")
        os.makedirs(os.path.dirname(worker_log_path), exist_ok=True)
        with open(worker_log_path, "w", encoding="utf-8") as f:
            f.write("Worker line 1\n")
            f.write("Worker line 2\n")

        # Also register an event in the event stream
        event_log = handler.event_registry.get_or_create(task.task_id, task.working_dir)
        event_log.append("worker", "files_read", "README.md", "file1 file2")

        result = self._call_tool(handler, "task_read_peer", {"task_id": task.task_id})
        # Check event stream section
        assert "files_read" in result["logs"]
        # Check worker stdout section
        assert "Worker line 1" in result["logs"]
        assert "Worker line 2" in result["logs"]

    def test_multiple_messages_in_sequence(self, tmp_path):
        """Multiple messages sent → all appear in inbox before acknowledgment."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(name="Multi msg", prompt="test")

        # Send 3 messages
        self._call_tool(handler, "task_send_input", {"task_id": task.task_id, "content": "Msg 1"})
        self._call_tool(handler, "task_send_input", {"task_id": task.task_id, "content": "Msg 2"})
        self._call_tool(handler, "task_send_input", {"task_id": task.task_id, "content": "Msg 3"})

        # All 3 should appear
        inbox = self._call_tool(handler, "task_check_inbox", {"task_id": task.task_id})
        assert len(inbox["messages"]) == 3

        # Now should be empty
        inbox2 = self._call_tool(handler, "task_check_inbox", {"task_id": task.task_id})
        assert len(inbox2["messages"]) == 0


# ── Test: Deferred completion ────────────────────────────────

class TestDeferredCompletion:
    """Worker completion is deferred when supervisor is active."""

    def _make_handler(self, data_dir: str, telegram_token: str | None = None) -> MCPProtocolHandler:
        tm = TaskManager(data_dir=data_dir)
        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        scheduler = Scheduler()
        policy = ExecutionPolicy(allow_all=True)
        return MCPProtocolHandler(
            scheduler=scheduler,
            data_dir=data_dir,
            telegram_token=telegram_token,
            task_manager=tm,
            worker_pool=pool,
            owner_chat_id="12345",
            execution_policy=policy,
        )

    def _call_tool(self, handler: MCPProtocolHandler, tool_name: str, args: dict) -> dict:
        response = handler.handle_request({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        })
        content = response["result"]["content"][0]["text"]
        import json
        return json.loads(content)

    @patch("copenclaw.integrations.copilot_cli.CopilotCli.run_prompt")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_worker_completion_deferred_with_active_supervisor(self, mock_which, mock_run, tmp_path):
        """When supervisor is running, worker 'completed' should be deferred."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        # Don't let supervisor actually run prompts (block forever)
        mock_run.side_effect = lambda *a, **kw: time.sleep(100)

        task = handler.task_manager.create_task(
            name="Defer test",
            prompt="test",
            auto_supervise=True,
        )
        handler.task_manager.update_status(task.task_id, "running")

        # Start a supervisor (it will block on run_prompt)
        working_dir = task.working_dir
        handler.worker_pool.start_supervisor(
            task_id=task.task_id,
            prompt=task.prompt,
            check_interval=999,  # Won't actually fire
            working_dir=working_dir,
        )

        # Give supervisor thread a moment to start
        time.sleep(0.5)

        # Worker reports completed — should be DEFERRED
        result = self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "completed",
            "summary": "All done!",
            "from_tier": "worker",
        })
        assert result["status"] == "deferred"

        sup = handler.worker_pool.get_supervisor(task.task_id)
        assert sup is not None
        assert sup.last_check_requested_at is not None

        # Task should NOT be completed yet
        task = handler.task_manager.get(task.task_id)
        assert task.status != "completed"

        # Clean up
        handler.worker_pool.stop_all()

    def test_worker_completion_direct_without_supervisor(self, tmp_path):
        """Without supervisor, worker 'completed' should finalize immediately."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(
            name="Direct complete",
            prompt="test",
            auto_supervise=False,
        )
        handler.task_manager.update_status(task.task_id, "running")

        result = self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "completed",
            "summary": "All done!",
            "from_tier": "worker",
        })
        assert result["status"] == "reported"

        task = handler.task_manager.get(task.task_id)
        assert task.status == "completed"

    def test_supervisor_assessment_completes_deferred(self, tmp_path):
        """Supervisor assessment should finalize a deferred completion."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(
            name="Defer resolve",
            prompt="test",
            auto_supervise=True,
        )
        handler.task_manager.update_status(task.task_id, "running")

        task.completion_deferred = True
        handler.task_manager._save()

        result = self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "assessment",
            "summary": "Verified outputs look good",
            "detail": "Path and listing look correct.",
            "from_tier": "supervisor",
        })
        assert result["status"] == "reported"

        task = handler.task_manager.get(task.task_id)
        assert task.status == "completed"

    def test_supervisor_assessment_negative_does_not_complete(self, tmp_path):
        """Negative supervisor assessment should not finalize deferred completion."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(
            name="Defer negative",
            prompt="test",
            auto_supervise=True,
        )
        handler.task_manager.update_status(task.task_id, "running")

        task.completion_deferred = True
        handler.task_manager._save()

        result = self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "assessment",
            "summary": "Worker reports done but no captured output",
            "detail": "Missing Get-Location/Get-ChildItem output",
            "from_tier": "supervisor",
        })
        assert result["status"] == "reported"

        task = handler.task_manager.get(task.task_id)
        assert task.status != "completed"

    def test_supervisor_assessment_completes_when_worker_exited(self, tmp_path):
        """Positive assessment should complete when worker is not running."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(
            name="No worker",
            prompt="test",
            auto_supervise=True,
        )
        handler.task_manager.update_status(task.task_id, "running")

        result = self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "assessment",
            "summary": "Verified outputs look good",
            "detail": "Path and listing look correct.",
            "from_tier": "supervisor",
        })
        assert result["status"] == "reported"

        task = handler.task_manager.get(task.task_id)
        assert task.status == "completed"


# ── Test: Task listing and filtering ─────────────────────────

class TestTaskListingE2E:

    def _make_handler(self, data_dir: str) -> MCPProtocolHandler:
        tm = TaskManager(data_dir=data_dir)
        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        scheduler = Scheduler()
        policy = ExecutionPolicy(allow_all=True)
        return MCPProtocolHandler(
            scheduler=scheduler,
            data_dir=data_dir,
            task_manager=tm,
            worker_pool=pool,
            execution_policy=policy,
        )

    def _call_tool(self, handler: MCPProtocolHandler, tool_name: str, args: dict) -> dict:
        response = handler.handle_request({
            "jsonrpc": "2.0", "id": 1,
            "method": "tools/call",
            "params": {"name": tool_name, "arguments": args},
        })
        content = response["result"]["content"][0]["text"]
        import json
        return json.loads(content)

    def test_list_empty(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        result = self._call_tool(handler, "tasks_list", {})
        assert result["tasks"] == []

    def test_list_with_filter(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        handler.task_manager.create_task(name="T1", prompt="test", status="proposed")
        handler.task_manager.create_task(name="T2", prompt="test")
        handler.task_manager.update_status(
            handler.task_manager.list_tasks()[-1].task_id, "running"
        )

        result = self._call_tool(handler, "tasks_list", {"status": "proposed"})
        assert len(result["tasks"]) == 1
        assert result["tasks"][0]["name"] == "T1"

# ── Test: Audit role/task context (Fix 1) ────────────────────

class TestAuditRoleContext:
    """Audit events should include role and task_id."""

    def _make_handler(self, data_dir: str, telegram_token: str | None = None) -> MCPProtocolHandler:
        tm = TaskManager(data_dir=data_dir)
        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        scheduler = Scheduler()
        policy = ExecutionPolicy(allow_all=True)
        return MCPProtocolHandler(
            scheduler=scheduler,
            data_dir=data_dir,
            telegram_token=telegram_token,
            task_manager=tm,
            worker_pool=pool,
            owner_chat_id="12345",
            execution_policy=policy,
        )

    def _call_tool_with_context(self, handler, tool_name, args, task_id=None, role=None):
        response = handler.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": tool_name, "arguments": args}},
            task_id=task_id, role=role,
        )
        import json
        content = response["result"]["content"][0]["text"]
        return json.loads(content)

    def test_files_write_audit_includes_role_and_task(self, tmp_path):
        """files_write called by a worker should log 'worker.files.write' with task_id."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(name="Audit test", prompt="test")

        self._call_tool_with_context(
            handler, "files_write", {"path": "note.txt", "content": "hello"},
            task_id=task.task_id, role="worker",
        )

        # Read audit.jsonl
        import json
        audit_path = os.path.join(data_dir, "audit.jsonl")
        assert os.path.exists(audit_path)
        with open(audit_path, "r") as f:
            events = [json.loads(line) for line in f if line.strip()]

        file_events = [e for e in events if "files.write" in e["type"]]
        assert len(file_events) >= 1
        last = file_events[-1]
        assert last["type"] == "worker.files.write"
        assert last["payload"]["task_id"] == task.task_id
        assert last["payload"]["task_name"] == "Audit test"

    def test_audit_defaults_to_orchestrator(self, tmp_path):
        """When no role is specified, audit should default to 'orchestrator'."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        self._call_tool_with_context(
            handler, "files_write", {"path": "note2.txt", "content": "test"},
        )

        import json
        audit_path = os.path.join(data_dir, "audit.jsonl")
        with open(audit_path, "r") as f:
            events = [json.loads(line) for line in f if line.strip()]

        file_events = [e for e in events if "files.write" in e["type"]]
        assert file_events[-1]["type"] == "orchestrator.files.write"

    def test_task_report_audit_includes_role(self, tmp_path):
        """task_report audit events should be prefixed with role."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(name="Report audit", prompt="test")
        handler.task_manager.update_status(task.task_id, "running")

        self._call_tool_with_context(
            handler, "task_report",
            {"task_id": task.task_id, "type": "progress", "summary": "Step 1", "from_tier": "worker"},
            task_id=task.task_id, role="worker",
        )

        import json
        audit_path = os.path.join(data_dir, "audit.jsonl")
        with open(audit_path, "r") as f:
            events = [json.loads(line) for line in f if line.strip()]

        report_events = [e for e in events if "task.report" in e["type"]]
        assert len(report_events) >= 1
        assert report_events[-1]["type"] == "worker.task.report.progress"

    @patch("copenclaw.mcp.protocol.TelegramAdapter")
    def test_send_message_audit_includes_details(self, mock_telegram, tmp_path):
        """send_message audit events should include channel/target and image usage."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir, telegram_token="token")

        task = handler.task_manager.create_task(name="Send audit", prompt="test")

        self._call_tool_with_context(
            handler,
            "send_message",
            {"channel": "telegram", "target": "999", "text": "hello"},
            task_id=task.task_id,
            role="worker",
        )

        import json
        audit_path = os.path.join(data_dir, "audit.jsonl")
        with open(audit_path, "r") as f:
            events = [json.loads(line) for line in f if line.strip()]

        send_events = [e for e in events if e["type"] == "worker.send_message"]
        assert len(send_events) >= 1
        payload = send_events[-1]["payload"]
        assert payload["channel"] == "telegram"
        assert payload["target"] == "999"
        assert payload["message_type"] == "text"
        assert payload["image_path_used"] is False


# ── Test: send_message visibility in supervisor views ──────────

class TestSendMessageVisibility:
    """send_message tool calls should be visible to supervisors."""

    def _make_handler(self, data_dir: str) -> MCPProtocolHandler:
        tm = TaskManager(data_dir=data_dir)
        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        scheduler = Scheduler()
        policy = ExecutionPolicy(allow_all=True)
        return MCPProtocolHandler(
            scheduler=scheduler,
            data_dir=data_dir,
            telegram_token="token",
            task_manager=tm,
            worker_pool=pool,
            owner_chat_id="12345",
            execution_policy=policy,
        )

    def _call_tool_with_context(self, handler, tool_name, args, task_id=None, role=None):
        response = handler.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": tool_name, "arguments": args}},
            task_id=task_id, role=role,
        )
        import json
        content = response["result"]["content"][0]["text"]
        return json.loads(content)

    @patch("copenclaw.mcp.protocol.TelegramAdapter")
    def test_send_message_visible_in_task_read_peer(self, mock_telegram, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(name="Send visibility", prompt="test")
        handler.task_manager.update_status(task.task_id, "running")

        self._call_tool_with_context(
            handler,
            "send_message",
            {"channel": "telegram", "target": "999", "text": "hello"},
            task_id=task.task_id,
            role="worker",
        )

        result = self._call_tool_with_context(
            handler,
            "task_read_peer",
            {"task_id": task.task_id},
            task_id=task.task_id,
            role="supervisor",
        )
        logs = result["logs"]
        assert "send_message" in logs
        assert "message_type=text" in logs
        assert "channel=telegram" in logs
        assert "target=999" in logs
        assert "image_path=no" in logs

        status = self._call_tool_with_context(
            handler, "tasks_status", {"task_id": task.task_id}
        )
        assert "Sent user message" in status["timeline"]
# ── Test: files_write MCP tool (Fix 3b) ─────────────────────

class TestFilesWrite:
    """Tests for the files_write MCP tool."""

    def _make_handler(self, data_dir: str) -> MCPProtocolHandler:
        tm = TaskManager(data_dir=data_dir)
        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        scheduler = Scheduler()
        policy = ExecutionPolicy(allow_all=True)
        return MCPProtocolHandler(
            scheduler=scheduler,
            data_dir=data_dir,
            task_manager=tm,
            worker_pool=pool,
            execution_policy=policy,
        )

    def _call_tool(self, handler, tool_name, args):
        response = handler.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": tool_name, "arguments": args}},
        )
        import json
        content = response["result"]["content"][0]["text"]
        return json.loads(content)

    def test_write_relative_path(self, tmp_path):
        """files_write with relative path creates file inside data_dir."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        result = self._call_tool(handler, "files_write", {
            "path": "test-output/hello.txt",
            "content": "Hello, world!",
        })
        assert result["status"] == "written"
        assert result["size"] == 13

        written = os.path.join(data_dir, "test-output", "hello.txt")
        assert os.path.exists(written)
        with open(written, "r") as f:
            assert f.read() == "Hello, world!"

    def test_write_outside_data_dir_allowed_with_warning(self, tmp_path):
        """files_write should allow paths outside data_dir (with a warning log)."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        # Use a safe temp path rather than ../../etc/passwd
        outside_path = str(tmp_path / "outside" / "test.txt")
        response = handler.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
"params": {"name": "files_write", "arguments": {"path": outside_path, "content": "hello"}}},
        )
        content = response["result"]["content"][0]["text"]
        assert response["result"]["isError"] is False
        assert os.path.exists(outside_path)
        with open(outside_path) as f:
            assert f.read() == "hello"

    def test_write_creates_parent_dirs(self, tmp_path):
        """files_write should auto-create parent directories."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        result = self._call_tool(handler, "files_write", {
            "path": "deep/nested/dir/file.py",
            "content": "print('hello')",
        })
        assert result["status"] == "written"
        assert os.path.exists(os.path.join(data_dir, "deep", "nested", "dir", "file.py"))

    def test_files_write_listed_in_tools(self, tmp_path):
        """files_write should appear in tools/list."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        response = handler.handle_request({
            "jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {},
        })
        tool_names = [t["name"] for t in response["result"]["tools"]]
        assert "files_write" in tool_names

# ── Test: CopilotCli add_dirs (Fix 3a) ──────────────────────

class TestCopilotCliAddDirs:
    """Tests for --add-dir support in CopilotCli."""

    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_add_dirs_included_in_command(self, mock_which, tmp_path):
        """add_dirs should produce --add-dir flags in the base command."""
        from copenclaw.integrations.copilot_cli import CopilotCli

        test_dir = str(tmp_path / "project")
        os.makedirs(test_dir)

        cli = CopilotCli(
            workspace_dir=str(tmp_path),
            add_dirs=[test_dir],
        )
        cmd = cli._base_cmd()
        assert "--add-dir" in cmd
        abs_test_dir = os.path.abspath(test_dir)
        idx = cmd.index("--add-dir")
        assert cmd[idx + 1] == abs_test_dir

    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_no_add_dirs_by_default(self, mock_which, tmp_path):
        """Without add_dirs, no --add-dir flags should appear."""
        from copenclaw.integrations.copilot_cli import CopilotCli

        cli = CopilotCli(workspace_dir=str(tmp_path))
        cmd = cli._base_cmd()
        assert "--add-dir" not in cmd

    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_nonexistent_add_dir_skipped(self, mock_which, tmp_path):
        """Non-existent directories in add_dirs should be silently skipped."""
        from copenclaw.integrations.copilot_cli import CopilotCli

        cli = CopilotCli(
            workspace_dir=str(tmp_path),
            add_dirs=[str(tmp_path / "does-not-exist")],
        )
        cmd = cli._base_cmd()
        assert "--add-dir" not in cmd

    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_autopilot_enabled_by_default(self, mock_which, tmp_path):
        from copenclaw.integrations.copilot_cli import CopilotCli

        cli = CopilotCli(workspace_dir=str(tmp_path))
        cmd = cli._base_cmd()
        assert "--autopilot" in cmd

    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_autopilot_can_be_disabled_per_instance(self, mock_which, tmp_path):
        from copenclaw.integrations.copilot_cli import CopilotCli

        cli = CopilotCli(workspace_dir=str(tmp_path), autopilot=False)
        cmd = cli._base_cmd()
        assert "--autopilot" not in cmd

    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_subprocess_launch_uses_explicit_cli_fallback(self, mock_which, tmp_path):
        from copenclaw.integrations.copilot_cli import CopilotCli

        cli = CopilotCli(
            workspace_dir=str(tmp_path),
            execution_backend="api",
            allow_cli_fallback=True,
        )
        cmd = cli.build_launch_command(require_subprocess=True)
        assert cmd[0] == "copilot"
        assert "--no-ask-user" in cmd

    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_subprocess_launch_requires_explicit_fallback(self, mock_which, tmp_path):
        from copenclaw.integrations.copilot_cli import CopilotCli, CopilotCliError

        cli = CopilotCli(
            workspace_dir=str(tmp_path),
            execution_backend="api",
            allow_cli_fallback=False,
        )
        with pytest.raises(CopilotCliError, match="subprocess launch"):
            cli.build_launch_command(require_subprocess=True)

    @patch("copenclaw.integrations.copilot_cli.subprocess.Popen")
    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_run_prompt_api_backend_falls_back_to_cli(self, mock_which, mock_popen, tmp_path):
        from copenclaw.integrations.copilot_cli import CopilotCli

        mock_popen.return_value = FakeProcess(["api fallback ok"], exit_code=0)
        cli = CopilotCli(
            workspace_dir=str(tmp_path),
            execution_backend="api",
            allow_cli_fallback=True,
        )
        output = cli.run_prompt("test prompt")
        assert "api fallback ok" in output

    @patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
    def test_run_prompt_api_backend_without_fallback_raises(self, mock_which, tmp_path):
        from copenclaw.integrations.copilot_cli import CopilotCli, CopilotCliError

        cli = CopilotCli(
            workspace_dir=str(tmp_path),
            execution_backend="api",
            allow_cli_fallback=False,
        )
        with pytest.raises(CopilotCliError, match="SDK backend unavailable"):
            cli.run_prompt("test prompt")

# ── Test: Worker instructions mention files_write (Fix 3d) ───

class TestWorkerInstructionsUpdated:
    """Worker instructions should mention files_write and built-in file tools."""

    def test_worker_template_mentions_files_write(self):
        result = worker_template(
            task_id="task-test",
            prompt="test prompt",
            workspace_root="/tmp/workspace",
        )
        assert "files_write" in result

    def test_worker_template_mentions_builtin_file_tools(self):
        result = worker_template(
            task_id="task-test",
            prompt="test prompt",
            workspace_root="/tmp/workspace",
        )
        assert "built-in file" in result.lower() or "built-in file" in result

    def test_worker_template_mentions_wait_loop(self):
        result = worker_template(
            task_id="task-test",
            prompt="test prompt",
            workspace_root="/tmp/workspace",
        )
        assert "wait loop" in result.lower() or "wait loop" in result
        assert "task_check_inbox" in result

# ── Test: Stuck-detection and auto-finalization ──────────────

class TestStuckDetection:
    """Tests for supervisor stuck-assessment detection and auto-finalization."""

    def _make_handler(self, data_dir: str) -> MCPProtocolHandler:
        tm = TaskManager(data_dir=data_dir)
        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        scheduler = Scheduler()
        policy = ExecutionPolicy(allow_all=True)
        return MCPProtocolHandler(
            scheduler=scheduler,
            data_dir=data_dir,
            task_manager=tm,
            worker_pool=pool,
            owner_chat_id="12345",
            execution_policy=policy,
        )

    def _call_tool(self, handler: MCPProtocolHandler, tool_name: str, args: dict, task_id: str | None = None, role: str | None = None) -> dict:
        response = handler.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
             "params": {"name": tool_name, "arguments": args}},
            task_id=task_id, role=role,
        )
        content = response["result"]["content"][0]["text"]
        import json
        return json.loads(content)

    def test_supervisor_assessment_count_increments(self, tmp_path):
        """Each supervisor assessment should increment the counter."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(
            name="Count test", prompt="test", auto_supervise=True,
        )
        handler.task_manager.update_status(task.task_id, "running")
        task.completion_deferred = True
        handler.task_manager._save()

        # First assessment with negative signal — should NOT complete
        self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "assessment",
            "summary": "Worker output is truncated and incomplete",
            "from_tier": "supervisor",
        }, task_id=task.task_id, role="supervisor")

        task = handler.task_manager.get(task.task_id)
        assert task.supervisor_assessment_count == 1
        assert task.status != "completed"

        # Second assessment with negative signal — should auto-complete
        # because worker is dead (not in pool) and count >= 2
        # BUT it has strong_negative so it should NOT auto-complete
        self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "assessment",
            "summary": "Still incomplete, missing output",
            "from_tier": "supervisor",
        }, task_id=task.task_id, role="supervisor")

        task = handler.task_manager.get(task.task_id)
        assert task.supervisor_assessment_count == 2
        # Has strong negative ("incomplete", "missing") so NOT auto-completed
        assert task.status != "completed"

    def test_stuck_assessment_auto_completes_without_negative(self, tmp_path):
        """After 2+ assessments with no strong negative and dead worker, auto-complete."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(
            name="Auto-complete test", prompt="test", auto_supervise=True,
        )
        handler.task_manager.update_status(task.task_id, "running")
        task.completion_deferred = True
        task.completion_deferred_summary = "All work done"
        task.supervisor_assessment_count = 1  # Pre-set to 1
        handler.task_manager._save()

        # Second assessment with neutral/positive signal and dead worker
        self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "assessment",
            "summary": "Worker output looks reasonable, task appears done",
            "from_tier": "supervisor",
        }, task_id=task.task_id, role="supervisor")

        task = handler.task_manager.get(task.task_id)
        # Should be auto-completed: count >= 2, no strong negative, worker dead
        assert task.status == "completed"
        assert task.supervisor_assessment_count == 0  # Reset after completion

    def test_worker_activity_tracking(self, tmp_path):
        """Worker MCP calls should update last_worker_activity_at."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(
            name="Activity test", prompt="test",
        )
        handler.task_manager.update_status(task.task_id, "running")

        assert task.last_worker_activity_at is None

        # Worker makes an MCP call
        self._call_tool(handler, "task_check_inbox", {
            "task_id": task.task_id,
        }, task_id=task.task_id, role="worker")

        task = handler.task_manager.get(task.task_id)
        assert task.last_worker_activity_at is not None

    def test_task_read_peer_includes_worker_status_block(self, tmp_path):
        """task_read_peer should include worker status block with process state."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(
            name="Status block test", prompt="test",
        )
        handler.task_manager.update_status(task.task_id, "running")
        task.completion_deferred = True
        task.completion_deferred_summary = "Work done"
        task.supervisor_assessment_count = 3
        handler.task_manager._save()

        result = self._call_tool(handler, "task_read_peer", {
            "task_id": task.task_id,
        }, task_id=task.task_id, role="supervisor")

        logs = result["logs"]
        assert "=== Worker Status ===" in logs
        assert "Completion deferred: YES" in logs
        assert 'Worker said: "Work done"' in logs
        assert "Supervisor assessments so far: 3" in logs
        assert "ACTION REQUIRED" in logs  # Worker not running + deferred

    def test_supervisor_explicit_completed_clears_deferred(self, tmp_path):
        """Supervisor reporting type='completed' directly should clear deferred state."""
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        task = handler.task_manager.create_task(
            name="Explicit complete", prompt="test", auto_supervise=True,
        )
        handler.task_manager.update_status(task.task_id, "running")
        task.completion_deferred = True
        task.completion_deferred_summary = "Work done"
        task.supervisor_assessment_count = 1
        handler.task_manager._save()

        # Supervisor explicitly reports completed
        self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "completed",
            "summary": "Verified all work is correct",
            "from_tier": "supervisor",
        }, task_id=task.task_id, role="supervisor")

        task = handler.task_manager.get(task.task_id)
        assert task.status == "completed"
        assert task.completion_deferred is False
        assert task.supervisor_assessment_count == 0

    def test_supervisor_template_mentions_decision_deadlines(self):
        """Supervisor template should contain decision deadline rules."""
        result = supervisor_template(
            task_id="task-test",
            prompt="test",
            worker_session_id="session-1",
            supervisor_instructions="verify",
            workspace_root="/tmp",
        )
        assert "DECISION DEADLINES" in result
        assert "Do NOT report" in result
        assert "assessment" in result


class TestContinuousImprovementProtocol:
    def _make_handler(self, data_dir: str) -> MCPProtocolHandler:
        tm = TaskManager(data_dir=data_dir)
        pool = WorkerPool(mcp_server_url="http://127.0.0.1:18790/mcp")
        scheduler = Scheduler()
        policy = ExecutionPolicy(allow_all=True)
        return MCPProtocolHandler(
            scheduler=scheduler,
            data_dir=data_dir,
            task_manager=tm,
            worker_pool=pool,
            execution_policy=policy,
        )

    def _call_tool(self, handler: MCPProtocolHandler, tool_name: str, args: dict, task_id: str | None = None, role: str | None = None) -> dict:
        response = handler.handle_request(
            {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": tool_name, "arguments": args}},
            task_id=task_id,
            role=role,
        )
        import json
        return json.loads(response["result"]["content"][0]["text"])

    def test_tasks_create_continuous_includes_status_block(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)

        result = self._call_tool(handler, "tasks_create", {
            "prompt": "Run continuous loop",
            "task_type": "continuous_improvement",
            "continuous": {"max_iterations": 4, "objective": "Improve tests"},
            "auto_supervise": False,
        })
        task_id = result["task_id"]
        status = self._call_tool(handler, "tasks_status", {"task_id": task_id})
        assert status["task_type"] == "continuous_improvement"
        assert "continuous" in status
        assert status["continuous"]["config"]["max_iterations"] == 4

    def test_supervisor_assessment_keywords_do_not_autocomplete_continuous(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)
        task = handler.task_manager.create_task(
            name="CI",
            prompt="Continuous",
            task_type="continuous_improvement",
            auto_supervise=True,
        )
        handler.task_manager.update_status(task.task_id, "running")
        task.completion_deferred = True
        handler.task_manager._save()

        result = self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "assessment",
            "summary": "ITERATION_SCORE: {'completed_checks': 5, 'failed': 0}",
            "detail": "{\"completed\": false, \"failed\": 0}",
            "from_tier": "supervisor",
            "continuous": {"iteration": 1, "score": 0.3},
        }, task_id=task.task_id, role="supervisor")
        assert result["status"] == "reported"
        updated = handler.task_manager.get(task.task_id)
        assert updated.status != "completed"

    def test_continuous_budget_completion_propagates_through_task_report(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)
        task = handler.task_manager.create_task(
            name="Budget",
            prompt="Continuous",
            task_type="continuous_improvement",
            ci_config={"max_iterations": 1},
            auto_supervise=False,
        )
        handler.task_manager.update_status(task.task_id, "running")
        result = self._call_tool(handler, "task_report", {
            "task_id": task.task_id,
            "type": "progress",
            "summary": "ITERATION_RESULT: 1",
            "from_tier": "worker",
            "continuous": {"iteration": 1, "checkpoint": True},
        }, task_id=task.task_id, role="worker")
        assert result["status"] == "reported"
        updated = handler.task_manager.get(task.task_id)
        assert updated.status == "completed"
        assert updated.ci_state["stop_reason"] == "max_iterations_reached"

    def test_tasks_send_priority_updates_continuous_budget(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)
        task = handler.task_manager.create_task(
            name="Priority",
            prompt="Continuous",
            task_type="continuous_improvement",
            auto_supervise=False,
        )

        result = self._call_tool(handler, "tasks_send", {
            "task_id": task.task_id,
            "msg_type": "priority",
            "content": "{\"budget_patch\": {\"max_iterations\": 7}}",
        })
        assert result["status"] == "sent"
        updated = handler.task_manager.get(task.task_id)
        assert updated.ci_config["max_iterations"] == 7

    def test_continuous_completion_auto_chains_and_dispatches(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)
        task = handler.task_manager.create_task(
            name="Mission",
            prompt="Improve system quality",
            task_type="continuous_improvement",
            ci_config={"objective": "Improve system quality"},
            auto_supervise=False,
        )
        handler.task_manager.update_status(task.task_id, "completed")
        task.ci_state["mission_generation"] = 1
        task.ci_state["current_direction"] = "ux"
        handler.task_manager._save()

        with patch.object(handler, "_start_task", return_value={"status": "running"}) as mock_start:
            handler._fire_on_complete_hook(
                task,
                "COMPLETED successfully",
                "Improved validation and tests",
                "Added stronger validation and fixed flaky tests.",
            )

        tasks = handler.task_manager.list_tasks()
        assert len(tasks) == 2
        follow_up = next(t for t in tasks if t.task_id != task.task_id)
        assert follow_up.task_type == "continuous_improvement"
        assert mock_start.call_count == 1
        assert "[CONTINUOUS_MISSION_HANDOFF]" in follow_up.prompt
        assert "What changed:" in follow_up.prompt
        assert "What remains:" in follow_up.prompt
        assert "Current risks:" in follow_up.prompt
        assert follow_up.ci_state["mission_id"] == task.task_id

    def test_continuous_chain_preserves_mission_context(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)
        task = handler.task_manager.create_task(
            name="Mission",
            prompt="Improve release reliability",
            task_type="continuous_improvement",
            ci_config={"objective": "Improve release reliability"},
            auto_supervise=False,
        )
        handler.task_manager.update_status(task.task_id, "completed")
        task.ci_state["mission_generation"] = 2
        task.ci_state["mission_id"] = "mission-123"
        task.ci_state["mission_base_prompt"] = "Improve release reliability"
        task.ci_state["mission_objective"] = "Improve release reliability"
        handler.task_manager._save()

        with patch.object(handler, "_start_task", return_value={"status": "running"}):
            handler._fire_on_complete_hook(
                task,
                "COMPLETED successfully",
                "Stabilized release checks",
                "Reduced flaky release checks and improved retries.",
            )

        follow_up = max(handler.task_manager.list_tasks(), key=lambda t: t.created_at)
        assert "Mission objective: Improve release reliability" in follow_up.prompt
        assert "Prior task: Mission" in follow_up.prompt
        assert "Chosen direction:" in follow_up.prompt
        assert follow_up.ci_state["mission_id"] == "mission-123"
        assert follow_up.ci_state["mission_generation"] == 3

    def test_continuous_chain_direction_is_diverse_across_iterations(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)
        seed = handler.task_manager.create_task(
            name="Mission",
            prompt="Improve platform",
            task_type="continuous_improvement",
            ci_config={"objective": "Improve platform"},
            auto_supervise=False,
        )
        handler.task_manager.update_status(seed.task_id, "completed")
        seed.ci_state["mission_generation"] = 1
        seed.ci_state["current_direction"] = "performance"
        seed.ci_state["mission_direction_history"] = ["ux", "reliability", "performance"]
        handler.task_manager._save()

        with patch.object(handler, "_start_task", return_value={"status": "running"}):
            handler._fire_on_complete_hook(seed, "COMPLETED successfully", "Pass 1", "")

        first_follow_up = max(handler.task_manager.list_tasks(), key=lambda t: t.created_at)
        first_direction = first_follow_up.ci_state.get("current_direction")
        assert first_direction != "performance"

        handler.task_manager.update_status(first_follow_up.task_id, "completed")
        with patch.object(handler, "_start_task", return_value={"status": "running"}):
            handler._fire_on_complete_hook(first_follow_up, "COMPLETED successfully", "Pass 2", "")
        all_tasks = handler.task_manager.list_tasks()
        second_follow_up = max(
            [t for t in all_tasks if t.task_id not in {seed.task_id, first_follow_up.task_id}],
            key=lambda t: t.created_at,
        )
        second_direction = second_follow_up.ci_state.get("current_direction")
        assert second_direction != first_direction

    def test_continuous_cancelled_task_does_not_chain(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)
        task = handler.task_manager.create_task(
            name="Mission",
            prompt="Improve safety",
            task_type="continuous_improvement",
            auto_supervise=False,
        )
        handler.task_manager.update_status(task.task_id, "cancelled")
        with patch.object(handler, "_start_task", return_value={"status": "running"}) as mock_start:
            handler._fire_on_complete_hook(task, "CANCELLED by user", "Cancelled", "")
        assert len(handler.task_manager.list_tasks()) == 1
        assert mock_start.call_count == 0
        updated = handler.task_manager.get(task.task_id)
        assert any(e.event == "chain_stopped" for e in updated.timeline)

    def test_continuous_failure_chain_stops_at_failure_limit(self, tmp_path):
        data_dir = str(tmp_path / "data")
        os.makedirs(data_dir)
        handler = self._make_handler(data_dir)
        task = handler.task_manager.create_task(
            name="Mission",
            prompt="Improve reliability",
            task_type="continuous_improvement",
            ci_config={"auto_chain_failure_limit": 2},
            auto_supervise=False,
        )
        handler.task_manager.update_status(task.task_id, "failed")

        with patch.object(handler, "_start_task", return_value={"status": "running"}) as mock_start:
            handler._fire_on_complete_hook(task, "FAILED — first", "First failure", "")
            first_follow_up = max(handler.task_manager.list_tasks(), key=lambda t: t.created_at)
            assert first_follow_up.task_id != task.task_id
            assert first_follow_up.ci_state["chain_failure_streak"] == 1

            handler.task_manager.update_status(first_follow_up.task_id, "failed")
            handler._fire_on_complete_hook(first_follow_up, "FAILED — second", "Second failure", "")

        assert mock_start.call_count == 1
        assert len(handler.task_manager.list_tasks()) == 2
        assert handler.task_manager.get(first_follow_up.task_id).status == "failed"

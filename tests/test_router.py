import tempfile
from datetime import datetime, timedelta, timezone
from unittest.mock import patch

from copenclaw.core.pairing import PairingStore
from copenclaw.core.router import (
    ChatRequest,
    ChatResponse,
    _should_stop_after_proposal_line,
    handle_chat,
)
from copenclaw.core.scheduler import Scheduler
from copenclaw.core.session import SessionStore
from copenclaw.core.tasks import TaskManager
from copenclaw.integrations.copilot_cli import CopilotCli, CopilotCliError
from copenclaw.integrations.telegram import TelegramAdapter

def _make_deps(
    tmpdir: str,
    allow_from: list[str] | None = None,
    with_tasks: bool = False,
    with_scheduler: bool = False,
):
    pairing = PairingStore(store_path=f"{tmpdir}/pairing.json")
    sessions = SessionStore(store_path=f"{tmpdir}/sessions.json")
    cli = CopilotCli()
    deps = {
        "pairing": pairing,
        "sessions": sessions,
        "cli": cli,
        "allow_from": allow_from or ["42", "u1", "alice", "bob"],
        "data_dir": tmpdir,
    }
    if with_tasks:
        deps["task_manager"] = TaskManager(data_dir=tmpdir)
    if with_scheduler:
        deps["scheduler"] = Scheduler(
            store_path=f"{tmpdir}/jobs.json",
            run_log_path=f"{tmpdir}/job-runs.jsonl",
        )
    return deps

def test_whoami() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/whoami")
        resp = handle_chat(req, **deps)
        assert resp.text == "telegram:42"
        assert resp.status == "ok"

def test_status() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        req = ChatRequest(channel="msteams", sender_id="u1", chat_id="c1", text="/status")
        resp = handle_chat(req, **deps)
        assert "COpenClaw" in resp.text

def test_help() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/help")
        resp = handle_chat(req, **deps)
        assert "COpenClaw commands" in resp.text
        assert "/tasks" in resp.text
        assert "/jobs" in resp.text
        assert "/cancel" in resp.text

def test_exec_denied() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, allow_from=["admin1"])
        req = ChatRequest(channel="telegram", sender_id="stranger", chat_id="100", text="/exec whoami")
        resp = handle_chat(req, **deps)
        assert resp.status == "denied"
        assert "Not authorized" in resp.text

def test_exec_allowed() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, allow_from=["admin1"])
        req = ChatRequest(channel="telegram", sender_id="admin1", chat_id="100", text="/exec echo hello")
        with patch.dict("os.environ", {"copenclaw_ALLOW_ALL_COMMANDS": "true"}):
            resp = handle_chat(req, **deps)
        assert resp.status == "ok"
        assert "hello" in resp.text

def test_unauthorized_user_denied() -> None:
    """Unauthorized user gets denied with instructions to edit .env."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, allow_from=["admin1"])
        req = ChatRequest(channel="telegram", sender_id="newuser", chat_id="100", text="hello")
        resp = handle_chat(req, **deps)
        assert resp.status == "denied"
        assert "not authorized" in resp.text.lower()
        assert "newuser" in resp.text  # shows the user's ID
        assert "TELEGRAM_ALLOW_FROM" in resp.text  # shows env var instructions

def test_owner_not_auto_authorized() -> None:
    """Owner ID is not auto-authorized unless allowlisted."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, allow_from=[])
        deps["owner_id"] = "owner123"
        monkeypatch_cli = deps["cli"]
        import types
        monkeypatch_cli.run_prompt = types.MethodType(lambda self, prompt, **kw: "echo:hello", monkeypatch_cli)

        req = ChatRequest(channel="telegram", sender_id="owner123", chat_id="100", text="hello")
        resp = handle_chat(req, **deps)
        assert resp.status == "denied"
        assert "not authorized" in resp.text.lower()

def test_freetext_calls_copilot(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        monkeypatch.setattr(deps["cli"], "run_prompt", lambda prompt, **kw: f"echo:{prompt}")
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="explain quantum computing")
        resp = handle_chat(req, **deps)
        # First message has no history, so prompt is passed through as-is
        # (delegation reminder suffix is appended but the core prompt is there)
        assert resp.text.startswith("echo:explain quantum computing")

def test_proposal_stop_line_detection() -> None:
    assert _should_stop_after_proposal_line("Reply Yes to approve or No to reject.")
    assert _should_stop_after_proposal_line("reply yes to approve or no to reject")
    assert not _should_stop_after_proposal_line("I will continue investigating now.")

def test_freetext_passes_proposal_stop_callback(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        captured = {}

        def mock_run(prompt, **kw):
            captured["on_line"] = kw.get("on_line")
            return "ok"

        monkeypatch.setattr(deps["cli"], "run_prompt", mock_run)
        monkeypatch.setattr(deps["cli"], "_discover_latest_non_task_session_id", lambda: None)

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="propose something")
        resp = handle_chat(req, **deps)

        assert resp.text == "ok"
        assert callable(captured.get("on_line"))
        assert captured["on_line"]("Reply Yes to approve or No to reject.") is True

def test_freetext_recovers_from_stale_resume_session(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        session_key = "telegram:dm:42"
        deps["sessions"].set_copilot_session_id(session_key, "stale-session-id")

        calls = {"count": 0}

        def flaky_run(prompt, **kw):
            calls["count"] += 1
            if calls["count"] == 1:
                raise CopilotCliError("copilot CLI failed with exit code 1")
            assert kw.get("resume_id") is None
            return "recovered"

        monkeypatch.setattr(deps["cli"], "run_prompt", flaky_run)
        monkeypatch.setattr(deps["cli"], "_discover_latest_non_task_session_id", lambda: None)

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="status")
        resp = handle_chat(req, **deps)

        assert resp.text == "recovered"
        assert calls["count"] == 2
        assert deps["sessions"].get_copilot_session_id(session_key) is None

def test_freetext_ignores_task_role_session_id(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        session_key = "telegram:dm:42"
        deps["sessions"].set_copilot_session_id(session_key, "task-session-id")

        resume_ids = []

        def run_prompt(prompt, **kw):
            resume_ids.append(kw.get("resume_id"))
            return "ok"

        monkeypatch.setattr(deps["cli"], "session_is_task_role", lambda sid: sid == "task-session-id")
        monkeypatch.setattr(deps["cli"], "run_prompt", run_prompt)
        monkeypatch.setattr(deps["cli"], "_discover_latest_non_task_session_id", lambda: "fresh-session-id")

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="Status")
        resp = handle_chat(req, **deps)

        assert resp.text == "ok"
        assert resume_ids == [None]
        assert deps["sessions"].get_copilot_session_id(session_key) == "fresh-session-id"

def test_telegram_ping_back_schedules_and_delivers(monkeypatch) -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_scheduler=True)
        sched: Scheduler = deps["scheduler"]

        requested_at = datetime.utcnow()
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="ping back in 20 seconds")
        resp = handle_chat(req, **deps)
        assert "Ping scheduled" in resp.text

        jobs = sched.list()
        assert len(jobs) == 1
        job = jobs[0]
        assert job.payload["channel"] == "telegram"
        assert job.payload["target"] == "100"
        assert (job.run_at - requested_at).total_seconds() <= 60

        monkeypatch.setattr(deps["cli"], "run_prompt", lambda prompt, **kw: "ping")
        due = sched.due(now=requested_at + timedelta(seconds=59))
        assert job in due

        with patch("copenclaw.integrations.telegram.TelegramAdapter.send_message") as send_mock:
            TelegramAdapter("token").send_message(
                chat_id=int(job.payload["target"]),
                text=deps["cli"].run_prompt(job.payload["prompt"]),
            )
            send_mock.assert_called_once()
            assert send_mock.call_args.kwargs.get("text") == "ping"

        sched.log_run(job.job_id, "delivered")
        sched.mark_completed(job.job_id)

# ── Task proposal approval tests ─────────────────────────────

def test_approve_proposed_task() -> None:
    """User replies 'Yes' to approve a proposed task."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]

        task = tm.create_task(
            name="test-deploy-alpha",
            prompt="Deploy the app",
            plan="1. Build\n2. Deploy\n3. Verify",
            channel="telegram",
            target="100",
            status="proposed",
        )

        approved_ids = []
        def mock_approve(task_id):
            approved_ids.append(task_id)
            tm.update_status(task_id, "running")
            return {"task_id": task_id, "status": "running"}

        deps["on_task_approved"] = mock_approve

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="Yes")
        resp = handle_chat(req, **deps)
        assert "Approved" in resp.text
        assert "test-deploy-alpha" in resp.text
        assert task.task_id in approved_ids

def test_reject_proposed_task() -> None:
    """User replies 'No' to reject a proposed task."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]

        task = tm.create_task(
            name="test-scan-bravo",
            prompt="Scan the repo",
            plan="1. Scan all files",
            channel="telegram",
            target="100",
            status="proposed",
        )

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="No")
        resp = handle_chat(req, **deps)
        assert "Rejected" in resp.text
        assert "test-scan-bravo" in resp.text
        assert tm.get(task.task_id).status == "cancelled"

def test_no_proposal_passes_through(monkeypatch) -> None:
    """When no proposed task exists, 'Yes' goes to Copilot CLI as free text."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        monkeypatch.setattr(deps["cli"], "run_prompt", lambda prompt, **kw: f"echo:{prompt}")
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="Yes")
        resp = handle_chat(req, **deps)
        assert resp.text.startswith("echo:Yes")

def test_approve_with_emoji() -> None:
    """User replies with 👍 emoji to approve."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]

        task = tm.create_task(
            name="emoji-task",
            prompt="Do something",
            plan="Plan here",
            channel="telegram",
            target="100",
            status="proposed",
        )

        approved_ids = []
        deps["on_task_approved"] = lambda tid: approved_ids.append(tid) or {"task_id": tid}

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="👍")
        resp = handle_chat(req, **deps)
        assert "Approved" in resp.text
        assert task.task_id in approved_ids

def test_reject_with_emoji() -> None:
    """User replies with 👎 emoji to reject."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]

        task = tm.create_task(
            name="reject-task",
            prompt="Do something",
            plan="Plan",
            channel="telegram",
            target="100",
            status="proposed",
        )

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="👎")
        resp = handle_chat(req, **deps)
        assert "Rejected" in resp.text
        assert tm.get(task.task_id).status == "cancelled"

def test_proposal_filtered_by_channel() -> None:
    """Proposals are filtered by channel so a Teams proposal doesn't match Telegram 'Yes'."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]

        # Proposal for Teams, not Telegram
        tm.create_task(
            name="teams-task",
            prompt="Do something",
            plan="Plan",
            channel="msteams",
            target="conv1",
            status="proposed",
        )

        import types
        deps["cli"].run_prompt = types.MethodType(lambda self, prompt, **kw: f"echo:{prompt}", deps["cli"])

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="Yes")
        resp = handle_chat(req, **deps)
        assert resp.text.startswith("echo:Yes")

# ── Slash command tests: /tasks, /task, /jobs, /job, /cancel, /logs, /proposed ─

def test_tasks_no_manager() -> None:
    """/tasks when task_manager is not provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/tasks")
        resp = handle_chat(req, **deps)
        assert "not available" in resp.text

def test_tasks_empty() -> None:
    """/tasks when no tasks exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/tasks")
        resp = handle_chat(req, **deps)
        assert "No active" in resp.text

def test_tasks_lists_active() -> None:
    """/tasks lists active and proposed tasks."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]
        tm.create_task(name="running-task", prompt="Do work", status="pending")
        tm.create_task(name="proposed-task", prompt="Plan", plan="The plan", status="proposed")

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/tasks")
        resp = handle_chat(req, **deps)
        assert "2 task(s)" in resp.text
        assert "running-task" in resp.text
        assert "proposed-task" in resp.text

def test_task_detail() -> None:
    """/task <id> shows detailed info."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]
        task = tm.create_task(name="detail-task", prompt="Do it", plan="Step 1\nStep 2")

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text=f"/task {task.task_id}")
        resp = handle_chat(req, **deps)
        assert "detail-task" in resp.text
        assert task.task_id in resp.text
        assert "Step 1" in resp.text
        assert "Timeline" in resp.text

def test_task_detail_not_found() -> None:
    """/task with invalid ID."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/task task-nonexistent")
        resp = handle_chat(req, **deps)
        assert "not found" in resp.text.lower()

def test_proposed_empty() -> None:
    """/proposed when none pending."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/proposed")
        resp = handle_chat(req, **deps)
        assert "No pending" in resp.text

def test_proposed_lists() -> None:
    """/proposed lists awaiting proposals."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]
        tm.create_task(name="prop-a", prompt="A", plan="Plan A", status="proposed")
        tm.create_task(name="prop-b", prompt="B", plan="Plan B", status="proposed")

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/proposed")
        resp = handle_chat(req, **deps)
        assert "2 proposal(s)" in resp.text
        assert "prop-a" in resp.text
        assert "prop-b" in resp.text

def test_jobs_no_scheduler() -> None:
    """/jobs when scheduler is not provided."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/jobs")
        resp = handle_chat(req, **deps)
        assert "not available" in resp.text

def test_jobs_empty() -> None:
    """/jobs when no jobs exist."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_scheduler=True)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/jobs")
        resp = handle_chat(req, **deps)
        assert "No active" in resp.text

def test_jobs_lists_active() -> None:
    """/jobs lists scheduled jobs."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_scheduler=True)
        sched: Scheduler = deps["scheduler"]
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        sched.schedule("daily-report", future, {"prompt": "report", "channel": "telegram", "target": "123"})

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/jobs")
        resp = handle_chat(req, **deps)
        assert "1 active job" in resp.text
        assert "daily-report" in resp.text

def test_job_detail() -> None:
    """/job <id> shows detailed info."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_scheduler=True)
        sched: Scheduler = deps["scheduler"]
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        job = sched.schedule("my-job", future, {"prompt": "do stuff", "channel": "telegram", "target": "123"}, cron_expr="0 8 * * *")

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text=f"/job {job.job_id}")
        resp = handle_chat(req, **deps)
        assert "my-job" in resp.text
        assert "0 8 * * *" in resp.text
        assert "do stuff" in resp.text

def test_job_detail_not_found() -> None:
    """/job with invalid ID."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_scheduler=True)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/job job-nonexistent")
        resp = handle_chat(req, **deps)
        assert "not found" in resp.text.lower()

def test_logs_empty() -> None:
    """/logs for a task with no logs yet."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]
        task = tm.create_task(name="log-task", prompt="Work")

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text=f"/logs {task.task_id}")
        resp = handle_chat(req, **deps)
        assert "No logs" in resp.text

def test_logs_with_content() -> None:
    """/logs for a task with log output."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]
        task = tm.create_task(name="log-task", prompt="Work")
        tm.append_log(task.task_id, "Line 1: Starting")
        tm.append_log(task.task_id, "Line 2: Processing")

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text=f"/logs {task.task_id}")
        resp = handle_chat(req, **deps)
        assert "Line 1" in resp.text
        assert "Line 2" in resp.text

def test_cancel_task() -> None:
    """/cancel cancels a task."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]
        task = tm.create_task(name="cancel-me", prompt="Work")
        tm.update_status(task.task_id, "running")

        cancelled_ids = []
        deps["on_task_cancelled"] = lambda tid: cancelled_ids.append(tid)

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text=f"/cancel {task.task_id}")
        resp = handle_chat(req, **deps)
        assert "Cancelled" in resp.text
        assert "cancel-me" in resp.text
        assert tm.get(task.task_id).status == "cancelled"
        assert task.task_id in cancelled_ids

def test_cancel_already_done() -> None:
    """/cancel on an already completed task."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True)
        tm: TaskManager = deps["task_manager"]
        task = tm.create_task(name="done-task", prompt="Work")
        tm.update_status(task.task_id, "completed")

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text=f"/cancel {task.task_id}")
        resp = handle_chat(req, **deps)
        assert "already completed" in resp.text

def test_cancel_job() -> None:
    """/cancel cancels a job."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_scheduler=True)
        sched: Scheduler = deps["scheduler"]
        future = datetime.now(timezone.utc) + timedelta(hours=1)
        job = sched.schedule("cancel-job", future, {"prompt": "x", "channel": "telegram", "target": "1"})

        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text=f"/cancel {job.job_id}")
        resp = handle_chat(req, **deps)
        assert "Cancelled" in resp.text

def test_cancel_not_found() -> None:
    """/cancel with unknown ID."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, with_tasks=True, with_scheduler=True)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/cancel task-nonexistent")
        resp = handle_chat(req, **deps)
        assert "Not found" in resp.text

# ── Conversation context tests ────────────────────────────────

def test_conversation_context_stored(monkeypatch) -> None:
    """Free-text messages should store user and assistant in session history."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        monkeypatch.setattr(deps["cli"], "run_prompt", lambda prompt, **kw: "I know about quantum stuff")
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="explain quantum computing")
        handle_chat(req, **deps)

        sessions: SessionStore = deps["sessions"]
        history = sessions.get_history("telegram:dm:42")
        assert len(history) == 2
        assert history[0]["role"] == "user"
        assert history[0]["text"] == "explain quantum computing"
        assert history[1]["role"] == "assistant"
        assert history[1]["text"] == "I know about quantum stuff"

def test_conversation_context_sent_to_cli(monkeypatch) -> None:
    """Second message should use --resume instead of prepending history.

    The router no longer builds a context prompt; instead it passes the
    raw user text and a ``resume_id`` kwarg so Copilot CLI resumes the
    previous session natively.
    """
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        calls: list[dict] = []

        def mock_run(prompt, **kw):
            calls.append({"prompt": prompt, **kw})
            return "response"

        monkeypatch.setattr(deps["cli"], "run_prompt", mock_run)
        # Simulate that non-task session discovery returns a session ID
        monkeypatch.setattr(deps["cli"], "_discover_latest_non_task_session_id", lambda: "sess-abc123")

        # First message — no stored copilot session ID yet
        req1 = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="What is Python?")
        handle_chat(req1, **deps)
        assert calls[0]["prompt"].startswith("What is Python?")
        assert calls[0].get("resume_id") is None  # no session to resume yet

        # After first call, session ID should have been stored
        sessions: SessionStore = deps["sessions"]
        stored_sid = sessions.get_copilot_session_id("telegram:dm:42")
        assert stored_sid == "sess-abc123"

        # Second message — should pass resume_id
        req2 = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="Tell me more")
        handle_chat(req2, **deps)
        assert calls[1]["prompt"].startswith("Tell me more")
        assert calls[1].get("resume_id") == "sess-abc123"
        # No conversation history prefix should be in the prompt
        assert "[Conversation history" not in calls[1]["prompt"]

def test_conversation_context_per_sender(monkeypatch) -> None:
    """Different senders should have separate conversation histories."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        prompts_received = []
        monkeypatch.setattr(deps["cli"], "run_prompt", lambda prompt, **kw: prompts_received.append(prompt) or "reply")

        # Sender A sends a message
        req_a = ChatRequest(channel="telegram", sender_id="alice", chat_id="100", text="Hello from Alice")
        handle_chat(req_a, **deps)

        # Sender B sends a message — should NOT see Alice's history
        req_b = ChatRequest(channel="telegram", sender_id="bob", chat_id="200", text="Hello from Bob")
        handle_chat(req_b, **deps)
        assert prompts_received[1].startswith("Hello from Bob")  # No context prefix

        sessions: SessionStore = deps["sessions"]
        alice_history = sessions.get_history("telegram:dm:alice")
        bob_history = sessions.get_history("telegram:dm:bob")
        assert len(alice_history) == 2
        assert len(bob_history) == 2
        assert alice_history[0]["text"] == "Hello from Alice"
        assert bob_history[0]["text"] == "Hello from Bob"

def test_conversation_context_multi_turn(monkeypatch) -> None:
    """Multiple turns build up conversation context correctly."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        call_count = [0]
        def mock_run(prompt, **kw):
            call_count[0] += 1
            return f"Answer {call_count[0]}"
        monkeypatch.setattr(deps["cli"], "run_prompt", mock_run)

        for i in range(5):
            req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text=f"Question {i+1}")
            handle_chat(req, **deps)

        sessions: SessionStore = deps["sessions"]
        history = sessions.get_history("telegram:dm:42")
        assert len(history) == 10  # 5 user + 5 assistant
        assert history[0]["text"] == "Question 1"
        assert history[1]["text"] == "Answer 1"
        assert history[8]["text"] == "Question 5"
        assert history[9]["text"] == "Answer 5"

def test_slash_commands_dont_store_history(monkeypatch) -> None:
    """Slash commands should not add to conversation history."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/status")
        handle_chat(req, **deps)

        sessions: SessionStore = deps["sessions"]
        history = sessions.get_history("telegram:dm:42")
        assert len(history) == 0

# ── SessionStore unit tests ───────────────────────────────────

def test_session_store_build_context_no_history() -> None:
    """build_context_prompt with no history returns message as-is."""
    store = SessionStore()
    result = store.build_context_prompt("nonexistent", "hello")
    assert result == "hello"

def test_session_store_build_context_with_history() -> None:
    """build_context_prompt prepends history."""
    store = SessionStore()
    store.append_message("key1", "user", "What is 2+2?")
    store.append_message("key1", "assistant", "4")

    result = store.build_context_prompt("key1", "And 3+3?")
    assert "[Conversation history" in result
    assert "User: What is 2+2?" in result
    assert "Assistant: 4" in result
    assert "[Current message]" in result
    assert "And 3+3?" in result

def test_session_store_trims_old_messages() -> None:
    """History should be trimmed to max_turns * 2 messages."""
    store = SessionStore(max_turns=3)
    for i in range(10):
        store.append_message("key1", "user", f"Q{i}")
        store.append_message("key1", "assistant", f"A{i}")

    history = store.get_history("key1")
    assert len(history) == 6  # 3 turns * 2 messages
    assert history[0]["text"] == "Q7"
    assert history[-1]["text"] == "A9"

def test_session_store_clear_history() -> None:
    """clear_history should empty the message list."""
    store = SessionStore()
    store.append_message("key1", "user", "hello")
    store.append_message("key1", "assistant", "hi")
    assert len(store.get_history("key1")) == 2

    store.clear_history("key1")
    assert len(store.get_history("key1")) == 0

def test_session_store_context_cap() -> None:
    """build_context_prompt should drop old messages when context is too long."""
    store = SessionStore(max_context_chars=100)
    # Add messages that exceed 100 chars total
    store.append_message("key1", "user", "A" * 60)
    store.append_message("key1", "assistant", "B" * 60)
    store.append_message("key1", "user", "C" * 30)
    store.append_message("key1", "assistant", "D" * 30)

    result = store.build_context_prompt("key1", "new message")
    # Should have dropped older messages to fit within cap
    assert len(result) < 300  # reasonable bound

# ── Restart command tests ─────────────────────────────────────

def test_restart_denied_for_unauthorized() -> None:
    """/restart should be denied for non-authorized users."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, allow_from=["admin1"])
        req = ChatRequest(channel="telegram", sender_id="stranger", chat_id="100", text="/restart")
        resp = handle_chat(req, **deps)
        assert resp.status == "denied"
        assert "Not authorized" in resp.text

def test_restart_allowed_with_callback() -> None:
    """/restart calls the on_restart callback for authorized users."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, allow_from=["admin1"])
        restart_reasons = []
        deps["on_restart"] = lambda reason: restart_reasons.append(reason)

        req = ChatRequest(channel="telegram", sender_id="admin1", chat_id="100", text="/restart")
        resp = handle_chat(req, **deps)
        assert "Restarting" in resp.text
        # Give the thread a moment to run
        import time
        time.sleep(0.1)
        assert len(restart_reasons) == 1
        assert "User requested" in restart_reasons[0]

def test_restart_with_reason() -> None:
    """/restart with a reason passes it through."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, allow_from=["admin1"])
        restart_reasons = []
        deps["on_restart"] = lambda reason: restart_reasons.append(reason)

        req = ChatRequest(channel="telegram", sender_id="admin1", chat_id="100", text="/restart config changed")
        resp = handle_chat(req, **deps)
        assert "Restarting" in resp.text
        import time
        time.sleep(0.1)
        assert restart_reasons[0] == "config changed"

def test_restart_no_callback() -> None:
    """/restart without a callback configured returns an error."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir, allow_from=["admin1"])
        # on_restart not set
        req = ChatRequest(channel="telegram", sender_id="admin1", chat_id="100", text="/restart")
        resp = handle_chat(req, **deps)
        assert "not available" in resp.text.lower()

def test_help_includes_restart() -> None:
    """/help output should mention /restart."""
    with tempfile.TemporaryDirectory() as tmpdir:
        deps = _make_deps(tmpdir)
        req = ChatRequest(channel="telegram", sender_id="42", chat_id="100", text="/help")
        resp = handle_chat(req, **deps)
        assert "/restart" in resp.text


def test_session_store_persistence(tmp_path) -> None:
    """Session history should persist to disk and reload."""
    store_path = str(tmp_path / "sessions.json")
    store1 = SessionStore(store_path=store_path)
    store1.append_message("key1", "user", "hello")
    store1.append_message("key1", "assistant", "hi there")

    # Reload from disk
    store2 = SessionStore(store_path=store_path)
    history = store2.get_history("key1")
    assert len(history) == 2
    assert history[0]["text"] == "hello"
    assert history[1]["text"] == "hi there"

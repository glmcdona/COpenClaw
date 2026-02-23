"""Tests for task dispatch, lifecycle, and bidirectional ITC protocol."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import json
import tempfile

import pytest

from copenclaw.core.tasks import (
    Task,
    TaskManager,
    TaskMessage,
    TimelineEntry,
    AUTO_NOTIFY_TYPES,
    UP_MSG_TYPES,
    DOWN_MSG_TYPES,
)


@pytest.fixture
def data_dir(tmp_path):
    return str(tmp_path)


@pytest.fixture
def tm(data_dir):
    return TaskManager(data_dir=data_dir)


# ── Task creation ────────────────────────────────────────────

class TestTaskCreation:
    def test_create_task_basic(self, tm):
        task = tm.create_task(name="Build app", prompt="Create a todo app")
        assert task.task_id.startswith("task-")
        assert task.name == "Build app"
        assert task.prompt == "Create a todo app"
        assert task.status == "pending"
        assert task.check_interval == 600
        assert len(task.timeline) == 1
        assert task.timeline[0].event == "created"

    def test_create_task_with_channel(self, tm):
        task = tm.create_task(
            name="Deploy",
            prompt="Deploy to vercel",
            channel="telegram",
            target="12345",
            check_interval=120,
            auto_supervise=False,
        )
        assert task.channel == "telegram"
        assert task.target == "12345"
        assert task.check_interval == 120
        assert task.auto_supervise is False

    def test_create_task_creates_directory(self, tm, data_dir):
        task = tm.create_task(name="Test", prompt="Test prompt")
        assert os.path.isdir(task.working_dir)
        assert task.working_dir.endswith(task.task_id)

    def test_create_task_persists(self, data_dir):
        tm1 = TaskManager(data_dir=data_dir)
        task = tm1.create_task(name="Persist", prompt="Check persistence")
        task_id = task.task_id

        tm2 = TaskManager(data_dir=data_dir)
        loaded = tm2.get(task_id)
        assert loaded is not None
        assert loaded.name == "Persist"
        assert loaded.prompt == "Check persistence"

    def test_unique_task_ids(self, tm):
        t1 = tm.create_task(name="A", prompt="a")
        t2 = tm.create_task(name="B", prompt="b")
        assert t1.task_id != t2.task_id


# ── Task listing / querying ──────────────────────────────────

class TestTaskListing:
    def test_list_all(self, tm):
        tm.create_task(name="A", prompt="a")
        tm.create_task(name="B", prompt="b")
        assert len(tm.list_tasks()) == 2

    def test_list_by_status(self, tm):
        t1 = tm.create_task(name="A", prompt="a")
        t2 = tm.create_task(name="B", prompt="b")
        tm.update_status(t1.task_id, "running")
        assert len(tm.list_tasks(status="running")) == 1
        assert len(tm.list_tasks(status="pending")) == 1

    def test_active_tasks(self, tm):
        t1 = tm.create_task(name="A", prompt="a")
        t2 = tm.create_task(name="B", prompt="b")
        tm.update_status(t1.task_id, "running")
        tm.update_status(t2.task_id, "completed")
        active = tm.active_tasks()
        assert len(active) == 1
        assert active[0].task_id == t1.task_id

    def test_active_tasks_correct(self, tm):
        t1 = tm.create_task(name="A", prompt="a")  # pending
        t2 = tm.create_task(name="B", prompt="b")  # pending
        tm.update_status(t1.task_id, "running")
        tm.update_status(t2.task_id, "completed")
        active = tm.active_tasks()
        assert len(active) == 1
        assert active[0].task_id == t1.task_id

    def test_get_nonexistent(self, tm):
        assert tm.get("task-doesnotexist") is None


# ── Status management ────────────────────────────────────────

class TestStatusManagement:
    def test_update_status(self, tm):
        task = tm.create_task(name="A", prompt="a")
        result = tm.update_status(task.task_id, "running")
        assert result is not None
        assert result.status == "running"

    def test_update_status_invalid(self, tm):
        task = tm.create_task(name="A", prompt="a")
        with pytest.raises(ValueError, match="Invalid status"):
            tm.update_status(task.task_id, "bogus")

    def test_update_status_nonexistent(self, tm):
        assert tm.update_status("task-nope", "running") is None

    def test_completed_sets_completed_at(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.update_status(task.task_id, "completed")
        updated = tm.get(task.task_id)
        assert updated.completed_at is not None

    def test_cancel_task(self, tm):
        task = tm.create_task(name="A", prompt="a")
        result = tm.cancel_task(task.task_id)
        assert result.status == "cancelled"
        assert result.completed_at is not None

    def test_cancel_nonexistent(self, tm):
        assert tm.cancel_task("task-nope") is None

    def test_clear_all(self, tm):
        tm.create_task(name="A", prompt="a")
        tm.create_task(name="B", prompt="b")
        tm.create_task(name="C", prompt="c")
        assert len(tm.list_tasks()) == 3
        count = tm.clear_all()
        assert count == 3
        assert len(tm.list_tasks()) == 0

    def test_clear_all_empty(self, tm):
        count = tm.clear_all()
        assert count == 0


# ── Upward messages (worker/supervisor → orchestrator) ───────

class TestUpwardMessages:
    def test_handle_report_progress(self, tm):
        task = tm.create_task(name="A", prompt="a")
        msg = tm.handle_report(task.task_id, "progress", "Built frontend")
        assert msg is not None
        assert msg.direction == "up"
        assert msg.msg_type == "progress"
        assert msg.content == "Built frontend"
        # Check timeline
        updated = tm.get(task.task_id)
        assert any(e.event == "checkpoint" for e in updated.timeline)

    def test_handle_report_completed(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.update_status(task.task_id, "running")
        msg = tm.handle_report(task.task_id, "completed", "All done!")
        assert msg is not None
        updated = tm.get(task.task_id)
        assert updated.status == "completed"
        assert updated.completed_at is not None

    def test_handle_report_failed(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.update_status(task.task_id, "running")
        msg = tm.handle_report(task.task_id, "failed", "Deploy crashed")
        updated = tm.get(task.task_id)
        assert updated.status == "failed"

    def test_handle_report_needs_input(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.update_status(task.task_id, "running")
        msg = tm.handle_report(task.task_id, "needs_input", "Which template?")
        updated = tm.get(task.task_id)
        assert updated.status == "needs_input"

    def test_handle_report_invalid_type(self, tm):
        task = tm.create_task(name="A", prompt="a")
        with pytest.raises(ValueError, match="Invalid upward"):
            tm.handle_report(task.task_id, "instruction", "bad")

    def test_handle_report_unknown_task(self, tm):
        assert tm.handle_report("task-nope", "progress", "x") is None

    def test_handle_report_with_detail_and_artifact(self, tm):
        task = tm.create_task(name="A", prompt="a")
        msg = tm.handle_report(
            task.task_id, "artifact", "Deployed app",
            detail="Full deployment log here",
            artifact_url="https://example.vercel.app",
        )
        assert msg.detail == "Full deployment log here"
        assert msg.artifact_url == "https://example.vercel.app"

    def test_supervisor_assessment(self, tm):
        task = tm.create_task(name="A", prompt="a")
        msg = tm.handle_report(task.task_id, "assessment", "Worker progressing well", from_tier="supervisor")
        assert msg.from_tier == "supervisor"
        updated = tm.get(task.task_id)
        assert any(e.event == "supervised" for e in updated.timeline)

    def test_outbox_records_all(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.handle_report(task.task_id, "progress", "Step 1")
        tm.handle_report(task.task_id, "progress", "Step 2")
        updated = tm.get(task.task_id)
        assert len(updated.outbox) == 2


# ── Downward messages (orchestrator → worker/supervisor) ─────

class TestDownwardMessages:
    def test_send_instruction(self, tm):
        task = tm.create_task(name="A", prompt="a")
        msg = tm.send_message(task.task_id, "instruction", "Use Next.js")
        assert msg is not None
        assert msg.direction == "down"
        assert msg.msg_type == "instruction"

    def test_send_input(self, tm):
        task = tm.create_task(name="A", prompt="a")
        msg = tm.send_message(task.task_id, "input", "Personal account")
        assert msg is not None
        assert msg.content == "Personal account"

    def test_send_pause(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.update_status(task.task_id, "running")
        tm.send_message(task.task_id, "pause", "Hold on")
        updated = tm.get(task.task_id)
        assert updated.status == "paused"

    def test_send_resume(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.update_status(task.task_id, "running")
        tm.send_message(task.task_id, "pause", "Hold")
        tm.send_message(task.task_id, "resume", "Continue")
        updated = tm.get(task.task_id)
        assert updated.status == "running"

    def test_send_redirect(self, tm):
        task = tm.create_task(name="A", prompt="a")
        msg = tm.send_message(task.task_id, "redirect", "Use Svelte instead")
        assert msg is not None
        updated = tm.get(task.task_id)
        assert any(e.event == "redirected" for e in updated.timeline)

    def test_send_cancel(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.send_message(task.task_id, "cancel", "Nevermind")
        updated = tm.get(task.task_id)
        assert updated.status == "cancelled"

    def test_send_invalid_type(self, tm):
        task = tm.create_task(name="A", prompt="a")
        with pytest.raises(ValueError, match="Invalid downward"):
            tm.send_message(task.task_id, "progress", "wrong direction")

    def test_send_to_unknown_task(self, tm):
        assert tm.send_message("task-nope", "instruction", "x") is None

    def test_inbox_populated(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.send_message(task.task_id, "instruction", "Do X")
        tm.send_message(task.task_id, "instruction", "Do Y")
        updated = tm.get(task.task_id)
        assert len(updated.inbox) == 2

    def test_outbox_includes_downward(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.send_message(task.task_id, "instruction", "Do X")
        updated = tm.get(task.task_id)
        assert len(updated.outbox) == 1
        assert updated.outbox[0].direction == "down"


# ── Inbox management ─────────────────────────────────────────

class TestInbox:
    def test_check_inbox_returns_unread(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.send_message(task.task_id, "instruction", "Do X")
        tm.send_message(task.task_id, "input", "Answer Y")
        messages = tm.check_inbox(task.task_id)
        assert len(messages) == 2
        assert messages[0].msg_type == "instruction"
        assert messages[1].msg_type == "input"

    def test_check_inbox_acknowledges(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.send_message(task.task_id, "instruction", "Do X")
        first_read = tm.check_inbox(task.task_id)
        assert len(first_read) == 1
        second_read = tm.check_inbox(task.task_id)
        assert len(second_read) == 0  # Already acknowledged

    def test_check_inbox_no_acknowledge(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.send_message(task.task_id, "instruction", "Do X")
        first_read = tm.check_inbox(task.task_id, acknowledge=False)
        assert len(first_read) == 1
        second_read = tm.check_inbox(task.task_id, acknowledge=False)
        assert len(second_read) == 1  # Still unread

    def test_check_inbox_empty_for_unknown(self, tm):
        assert tm.check_inbox("task-nope") == []


# ── Log management ───────────────────────────────────────────

class TestLogs:
    def test_append_and_read_log(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.append_log(task.task_id, "Line 1")
        tm.append_log(task.task_id, "Line 2")
        tm.append_log(task.task_id, "Line 3")
        logs = tm.read_log(task.task_id)
        assert "Line 1" in logs
        assert "Line 3" in logs

    def test_read_log_tail(self, tm):
        task = tm.create_task(name="A", prompt="a")
        for i in range(100):
            tm.append_log(task.task_id, f"Line {i}")
        logs = tm.read_log(task.task_id, tail=5)
        assert "Line 99" in logs
        assert "Line 0" not in logs

    def test_read_log_no_file(self, tm):
        task = tm.create_task(name="A", prompt="a")
        assert tm.read_log(task.task_id) == "(no logs)"

    def test_read_log_unknown_task(self, tm):
        assert tm.read_log("task-nope") == "(no logs)"


# ── Notification logic ───────────────────────────────────────

class TestNotification:
    def test_auto_notify_types(self, tm):
        task = tm.create_task(name="A", prompt="a")
        for msg_type in AUTO_NOTIFY_TYPES:
            msg = tm.handle_report(task.task_id, msg_type, f"Test {msg_type}")
            assert tm.should_notify_user(msg) is True

    def test_non_notify_types(self, tm):
        task = tm.create_task(name="A", prompt="a")
        msg = tm.handle_report(task.task_id, "progress", "Just a checkpoint")
        assert tm.should_notify_user(msg) is False

    def test_periodic_progress_cadence(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.update_status(task.task_id, "running")

        first = tm.maybe_record_periodic_progress(task.task_id, "Heartbeat", interval_seconds=600)
        assert first is not None
        second = tm.maybe_record_periodic_progress(task.task_id, "Heartbeat", interval_seconds=600)
        assert second is None

        updated = tm.get(task.task_id)
        updated.last_progress_report_at = datetime.now(timezone.utc) - timedelta(seconds=601)
        tm._save()

        third = tm.maybe_record_periodic_progress(task.task_id, "Heartbeat", interval_seconds=600)
        assert third is not None


# ── Timeline ─────────────────────────────────────────────────

class TestTimeline:
    def test_concise_timeline(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.handle_report(task.task_id, "progress", "Step 1")
        tm.handle_report(task.task_id, "progress", "Step 2")
        updated = tm.get(task.task_id)
        timeline = updated.concise_timeline()
        assert "Step 1" in timeline
        assert "Step 2" in timeline

    def test_concise_timeline_limit(self, tm):
        task = tm.create_task(name="A", prompt="a")
        for i in range(30):
            tm.handle_report(task.task_id, "progress", f"Step {i}")
        updated = tm.get(task.task_id)
        timeline = updated.concise_timeline(limit=5)
        lines = timeline.strip().split("\n")
        assert len(lines) == 5

    def test_empty_timeline(self):
        task = Task(task_id="t1", name="X", prompt="x")
        task.timeline = []
        assert task.concise_timeline() == "(no timeline entries)"


# ── Serialization round-trip ─────────────────────────────────

class TestSerialization:
    def test_task_round_trip(self, tm):
        task = tm.create_task(name="RT", prompt="Round trip test")
        tm.update_status(task.task_id, "running")
        tm.handle_report(task.task_id, "progress", "Mid-flight")
        tm.send_message(task.task_id, "instruction", "Go fast")

        d = task.to_dict()
        restored = Task.from_dict(d)
        assert restored.task_id == task.task_id
        assert restored.status == "running"
        assert len(restored.timeline) >= 2
        assert len(restored.outbox) >= 1
        assert len(restored.inbox) >= 1

    def test_task_message_round_trip(self):
        from copenclaw.core.tasks import _now
        msg = TaskMessage(
            msg_id="msg-test",
            ts=_now(),
            direction="up",
            msg_type="progress",
            from_tier="worker",
            content="Hello",
            detail="Detail here",
            artifact_url="https://x.com",
        )
        d = msg.to_dict()
        restored = TaskMessage.from_dict(d)
        assert restored.msg_id == "msg-test"
        assert restored.content == "Hello"
        assert restored.artifact_url == "https://x.com"

    def test_timeline_entry_round_trip(self):
        from copenclaw.core.tasks import _now
        entry = TimelineEntry(ts=_now(), event="checkpoint", summary="Built it", detail="Long desc")
        d = entry.to_dict()
        restored = TimelineEntry.from_dict(d)
        assert restored.event == "checkpoint"
        assert restored.summary == "Built it"


# ── Session ID tracking ──────────────────────────────────────

# ── Recovery management ──────────────────────────────────────

class TestRecoveryManagement:
    def test_stale_active_tasks(self, tm):
        t1 = tm.create_task(name="A", prompt="a")
        t2 = tm.create_task(name="B", prompt="b")
        tm.update_status(t1.task_id, "running")
        tm.update_status(t2.task_id, "completed")
        stale = tm.stale_active_tasks()
        # t1 is running (stale), t2 is completed (not stale)
        # But we also have t2 as completed which doesn't count
        # t1 was updated to running, so it's stale
        assert len(stale) == 1
        assert stale[0].task_id == t1.task_id

    def test_stale_active_excludes_recovery_pending(self, tm):
        t1 = tm.create_task(name="A", prompt="a")
        tm.update_status(t1.task_id, "running")
        tm.mark_recovery_pending(t1.task_id)
        stale = tm.stale_active_tasks()
        assert len(stale) == 0

    def test_stale_active_includes_all_in_progress_statuses(self, tm):
        t1 = tm.create_task(name="Running", prompt="a")
        t2 = tm.create_task(name="Paused", prompt="b")
        t3 = tm.create_task(name="NeedsInput", prompt="c")
        t4 = tm.create_task(name="Pending", prompt="d")
        t5 = tm.create_task(name="Completed", prompt="e")
        tm.update_status(t1.task_id, "running")
        tm.update_status(t2.task_id, "paused")
        tm.update_status(t3.task_id, "needs_input")
        # t4 is already pending
        tm.update_status(t5.task_id, "completed")
        stale = tm.stale_active_tasks()
        stale_ids = {t.task_id for t in stale}
        assert t1.task_id in stale_ids
        assert t2.task_id in stale_ids
        assert t3.task_id in stale_ids
        assert t4.task_id in stale_ids
        assert t5.task_id not in stale_ids

    def test_mark_recovery_pending(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.update_status(task.task_id, "running")
        result = tm.mark_recovery_pending(task.task_id)
        assert result is not None
        assert result.recovery_pending is True
        # Check timeline entry
        assert any(e.event == "recovery_pending" for e in result.timeline)

    def test_mark_recovery_pending_nonexistent(self, tm):
        assert tm.mark_recovery_pending("task-nope") is None

    def test_recovery_pending_tasks(self, tm):
        t1 = tm.create_task(name="A", prompt="a", channel="telegram", target="123")
        t2 = tm.create_task(name="B", prompt="b", channel="telegram", target="456")
        tm.update_status(t1.task_id, "running")
        tm.update_status(t2.task_id, "running")
        tm.mark_recovery_pending(t1.task_id)
        tm.mark_recovery_pending(t2.task_id)
        # All recovery pending
        all_pending = tm.recovery_pending_tasks()
        assert len(all_pending) == 2
        # Filtered by channel/target
        filtered = tm.recovery_pending_tasks(channel="telegram", target="123")
        assert len(filtered) == 1
        assert filtered[0].task_id == t1.task_id

    def test_resolve_recovery_resume(self, tm):
        task = tm.create_task(name="Resume Me", prompt="a")
        tm.update_status(task.task_id, "running")
        tm.mark_recovery_pending(task.task_id)
        result = tm.resolve_recovery(task.task_id, resume=True)
        assert result is not None
        assert result.recovery_pending is False
        assert result.status == "pending"
        assert result.completed_at is None
        assert any(e.event == "recovery_resumed" for e in result.timeline)

    def test_resolve_recovery_cancel(self, tm):
        task = tm.create_task(name="Cancel Me", prompt="a")
        tm.update_status(task.task_id, "running")
        tm.mark_recovery_pending(task.task_id)
        result = tm.resolve_recovery(task.task_id, resume=False)
        assert result is not None
        assert result.recovery_pending is False
        assert result.status == "cancelled"
        assert result.completed_at is not None
        assert any(e.event == "recovery_cancelled" for e in result.timeline)

    def test_resolve_recovery_nonexistent(self, tm):
        assert tm.resolve_recovery("task-nope", resume=True) is None

    def test_recovery_pending_persists(self, data_dir):
        tm1 = TaskManager(data_dir=data_dir)
        task = tm1.create_task(name="Persist", prompt="a")
        tm1.update_status(task.task_id, "running")
        tm1.mark_recovery_pending(task.task_id)
        # Reload
        tm2 = TaskManager(data_dir=data_dir)
        loaded = tm2.get(task.task_id)
        assert loaded is not None
        assert loaded.recovery_pending is True

    def test_recovery_round_trip_serialization(self, tm):
        task = tm.create_task(name="RT", prompt="a")
        tm.update_status(task.task_id, "running")
        tm.mark_recovery_pending(task.task_id)
        d = task.to_dict()
        restored = Task.from_dict(d)
        assert restored.recovery_pending is True

    def test_recovery_full_flow(self, tm):
        """Simulate: task running → app restart → stale detected → user resumes."""
        task = tm.create_task(name="Build app", prompt="Build it", channel="telegram", target="999")
        tm.update_status(task.task_id, "running")
        # Simulate app restart: detect stale tasks
        stale = tm.stale_active_tasks()
        assert len(stale) == 1
        # Mark as recovery pending
        tm.mark_recovery_pending(task.task_id)
        # Verify it's no longer in stale (already flagged)
        assert len(tm.stale_active_tasks()) == 0
        # Verify it's in recovery pending
        assert len(tm.recovery_pending_tasks()) == 1
        # User approves resume
        tm.resolve_recovery(task.task_id, resume=True)
        # Task is now pending and ready for re-dispatch
        updated = tm.get(task.task_id)
        assert updated.status == "pending"
        assert updated.recovery_pending is False
        assert len(tm.recovery_pending_tasks()) == 0


class TestContinuousImprovement:
    def test_create_continuous_task_initializes_state_and_checkpoints(self, tm):
        task = tm.create_task(
            name="CI Task",
            prompt="Improve quality",
            task_type="continuous_improvement",
            ci_config={"max_iterations": 3},
        )
        assert task.task_type == "continuous_improvement"
        assert task.ci_config["max_iterations"] == 3
        assert task.ci_state["phase"] == "plan"
        assert os.path.exists(os.path.join(task.working_dir, "ci-checkpoints.jsonl"))
        assert os.path.exists(os.path.join(task.working_dir, "ci-latest-checkpoint.json"))

    def test_progress_updates_iteration_and_iteration_log(self, tm):
        task = tm.create_task(
            name="CI Task",
            prompt="Improve quality",
            task_type="continuous_improvement",
            ci_config={"max_iterations": 5},
        )
        tm.update_status(task.task_id, "running")
        msg = tm.handle_report(
            task.task_id,
            "progress",
            "ITERATION_RESULT: first pass",
            from_tier="worker",
            continuous={"score": 0.42, "checkpoint": True},
        )
        updated = tm.get(task.task_id)
        assert msg.msg_type == "progress"
        assert updated.ci_state["iteration"] == 1
        assert updated.ci_state["last_score"] == 0.42
        assert os.path.exists(os.path.join(task.working_dir, "ci-iterations.jsonl"))

    def test_max_iterations_budget_auto_completes(self, tm):
        task = tm.create_task(
            name="CI Budget",
            prompt="Improve",
            task_type="continuous_improvement",
            ci_config={"max_iterations": 1},
        )
        tm.update_status(task.task_id, "running")
        msg = tm.handle_report(
            task.task_id,
            "progress",
            "ITERATION_RESULT: done",
            from_tier="worker",
            continuous={"checkpoint": True},
        )
        updated = tm.get(task.task_id)
        assert msg.msg_type == "completed"
        assert updated.status == "completed"
        assert updated.ci_state["stop_reason"] == "max_iterations_reached"

    def test_stale_active_continuous_without_checkpoint_requires_input(self, tm):
        task = tm.create_task(
            name="CI Resume",
            prompt="Improve",
            task_type="continuous_improvement",
        )
        tm.update_status(task.task_id, "running")
        for fname in ("ci-latest-checkpoint.json", "ci-checkpoints.jsonl"):
            path = os.path.join(task.working_dir, fname)
            if os.path.exists(path):
                os.remove(path)
        stale = tm.stale_active_tasks()
        assert all(t.task_id != task.task_id for t in stale)
        updated = tm.get(task.task_id)
        assert updated.status == "needs_input"
        assert updated.ci_state["stop_reason"] == "checkpoint_missing_or_invalid"

    def test_priority_patch_updates_ci_budgets(self, tm):
        task = tm.create_task(
            name="CI Priority",
            prompt="Improve",
            task_type="continuous_improvement",
        )
        tm.send_message(
            task.task_id,
            "priority",
            json.dumps({"budget_patch": {"max_iterations": 9, "max_wall_clock_seconds": 120}}),
        )
        updated = tm.get(task.task_id)
        assert updated.ci_config["max_iterations"] == 9
        assert updated.ci_config["max_wall_clock_seconds"] == 120

# ── Session ID tracking ──────────────────────────────────────

class TestSessionTracking:
    def test_set_worker_session(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.set_worker_session(task.task_id, "ws-123")
        updated = tm.get(task.task_id)
        assert updated.worker_session_id == "ws-123"

    def test_set_supervisor_session(self, tm):
        task = tm.create_task(name="A", prompt="a")
        tm.set_supervisor_session(task.task_id, "sv-456")
        updated = tm.get(task.task_id)
        assert updated.supervisor_session_id == "sv-456"


# ── Bidirectional flow integration ───────────────────────────

class TestBidirectionalFlow:
    def test_full_task_lifecycle(self, tm):
        """Simulate: create → run → progress → needs_input → user_input → progress → complete."""
        task = tm.create_task(name="Unicorn", prompt="Build unicorn game", channel="telegram", target="999")
        tm.update_status(task.task_id, "running")

        # Worker reports progress
        tm.handle_report(task.task_id, "progress", "Created Next.js project")
        tm.handle_report(task.task_id, "progress", "Built game mechanics")

        # Worker needs input
        tm.handle_report(task.task_id, "needs_input", "Which Vercel team?")
        assert tm.get(task.task_id).status == "needs_input"

        # Orchestrator sends answer down
        tm.send_message(task.task_id, "input", "Personal account")

        # Worker picks up answer
        inbox = tm.check_inbox(task.task_id)
        assert len(inbox) == 1
        assert inbox[0].content == "Personal account"

        # Worker resumes
        tm.update_status(task.task_id, "running")
        tm.handle_report(task.task_id, "progress", "Deploying to Vercel")

        # Supervisor checks in
        tm.handle_report(task.task_id, "assessment", "Worker progressing, 80% done", from_tier="supervisor")

        # Worker completes
        tm.handle_report(task.task_id, "completed", "Deployed to https://unicorn.vercel.app")

        final = tm.get(task.task_id)
        assert final.status == "completed"
        assert final.completed_at is not None
        assert len(final.timeline) >= 8  # created + running + 3 progress + needs_input + input + assessment + completed
        assert len(final.outbox) >= 5

    def test_redirect_flow(self, tm):
        """Simulate: create → run → redirect → worker adapts."""
        task = tm.create_task(name="App", prompt="Build Next.js app")
        tm.update_status(task.task_id, "running")
        tm.handle_report(task.task_id, "progress", "Started Next.js")

        # User redirects
        tm.send_message(task.task_id, "redirect", "Use Svelte instead")

        # Worker checks inbox
        inbox = tm.check_inbox(task.task_id)
        assert len(inbox) == 1
        assert inbox[0].msg_type == "redirect"
        assert inbox[0].content == "Use Svelte instead"

        # Worker adapts
        tm.handle_report(task.task_id, "progress", "Switching to SvelteKit")

        updated = tm.get(task.task_id)
        assert any("redirected" in e.event for e in updated.timeline)

    def test_supervisor_intervention(self, tm):
        """Simulate: supervisor detects stuck worker and sends guidance."""
        task = tm.create_task(name="Deploy", prompt="Deploy app")
        tm.update_status(task.task_id, "running")

        # Supervisor sends guidance via downward message
        tm.send_message(task.task_id, "instruction", "Try using VERCEL_TOKEN", from_tier="supervisor")

        # Supervisor also reports the intervention upward
        tm.handle_report(task.task_id, "intervention", "Sent auth guidance to worker", from_tier="supervisor")

        # Worker picks up guidance
        inbox = tm.check_inbox(task.task_id)
        assert len(inbox) == 1
        assert "VERCEL_TOKEN" in inbox[0].content

        updated = tm.get(task.task_id)
        assert any(e.event == "supervised" for e in updated.timeline)

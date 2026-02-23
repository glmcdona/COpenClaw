import os
from datetime import datetime, timedelta, timezone

from copenclaw.core.gateway import (
    _build_watchdog_progress_update,
    _find_src_dir_for_restart,
    _prepend_pythonpath,
)
from copenclaw.core.tasks import TaskMessage, TimelineEntry


def test_find_src_dir_for_restart_from_workspace(tmp_path) -> None:
    workspace = tmp_path / "repo"
    src_dir = workspace / "src" / "copenclaw"
    src_dir.mkdir(parents=True)

    found = _find_src_dir_for_restart(str(workspace))
    assert found == os.path.abspath(str(workspace / "src"))


def test_find_src_dir_for_restart_when_workspace_is_src(tmp_path) -> None:
    src_root = tmp_path / "src"
    (src_root / "copenclaw").mkdir(parents=True)

    found = _find_src_dir_for_restart(str(src_root))
    assert found == os.path.abspath(str(src_root))


def test_prepend_pythonpath_is_idempotent() -> None:
    env = {"PYTHONPATH": f"first{os.pathsep}second"}
    _prepend_pythonpath("new-path", env)
    _prepend_pythonpath("new-path", env)

    parts = env["PYTHONPATH"].split(os.pathsep)
    assert parts[0] == "new-path"
    assert parts.count("new-path") == 1


def test_watchdog_progress_update_includes_current_completed_and_next() -> None:
    now = datetime.now(timezone.utc)
    task = type("TaskStub", (), {})()
    task.status = "running"
    task.check_interval = 600
    task.completion_deferred = False
    task.updated_at = now - timedelta(minutes=2)
    task.last_worker_activity_at = now - timedelta(seconds=45)
    task.last_progress_report_at = now - timedelta(minutes=10)
    task.outbox = [
        TaskMessage(
            msg_id="msg-1",
            ts=now - timedelta(minutes=1),
            direction="up",
            msg_type="progress",
            from_tier="worker",
            content="Running pytest for supervisor summary updates",
        )
    ]
    task.timeline = [
        TimelineEntry(ts=now - timedelta(minutes=5), event="checkpoint", summary="Implemented structured progress formatter"),
        TimelineEntry(ts=now - timedelta(minutes=3), event="checkpoint", summary="Added regression tests for watchdog summary"),
    ]

    summary, detail = _build_watchdog_progress_update(
        task,
        {"running": True, "pid": 1234, "active_pids": [1234, 1235], "child_pids": [1235]},
        now=now,
    )

    assert "Current: Running pytest for supervisor summary updates" in summary
    assert "Completed: Implemented structured progress formatter; Added regression tests for watchdog summary" in summary
    assert "| Next:" in summary
    assert "Completed since last update" in detail


def test_watchdog_progress_update_reports_blocker_for_exited_worker() -> None:
    now = datetime.now(timezone.utc)
    task = type("TaskStub", (), {})()
    task.status = "running"
    task.check_interval = 600
    task.completion_deferred = False
    task.updated_at = now - timedelta(minutes=20)
    task.last_worker_activity_at = now - timedelta(minutes=20)
    task.last_progress_report_at = now - timedelta(minutes=5)
    task.outbox = []
    task.timeline = []

    summary, _ = _build_watchdog_progress_update(
        task,
        {"running": False, "pid": None, "active_pids": [], "child_pids": []},
        now=now,
    )

    assert "Current: No fresh worker status text yet" in summary
    assert "Completed: No new completed work since last update" in summary
    assert "Blocker: worker process exited; supervisor follow-up required" in summary

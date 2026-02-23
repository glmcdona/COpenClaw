from pathlib import Path

from copenclaw.integrations.copilot_cli import CopilotCli


def _write_workspace_yaml(root: Path, sid: str, summary: str) -> None:
    session_dir = root / ".copilot" / "session-state" / sid
    session_dir.mkdir(parents=True, exist_ok=True)
    (session_dir / "workspace.yaml").write_text(
        f"id: {sid}\n"
        "cwd: C:\\repo\n"
        f"summary: {summary}\n",
        encoding="utf-8",
    )


def test_session_is_task_role_true_for_worker(monkeypatch, tmp_path) -> None:
    _write_workspace_yaml(tmp_path, "sid-worker", "You are worker for task task-123. Do work.")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    cli = CopilotCli()
    assert cli.session_is_task_role("sid-worker") is True


def test_discover_latest_non_task_session_id_skips_task_sessions(monkeypatch, tmp_path) -> None:
    _write_workspace_yaml(tmp_path, "sid-task", "You are supervisor for task task-123.")
    _write_workspace_yaml(tmp_path, "sid-orch", "Status summary for orchestrator session.")
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("USERPROFILE", str(tmp_path))

    cli = CopilotCli()
    assert cli._discover_latest_non_task_session_id() == "sid-orch"

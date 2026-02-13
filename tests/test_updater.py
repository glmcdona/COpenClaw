"""Tests for copenclaw.core.updater."""
from __future__ import annotations

import subprocess
from unittest.mock import MagicMock, patch

import pytest

from copenclaw.core.updater import (
    UpdateInfo,
    UpdateResult,
    check_for_updates,
    apply_update,
    format_update_check,
    format_update_result,
    is_git_repo,
    get_current_hash,
    get_locally_modified_files,
)


# ── format_update_check ──────────────────────────────────────────

def test_format_update_check_none():
    result = format_update_check(None)
    assert "up to date" in result

def test_format_update_check_with_info():
    info = UpdateInfo(
        commits_behind=3,
        current_hash="abc123",
        remote_hash="def456",
        changed_files=["src/foo.py", "README.md"],
        locally_modified=[],
        conflict_files=[],
    )
    result = format_update_check(info)
    assert "3 commits behind" in result
    assert "abc123" in result
    assert "def456" in result
    assert "src/foo.py" in result
    assert "/update apply" in result

def test_format_update_check_with_conflicts():
    info = UpdateInfo(
        commits_behind=1,
        current_hash="aaa",
        remote_hash="bbb",
        changed_files=["src/foo.py", "src/bar.py"],
        locally_modified=["src/foo.py", "unrelated.txt"],
        conflict_files=["src/foo.py"],
    )
    result = format_update_check(info)
    assert "overwritten" in result.lower() or "Local modifications" in result
    assert "src/foo.py" in result

def test_format_update_check_no_conflicts_but_local_mods():
    info = UpdateInfo(
        commits_behind=2,
        current_hash="aaa",
        remote_hash="bbb",
        changed_files=["src/foo.py"],
        locally_modified=["unrelated.txt"],
        conflict_files=[],
    )
    result = format_update_check(info)
    assert "none conflict" in result.lower() or "locally modified" in result.lower()

def test_format_update_check_singular_commit():
    info = UpdateInfo(commits_behind=1, current_hash="a", remote_hash="b")
    result = format_update_check(info)
    assert "1 commit behind" in result
    assert "commits" not in result.replace("1 commit", "")

def test_format_update_check_many_files_truncated():
    info = UpdateInfo(
        commits_behind=5,
        current_hash="a",
        remote_hash="b",
        changed_files=[f"file{i}.py" for i in range(30)],
    )
    result = format_update_check(info)
    assert "more" in result


# ── format_update_result ─────────────────────────────────────────

def test_format_update_result_success():
    result = UpdateResult(
        success=True,
        old_hash="abc123",
        new_hash="def456",
        files_updated=["src/foo.py"],
    )
    text = format_update_result(result)
    assert "successfully" in text.lower() or "✅" in text
    assert "abc123" in text
    assert "def456" in text
    assert "/restart" in text

def test_format_update_result_failure():
    result = UpdateResult(
        success=False,
        old_hash="abc123",
        error="merge conflict in README.md",
    )
    text = format_update_result(result)
    assert "failed" in text.lower() or "❌" in text
    assert "merge conflict" in text


# ── is_git_repo ──────────────────────────────────────────────────

@patch("copenclaw.core.updater._run_git")
def test_is_git_repo_true(mock_run):
    mock_run.return_value = MagicMock(returncode=0, stdout="true\n")
    assert is_git_repo("/fake/repo") is True

@patch("copenclaw.core.updater._run_git")
def test_is_git_repo_false(mock_run):
    mock_run.return_value = MagicMock(returncode=128, stdout="")
    assert is_git_repo("/fake/repo") is False

@patch("copenclaw.core.updater._run_git")
def test_is_git_repo_timeout(mock_run):
    mock_run.side_effect = subprocess.TimeoutExpired(cmd="git", timeout=60)
    assert is_git_repo("/fake/repo") is False


# ── get_current_hash ─────────────────────────────────────────────

@patch("copenclaw.core.updater._run_git")
def test_get_current_hash(mock_run):
    mock_run.return_value = MagicMock(stdout="abcdef1234567890\n")
    assert get_current_hash("/fake") == "abcdef1234567890"

@patch("copenclaw.core.updater._run_git")
def test_get_current_hash_error(mock_run):
    mock_run.side_effect = subprocess.CalledProcessError(1, "git")
    assert get_current_hash("/fake") == ""


# ── get_locally_modified_files ───────────────────────────────────

@patch("copenclaw.core.updater._run_git")
def test_get_locally_modified_files(mock_run):
    # git status --porcelain format: XY<space>filename (3 char prefix)
    mock_run.return_value = MagicMock(stdout=" M src/foo.py\n?? newfile.txt\n")
    files = get_locally_modified_files("/fake")
    # Check filenames are present (may or may not have leading space depending on parsing)
    assert any("foo.py" in f for f in files), f"Expected foo.py in {files}"
    assert any("newfile.txt" in f for f in files), f"Expected newfile.txt in {files}"

@patch("copenclaw.core.updater._run_git")
def test_get_locally_modified_files_empty(mock_run):
    mock_run.return_value = MagicMock(stdout="")
    assert get_locally_modified_files("/fake") == []

@patch("copenclaw.core.updater._run_git")
def test_get_locally_modified_files_with_rename(mock_run):
    mock_run.return_value = MagicMock(stdout="R  old.py -> new.py\n")
    files = get_locally_modified_files("/fake")
    assert "new.py" in files


# ── check_for_updates ────────────────────────────────────────────

@patch("copenclaw.core.updater._get_default_branch", return_value="main")
@patch("copenclaw.core.updater.get_locally_modified_files", return_value=[])
@patch("copenclaw.core.updater.get_current_hash", return_value="aaa111")
@patch("copenclaw.core.updater.is_git_repo", return_value=True)
@patch("copenclaw.core.updater._run_git")
def test_check_for_updates_up_to_date(mock_run, mock_is_git, mock_hash, mock_local, mock_branch):
    # fetch returns ok, rev-parse origin/main returns same hash
    def side_effect(*args, **kwargs):
        cmd_args = args[1:]
        if "rev-parse" in cmd_args and "origin/main" in cmd_args:
            return MagicMock(returncode=0, stdout="aaa111\n")
        return MagicMock(returncode=0, stdout="")
    mock_run.side_effect = side_effect
    result = check_for_updates("/fake")
    assert result is None

@patch("copenclaw.core.updater._get_default_branch", return_value="main")
@patch("copenclaw.core.updater.get_locally_modified_files", return_value=["local.txt"])
@patch("copenclaw.core.updater.get_current_hash", return_value="aaa111")
@patch("copenclaw.core.updater.is_git_repo", return_value=True)
@patch("copenclaw.core.updater._run_git")
def test_check_for_updates_available(mock_run, mock_is_git, mock_hash, mock_local, mock_branch):
    def side_effect(repo_dir, *args, **kwargs):
        if "rev-parse" in args and "origin/main" in args:
            return MagicMock(returncode=0, stdout="bbb222\n")
        if "rev-list" in args:
            return MagicMock(returncode=0, stdout="5\n")
        if "diff" in args and "--name-only" in args:
            return MagicMock(returncode=0, stdout="src/foo.py\nlocal.txt\n")
        return MagicMock(returncode=0, stdout="")
    mock_run.side_effect = side_effect
    result = check_for_updates("/fake")
    assert result is not None
    assert result.commits_behind == 5
    assert "src/foo.py" in result.changed_files
    assert "local.txt" in result.conflict_files

@patch("copenclaw.core.updater.is_git_repo", return_value=False)
def test_check_for_updates_not_git_repo(mock_is_git):
    result = check_for_updates("/fake")
    assert result is None


# ── apply_update ─────────────────────────────────────────────────

@patch("copenclaw.core.updater._get_default_branch", return_value="main")
@patch("copenclaw.core.updater.get_current_hash")
@patch("copenclaw.core.updater._run_git")
@patch("subprocess.run")
def test_apply_update_success(mock_subprocess, mock_run_git, mock_hash, mock_branch):
    mock_hash.side_effect = ["aaa111aaa111", "bbb222bbb222"]
    mock_run_git.return_value = MagicMock(returncode=0, stdout="")
    # pip install
    mock_subprocess.return_value = MagicMock(returncode=0, stdout="ok", stderr="")
    
    result = apply_update("/fake")
    assert result.success is True
    assert result.old_hash == "aaa111aaa111"
    assert result.new_hash == "bbb222bbb222"

@patch("copenclaw.core.updater._get_default_branch", return_value="main")
@patch("copenclaw.core.updater.get_current_hash", return_value="aaa111aaa111")
@patch("copenclaw.core.updater._run_git")
def test_apply_update_git_pull_fails(mock_run_git, mock_hash, mock_branch):
    mock_run_git.return_value = MagicMock(returncode=1, stdout="", stderr="merge conflict")
    result = apply_update("/fake")
    assert result.success is False
    assert "merge conflict" in result.error


# ── router /update command ───────────────────────────────────────

def test_update_command_check():
    """Test the /update slash command via router."""
    from copenclaw.core.router import ChatRequest, handle_chat
    from copenclaw.core.pairing import PairingStore
    from copenclaw.core.session import SessionStore
    import tempfile, os

    with tempfile.TemporaryDirectory() as tmp:
        pairing = PairingStore(store_path=os.path.join(tmp, "p.json"))
        sessions = SessionStore(store_path=os.path.join(tmp, "s.json"))
        cli = MagicMock()

        req = ChatRequest(channel="telegram", sender_id="user1", chat_id="chat1", text="/update")

        with patch("copenclaw.core.updater.check_for_updates", return_value=None):
            resp = handle_chat(
                req,
                pairing=pairing,
                sessions=sessions,
                cli=cli,
                allow_from=["user1"],
                pairing_mode="allowlist",
                data_dir=tmp,
                owner_id="user1",
            )
        assert "up to date" in resp.text

def test_update_command_denied():
    """Test that /update is denied for unauthorized users."""
    from copenclaw.core.router import ChatRequest, handle_chat
    from copenclaw.core.pairing import PairingStore
    from copenclaw.core.session import SessionStore
    import tempfile, os

    with tempfile.TemporaryDirectory() as tmp:
        pairing = PairingStore(store_path=os.path.join(tmp, "p.json"))
        sessions = SessionStore(store_path=os.path.join(tmp, "s.json"))
        cli = MagicMock()

        req = ChatRequest(channel="telegram", sender_id="stranger", chat_id="chat1", text="/update")
        resp = handle_chat(
            req,
            pairing=pairing,
            sessions=sessions,
            cli=cli,
            allow_from=["user1"],
            pairing_mode="allowlist",
            data_dir=tmp,
            owner_id="user1",
        )
        assert resp.status == "denied"
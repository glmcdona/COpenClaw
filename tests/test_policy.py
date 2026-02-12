import os
from unittest.mock import patch

from copenclaw.core.policy import ExecutionPolicy, load_execution_policy, run_command
import pytest

# ── Basic allow modes ────────────────────────────────────────

def test_allow_all() -> None:
    policy = ExecutionPolicy(allow_all=True)
    assert policy.is_allowed("anything") is True

def test_allowlist() -> None:
    policy = ExecutionPolicy(allowed_commands={"whoami", "dir"})
    assert policy.is_allowed("whoami") is True
    assert policy.is_allowed("rm -rf /") is False

def test_empty_policy_denies_all() -> None:
    policy = ExecutionPolicy()
    assert policy.is_allowed("echo hello") is False
    assert policy.is_allowed("ls") is False

def test_empty_command_denied() -> None:
    policy = ExecutionPolicy(allow_all=True)
    assert policy.is_allowed("") is False
    assert policy.is_allowed("   ") is False

# ── Base command matching ────────────────────────────────────

def test_base_command_matching() -> None:
    """Allowing 'git' should permit 'git status', 'git log --oneline', etc."""
    policy = ExecutionPolicy(allowed_commands={"git", "npm", "python"})
    assert policy.is_allowed("git") is True
    assert policy.is_allowed("git status") is True
    assert policy.is_allowed("git log --oneline") is True
    assert policy.is_allowed("npm install react") is True
    assert policy.is_allowed("python -m pytest") is True
    assert policy.is_allowed("pip install flask") is False  # pip not in allowed

def test_base_command_case_insensitive() -> None:
    policy = ExecutionPolicy(allowed_commands={"git"})
    assert policy.is_allowed("GIT status") is True
    assert policy.is_allowed("Git log") is True

def test_base_command_with_env_vars() -> None:
    """Commands prefixed with VAR=value should still match."""
    policy = ExecutionPolicy(allowed_commands={"python"})
    assert policy.is_allowed("PYTHONPATH=/app python script.py") is True

# ── Deny list ────────────────────────────────────────────────

def test_deny_list() -> None:
    policy = ExecutionPolicy(allow_all=True, denied_commands={"shutdown", "reboot"})
    assert policy.is_allowed("echo hello") is True
    assert policy.is_allowed("shutdown -s") is False
    assert policy.is_allowed("reboot") is False

def test_deny_overrides_allow() -> None:
    """Deny list takes priority over allowed_commands."""
    policy = ExecutionPolicy(allowed_commands={"git", "rm"}, denied_commands={"rm"})
    assert policy.is_allowed("git status") is True
    assert policy.is_allowed("rm -rf somedir") is False

def test_default_denied_patterns() -> None:
    """Dangerous patterns are always blocked even in allow_all mode."""
    policy = ExecutionPolicy(allow_all=True)
    # Substring patterns
    assert policy.is_allowed("rm -rf /") is False
    # Base-command patterns
    assert policy.is_allowed("mkfs.ext4 /dev/sda") is False
    assert policy.is_allowed("dd if=/dev/zero of=/dev/sda") is False
    # Safe variants should be allowed
    assert policy.is_allowed("rm -rf ./build") is True  # doesn't contain "rm -rf /"

def test_dd_in_path_not_blocked() -> None:
    """Commands with 'dd' in paths/task IDs should NOT be blocked (only 'dd' as base command)."""
    policy = ExecutionPolicy(allow_all=True)
    # These should be ALLOWED — 'dd' appears in the path, not as the command
    assert policy.is_allowed("cmd /c npx create-next-app D:\\tasks\\task-4dd1221d\\workspace") is True
    assert policy.is_allowed('cmd /c "mkdir D:\\tasks\\task-4dd1221d\\workspace\\src"') is True
    assert policy.is_allowed("mkdir /tmp/add-stuff") is True
    assert policy.is_allowed("echo 'added to directory'") is True
    # These should still be DENIED — 'dd' is the actual base command
    assert policy.is_allowed("dd if=/dev/zero of=/dev/sda") is False
    assert policy.is_allowed("DD if=input of=output") is False

def test_load_denied_from_env() -> None:
    with patch.dict(os.environ, {
        "copenclaw_ALLOW_ALL_COMMANDS": "true",
        "copenclaw_DENIED_COMMANDS": "shutdown,reboot,format",
    }):
        policy = load_execution_policy()
        assert policy.is_allowed("echo hi") is True
        assert policy.is_allowed("shutdown -s") is False
        assert policy.is_allowed("reboot") is False

# ── run_command ──────────────────────────────────────────────

def test_run_command_denied() -> None:
    policy = ExecutionPolicy()
    with pytest.raises(PermissionError):
        run_command("echo hello", policy)

def test_run_command_ok() -> None:
    policy = ExecutionPolicy(allow_all=True)
    output = run_command("echo hello", policy)
    assert "hello" in output

def test_run_command_with_allowlist() -> None:
    policy = ExecutionPolicy(allowed_commands={"echo"})
    output = run_command("echo base_match_works", policy)
    assert "base_match_works" in output

# ── load_execution_policy ────────────────────────────────────

def test_load_execution_policy_from_env() -> None:
    with patch.dict(os.environ, {
        "copenclaw_ALLOW_ALL_COMMANDS": "false",
        "copenclaw_ALLOWED_COMMANDS": "whoami,dir",
    }):
        policy = load_execution_policy()
        assert policy.is_allowed("whoami") is True
        assert policy.is_allowed("rm") is False
        assert policy.allow_all is False

def test_load_execution_policy_allow_all() -> None:
    with patch.dict(os.environ, {"copenclaw_ALLOW_ALL_COMMANDS": "true"}):
        policy = load_execution_policy()
        assert policy.allow_all is True
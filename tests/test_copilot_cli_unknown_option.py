from __future__ import annotations

import io
from unittest.mock import patch

import pytest

from copenclaw.integrations.copilot_cli import CopilotCli, CopilotCliError


class _FakeProcess:
    def __init__(self, lines: list[str], exit_code: int = 0) -> None:
        self.returncode = exit_code
        self.stdout = io.StringIO("\n".join(lines) + ("\n" if lines else ""))

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode

    def terminate(self):
        self.returncode = 1

    def kill(self):
        self.returncode = 1


@patch("copenclaw.integrations.copilot_cli.subprocess.Popen")
@patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
def test_run_prompt_cli_uses_single_prompt_argument(mock_which, mock_popen, tmp_path):
    proc = _FakeProcess(["ok"], exit_code=0)
    mock_popen.return_value = proc

    cli = CopilotCli(workspace_dir=str(tmp_path), execution_backend="cli")
    cli._run_prompt_cli(
        prompt="--no-warnings should stay inside prompt text",
        model=None,
        cwd=str(tmp_path),
        log_prefix="TEST",
        resume_id=None,
        allow_retry=False,
        autopilot=False,
        on_line=None,
    )

    cmd = mock_popen.call_args.args[0]
    assert "-p" not in cmd
    assert any(str(arg).startswith("--prompt=--no-warnings should stay inside prompt text") for arg in cmd)


@patch("copenclaw.integrations.copilot_cli.subprocess.Popen")
@patch("copenclaw.integrations.copilot_cli.shutil.which", return_value="copilot")
def test_run_prompt_cli_aborts_repeated_unknown_option_loop(mock_which, mock_popen, tmp_path):
    proc = _FakeProcess(
        [
            "error: unknown option '--no-warnings'",
            "Try 'copilot --help' for more information.",
            "error: unknown option '--no-warnings'",
            "Try 'copilot --help' for more information.",
            "error: unknown option '--no-warnings'",
            "Try 'copilot --help' for more information.",
        ],
        exit_code=1,
    )
    mock_popen.return_value = proc

    cli = CopilotCli(workspace_dir=str(tmp_path), execution_backend="cli")
    with pytest.raises(CopilotCliError, match="unknown-option"):
        cli._run_prompt_cli(
            prompt="loop test",
            model=None,
            cwd=str(tmp_path),
            log_prefix="TEST",
            resume_id=None,
            allow_retry=False,
            autopilot=False,
            on_line=None,
        )

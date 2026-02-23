from __future__ import annotations

import sys
import time

import pytest

from copenclaw.integrations.copilot_cli import CopilotCli, CopilotCliError


def test_run_prompt_cli_times_out_when_subprocess_is_silent(tmp_path) -> None:
    cli = CopilotCli(timeout=1)
    cli._base_cmd = lambda resume_id=None, autopilot=None: [  # type: ignore[method-assign]
        sys.executable,
        "-c",
        "import time; time.sleep(10)",
    ]

    start = time.monotonic()
    with pytest.raises(CopilotCliError, match="timed out"):
        cli._run_prompt_cli(
            prompt="ignored",
            model=None,
            cwd=str(tmp_path),
            log_prefix="TEST",
            resume_id=None,
            allow_retry=False,
            autopilot=None,
            on_line=None,
        )
    elapsed = time.monotonic() - start
    assert elapsed < 5, "silent subprocess should be killed promptly on timeout"

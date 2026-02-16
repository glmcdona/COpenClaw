"""Regression tests for Windows installer batch parsing behavior."""
from __future__ import annotations

import subprocess
import sys
import tempfile
from pathlib import Path

import pytest


INSTALL_BAT = Path(__file__).resolve().parents[1] / "install.bat"


def _run_cmd_script(script_text: str) -> subprocess.CompletedProcess[str]:
    with tempfile.NamedTemporaryFile("w", suffix=".cmd", delete=False, encoding="utf-8", newline="\r\n") as handle:
        handle.write(script_text)
        script_path = Path(handle.name)
    try:
        return subprocess.run(
            ["cmd.exe", "/c", str(script_path)],
            capture_output=True,
            text=True,
            check=False,
        )
    finally:
        script_path.unlink(missing_ok=True)


def test_install_bat_uses_registry_path_refresh():
    content = INSTALL_BAT.read_text(encoding="utf-8")
    assert "for /f \"tokens=*\" %%p in ('echo %PATH%') do set \"PATH=%%p\"" not in content
    assert ":refresh_path_from_registry" in content
    assert "reg query \"HKLM\\SYSTEM\\CurrentControlSet\\Control\\Session Manager\\Environment\" /v Path" in content


@pytest.mark.skipif(sys.platform != "win32", reason="Windows cmd parsing behavior")
def test_cmd_path_refresh_handles_parentheses_entries():
    failing_script = r"""@echo off
setlocal enabledelayedexpansion
set "PATH=C:\Program Files (x86)\Plantronics\Spokes3G\;C:\Windows\System32"
(for /f "tokens=*" %%p in ('echo %PATH%') do set "PATH=%%p")
"""
    failing = _run_cmd_script(failing_script)
    failing_text = f"{failing.stdout}\n{failing.stderr}"
    assert failing.returncode != 0
    assert "was unexpected at this time." in failing_text

    safe_script = r"""@echo off
setlocal enabledelayedexpansion
set "MACHINE_PATH=C:\Windows\System32"
set "USER_PATH=C:\Program Files (x86)\Plantronics\Spokes3G\"
set "MERGED_PATH="
if defined MACHINE_PATH (
    if defined USER_PATH (
        set "MERGED_PATH=!MACHINE_PATH!;!USER_PATH!"
    ) else (
        set "MERGED_PATH=!MACHINE_PATH!"
    )
)
if defined MERGED_PATH set "PATH=!MERGED_PATH!"
"""
    safe = _run_cmd_script(safe_script)
    assert safe.returncode == 0

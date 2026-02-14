from __future__ import annotations

import json
import logging
import os
import platform
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Optional

from copenclaw.core.logging_config import (
    append_to_file,
    get_activity_log_path,
    get_copilot_boot_failure_log_path,
    get_log_dir,
    get_orchestrator_log_path,
    get_repair_log_path,
)
from copenclaw.core.templates import repair_template
from copenclaw.integrations.copilot_cli import CopilotCli, CopilotCliError

logger = logging.getLogger("copenclaw.repair")

_PENDING_FILE = "repair.json"
_PENDING_TTL_SECONDS = 15 * 60


def _pending_path(data_dir: str) -> str:
    return os.path.join(data_dir, _PENDING_FILE)


def _load_pending(data_dir: str) -> dict:
    path = _pending_path(data_dir)
    if not os.path.isfile(path):
        return {"pending": []}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            return json.load(handle)
    except Exception:  # noqa: BLE001
        return {"pending": []}


def _save_pending(data_dir: str, payload: dict) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = _pending_path(data_dir)
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2)


def _prune_pending(items: list[dict]) -> list[dict]:
    now = time.time()
    pruned: list[dict] = []
    for item in items:
        created_at = float(item.get("created_at", 0))
        if created_at and now - created_at <= _PENDING_TTL_SECONDS:
            pruned.append(item)
    return pruned


def set_pending_repair(data_dir: str, channel: str, chat_id: str, sender_id: str) -> None:
    payload = _load_pending(data_dir)
    pending = _prune_pending(payload.get("pending", []))
    pending = [p for p in pending if not (p.get("channel") == channel and p.get("chat_id") == chat_id)]
    pending.append({
        "channel": channel,
        "chat_id": chat_id,
        "sender_id": sender_id,
        "created_at": time.time(),
    })
    _save_pending(data_dir, {"pending": pending})


def get_pending_repair(data_dir: str, channel: str, chat_id: str) -> Optional[dict]:
    payload = _load_pending(data_dir)
    pending = _prune_pending(payload.get("pending", []))
    for item in pending:
        if item.get("channel") == channel and item.get("chat_id") == chat_id:
            return item
    return None


def clear_pending_repair(data_dir: str, channel: str, chat_id: str) -> None:
    payload = _load_pending(data_dir)
    pending = _prune_pending(payload.get("pending", []))
    pending = [p for p in pending if not (p.get("channel") == channel and p.get("chat_id") == chat_id)]
    _save_pending(data_dir, {"pending": pending})


def _tail_lines(path: str, max_lines: int = 120) -> list[str]:
    if not path or not os.path.isfile(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as handle:
            lines = [line.rstrip() for line in handle.readlines() if line.strip()]
        return lines[-max_lines:]
    except Exception:  # noqa: BLE001
        return []


def _recent_errors(path: str, limit: int = 12) -> list[str]:
    lines = _tail_lines(path, max_lines=400)
    errors = [line for line in lines if " ERROR" in line or "CRITICAL" in line]
    return errors[-limit:]


def _format_block(lines: list[str], empty_label: str = "(none)") -> str:
    if not lines:
        return empty_label
    return "\n".join(lines)


def _command_exists(cmd: str) -> bool:
    return shutil.which(cmd) is not None


def _run_cmd(cmd: list[str], timeout: int = 30) -> tuple[int, str, str]:
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=timeout,
            encoding="utf-8",
            errors="replace",
        )
        return result.returncode, result.stdout.strip(), result.stderr.strip()
    except FileNotFoundError:
        return 127, "", "command not found"
    except subprocess.TimeoutExpired:
        return 124, "", "command timed out"


def _format_diagnostics(lines: list[str]) -> str:
    if not lines:
        return "(no diagnostics available)"
    return "\n".join(f"- {line}" for line in lines)


def _write_repair_instructions(
    repair_dir: str,
    *,
    description: str,
    workspace_root: str,
    repo_root: str,
    log_dir: str,
    log_paths: str,
    diagnostics: str,
    recent_errors: str,
    activity_tail: str,
    orchestrator_tail: str,
    boot_failure_output: str,
) -> str:
    def _escape(text: str) -> str:
        return text.replace("{", "{{").replace("}", "}}")

    instructions = repair_template(
        description=_escape(description),
        workspace_root=_escape(workspace_root),
        repo_root=_escape(repo_root),
        log_dir=_escape(log_dir),
        log_paths=_escape(log_paths),
        diagnostics=_escape(diagnostics),
        recent_errors=_escape(recent_errors),
        activity_tail=_escape(activity_tail),
        orchestrator_tail=_escape(orchestrator_tail),
        boot_failure_output=_escape(boot_failure_output),
    )
    dest_dir = os.path.join(repair_dir, ".github")
    os.makedirs(dest_dir, exist_ok=True)
    dest = os.path.join(dest_dir, "copilot-instructions.md")
    with open(dest, "w", encoding="utf-8") as handle:
        handle.write(instructions)
    return dest


def _diagnostics(
    repair_dir: str,
    add_dirs: list[str],
    timeout: int,
) -> tuple[str, Optional[str], bool]:
    lines: list[str] = []
    has_copilot = _command_exists("copilot")
    has_gh = _command_exists("gh")
    gh_copilot = False
    if has_gh:
        code, out, err = _run_cmd(["gh", "copilot", "--version"], timeout=15)
        gh_copilot = code == 0
        lines.append(f"gh copilot --version: {out or err or 'failed'}")
    else:
        lines.append("gh copilot --version: not available")

    if has_copilot:
        code, out, err = _run_cmd(["copilot", "--version"], timeout=15)
        lines.append(f"copilot --version: {out or err or 'failed'}")
    else:
        lines.append("copilot --version: not available")

    if has_copilot:
        code, out, err = _run_cmd(["copilot", "auth", "status"], timeout=15)
        auth_output = out or err or "no output"
        lines.append(f"copilot auth status: {auth_output[:200]}")
    else:
        lines.append("copilot auth status: not available")

    model_error: Optional[str] = None
    if has_copilot:
        cli = CopilotCli(
            workspace_dir=repair_dir,
            timeout=min(timeout, 90),
            mcp_server_url=None,
            add_dirs=add_dirs,
            yolo=True,
        )
        try:
            output = cli.run_prompt("Diagnostic check: reply with OK.", log_prefix="REPAIR DIAG")
            lines.append(f"copilot prompt test: {output[:120] or 'ok'}")
        except CopilotCliError as exc:
            model_error = str(exc)
            lines.append(f"copilot prompt test: failed ({model_error[:160]})")
    else:
        lines.append("copilot prompt test: skipped (copilot missing)")

    return _format_diagnostics(lines), model_error, has_copilot


def _attempt_cli_repair() -> tuple[bool, str]:
    system = platform.system()
    details: list[str] = []
    if system == "Windows":
        if not _command_exists("winget"):
            return False, "winget not found"
        code, out, err = _run_cmd(
            [
                "winget",
                "install",
                "GitHub.Copilot",
                "--accept-source-agreements",
                "--accept-package-agreements",
            ],
            timeout=300,
        )
        details.append(out or err or "winget install completed")
        return code == 0, "\n".join(details)

    if system == "Darwin":
        if not _command_exists("brew"):
            return False, "brew not found"
        code, out, err = _run_cmd(["brew", "reinstall", "copilot-cli"], timeout=300)
        details.append(out or err or "brew reinstall completed")
        return code == 0, "\n".join(details)

    if _command_exists("brew"):
        code, out, err = _run_cmd(["brew", "reinstall", "copilot-cli"], timeout=300)
        details.append(out or err or "brew reinstall completed")
        return code == 0, "\n".join(details)

    return False, "No supported package manager found for Copilot CLI reinstall"


def resolve_repo_root() -> str:
    env_root = os.getenv("copenclaw_REPO_ROOT")
    if env_root:
        return os.path.abspath(env_root)
    here = os.path.abspath(os.path.dirname(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", ".."))


def run_repair(
    *,
    description: str,
    workspace_root: str,
    repo_root: Optional[str] = None,
    log_dir: Optional[str] = None,
    timeout: int = 3600,
    notify: Optional[Callable[[str], None]] = None,
    attempt_cli_repair: bool = True,
) -> None:
    log_dir = log_dir or get_log_dir()
    repo_root = repo_root or resolve_repo_root()
    repair_dir = os.path.join(workspace_root, ".repair")
    os.makedirs(repair_dir, exist_ok=True)

    add_dirs = [repo_root]
    if workspace_root and workspace_root != repo_root:
        add_dirs.append(workspace_root)

    def _emit(message: str) -> None:
        append_to_file(get_repair_log_path(), message)
        logger.info(message)
        if notify:
            notify(message)

    _emit("Repair: starting diagnostics...")

    log_paths = "\n".join(
        [
            f"- {os.path.join(log_dir, 'copenclaw.log')}",
            f"- {get_orchestrator_log_path()}",
            f"- {get_activity_log_path()}",
            f"- {get_repair_log_path()}",
        ]
    )

    recent_errors = _format_block(_recent_errors(os.path.join(log_dir, "copenclaw.log")))
    activity_tail = _format_block(_tail_lines(get_activity_log_path(), max_lines=120))
    orchestrator_tail = _format_block(_tail_lines(get_orchestrator_log_path(), max_lines=120))
    boot_failure_output = _format_block(_tail_lines(get_copilot_boot_failure_log_path(), max_lines=80))

    _write_repair_instructions(
        repair_dir,
        description="Diagnostics pending...",
        workspace_root=workspace_root,
        repo_root=repo_root,
        log_dir=log_dir,
        log_paths=log_paths,
        diagnostics="(pending)",
        recent_errors=recent_errors,
        activity_tail=activity_tail,
        orchestrator_tail=orchestrator_tail,
        boot_failure_output=boot_failure_output,
    )

    diagnostics, model_error, has_copilot = _diagnostics(repair_dir, add_dirs, timeout)
    if model_error and ("model" in model_error.lower() or "unknown" in model_error.lower()):
        diagnostics += "\n- model warning: CLI reported a model selection error"

    if not has_copilot and attempt_cli_repair:
        _emit("Repair: Copilot CLI missing; attempting reinstall...")
        ok, detail = _attempt_cli_repair()
        _emit(f"Repair: CLI reinstall {'succeeded' if ok else 'failed'}")
        if detail:
            _emit(f"Repair: CLI reinstall details: {detail[:400]}")
        diagnostics, model_error, has_copilot = _diagnostics(repair_dir, add_dirs, timeout)

    _write_repair_instructions(
        repair_dir,
        description=description,
        workspace_root=workspace_root,
        repo_root=repo_root,
        log_dir=log_dir,
        log_paths=log_paths,
        diagnostics=diagnostics,
        recent_errors=recent_errors,
        activity_tail=activity_tail,
        orchestrator_tail=orchestrator_tail,
        boot_failure_output=boot_failure_output,
    )

    _emit("Repair: diagnostics complete. Starting repair run...")

    cli = CopilotCli(
        workspace_dir=repair_dir,
        timeout=timeout,
        mcp_server_url=None,
        add_dirs=add_dirs,
        yolo=True,
    )
    try:
        output = cli.run_prompt(
            "Begin repair. Follow the repair system instructions and report results.",
            log_prefix="REPAIR",
        )
        append_to_file(get_repair_log_path(), output)
        _emit("Repair: run completed. Review repair log for details.")
    except CopilotCliError as exc:
        err_text = str(exc)
        append_to_file(get_repair_log_path(), f"Repair failed: {err_text}")
        _emit(f"Repair: failed to start Copilot CLI ({err_text[:200]}).")
        if attempt_cli_repair:
            ok, detail = _attempt_cli_repair()
            _emit(f"Repair: CLI reinstall {'succeeded' if ok else 'failed'}")
            if detail:
                _emit(f"Repair: CLI reinstall details: {detail[:400]}")
            if ok:
                try:
                    output = cli.run_prompt(
                        "Begin repair. Follow the repair system instructions and report results.",
                        log_prefix="REPAIR",
                    )
                    append_to_file(get_repair_log_path(), output)
                    _emit("Repair: run completed after CLI reinstall.")
                except CopilotCliError as exc2:
                    err_text = str(exc2)
                    append_to_file(get_repair_log_path(), f"Repair retry failed: {err_text}")
                    _emit(f"Repair: retry failed ({err_text[:200]}). See repair.log.")

from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
import glob
import logging
from typing import Optional
import os
import shutil
import subprocess
import sys
import threading
import time

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Request
from pydantic import BaseModel

from copenclaw.core.audit import log_event
from copenclaw.core.backup import create_snapshot
from copenclaw.core.config import Settings
from copenclaw.core.logging_config import setup_logging
from copenclaw.core.templates import orchestrator_template
from copenclaw.core.pairing import PairingStore
from copenclaw.core.policy import load_execution_policy
from copenclaw.core.rate_limit import RateLimiter
from copenclaw.core.router import ChatRequest, handle_chat
from copenclaw.core.scheduler import Scheduler
from copenclaw.core.session import SessionStore
from copenclaw.core.tasks import TaskManager
from copenclaw.core.worker import WorkerPool
from copenclaw.integrations.copilot_cli import CopilotCli, CopilotCliError
from copenclaw.integrations.telegram import TelegramAdapter
from copenclaw.integrations.teams import TeamsAdapter
from copenclaw.integrations.teams_auth import validate_bearer_token
from copenclaw.integrations.whatsapp import WhatsAppAdapter
from copenclaw.integrations.signal import SignalAdapter
from copenclaw.integrations.slack import SlackAdapter
from copenclaw.mcp.protocol import MCPProtocolHandler

logger = logging.getLogger("copenclaw.gateway")

import platform
import re
import socket


def _get_git_branch_info(repo_root: str) -> dict:
    """Get current branch name and .py diff stats vs main.

    Returns dict with keys: branch, py_lines_changed, diff_summary, main_ref.
    Returns empty dict on any failure.
    """
    try:
        branch = subprocess.run(
            ["git", "rev-parse", "--abbrev-ref", "HEAD"],
            cwd=repo_root, capture_output=True, text=True, timeout=10,
        )
        if branch.returncode != 0:
            return {}
        branch_name = branch.stdout.strip()

        # Check if 'main' branch exists (could be 'master' etc.)
        main_ref = None
        for candidate in ("main", "master", "origin/main", "origin/master"):
            check = subprocess.run(
                ["git", "rev-parse", "--verify", candidate],
                cwd=repo_root, capture_output=True, text=True, timeout=10,
            )
            if check.returncode == 0:
                main_ref = candidate
                break

        py_lines = 0
        diff_summary = ""
        if main_ref and branch_name not in ("main", "master"):
            diff = subprocess.run(
                ["git", "diff", main_ref, "--stat", "--", "*.py"],
                cwd=repo_root, capture_output=True, text=True, timeout=10,
            )
            if diff.returncode == 0 and diff.stdout.strip():
                diff_summary = diff.stdout.strip()
                last_line = diff_summary.split("\n")[-1]
                for m in re.finditer(r"(\d+)\s+insertion", last_line):
                    py_lines += int(m.group(1))
                for m in re.finditer(r"(\d+)\s+deletion", last_line):
                    py_lines += int(m.group(1))

        return {
            "branch": branch_name,
            "py_lines_changed": py_lines,
            "diff_summary": diff_summary,
            "main_ref": main_ref or "",
        }
    except Exception:  # noqa: BLE001
        return {}


def _build_boot_message(
    settings: Settings,
    cli: CopilotCli,
    mcp_server_url: str,
    task_manager: TaskManager,
    scheduler: Scheduler,
) -> str:
    """Build an informative boot notification message."""
    lines = ["🦀 **COpenClaw** is online and awaiting commands!"]

    # Brain session
    if cli.session_id:
        lines.append(f"\n🧠 Brain session: `{cli.session_id}`")

    # System info
    hostname = socket.gethostname()
    os_info = f"{platform.system()} {platform.release()}"
    lines.append(f"\n🖥️ Host: **{hostname}** ({os_info})")

    # Working directory
    workspace = settings.workspace_dir or os.getcwd()
    lines.append(f"📂 Workspace: `{os.path.abspath(workspace)}`")

    # List top-level folders in workspace
    try:
        if os.path.isdir(workspace):
            entries = sorted(os.listdir(workspace))
            dirs = [e + "/" for e in entries if os.path.isdir(os.path.join(workspace, e)) and not e.startswith(".")]
            files = [e for e in entries if os.path.isfile(os.path.join(workspace, e)) and not e.startswith(".")]
            if dirs:
                dir_list = ", ".join(dirs[:15])
                if len(dirs) > 15:
                    dir_list += f" … (+{len(dirs) - 15} more)"
                lines.append(f"   📁 Folders: {dir_list}")
            if files:
                file_list = ", ".join(files[:10])
                if len(files) > 10:
                    file_list += f" … (+{len(files) - 10} more)"
                lines.append(f"   📄 Files: {file_list}")
    except Exception:  # noqa: BLE001
        pass

    # MCP server
    lines.append(f"🔌 MCP: `{mcp_server_url}`")

    # Task status summary
    all_tasks = task_manager.list_tasks()
    if all_tasks:
        active = len([t for t in all_tasks if t.status in ("running", "paused", "needs_input", "pending")])
        proposed = len([t for t in all_tasks if t.status == "proposed"])
        completed = len([t for t in all_tasks if t.status == "completed"])
        failed = len([t for t in all_tasks if t.status == "failed"])
        parts = []
        if active:
            parts.append(f"{active} active")
        if proposed:
            parts.append(f"{proposed} proposed")
        if completed:
            parts.append(f"{completed} completed")
        if failed:
            parts.append(f"{failed} failed")
        lines.append(f"📋 Tasks: {', '.join(parts)}")
    else:
        lines.append("📋 Tasks: none")

    # Jobs
    jobs = scheduler.list()
    active_jobs = [j for j in jobs if j.completed_at is None and not j.cancelled]
    if active_jobs:
        lines.append(f"⏰ Jobs: {len(active_jobs)} scheduled")
    else:
        lines.append("⏰ Jobs: none")

    # README.md status
    workspace = settings.workspace_dir or os.getcwd()
    readme_path = os.path.join(workspace, "README.md")
    if os.path.isfile(readme_path):
        try:
            size = os.path.getsize(readme_path)
            lines.append(f"📝 README.md: {size} bytes (project log loaded)")
        except Exception:  # noqa: BLE001
            lines.append("📝 README.md: present")
    else:
        lines.append("📝 README.md: not found")

    # Timeout info
    timeout_min = settings.copilot_cli_timeout // 60
    lines.append(f"⏱️ CLI timeout: {timeout_min}min")

    # Git branch info
    repo_root = _resolve_repo_root()
    if repo_root and os.path.isdir(repo_root):
        git_info = _get_git_branch_info(repo_root)
        if git_info.get("branch"):
            branch_line = f"🌿 Branch: **{git_info['branch']}**"
            py_lines = git_info.get("py_lines_changed", 0)
            main_ref = git_info.get("main_ref", "")
            if main_ref and git_info["branch"] not in ("main", "master") and py_lines > 0:
                branch_line += f" ({py_lines} lines changed in .py files vs {main_ref})"
            lines.append(branch_line)

    return "\n".join(lines)


_README_TEMPLATE = """\
# COpenClaw Workspace

This file is a persistent project log. Workers update it after completing
tasks so the orchestrator and future workers know what has been done.

## Active Projects

(none yet)

## Completed Tasks

| Date | Task | Summary |
|------|------|---------|
"""


def _seed_readme(workspace_dir: str) -> None:
    """Create README.md in the workspace if it doesn't already exist."""
    readme_path = os.path.join(workspace_dir, "README.md")
    if not os.path.exists(readme_path):
        os.makedirs(workspace_dir, exist_ok=True)
        with open(readme_path, "w", encoding="utf-8") as f:
            f.write(_README_TEMPLATE)
        logger.info("Seeded workspace README.md at %s", readme_path)


def _read_readme(workspace_dir: str, max_chars: int = 8000) -> str:
    """Read the workspace README.md, returning up to *max_chars*."""
    readme_path = os.path.join(workspace_dir, "README.md")
    if not os.path.isfile(readme_path):
        return ""
    try:
        with open(readme_path, "r", encoding="utf-8") as f:
            content = f.read(max_chars + 100)
        if len(content) > max_chars:
            content = content[:max_chars] + "\n\n… (truncated)"
        return content
    except Exception:  # noqa: BLE001
        return ""


def _resolve_repo_root() -> str:
    env_root = os.getenv("copenclaw_REPO_ROOT")
    if env_root:
        return os.path.abspath(env_root)
    here = os.path.abspath(os.path.dirname(__file__))
    return os.path.normpath(os.path.join(here, "..", "..", ".."))


def _ensure_code_link(workspace_dir: str) -> None:
    """Create or update a OwnCode link in the workspace pointing to the app code."""
    link_path = os.path.join(workspace_dir, "OwnCode")
    repo_root = _resolve_repo_root()
    if not repo_root or not os.path.isdir(repo_root):
        logger.warning("Code link skipped; repo root not found at %s", repo_root)
        return

    # If the link already exists, check whether it points to the right place
    if os.path.lexists(link_path):
        try:
            if os.path.samefile(link_path, repo_root):
                logger.debug("Code link already up-to-date: %s", link_path)
                return
        except Exception:  # noqa: BLE001
            pass
        # Stale or broken link — remove it so we can recreate
        logger.info("Removing stale code link: %s", link_path)
        try:
            if os.path.isdir(link_path) and not os.path.islink(link_path):
                # Junction on Windows appears as a dir to os.path.islink
                # but we can safely rmdir it (junctions don't delete contents)
                os.rmdir(link_path)
            else:
                os.remove(link_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to remove stale code link %s: %s", link_path, exc)
            return

    os.makedirs(workspace_dir, exist_ok=True)
    if os.name == "nt":
        try:
            os.symlink(repo_root, link_path, target_is_directory=True)
            logger.info("Created code link %s -> %s", link_path, repo_root)
            return
        except OSError:
            pass
        try:
            subprocess.run(
                ["cmd", "/c", "mklink", "/J", link_path, repo_root],
                check=True,
                capture_output=True,
                text=True,
            )
            logger.info("Created code junction %s -> %s", link_path, repo_root)
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to create code junction: %s", exc)
            return
    try:
        os.symlink(repo_root, link_path)
        logger.info("Created code link %s -> %s", link_path, repo_root)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Failed to create code link: %s", exc)


def _deploy_instructions(workspace_dir: str) -> None:
    """Write the orchestrator system prompt into the workspace.

    Copilot CLI reads ``.github/copilot-instructions.md`` relative to its
    cwd (the workspace directory).  The source-of-truth template lives in
    ``copenclaw/templates/orchestrator.md`` and is loaded via
    :func:`copenclaw.core.templates.orchestrator_template`.
    """
    try:
        content = orchestrator_template()
    except FileNotFoundError:
        logger.warning("Orchestrator template not found — brain will have no system prompt!")
        return

    dest_dir = os.path.join(workspace_dir, ".github")
    dest = os.path.join(dest_dir, "copilot-instructions.md")

    os.makedirs(dest_dir, exist_ok=True)
    # Always overwrite so the latest template is deployed on each boot
    with open(dest, "w", encoding="utf-8") as f:
        f.write(content)
    logger.info("Deployed orchestrator instructions to %s", dest)


def _build_stale_tasks_message(stale_tasks: list) -> str:
    """Build a notification message listing stale in-progress tasks."""
    status_emoji = {
        "running": "🔄",
        "paused": "⏸️",
        "needs_input": "❓",
        "pending": "⏳",
    }
    lines = ["⚠️ **Stale tasks detected**\n"]
    lines.append("These tasks were in-progress when COpenClaw last shut down:\n")
    for t in stale_tasks:
        emoji = status_emoji.get(t.status, "•")
        lines.append(f"{emoji} **{t.name}** (`{t.task_id}`) — was {t.status}")
    lines.append("\nReply **yes** to resume all, **no** to cancel all, or use `/cancel <id>` to cancel individually.")
    return "\n".join(lines)


def _notify_stale_tasks(settings: Settings, task_manager: TaskManager) -> None:
    """Detect stale in-progress tasks and notify the user.

    Marks each stale task as ``recovery_pending`` and sends a message
    to all configured channels asking the user to resume or cancel.
    """
    stale = task_manager.stale_active_tasks()
    if not stale:
        return

    logger.info("Found %d stale in-progress task(s) from previous run", len(stale))

    # Mark all stale tasks as recovery_pending
    for t in stale:
        task_manager.mark_recovery_pending(t.task_id)

    msg = _build_stale_tasks_message(stale)

    # Notify via Telegram
    owner_chat_id = settings.telegram_owner_chat_id
    if settings.telegram_bot_token and owner_chat_id:
        try:
            from copenclaw.integrations.telegram import TelegramAdapter
            tg = TelegramAdapter(settings.telegram_bot_token)
            tg.send_message(chat_id=int(owner_chat_id), text=msg)
            logger.info("Stale task notification sent to Telegram chat %s", owner_chat_id)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send stale task notification via Telegram: %s", exc)

    # Also notify via each stale task's own channel/target if different from owner
    notified_targets: set[tuple[str, str]] = set()
    if owner_chat_id:
        notified_targets.add(("telegram", owner_chat_id))
    for t in stale:
        if not t.channel or not t.target:
            continue
        key = (t.channel, t.target)
        if key in notified_targets:
            continue
        notified_targets.add(key)
        try:
            if t.channel == "telegram" and settings.telegram_bot_token:
                from copenclaw.integrations.telegram import TelegramAdapter
                tg = TelegramAdapter(settings.telegram_bot_token)
                tg.send_message(chat_id=int(t.target), text=msg)
            elif t.channel in ("teams", "msteams") and settings.msteams_app_id and t.service_url:
                from copenclaw.integrations.teams import TeamsAdapter
                teams = TeamsAdapter(
                    app_id=settings.msteams_app_id,
                    app_password=settings.msteams_app_password,
                    tenant_id=settings.msteams_tenant_id,
                )
                teams.send_message(
                    service_url=t.service_url,
                    conversation_id=t.target,
                    text=msg,
                )
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send stale task notification to %s:%s: %s", t.channel, t.target, exc)


def _clear_data_dir(data_dir: str) -> None:
    """Remove variable data from .data/ so each run starts fresh.

    Clears: tasks.json, tasks/, sessions.json, jobs.json, job-runs.jsonl,
    audit.jsonl, orchestrator.log, copilot-mcp-config.json.
    Preserves: pairing.json (user identity).
    """
    logger.info("Clearing .data/ for fresh run...")
    # Files to delete
    for fname in [
        "tasks.json",
        "sessions.json",
        "jobs.json",
        "job-runs.jsonl",
        "audit.jsonl",
        "orchestrator.log",
        "copilot-mcp-config.json",
    ]:
        fpath = os.path.join(data_dir, fname)
        if os.path.exists(fpath):
            os.remove(fpath)
            logger.debug("  removed %s", fname)

    # Directories to delete
    tasks_dir = os.path.join(data_dir, ".tasks")
    if os.path.isdir(tasks_dir):
        shutil.rmtree(tasks_dir, ignore_errors=True)
        logger.debug("  removed tasks/")

    logger.info("Data directory cleared.")

def create_app() -> FastAPI:
    # Ensure .env is loaded before anything reads os.getenv
    load_dotenv(override=False)

    settings = Settings.from_env()

    # Initialize centralized logging (idempotent — safe to call again if
    # cli.py already called it; handlers are cleared and re-created)
    setup_logging(log_dir=settings.log_dir, log_level=settings.log_level)

    os.makedirs(settings.data_dir, exist_ok=True)
    os.makedirs(settings.log_dir, exist_ok=True)
    if settings.workspace_dir:
        os.makedirs(settings.workspace_dir, exist_ok=True)
    if os.getenv("copenclaw_CLEAR_DATA", "").lower() in {"1", "true", "yes"} or os.getenv("PYTEST_CURRENT_TEST"):
        _clear_data_dir(settings.data_dir)
    scheduler = Scheduler(
        store_path=f"{settings.data_dir}/jobs.json",
        run_log_path=f"{settings.data_dir}/job-runs.jsonl",
    )
    sessions = SessionStore(store_path=f"{settings.data_dir}/sessions.json")
    pairing = PairingStore(store_path=f"{settings.data_dir}/pairing.json")
    rate_limiter = RateLimiter(
        max_calls=settings.webhook_rate_limit_calls,
        window_seconds=settings.webhook_rate_limit_seconds,
    )

    # Build the MCP server URL that Copilot CLI will call back to
    mcp_server_url = f"http://{settings.host}:{settings.port}/mcp"

    cli = CopilotCli(
        workspace_dir=settings.workspace_dir,
        timeout=settings.copilot_cli_timeout,
        mcp_server_url=mcp_server_url,
        mcp_token=settings.mcp_token or None,
    )

    # Task dispatch system
    task_manager = TaskManager(data_dir=settings.data_dir, workspace_dir=settings.workspace_dir)
    worker_pool = WorkerPool(
        mcp_server_url=mcp_server_url,
        mcp_token=settings.mcp_token or None,
        supervisor_timeout=settings.copilot_cli_timeout,
        worker_timeout=settings.copilot_cli_timeout,
        root_workspace_dir=settings.workspace_dir,
    )

    stop_event = threading.Event()

    # ---- shared helpers ----

    def _telegram_adapter() -> TelegramAdapter:
        return TelegramAdapter(settings.telegram_bot_token)  # type: ignore[arg-type]

    def _teams_adapter() -> TeamsAdapter:
        return TeamsAdapter(
            app_id=settings.msteams_app_id,  # type: ignore[arg-type]
            app_password=settings.msteams_app_password,  # type: ignore[arg-type]
            tenant_id=settings.msteams_tenant_id,  # type: ignore[arg-type]
        )

    def _format_telegram_attachments(attachments: list[dict[str, str]]) -> str:
        lines: list[str] = []
        for attachment in attachments:
            label = attachment.get("kind", "attachment")
            line = f"[{label}] {attachment.get('path', '')}"
            mime_type = attachment.get("mime_type")
            if mime_type:
                line += f" ({mime_type})"
            lines.append(line)
        return "\n".join(lines)

    def _extract_telegram_attachments(message: dict) -> list[dict[str, str]]:
        attachments: list[dict[str, str]] = []
        upload_dir = os.path.join(settings.data_dir, "telegram_uploads")

        photos = message.get("photo")
        if isinstance(photos, list) and photos:
            best = max(photos, key=lambda item: item.get("file_size", 0))
            file_id = best.get("file_id")
            if file_id:
                filename_hint = f"photo-{message.get('message_id', 'unknown')}.jpg"
                path = _telegram_adapter().download_file(file_id, upload_dir, filename_hint=filename_hint)
                if path:
                    attachments.append({"kind": "photo", "path": path, "mime_type": "image/jpeg"})

        document = message.get("document")
        if isinstance(document, dict):
            mime_type = document.get("mime_type", "")
            if mime_type.startswith("image/"):
                file_id = document.get("file_id")
                if file_id:
                    filename_hint = document.get("file_name")
                    path = _telegram_adapter().download_file(file_id, upload_dir, filename_hint=filename_hint)
                    if path:
                        attachments.append({"kind": "image", "path": path, "mime_type": mime_type})

        return attachments

    # ---- scheduler loop ----

    def _deliver_job(job) -> tuple[str, bool]:  # noqa: ANN001
        """Attempt to deliver a single job."""
        payload_type = job.payload.get("type")
        if payload_type == "supervisor_check":
            task_id = job.payload.get("task_id")
            repeat_seconds = int(job.payload.get("repeat_seconds", 0) or 0)
            task = task_manager.get(task_id) if task_manager else None
            if not task or task.status in ("completed", "failed", "cancelled"):
                scheduler.cancel(job.job_id)
                return "cancelled", False
            if not task.auto_supervise:
                scheduler.cancel(job.job_id)
                return "cancelled", False
            requested = worker_pool.request_supervisor_check(task_id) if worker_pool else False
            status = "requested" if requested else "missing_supervisor"
            if repeat_seconds > 0:
                scheduler.reschedule(job.job_id, datetime.utcnow() + timedelta(seconds=repeat_seconds))
                return status, True
            return status, False

        prompt = job.payload.get("prompt")
        channel = job.payload.get("channel")
        target = job.payload.get("target")

        if not prompt or not target:
            return "skipped", False

        try:
            output = cli.run_prompt(prompt)
        except CopilotCliError as exc:
            output = f"Error: {exc}"

        if len(output) > 8000 and channel != "telegram":
            output = output[:7950] + "\n\n… (truncated)"

        if channel == "telegram" and settings.telegram_bot_token:
            _telegram_adapter().send_message(chat_id=int(target), text=output)
            log_event(settings.data_dir, "job.deliver", {"job_id": job.job_id, "channel": "telegram"})
            return "delivered", False

        if channel == "teams" and settings.msteams_app_id:
            service_url = job.payload.get("service_url")
            if not service_url:
                return "skipped", False
            _teams_adapter().send_message(
                service_url=service_url,
                conversation_id=target,
                text=output,
            )
            log_event(settings.data_dir, "job.deliver", {"job_id": job.job_id, "channel": "teams"})
            return "delivered", False

        return "skipped", False

    def _scheduler_loop() -> None:
        while not stop_event.is_set():
            try:
                due = scheduler.due()
                for job in due:
                    try:
                        status, rescheduled = _deliver_job(job)
                    except Exception as exc:  # noqa: BLE001
                        logger.error("Job %s delivery failed: %s", job.job_id, exc)
                        log_event(settings.data_dir, "job.error", {"job_id": job.job_id, "error": str(exc)})
                        status = f"error:{exc}"
                        rescheduled = False
                    scheduler.log_run(job.job_id, status)
                    if not rescheduled:
                        scheduler.mark_completed(job.job_id)
            except Exception as exc:  # noqa: BLE001
                logger.error("Scheduler loop error: %s", exc)
            time.sleep(1.0)

    # ---- brain bootstrap ----

    def _bootstrap_brain() -> None:
        """Create the Copilot CLI brain session and send boot notification."""
        workspace = settings.workspace_dir or os.getcwd()

        _ensure_code_link(workspace)

        # Create a backup snapshot of the app source code
        repo_root = _resolve_repo_root()
        if repo_root and os.path.isdir(repo_root):
            backup_dir = os.path.join(workspace, ".backups")
            try:
                snap = create_snapshot(
                    source_dir=repo_root,
                    backup_root=backup_dir,
                    max_snapshots=settings.backup_max_snapshots,
                )
                if snap:
                    logger.info("Backup snapshot created: %s", snap)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Backup snapshot failed: %s", exc)

        # Deploy system prompt to workspace so Copilot CLI can find it
        _deploy_instructions(workspace)

        # Seed and read workspace README.md
        _seed_readme(workspace)
        readme_context = _read_readme(workspace)

        try:
            logger.info("Bootstrapping Copilot CLI brain session...")
            response = cli.create_session(context=readme_context)

            # Discover the session ID that Copilot CLI just created and
            # store it so subsequent user messages resume this session
            # (preserving the boot context including README.md).
            boot_sid = cli._discover_latest_session_id()
            if boot_sid:
                cli._resume_session_id = boot_sid
                cli._session_id = boot_sid
                logger.info("Brain session ready. Captured boot session ID: %s", boot_sid)
            else:
                logger.info("Brain session ready. Session ID: (not discovered)")

            log_event(settings.data_dir, "brain.boot", {
                "session_id": cli.session_id,
                "response_preview": response[:200] if response else "",
            })
        except CopilotCliError as exc:
            logger.warning("Brain session creation failed (will use per-call mode): %s", exc)
            log_event(settings.data_dir, "brain.boot.failed", {"error": str(exc)})

        # Check for updates
        update_notice = ""
        try:
            from copenclaw.core.updater import check_for_updates, format_update_check
            repo_root_for_update = _resolve_repo_root()
            update_info = check_for_updates(repo_root_for_update)
            if update_info:
                update_notice = "\n\n" + format_update_check(update_info)
                logger.info("Update available: %d commits behind", update_info.commits_behind)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Update check during boot failed: %s", exc)

        # Send boot notification via Telegram
        owner_chat_id = settings.telegram_owner_chat_id
        if settings.telegram_bot_token and owner_chat_id:
            try:
                msg = _build_boot_message(settings, cli, mcp_server_url, task_manager, scheduler)
                if update_notice:
                    msg += update_notice
                _telegram_adapter().send_message(chat_id=int(owner_chat_id), text=msg)
                logger.info("Boot notification sent to Telegram chat %s", owner_chat_id)

                # Send PR encouragement if .py diff vs main is > 5 lines
                try:
                    rr = _resolve_repo_root()
                    if rr and os.path.isdir(rr):
                        git_info = _get_git_branch_info(rr)
                        py_lines = git_info.get("py_lines_changed", 0)
                        branch = git_info.get("branch", "")
                        if py_lines > 5 and branch not in ("main", "master", ""):
                            pr_msg = (
                                f"💡 You have {py_lines} lines of Python changes vs main "
                                f"on branch '{branch}'.\n"
                                "Ask me to create a PR with your improvements to the COpenClaw project!"
                            )
                            _telegram_adapter().send_message(chat_id=int(owner_chat_id), text=pr_msg)
                            logger.info("PR encouragement sent (%d .py lines changed)", py_lines)
                except Exception as exc2:  # noqa: BLE001
                    logger.debug("PR encouragement check failed: %s", exc2)

            except Exception as exc:  # noqa: BLE001
                logger.error("Failed to send boot notification: %s", exc)

        # Detect stale in-progress tasks from previous run and notify the user
        _notify_stale_tasks(settings, task_manager)

    # ---- Telegram polling handler ----

    tg_adapter: TelegramAdapter | None = None

    # ---- Telegram message dedup ----
    _tg_seen_msgs: set[int] = set()
    _tg_seen_lock = threading.Lock()
    _TG_SEEN_MAX = 500  # cap the set to avoid unbounded growth

    # ---- Stale message guard ----
    # Record boot time (unix epoch) so we can skip messages that were
    # sent before this process started (e.g. leftovers from a previous
    # crashed session).  We subtract a small grace window (10s) so
    # messages sent just before boot aren't accidentally dropped.
    _boot_epoch = int(time.time()) - 10

    # ---- task approval callback ----

    def _on_task_approved(task_id: str) -> dict:
        """Called when the user approves a proposed task via chat."""
        return mcp_handler._tool_tasks_approve({"task_id": task_id})

    def _on_task_cancelled(task_id: str) -> None:
        """Called when the user cancels a task via /cancel slash command."""
        if worker_pool:
            worker_pool.stop_task(task_id)

    def _on_task_retry_approved(task_id: str) -> dict:
        """Called when the user approves a retry for a failed task."""
        return mcp_handler.retry_task(task_id)

    def _on_task_retry_rejected(task_id: str) -> None:
        """Called when the user declines a retry for a failed task."""
        mcp_handler.decline_retry(task_id)

    def _handle_telegram_update(update: dict) -> None:
        """Process a single Telegram update from polling (same logic as webhook)."""
        if not isinstance(update, dict):
            return
        message = update.get("message") or update.get("edited_message")
        if not message:
            return

        # Dedup: skip messages we've already processed (guards against
        # long-poll re-delivery when processing takes longer than the
        # poll timeout).
        msg_id = message.get("message_id")
        if msg_id is not None:
            with _tg_seen_lock:
                if msg_id in _tg_seen_msgs:
                    logger.debug("Telegram dedup: skipping already-processed message_id=%s", msg_id)
                    return
                _tg_seen_msgs.add(msg_id)
                # Prune oldest entries if we exceed cap
                if len(_tg_seen_msgs) > _TG_SEEN_MAX:
                    # Remove roughly half the oldest entries
                    to_remove = sorted(_tg_seen_msgs)[: _TG_SEEN_MAX // 2]
                    _tg_seen_msgs.difference_update(to_remove)

        # Guard: skip messages sent before this process booted.
        # Telegram's message.date is a unix epoch (UTC).
        msg_date = message.get("date", 0)
        if msg_date and msg_date < _boot_epoch:
            logger.info(
                "Telegram stale-guard: dropping message_id=%s (date=%s < boot=%s, text=%s)",
                msg_id, msg_date, _boot_epoch, (message.get("text") or "")[:60],
            )
            return

        sender_id = message.get("from", {}).get("id")
        sender_id_str = str(sender_id)

        chat_id = message.get("chat", {}).get("id")
        text = message.get("text") or message.get("caption") or ""
        attachments = _extract_telegram_attachments(message)
        if chat_id is None or (not text and not attachments):
            return
        if attachments:
            attachment_note = _format_telegram_attachments(attachments)
            text = f"{text}\n\n{attachment_note}".strip()
        if len(text) > 4000:
            _telegram_adapter().send_message(chat_id=chat_id, text="Message too long")
            return

        logger.info("Telegram poll: [%s] %s", sender_id_str, text[:80])

        # Show "typing..." while the brain is thinking
        tg = _telegram_adapter()
        typing_stop = tg.start_typing_loop(chat_id)

        chat_req = ChatRequest(
            channel="telegram",
            sender_id=sender_id_str,
            chat_id=str(chat_id),
            text=text,
        )
        try:
            resp = handle_chat(
                chat_req,
                pairing=pairing,
                sessions=sessions,
                cli=cli,
                allow_from=settings.telegram_allow_from,
                data_dir=settings.data_dir,
                owner_id=settings.telegram_owner_chat_id,
                task_manager=task_manager,
                scheduler=scheduler,
                worker_pool=worker_pool,
                on_task_approved=_on_task_approved,
                on_task_cancelled=_on_task_cancelled,
                on_task_retry_approved=_on_task_retry_approved,
                on_task_retry_rejected=_on_task_retry_rejected,
                on_restart=_restart_app,
            )
        finally:
            typing_stop.set()
        tg.send_message(chat_id=chat_id, text=resp.text)

    # ---- lifespan ----

    @asynccontextmanager
    async def lifespan(application: FastAPI):
        nonlocal tg_adapter, signal_adapter

        # Start scheduler thread
        sched_thread = threading.Thread(target=_scheduler_loop, daemon=True)
        sched_thread.start()

        # Start watchdog thread for stuck tasks
        watchdog_thread = threading.Thread(target=_watchdog_loop, daemon=True, name="task-watchdog")
        watchdog_thread.start()

        # Start Telegram polling if configured
        if settings.telegram_bot_token:
            tg_adapter = _telegram_adapter()
            tg_adapter.start_polling(on_update=_handle_telegram_update)
            logger.info("Telegram polling started")

        # Start Signal polling if configured
        if settings.signal_api_url and settings.signal_phone_number:
            signal_adapter = _signal_adapter()
            signal_adapter.start_polling(on_update=_handle_signal_message)
            logger.info("Signal polling started")

        # Bootstrap brain in a separate thread so it doesn't block server startup
        boot_thread = threading.Thread(target=_bootstrap_brain, daemon=True)
        boot_thread.start()

        yield

        # Shutdown
        stop_event.set()
        worker_pool.stop_all()
        if tg_adapter:
            tg_adapter.stop_polling()
        if signal_adapter:
            signal_adapter.stop_polling()

    app = FastAPI(title="COpenClaw", version="0.2.0", lifespan=lifespan)

    # ---- models ----

    class AgentRequest(BaseModel):
        prompt: str
        model: Optional[str] = None

    class AgentResponse(BaseModel):
        response: str

    # ---- core routes ----

    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/control/status")
    def control_status() -> dict:
        return {
            "sessions": len(sessions.list_keys()),
            "jobs": len(scheduler.list()),
            "tasks_active": len(task_manager.active_tasks()),
            "tasks_total": len(task_manager.list_tasks()),
            "workers_active": worker_pool.active_count(),
            "brain_session_id": cli.session_id,
            "brain_initialized": cli._initialized,
        }

    @app.get("/control/health")
    def control_health() -> dict[str, dict[str, str]]:
        cli_ok = True
        cli_version = ""
        try:
            cli_version = cli.version()
        except Exception:  # noqa: BLE001
            cli_ok = False
        return {
            "copilot_cli": {"status": "ok" if cli_ok else "missing", "version": cli_version},
            "brain": {"status": "active" if cli._initialized else "not_started", "session_id": cli.session_id or ""},
            "telegram": {"status": "configured" if settings.telegram_bot_token else "missing"},
            "msteams": {
                "status": "configured"
                if settings.msteams_app_id and settings.msteams_app_password and settings.msteams_tenant_id
                else "missing"
            },
            "tasks": {"status": "active", "pool_size": str(worker_pool.active_count())},
        }

    @app.get("/control/metrics")
    def control_metrics() -> dict[str, int]:
        jobs = scheduler.list()
        all_tasks = task_manager.list_tasks()
        return {
            "total_jobs": len(jobs),
            "pending_jobs": len([j for j in jobs if j.completed_at is None and not j.cancelled]),
            "completed_jobs": len([j for j in jobs if j.completed_at is not None and not j.cancelled]),
            "cancelled_jobs": len([j for j in jobs if j.cancelled]),
            "recurring_jobs": len([j for j in jobs if j.cron_expr]),
            "sessions": len(sessions.list_keys()),
            "tasks_total": len(all_tasks),
            "tasks_active": len([t for t in all_tasks if t.status in ("running", "paused", "needs_input", "pending")]),
            "tasks_completed": len([t for t in all_tasks if t.status == "completed"]),
            "tasks_failed": len([t for t in all_tasks if t.status == "failed"]),
            "workers_active": worker_pool.active_count(),
        }

    @app.post("/agent", response_model=AgentResponse)
    def agent(req: AgentRequest) -> AgentResponse:
        try:
            output = cli.run_prompt(req.prompt, model=req.model)
        except CopilotCliError as exc:
            raise HTTPException(status_code=500, detail=str(exc)) from exc
        log_event(settings.data_dir, "agent.run", {"model": req.model, "prompt_len": len(req.prompt)})
        return AgentResponse(response=output)

    # ---- Telegram webhook ----

    @app.post("/telegram/webhook")
    async def telegram_webhook(
        request: Request,
        x_telegram_bot_api_secret_token: Optional[str] = Header(default=None),
    ) -> dict[str, str]:
        if not rate_limiter.allow("telegram"):
            raise HTTPException(status_code=429, detail="rate limited")
        if request.headers.get("content-length") and int(request.headers["content-length"]) > 200000:
            raise HTTPException(status_code=413, detail="payload too large")
        if not settings.telegram_bot_token:
            raise HTTPException(status_code=400, detail="Telegram not configured")
        if settings.telegram_webhook_secret and x_telegram_bot_api_secret_token != settings.telegram_webhook_secret:
            raise HTTPException(status_code=401, detail="Invalid Telegram secret")

        update = await request.json()
        if not isinstance(update, dict):
            return {"status": "ignored"}
        message = update.get("message") or update.get("edited_message")
        if not message:
            return {"status": "ignored"}
        sender_id = message.get("from", {}).get("id")
        sender_id_str = str(sender_id)

        chat_id = message.get("chat", {}).get("id")
        text = message.get("text") or message.get("caption") or ""
        attachments = _extract_telegram_attachments(message)
        if chat_id is None or (not text and not attachments):
            return {"status": "ignored"}
        if attachments:
            attachment_note = _format_telegram_attachments(attachments)
            text = f"{text}\n\n{attachment_note}".strip()
        if len(text) > 4000:
            _telegram_adapter().send_message(chat_id=chat_id, text="Message too long")
            return {"status": "rejected"}

        # Show "typing..." while the brain is thinking
        tg = _telegram_adapter()
        typing_stop = tg.start_typing_loop(chat_id)

        chat_req = ChatRequest(
            channel="telegram",
            sender_id=sender_id_str,
            chat_id=str(chat_id),
            text=text,
        )
        try:
            resp = handle_chat(
                chat_req,
                pairing=pairing,
                sessions=sessions,
                cli=cli,
                allow_from=settings.telegram_allow_from,
                data_dir=settings.data_dir,
                owner_id=settings.telegram_owner_chat_id,
                task_manager=task_manager,
                scheduler=scheduler,
                worker_pool=worker_pool,
                on_task_approved=_on_task_approved,
                on_task_cancelled=_on_task_cancelled,
                on_task_retry_approved=_on_task_retry_approved,
                on_task_retry_rejected=_on_task_retry_rejected,
                on_restart=_restart_app,
            )
        finally:
            typing_stop.set()
        tg.send_message(chat_id=chat_id, text=resp.text)
        return {"status": resp.status}

    # ---- Teams webhook ----

    @app.post("/teams/api/messages")
    async def teams_webhook(request: Request) -> dict[str, str]:
        if not rate_limiter.allow("teams"):
            raise HTTPException(status_code=429, detail="rate limited")
        if request.headers.get("content-length") and int(request.headers["content-length"]) > 200000:
            raise HTTPException(status_code=413, detail="payload too large")
        if not (settings.msteams_app_id and settings.msteams_app_password and settings.msteams_tenant_id):
            raise HTTPException(status_code=400, detail="Teams not configured")

        activity = await request.json()
        auth_header = request.headers.get("Authorization")
        if settings.msteams_validate_token:
            if not auth_header or not auth_header.lower().startswith("bearer "):
                raise HTTPException(status_code=401, detail="Missing bearer token")
            token = auth_header.split(" ", 1)[1]
            if not validate_bearer_token(token, settings.msteams_app_id):
                raise HTTPException(status_code=401, detail="Invalid bearer token")

        if activity.get("type") != "message":
            return {"status": "ignored"}
        text = activity.get("text")
        service_url = activity.get("serviceUrl")
        if not service_url or not str(service_url).startswith("https://"):
            return {"status": "ignored"}
        conversation = activity.get("conversation") or {}
        conversation_id = conversation.get("id")
        sender = activity.get("from") or {}
        sender_id = sender.get("id")
        recipient = activity.get("recipient") or {}
        if recipient.get("id") and recipient.get("id") != settings.msteams_app_id:
            return {"status": "ignored"}
        sender_id_str = str(sender_id)

        if not text or not service_url or not conversation_id:
            return {"status": "ignored"}

        chat_req = ChatRequest(
            channel="msteams",
            sender_id=sender_id_str,
            chat_id=conversation_id,
            text=text,
            service_url=service_url,
        )
        resp = handle_chat(
            chat_req,
            pairing=pairing,
            sessions=sessions,
            cli=cli,
            allow_from=settings.msteams_allow_from,
            data_dir=settings.data_dir,
            owner_id=None,
            task_manager=task_manager,
            scheduler=scheduler,
            worker_pool=worker_pool,
            on_task_approved=_on_task_approved,
            on_task_cancelled=_on_task_cancelled,
            on_task_retry_approved=_on_task_retry_approved,
            on_task_retry_rejected=_on_task_retry_rejected,
            on_restart=_restart_app,
        )
        _teams_adapter().send_message(
            service_url=service_url,
            conversation_id=conversation_id,
            text=resp.text,
        )
        return {"status": resp.status}

    # ---- WhatsApp webhook ----

    def _whatsapp_adapter() -> WhatsAppAdapter:
        return WhatsAppAdapter(
            phone_number_id=settings.whatsapp_phone_number_id or "",
            access_token=settings.whatsapp_access_token or "",
            verify_token=settings.whatsapp_verify_token or "",
        )

    @app.get("/whatsapp/webhook")
    async def whatsapp_verify(request: Request) -> str:
        """Handle Meta webhook verification challenge."""
        if not settings.whatsapp_phone_number_id:
            raise HTTPException(status_code=400, detail="WhatsApp not configured")
        adapter = _whatsapp_adapter()
        params = dict(request.query_params)
        ok, response_body = adapter.verify_webhook(params)
        if not ok:
            raise HTTPException(status_code=403, detail=response_body)
        from fastapi.responses import PlainTextResponse
        return PlainTextResponse(response_body)

    @app.post("/whatsapp/webhook")
    async def whatsapp_webhook(request: Request) -> dict[str, str]:
        if not rate_limiter.allow("whatsapp"):
            raise HTTPException(status_code=429, detail="rate limited")
        if not (settings.whatsapp_phone_number_id and settings.whatsapp_access_token):
            raise HTTPException(status_code=400, detail="WhatsApp not configured")

        body = await request.json()
        if not isinstance(body, dict):
            return {"status": "ignored"}

        adapter = _whatsapp_adapter()
        messages = adapter.parse_webhook(body)
        if not messages:
            return {"status": "ignored"}

        for msg in messages:
            sender = msg.get("sender", "")
            text = msg.get("text", "")
            message_id = msg.get("message_id", "")

            if not sender or not text:
                continue

            if len(text) > 4000:
                adapter.send_message(to=sender, text="Message too long")
                continue

            logger.info("WhatsApp: [%s] %s", sender, text[:80])

            # Mark as read
            if message_id:
                adapter.mark_read(message_id)

            chat_req = ChatRequest(
                channel="whatsapp",
                sender_id=sender,
                chat_id=sender,
                text=text,
            )
            resp = handle_chat(
                chat_req,
                pairing=pairing,
                sessions=sessions,
                cli=cli,
                allow_from=settings.whatsapp_allow_from,
                data_dir=settings.data_dir,
                owner_id=None,
                task_manager=task_manager,
                scheduler=scheduler,
                worker_pool=worker_pool,
                on_task_approved=_on_task_approved,
                on_task_cancelled=_on_task_cancelled,
                on_task_retry_approved=_on_task_retry_approved,
                on_task_retry_rejected=_on_task_retry_rejected,
                on_restart=_restart_app,
            )
            adapter.send_message(to=sender, text=resp.text)

        return {"status": "ok"}

    # ---- Signal polling handler ----

    signal_adapter: SignalAdapter | None = None

    def _signal_adapter() -> SignalAdapter:
        return SignalAdapter(
            api_url=settings.signal_api_url or "",
            phone_number=settings.signal_phone_number or "",
        )

    def _handle_signal_message(msg: dict) -> None:
        """Process a single Signal message from polling."""
        sender = msg.get("sender", "")
        text = msg.get("text", "")
        if not sender or not text:
            return

        if len(text) > 4000:
            _signal_adapter().send_message(recipient=sender, text="Message too long")
            return

        logger.info("Signal: [%s] %s", sender, text[:80])

        sig = _signal_adapter()
        sig.send_typing(sender)

        chat_req = ChatRequest(
            channel="signal",
            sender_id=sender,
            chat_id=sender,
            text=text,
        )
        resp = handle_chat(
            chat_req,
            pairing=pairing,
            sessions=sessions,
            cli=cli,
            allow_from=settings.signal_allow_from,
            data_dir=settings.data_dir,
            owner_id=None,
            task_manager=task_manager,
            scheduler=scheduler,
            worker_pool=worker_pool,
            on_task_approved=_on_task_approved,
            on_task_cancelled=_on_task_cancelled,
            on_task_retry_approved=_on_task_retry_approved,
            on_task_retry_rejected=_on_task_retry_rejected,
            on_restart=_restart_app,
        )
        sig.send_message(recipient=sender, text=resp.text)

    # ---- Slack Events API webhook ----

    def _slack_adapter() -> SlackAdapter:
        return SlackAdapter(
            bot_token=settings.slack_bot_token or "",
            signing_secret=settings.slack_signing_secret or "",
        )

    @app.post("/slack/events")
    async def slack_events(request: Request) -> dict[str, Any]:
        if not rate_limiter.allow("slack"):
            raise HTTPException(status_code=429, detail="rate limited")
        if not settings.slack_bot_token:
            raise HTTPException(status_code=400, detail="Slack not configured")

        raw_body = await request.body()
        payload = await request.json()

        # Verify request signature if signing secret is configured
        if settings.slack_signing_secret:
            timestamp = request.headers.get("X-Slack-Request-Timestamp", "")
            signature = request.headers.get("X-Slack-Signature", "")
            adapter = _slack_adapter()
            if not adapter.verify_signature(raw_body, timestamp, signature):
                raise HTTPException(status_code=401, detail="Invalid Slack signature")

        parsed = SlackAdapter.parse_event(payload)
        if not parsed:
            return {"status": "ignored"}

        # Handle URL verification challenge
        if parsed.get("type") == "url_verification":
            return {"challenge": parsed["challenge"]}

        if parsed.get("type") != "message":
            return {"status": "ignored"}

        sender = parsed.get("sender", "")
        text = parsed.get("text", "")
        channel_id = parsed.get("channel", "")

        if not sender or not text or not channel_id:
            return {"status": "ignored"}

        if len(text) > 4000:
            _slack_adapter().send_message(channel=channel_id, text="Message too long")
            return {"status": "rejected"}

        logger.info("Slack: [%s in %s] %s", sender, channel_id, text[:80])

        chat_req = ChatRequest(
            channel="slack",
            sender_id=sender,
            chat_id=channel_id,
            text=text,
        )
        resp = handle_chat(
            chat_req,
            pairing=pairing,
            sessions=sessions,
            cli=cli,
            allow_from=settings.slack_allow_from,
            data_dir=settings.data_dir,
            owner_id=None,
            task_manager=task_manager,
            scheduler=scheduler,
            worker_pool=worker_pool,
            on_task_approved=_on_task_approved,
            on_task_cancelled=_on_task_cancelled,
            on_task_retry_approved=_on_task_retry_approved,
            on_task_retry_rejected=_on_task_retry_rejected,
            on_restart=_restart_app,
        )
        _slack_adapter().send_message(channel=channel_id, text=resp.text)
        return {"status": resp.status}

    # ---- MCP JSON-RPC protocol endpoint ----

    msteams_creds = (
        {
            "app_id": settings.msteams_app_id,
            "app_password": settings.msteams_app_password,
            "tenant_id": settings.msteams_tenant_id,
        }
        if settings.msteams_app_id and settings.msteams_app_password and settings.msteams_tenant_id
        else None
    )

    # Cache the execution policy once at startup (after dotenv is loaded)
    execution_policy = load_execution_policy()

    mcp_handler = MCPProtocolHandler(
        scheduler=scheduler,
        data_dir=settings.data_dir,
        telegram_token=settings.telegram_bot_token,
        msteams_creds=msteams_creds,
        task_manager=task_manager,
        worker_pool=worker_pool,
        owner_chat_id=settings.telegram_owner_chat_id,
        execution_policy=execution_policy,
    )

    # Wire completion hook: when a task completes, feed a completion prompt
    # (including any on_complete instruction) to the orchestrator CLI session.
    def _on_complete_hook(prompt: str, channel: str, target: str, service_url: str, source_task_name: str) -> None:
        try:
            logger.info("on_complete hook firing for task '%s'", source_task_name)
            output = cli.run_prompt(prompt)
            # Deliver the orchestrator's response to the user
            if output and channel and target:
                if channel == "telegram" and settings.telegram_bot_token:
                    _telegram_adapter().send_message(chat_id=int(target), text=output)
                elif channel == "teams" and settings.msteams_app_id:
                    if service_url:
                        _teams_adapter().send_message(
                            service_url=service_url,
                            conversation_id=target,
                            text=output,
                        )
            log_event(settings.data_dir, "task.on_complete_delivered", {
                "source_task": source_task_name,
                "output_len": len(output) if output else 0,
            })
        except Exception as exc:  # noqa: BLE001
            logger.error("on_complete hook failed for task '%s': %s", source_task_name, exc)

    mcp_handler.on_complete_callback = _on_complete_hook

    # ---- task watchdog (auto-recovery for stuck workers) ----

    def _watchdog_idle_seconds(task, now: datetime) -> int:  # noqa: ANN001
        last = task.last_worker_activity_at or task.updated_at or task.created_at
        if task.watchdog_last_action_at and task.watchdog_last_action_at > last:
            last = task.watchdog_last_action_at
        if last.tzinfo is None:
            last = last.replace(tzinfo=timezone.utc)
        return max(0, int((now - last).total_seconds()))

    def _record_watchdog_report(task, msg_type: str, summary: str, detail: str = "") -> None:  # noqa: ANN001
        msg = task_manager.handle_report(
            task_id=task.task_id,
            msg_type=msg_type,
            summary=summary,
            detail=detail,
            from_tier="orchestrator",
        )
        if msg and msg_type == "needs_input":
            mcp_handler._notify_user_about_task(task.task_id, msg)
        if settings.data_dir:
            log_event(settings.data_dir, "task.watchdog", {
                "task_id": task.task_id,
                "name": task.name,
                "action": msg_type,
                "summary": summary[:300],
            })

    def _watchdog_loop() -> None:
        interval = max(5, int(settings.task_watchdog_interval))
        warn_after = max(0, int(settings.task_watchdog_idle_warn_seconds))
        restart_after = max(0, int(settings.task_watchdog_idle_restart_seconds))
        grace = max(0, int(settings.task_watchdog_grace_seconds))
        max_restarts = max(0, int(settings.task_watchdog_max_restarts))
        if warn_after <= 0 and restart_after <= 0:
            logger.info("Task watchdog disabled (warn=%s restart=%s)", warn_after, restart_after)
            return
        if restart_after and warn_after and restart_after < warn_after:
            restart_after = warn_after

        while not stop_event.wait(interval):
            try:
                now = datetime.now(timezone.utc)
                for task in task_manager.active_tasks():
                    if task.status != "running":
                        continue
                    if task.completion_deferred:
                        continue

                    worker = worker_pool.get_worker(task.task_id) if worker_pool else None
                    worker_running = worker.is_running if worker else False
                    idle_secs = _watchdog_idle_seconds(task, now)
                    if idle_secs < grace:
                        continue

                    if worker_running:
                        if warn_after and idle_secs >= warn_after and task.watchdog_state == "none":
                            msg = (
                                "Watchdog notice: no MCP activity detected. "
                                "If you are stuck on a blocking command, abort it and report status."
                            )
                            task_manager.send_message(
                                task_id=task.task_id,
                                msg_type="instruction",
                                content=msg,
                                from_tier="orchestrator",
                            )
                            _record_watchdog_report(
                                task,
                                "intervention",
                                f"Watchdog warning sent after {idle_secs}s of inactivity",
                                detail=msg,
                            )
                            task.watchdog_state = "warned"
                            task.watchdog_last_action_at = now
                            task_manager._save()
                            if task.auto_supervise and worker_pool:
                                worker_pool.request_supervisor_check(task.task_id)
                            continue

                        if restart_after and idle_secs >= restart_after:
                            if task.watchdog_restart_count < max_restarts:
                                _record_watchdog_report(
                                    task,
                                    "intervention",
                                    f"Watchdog restarting worker after {idle_secs}s of inactivity",
                                )
                                task.watchdog_state = "restarted"
                                task.watchdog_restart_count += 1
                                task.watchdog_last_action_at = now
                                task_manager._save()

                                if worker_pool:
                                    worker_pool.stop_task(task.task_id)
                                try:
                                    mcp_handler._start_task(task)
                                except RuntimeError as exc:
                                    logger.error("Watchdog restart failed for %s: %s", task.task_id, exc)
                                continue

                            if task.watchdog_state != "needs_input":
                                summary = "Watchdog: worker still inactive after restart attempts"
                                detail = (
                                    f"No MCP activity for {idle_secs}s. "
                                    "Please check logs or send updated instructions."
                                )
                                _record_watchdog_report(task, "needs_input", summary, detail=detail)
                                task.watchdog_state = "needs_input"
                                task.watchdog_last_action_at = now
                                task_manager._save()
                        continue

                    # Worker not running but task still marked running
                    if restart_after and idle_secs >= restart_after and task.watchdog_state != "needs_input":
                        summary = "Watchdog: worker is not running while task is still active"
                        detail = (
                            f"Worker has been inactive for {idle_secs}s and is no longer running. "
                            "Please decide whether to retry or cancel."
                        )
                        _record_watchdog_report(task, "needs_input", summary, detail=detail)
                        task.watchdog_state = "needs_input"
                        task.watchdog_last_action_at = now
                        task_manager._save()
            except Exception as exc:  # noqa: BLE001
                logger.error("Watchdog loop error: %s", exc)

    # ---- restart mechanism ----

    def _restart_app(reason: str = "manual") -> None:
        """Gracefully stop everything, then re-exec the process."""
        logger.warning("APP RESTART initiated: %s", reason)
        log_event(settings.data_dir, "app.restart", {"reason": reason})

        # Give a moment for the HTTP response / chat message to be sent
        time.sleep(2)

        # Graceful shutdown
        stop_event.set()
        worker_pool.stop_all()
        if tg_adapter:
            tg_adapter.stop_polling()

        # Re-exec the current process
        logger.info("Re-executing process: %s %s", sys.executable, sys.argv)
        os.execv(sys.executable, [sys.executable] + sys.argv)

    mcp_handler.restart_callback = _restart_app

    @app.post("/control/restart")
    def control_restart() -> dict[str, str]:
        """Restart the COpenClaw process."""
        import threading
        log_event(settings.data_dir, "app.restart", {"source": "http"})
        threading.Thread(target=_restart_app, args=("HTTP /control/restart",), daemon=True, name="app-restart").start()
        return {"status": "restarting"}

    @app.post("/mcp")
    async def mcp_jsonrpc(request: Request) -> dict:
        """MCP JSON-RPC endpoint for Copilot CLI tool calls.

        Workers and supervisors include ``?task_id=xxx&role=worker`` in
        their MCP config URL so the server can route events to the
        correct per-task event stream.
        """
        # Optional token auth
        if settings.mcp_token:
            token = request.headers.get("x-mcp-token")
            auth = request.headers.get("authorization", "")
            if not token and auth.lower().startswith("bearer "):
                token = auth.split(" ", 1)[1]
            if token != settings.mcp_token:
                raise HTTPException(status_code=401, detail="Invalid MCP token")

        # Extract per-task routing from query params
        task_id = request.query_params.get("task_id")
        role = request.query_params.get("role")

        body = await request.json()
        result = mcp_handler.handle_request(body, task_id=task_id, role=role)
        return result

    return app

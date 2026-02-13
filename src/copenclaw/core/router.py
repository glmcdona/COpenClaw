"""Normalized chat command router.

Parses incoming messages from any channel into a unified ChatRequest,
dispatches slash-commands, handles task proposal approval,
and falls through to Copilot CLI for free-text.
"""
from __future__ import annotations

import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional

from copenclaw.core.audit import generate_request_id, log_event
from copenclaw.core.logging_config import append_to_file, get_activity_log_path, get_orchestrator_log_path, log_command
from copenclaw.core.pairing import PairingStore
from copenclaw.core.policy import load_execution_policy, run_command
from copenclaw.core.scheduler import Scheduler
from copenclaw.core.session import SessionStore
from copenclaw.core.tasks import TaskManager
from copenclaw.core.worker import WorkerPool
from copenclaw.integrations.copilot_cli import CopilotCli, CopilotCliError

logger = logging.getLogger("copenclaw.router")

# Patterns that indicate approval or rejection
APPROVE_PATTERNS = re.compile(r"^(yes|approve|go|👍|yep|yeah|do it|ok|confirmed?)$", re.IGNORECASE)
REJECT_PATTERNS = re.compile(r"^(no|reject|cancel|👎|nope|nah|don'?t|stop)$", re.IGNORECASE)
PING_BACK_RE = re.compile(r"^ping(?:\s+back)?\s+in\s+(\d+)\s*(?:s|sec|secs|second|seconds)$", re.IGNORECASE)

@dataclass
class ChatRequest:
    """Channel-agnostic inbound message."""
    channel: str            # "telegram" | "msteams"
    sender_id: str
    chat_id: str            # Telegram chat_id or Teams conversation_id
    text: str
    service_url: Optional[str] = None  # Teams only
    request_id: Optional[str] = None

@dataclass
class ChatResponse:
    """What to send back."""
    text: str
    status: str = "ok"      # ok | denied | pairing | ignored | rejected

def handle_chat(
    req: ChatRequest,
    *,
    pairing: PairingStore,
    sessions: SessionStore,
    cli: CopilotCli,
    allow_from: list[str],
    pairing_mode: str,
    data_dir: str,
    owner_id: Optional[str] = None,
    task_manager: Optional[TaskManager] = None,
    scheduler: Optional[Scheduler] = None,
    worker_pool: Optional[WorkerPool] = None,
    on_task_approved: Optional[object] = None,  # callable(task_id) -> dict
    on_task_cancelled: Optional[object] = None,  # callable(task_id) -> None
    on_task_retry_approved: Optional[object] = None,  # callable(task_id) -> dict
    on_task_retry_rejected: Optional[object] = None,  # callable(task_id) -> None
    on_restart: Optional[object] = None,  # callable(reason: str) -> None
) -> ChatResponse:
    """Route a normalised chat request and return a response."""
    rid = req.request_id or generate_request_id()
    log_event(data_dir, f"{req.channel}.inbound", {
        "sender_id": req.sender_id, "chat_id": req.chat_id,
    }, request_id=rid)

    # Log every inbound command to the centralized commands log
    text = req.text.strip()
    cmd_type = "slash" if text.startswith("/") else "chat"
    log_command(
        channel=req.channel,
        sender_id=req.sender_id,
        chat_id=req.chat_id,
        command=text,
        command_type=cmd_type,
    )

    # --- slash commands ---

    if text.startswith("/whoami"):
        return ChatResponse(text=f"{req.channel}:{req.sender_id}")

    if text.startswith("/status"):
        return _cmd_status(task_manager, worker_pool)

    if text == "/help":
        return _cmd_help()

    if text.startswith("/restart"):
        if req.sender_id not in allow_from:
            return ChatResponse(text="Not authorized", status="denied")
        reason = text[len("/restart"):].strip() or "User requested via /restart"
        log_event(data_dir, f"{req.channel}.restart", {"sender_id": req.sender_id, "reason": reason}, request_id=rid)
        if on_restart:
            import threading
            threading.Thread(target=on_restart, args=(reason,), daemon=True, name="app-restart").start()
            return ChatResponse(text="🔄 Restarting COpenClaw… The app will be back online shortly.")
        return ChatResponse(text="Restart not available — no restart callback configured.")

    if text.startswith("/update"):
        if req.sender_id not in allow_from and not (owner_id and req.sender_id == owner_id):
            return ChatResponse(text="Not authorized", status="denied")
        from copenclaw.core.updater import check_for_updates, apply_update, format_update_check, format_update_result
        sub = text[len("/update"):].strip().lower()
        if sub == "apply":
            log_event(data_dir, f"{req.channel}.update.apply", {"sender_id": req.sender_id}, request_id=rid)
            info = check_for_updates()
            if info is None:
                return ChatResponse(text="✅ COpenClaw is already up to date.")
            result = apply_update()
            return ChatResponse(text=format_update_result(result))
        else:
            info = check_for_updates()
            return ChatResponse(text=format_update_check(info))

    if text.startswith("/exec "):
        if req.sender_id not in allow_from:
            return ChatResponse(text="Not authorized", status="denied")
        cmd = text[len("/exec "):]
        try:
            output = run_command(cmd, load_execution_policy())
        except Exception as exc:  # noqa: BLE001
            output = f"Error: {exc}"
        log_event(data_dir, f"{req.channel}.exec", {"command": cmd}, request_id=rid)
        return ChatResponse(text=output)


    # --- task / job management commands ---
    if text == "/tasks":
        return _cmd_tasks(task_manager)

    if text.startswith("/task "):
        task_id = text[len("/task "):].strip()
        return _cmd_task_detail(task_manager, worker_pool, task_id)

    if text == "/proposed":
        return _cmd_proposed(task_manager)

    if text == "/jobs":
        return _cmd_jobs(scheduler)

    if text.startswith("/job "):
        job_id = text[len("/job "):].strip()
        return _cmd_job_detail(scheduler, job_id)

    if text.startswith("/logs "):
        task_id = text[len("/logs "):].strip()
        return _cmd_logs(task_manager, task_id)

    if text.startswith("/cancel "):
        target_id = text[len("/cancel "):].strip()
        return _cmd_cancel(task_manager, scheduler, target_id, on_task_cancelled)

    # --- quick ping-back scheduling ---
    ping_match = PING_BACK_RE.match(text)
    if ping_match:
        if not scheduler:
            return ChatResponse(text="Scheduler not available.")
        seconds = int(ping_match.group(1))
        run_at = datetime.utcnow() + timedelta(seconds=seconds)
        payload = {
            "prompt": "ping",
            "channel": req.channel,
            "target": req.chat_id,
        }
        errors = scheduler.validate_payload(payload)
        if errors:
            return ChatResponse(text=f"Invalid ping request: {', '.join(errors)}")
        job = scheduler.schedule(name=f"ping-back-{req.sender_id}", run_at=run_at, payload=payload)
        log_event(data_dir, f"{req.channel}.ping.scheduled", {
            "job_id": job.job_id,
            "delay_seconds": seconds,
            "target": req.chat_id,
        }, request_id=rid)
        return ChatResponse(text=f"⏲️ Ping scheduled in {seconds} seconds.")

    # --- authorization gate ---
    if pairing_mode != "open" and req.sender_id not in allow_from and not pairing.is_allowed(req.channel, req.sender_id):
        # Auto-authorize the owner on first contact
        if owner_id and req.sender_id == owner_id:
            pairing.add_allowed(req.channel, req.sender_id)
            logger.info("Auto-authorized owner %s:%s", req.channel, req.sender_id)
            # Fall through to normal message handling
        else:
            msg = _build_unauthorized_message(req.channel, req.sender_id)
            return ChatResponse(text=msg, status="denied")

    # --- recovery approval (stale tasks from previous run) ---
    if task_manager:
        recovery_tasks = task_manager.recovery_pending_tasks(channel=req.channel, target=req.chat_id)
        if not recovery_tasks:
            # Also check tasks with no channel (e.g. tasks created without a channel)
            recovery_tasks = task_manager.recovery_pending_tasks()
        if recovery_tasks:
            if APPROVE_PATTERNS.match(text) or text.lower() == "resume":
                resolved_names = []
                for rt in recovery_tasks:
                    task_manager.resolve_recovery(rt.task_id, resume=True)
                    resolved_names.append(rt.name)
                    log_event(data_dir, f"{req.channel}.task.recovery.resumed", {
                        "task_id": rt.task_id, "name": rt.name,
                    }, request_id=rid)
                    # Re-dispatch resumed tasks if callback available
                    if on_task_approved:
                        try:
                            on_task_approved(rt.task_id)
                        except Exception:  # noqa: BLE001
                            pass
                names_str = ", ".join(f'"{n}"' for n in resolved_names)
                return ChatResponse(text=f"🔄 Resumed {len(resolved_names)} task(s): {names_str}")

            if REJECT_PATTERNS.match(text):
                resolved_names = []
                for rt in recovery_tasks:
                    task_manager.resolve_recovery(rt.task_id, resume=False)
                    resolved_names.append(rt.name)
                    log_event(data_dir, f"{req.channel}.task.recovery.cancelled", {
                        "task_id": rt.task_id, "name": rt.name,
                    }, request_id=rid)
                names_str = ", ".join(f'"{n}"' for n in resolved_names)
                return ChatResponse(text=f"❌ Cancelled {len(resolved_names)} stale task(s): {names_str}")

    # --- retry approval ---
    if task_manager:
        pending_retry = task_manager.latest_pending_retry(channel=req.channel, target=req.chat_id)
        if pending_retry:
            if APPROVE_PATTERNS.match(text):
                log_event(data_dir, f"{req.channel}.task.retry.approved", {
                    "task_id": pending_retry.task_id, "name": pending_retry.name,
                }, request_id=rid)
                if on_task_retry_approved:
                    try:
                        on_task_retry_approved(pending_retry.task_id)
                        return ChatResponse(text=f"🔁 Retry approved. Task \"{pending_retry.name}\" is restarting.")
                    except Exception as exc:  # noqa: BLE001
                        return ChatResponse(text=f"❌ Failed to retry task: {exc}")
                task_manager.approve_retry(pending_retry.task_id)
                return ChatResponse(text=f"🔁 Retry approved for \"{pending_retry.name}\" — but no worker pool available to start it.")

            if REJECT_PATTERNS.match(text):
                log_event(data_dir, f"{req.channel}.task.retry.rejected", {
                    "task_id": pending_retry.task_id, "name": pending_retry.name,
                }, request_id=rid)
                if on_task_retry_rejected:
                    try:
                        on_task_retry_rejected(pending_retry.task_id)
                    except Exception:  # noqa: BLE001
                        pass
                else:
                    task_manager.decline_retry(pending_retry.task_id)
                return ChatResponse(text=f"❌ Retry declined. Task \"{pending_retry.name}\" marked failed.")

    # --- task proposal approval ---
    if task_manager:
        proposed = task_manager.latest_proposed(channel=req.channel, target=req.chat_id)
        if proposed:
            if APPROVE_PATTERNS.match(text):
                log_event(data_dir, f"{req.channel}.task.approved", {
                    "task_id": proposed.task_id, "name": proposed.name,
                }, request_id=rid)
                if on_task_approved:
                    try:
                        on_task_approved(proposed.task_id)
                        return ChatResponse(text=f"✅ Approved! Task \"{proposed.name}\" is starting.")
                    except Exception as exc:  # noqa: BLE001
                        return ChatResponse(text=f"❌ Failed to start task: {exc}")
                else:
                    task_manager.update_status(proposed.task_id, "pending")
                    return ChatResponse(text=f"✅ Approved \"{proposed.name}\" — but no worker pool available to start it.")

            if REJECT_PATTERNS.match(text):
                task_manager.cancel_task(proposed.task_id)
                log_event(data_dir, f"{req.channel}.task.rejected", {
                    "task_id": proposed.task_id, "name": proposed.name,
                }, request_id=rid)
                return ChatResponse(text=f"❌ Rejected. Task \"{proposed.name}\" cancelled.")

    # --- free-text → Copilot CLI (with session resume) ---
    session_key = f"{req.channel}:dm:{req.sender_id}"
    sessions.upsert(session_key)

    # Look up previously stored Copilot CLI session ID for this user.
    # If one exists, the CLI will resume that session natively so it
    # retains full conversation context without us prepending history.
    # If none is stored, fall back to the CLI's default resume ID
    # (set during boot from the boot session).
    copilot_sid = sessions.get_copilot_session_id(session_key)

    # Append delegation reminder to the user message (recency bias)
    prompt_with_reminder = (
        f"{text}\n\n"
        "[SYSTEM REMINDER: You are the ORCHESTRATOR. "
        "For bigger or non-trivial work requests, use tasks_propose to dispatch a worker. "
        "For small/simple tasks, you may execute directly when the user explicitly asks. "
        "NEVER cancel or stop a task unless the user explicitly asks you to. "
        "NEVER use sleep, timeout, pause, or any blocking/waiting commands. "
        "NEVER run interactive commands that wait for input. "
        "After responding, STOP — do not loop or idle.]"
    )

    try:
        output = cli.run_prompt(prompt_with_reminder, resume_id=copilot_sid)
    except CopilotCliError as exc:
        output = f"Error: {exc}"

    # After the prompt completes, discover the session ID so we can
    # resume this conversation next time.  We always try to discover
    # (not just on the first message) because the boot session's ID
    # may have been used on the first call, and we need to capture
    # the actual session that now contains the user's conversation.
    discovered = cli._discover_latest_session_id()
    if discovered and discovered != copilot_sid:
        sessions.set_copilot_session_id(session_key, discovered)
        logger.info("Stored Copilot CLI session %s for %s", discovered, session_key)

    # Still log messages for audit trail (but no longer used for prompt building)
    sessions.append_message(session_key, "user", text)
    sessions.append_message(session_key, "assistant", output)

    _log_orchestrator(data_dir, req, output)
    return ChatResponse(text=output)


# ── Slash command implementations ─────────────────────────────

def _log_orchestrator(data_dir: str, req: ChatRequest, response: str) -> None:
    """Log orchestrator summary (detailed streaming is in copilot_cli.py)."""
    # Summary to stdout
    logger.info("ORCHESTRATOR REPLY [%s:%s] (%d chars): %s",
                req.channel, req.sender_id, len(response), response[:300])
    # Append summary to orchestrator.log (data dir)
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    reply_block = f"\n--- REPLY [{ts}] → {req.channel}:{req.sender_id} ---\n{response}\n"
    try:
        log_path = os.path.join(data_dir, "orchestrator.log")
        with open(log_path, "a", encoding="utf-8") as f:
            f.write(reply_block)
    except Exception as exc:  # noqa: BLE001
        logger.debug("Failed to write orchestrator.log: %s", exc)
    # Mirror to centralized orchestrator log and activity log
    append_to_file(get_orchestrator_log_path(), reply_block.strip())
    append_to_file(get_activity_log_path(), f"[ORCHESTRATOR] REPLY to {req.channel}:{req.sender_id} ({len(response)} chars)")


def _cmd_help() -> ChatResponse:
    help_text = (
        "🦀 **COpenClaw commands:**\n\n"
        "**Status & Info**\n"
        "`/status` — Check if COpenClaw is online\n"
        "`/whoami` — Show your channel:sender_id\n"
        "`/help` — This help message\n\n"
        "**Tasks**\n"
        "`/tasks` — List all active tasks\n"
        "`/task <id>` — Detailed status & timeline\n"
        "`/proposed` — List proposals awaiting approval\n"
        "`/logs <id>` — Recent worker output\n"
        "`/cancel <id>` — Cancel a task or job\n\n"
        "**Jobs**\n"
        "`/jobs` — List all scheduled jobs\n"
        "`/job <id>` — Job details\n\n"
        "**Admin**\n"
        "`/exec <cmd>` — Run a shell command\n"
        "`/update` — Check for code updates\n"
        "`/update apply` — Apply available update\n"
        "`/restart [reason]` — Restart the app\n\n"
        "Anything else is sent to the AI brain as free text."
    )
    return ChatResponse(text=help_text)

def _cmd_tasks(tm: Optional[TaskManager]) -> ChatResponse:
    if not tm:
        return ChatResponse(text="Task manager not available.")
    active = tm.active_tasks()
    proposed = tm.proposed_tasks()
    all_tasks = active + proposed
    if not all_tasks:
        return ChatResponse(text="No active or proposed tasks.")
    lines = []
    for t in all_tasks:
        emoji = {"proposed": "📋", "pending": "⏳", "running": "🔄", "paused": "⏸️", "needs_input": "❓"}.get(t.status, "•")
        age = _time_ago(t.created_at)
        latest = t.timeline[-1].summary if t.timeline else ""
        lines.append(f"{emoji} **{t.name}** (`{t.task_id}`)\n   Status: {t.status} | Created: {age}\n   Latest: {latest}")
    header = f"📋 **{len(all_tasks)} task(s):**\n"
    return ChatResponse(text=header + "\n\n".join(lines))

def _cmd_status(tm: Optional[TaskManager], pool: Optional[WorkerPool]) -> ChatResponse:
    lines = ["COpenClaw: ok"]
    if pool:
        status = pool.status()
        workers_running = [tid for tid, info in status.get("workers", {}).items() if info.get("running")]
        supers_running = [tid for tid, info in status.get("supervisors", {}).items() if info.get("running")]
        lines.append(f"Workers: {len(workers_running)} running")
        if workers_running:
            lines.append("- " + ", ".join(workers_running))
        lines.append(f"Supervisors: {len(supers_running)} running")
        if supers_running:
            lines.append("- " + ", ".join(supers_running))
    if tm:
        active = len(tm.active_tasks())
        proposed = len(tm.proposed_tasks())
        lines.append(f"Tasks: {active} active, {proposed} proposed")
    return ChatResponse(text="\n".join(lines))

def _cmd_task_detail(tm: Optional[TaskManager], pool: Optional[WorkerPool], task_id: str) -> ChatResponse:
    if not tm:
        return ChatResponse(text="Task manager not available.")
    task = tm.get(task_id)
    if not task:
        return ChatResponse(text=f"Task not found: `{task_id}`")
    sup = "✅ Yes" if task.auto_supervise else "❌ No"
    age = _time_ago(task.created_at)
    worker_state = "N/A"
    supervisor_state = "N/A"
    if pool:
        status = pool.status()
        worker_state = "✅ Running" if status.get("workers", {}).get(task_id, {}).get("running") else "⏹️ Stopped"
        supervisor_state = "✅ Running" if status.get("supervisors", {}).get(task_id, {}).get("running") else "⏹️ Stopped"
    lines = [
        f"🔍 **Task: \"{task.name}\"**",
        f"🆔 `{task.task_id}`",
        f"📊 Status: **{task.status}**",
        f"⏱️ Created: {age}",
        f"👁️ Supervisor: {sup} (every {task.check_interval // 60}m)",
        f"👷 Worker: {worker_state}",
        f"🧪 Supervisor: {supervisor_state}",
    ]
    if task.plan:
        lines.append(f"\n**Plan:**\n{task.plan}")
    lines.append(f"\n**Timeline (last 10):**\n{task.concise_timeline(10)}")
    return ChatResponse(text="\n".join(lines))

def _cmd_proposed(tm: Optional[TaskManager]) -> ChatResponse:
    if not tm:
        return ChatResponse(text="Task manager not available.")
    proposed = tm.proposed_tasks()
    if not proposed:
        return ChatResponse(text="No pending proposals.")
    lines = []
    for t in proposed:
        age = _time_ago(t.created_at)
        lines.append(f"📋 **{t.name}** (`{t.task_id}`) — proposed {age}\n   Plan: {(t.plan or 'N/A')[:100]}")
    header = f"📋 **{len(proposed)} proposal(s) awaiting approval:**\n"
    return ChatResponse(text=header + "\n\n".join(lines))

def _cmd_jobs(sched: Optional[Scheduler]) -> ChatResponse:
    if not sched:
        return ChatResponse(text="Scheduler not available.")
    jobs = sched.list()
    active = [j for j in jobs if j.completed_at is None and not j.cancelled]
    if not active:
        return ChatResponse(text="No active jobs.")
    lines = []
    for j in active:
        recurring = f" 🔄 `{j.cron_expr}`" if j.cron_expr else " (one-shot)"
        lines.append(f"⏰ **{j.name}** (`{j.job_id}`)\n   Next: {j.run_at.isoformat()}{recurring}")
    header = f"⏰ **{len(active)} active job(s):**\n"
    return ChatResponse(text=header + "\n\n".join(lines))

def _cmd_job_detail(sched: Optional[Scheduler], job_id: str) -> ChatResponse:
    if not sched:
        return ChatResponse(text="Scheduler not available.")
    jobs = sched.list()
    job = next((j for j in jobs if j.job_id == job_id), None)
    if not job:
        return ChatResponse(text=f"Job not found: `{job_id}`")
    status = "cancelled" if job.cancelled else ("completed" if job.completed_at else "scheduled")
    lines = [
        f"🔍 **Job: \"{job.name}\"**",
        f"🆔 `{job.job_id}`",
        f"📊 Status: **{status}**",
        f"⏱️ Next run: {job.run_at.isoformat()}",
    ]
    if job.cron_expr:
        lines.append(f"🔄 Cron: `{job.cron_expr}`")
    if job.completed_at:
        lines.append(f"✅ Completed: {job.completed_at.isoformat()}")
    prompt = job.payload.get("prompt", "N/A")
    lines.append(f"💬 Prompt: {prompt[:200]}")
    channel = job.payload.get("channel", "N/A")
    target = job.payload.get("target", "N/A")
    lines.append(f"📬 Deliver to: {channel}:{target}")
    return ChatResponse(text="\n".join(lines))

def _cmd_logs(tm: Optional[TaskManager], task_id: str) -> ChatResponse:
    if not tm:
        return ChatResponse(text="Task manager not available.")
    task = tm.get(task_id)
    if not task:
        return ChatResponse(text=f"Task not found: `{task_id}`")
    logs = tm.read_log(task_id, tail=50)
    if not logs or logs == "(no logs)":
        return ChatResponse(text=f"No logs yet for **{task.name}** (`{task_id}`)")
    # Truncate if too long for chat
    if len(logs) > 3500:
        logs = logs[-3500:]
        logs = "… (truncated)\n" + logs
    return ChatResponse(text=f"📜 **Logs for \"{task.name}\":**\n```\n{logs}\n```")

def _cmd_cancel(
    tm: Optional[TaskManager],
    sched: Optional[Scheduler],
    target_id: str,
    on_task_cancelled: Optional[object] = None,
) -> ChatResponse:
    # Try task first (task IDs start with "task-")
    if tm and target_id.startswith("task-"):
        task = tm.get(target_id)
        if task:
            if task.status in ("completed", "failed", "cancelled"):
                return ChatResponse(text=f"Task \"{task.name}\" is already {task.status}.")
            tm.cancel_task(target_id)
            if on_task_cancelled:
                try:
                    on_task_cancelled(target_id)
                except Exception:  # noqa: BLE001
                    pass
            return ChatResponse(text=f"❌ Cancelled task \"{task.name}\" (`{target_id}`)")

    # Try job (job IDs start with "job-")
    if sched and target_id.startswith("job-"):
        if sched.cancel(target_id):
            return ChatResponse(text=f"❌ Cancelled job `{target_id}`")

    return ChatResponse(text=f"Not found: `{target_id}`\n\nUse `/tasks` or `/jobs` to see valid IDs.")


# ── Unauthorized message builder ──────────────────────────────

# Map channel names to their env-var prefix for ALLOW_FROM
_CHANNEL_ENV_VARS: dict[str, str] = {
    "telegram": "TELEGRAM_ALLOW_FROM",
    "msteams": "MSTEAMS_ALLOW_FROM",
    "whatsapp": "WHATSAPP_ALLOW_FROM",
    "signal": "SIGNAL_ALLOW_FROM",
    "slack": "SLACK_ALLOW_FROM",
}

def _build_unauthorized_message(channel: str, sender_id: str) -> str:
    """Build a helpful message for unauthorized users showing how to get access."""
    env_var = _CHANNEL_ENV_VARS.get(channel, f"{channel.upper()}_ALLOW_FROM")

    lines = [
        "⚠️ You are not authorized to use this bot.\n",
        f"Your {channel} user ID is: {sender_id}\n",
        "To authorize yourself, add your ID to the allow list:\n",
        "**Edit your .env file:**",
        f"  {env_var}={sender_id}",
        "",
        "(To allow multiple users, comma-separate them:",
        f"  {env_var}=existing_id,{sender_id})\n",
        "**Or from the command line:**",
        f'  Linux/macOS: echo "{env_var}={sender_id}" >> .env',
        f'  Windows:     echo {env_var}={sender_id} >> .env\n',
        "Then restart COpenClaw for changes to take effect.",
        "",
        "Alternatively, if you ran the setup script (python scripts/configure.py),",
        "you can re-run it to reconfigure your channels.",
    ]
    return "\n".join(lines)

# ── Helpers ───────────────────────────────────────────────────

def _time_ago(dt) -> str:
    """Return a human-readable 'X ago' string."""
    from datetime import datetime, timezone
    now = datetime.now(timezone.utc)
    # Make dt timezone-aware if it isn't
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    delta = now - dt
    seconds = int(delta.total_seconds())
    if seconds < 60:
        return f"{seconds}s ago"
    minutes = seconds // 60
    if minutes < 60:
        return f"{minutes}m ago"
    hours = minutes // 60
    if hours < 24:
        return f"{hours}h ago"
    days = hours // 24
    return f"{days}d ago"

"""Centralized logging configuration for copenclaw.

Sets up Python's logging system to write to both stdout and rotating
log files in the configured log directory. Also provides dedicated
loggers for MCP calls, commands, and activity streams.

Log directory structure::

    ~/.githubclaw/.logs/
    ├── copenclaw.log          # All Python logger output (rotating)
    ├── mcp-calls.log             # Every MCP JSON-RPC request/response (JSONL)
    ├── commands.log              # All user commands (chat, slash, exec)
    ├── orchestrator.log          # Brain/orchestrator CLI session I/O
    ├── activity.log              # Unified activity stream
    ├── audit.jsonl               # Structured audit events (mirror)
    ├── task-events.log           # All per-task MCP tool call events (unified)
    └── workers/
        └── {task_id}/
            ├── worker.log        # Per-task worker output
            └── supervisor.log    # Per-task supervisor output
"""
from __future__ import annotations

import glob
import json
import logging
import logging.handlers
import os
import shutil
import time
from pathlib import Path
from typing import Any, Optional

# Module-level log directory — set by setup_logging()
_log_dir: Optional[str] = None

# Dedicated loggers for structured logging
mcp_call_logger = logging.getLogger("copenclaw._mcp_calls")
command_logger = logging.getLogger("copenclaw._commands")
task_event_logger = logging.getLogger("copenclaw._task_events")


def get_log_dir() -> str:
    """Return the configured log directory, falling back to default."""
    if _log_dir:
        return _log_dir
    default = str(Path(os.path.expanduser("~")) / ".githubclaw" / ".logs")
    return os.getenv("copenclaw_LOG_DIR", default)


def clear_logs(log_dir: str) -> None:
    """Remove all log files from the log directory.

    Deletes ``*.log``, ``*.jsonl`` files and the ``workers/`` sub-tree,
    then recreates the (now-empty) log directory.  Called **before** any
    handlers are attached so there are no open-file conflicts.
    """
    if not os.path.isdir(log_dir):
        return
    for pattern in ("*.log", "*.jsonl"):
        for path in glob.glob(os.path.join(log_dir, pattern)):
            try:
                os.remove(path)
            except OSError:
                pass
    workers_dir = os.path.join(log_dir, "workers")
    if os.path.isdir(workers_dir):
        shutil.rmtree(workers_dir, ignore_errors=True)


def setup_logging(log_dir: str, log_level: str = "info", *, clear_on_launch: bool = False) -> None:
    """Configure the logging system with both stdout and file handlers.

    This should be called once at application startup.
    """
    global _log_dir
    _log_dir = log_dir

    if clear_on_launch:
        clear_logs(log_dir)

    os.makedirs(log_dir, exist_ok=True)

    level = getattr(logging, log_level.upper(), logging.INFO)

    # ── Root logger: stdout + rotating file ──────────────────
    root = logging.getLogger()
    root.setLevel(level)

    # Clear any existing handlers (avoid duplicate output on re-init)
    root.handlers.clear()

    fmt = logging.Formatter(
        "%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        datefmt="%Y-%m-%dT%H:%M:%S",
    )

    # Stdout handler
    stdout_handler = logging.StreamHandler()
    stdout_handler.setLevel(level)
    stdout_handler.setFormatter(fmt)
    root.addHandler(stdout_handler)

    # Rotating file handler — main log
    main_log_path = os.path.join(log_dir, "copenclaw.log")
    file_handler = logging.handlers.RotatingFileHandler(
        main_log_path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)  # Capture everything to file
    file_handler.setFormatter(fmt)
    root.addHandler(file_handler)

    # ── MCP calls logger (JSONL) ─────────────────────────────
    _setup_jsonl_logger(
        mcp_call_logger,
        os.path.join(log_dir, "mcp-calls.log"),
    )

    # ── Commands logger (JSONL) ──────────────────────────────
    _setup_jsonl_logger(
        command_logger,
        os.path.join(log_dir, "commands.log"),
    )

    # ── Task events logger (JSONL) ───────────────────────────
    _setup_jsonl_logger(
        task_event_logger,
        os.path.join(log_dir, "task-events.log"),
    )

    logging.getLogger("copenclaw").info(
        "Logging initialized: log_dir=%s, level=%s", log_dir, log_level
    )


def _setup_jsonl_logger(logger_instance: logging.Logger, path: str) -> None:
    """Configure a logger to write raw JSONL messages to a rotating file."""
    logger_instance.setLevel(logging.INFO)
    logger_instance.propagate = False  # Don't bubble up to root
    logger_instance.handlers.clear()

    handler = logging.handlers.RotatingFileHandler(
        path,
        maxBytes=10 * 1024 * 1024,  # 10 MB
        backupCount=5,
        encoding="utf-8",
    )
    # Raw formatter — message is already JSON
    handler.setFormatter(logging.Formatter("%(message)s"))
    logger_instance.addHandler(handler)


# ── Structured logging helpers ───────────────────────────────


def log_mcp_call(
    method: str,
    params: dict[str, Any],
    result: Any = None,
    error: str | None = None,
    task_id: str | None = None,
    role: str | None = None,
    duration_ms: float | None = None,
    tool_name: str | None = None,
    tool_args: dict[str, Any] | None = None,
) -> None:
    """Log an MCP JSON-RPC call to the dedicated MCP calls log."""
    record: dict[str, Any] = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "method": method,
    }
    if tool_name:
        record["tool"] = tool_name
    if tool_args is not None:
        # Truncate large args for readability (generous limit for debugging)
        args_str = json.dumps(tool_args, default=str)
        record["tool_args"] = tool_args if len(args_str) < 10000 else args_str[:10000] + "…(truncated)"
    if task_id:
        record["task_id"] = task_id
    if role:
        record["role"] = role
    if duration_ms is not None:
        record["duration_ms"] = round(duration_ms, 1)
    if error:
        record["error"] = error[:5000]
    elif result is not None:
        result_str = json.dumps(result, default=str)
        if len(result_str) > 10000:
            record["result_preview"] = result_str[:10000] + "..."
        else:
            record["result"] = result
    try:
        mcp_call_logger.info(json.dumps(record, default=str))
    except Exception:  # noqa: BLE001
        pass


def log_command(
    channel: str,
    sender_id: str,
    chat_id: str,
    command: str,
    command_type: str = "chat",
    response_preview: str = "",
) -> None:
    """Log a user command to the dedicated commands log."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "channel": channel,
        "sender_id": sender_id,
        "chat_id": chat_id,
        "type": command_type,
        "command": command[:2000],
    }
    if response_preview:
        record["response_preview"] = response_preview[:500]
    try:
        command_logger.info(json.dumps(record, default=str))
    except Exception:  # noqa: BLE001
        pass


def log_task_event_central(
    task_id: str,
    role: str,
    tool: str,
    args_summary: str,
    result_summary: str,
    is_error: bool = False,
) -> None:
    """Log a task event to the centralized task-events log."""
    record = {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "task_id": task_id,
        "role": role,
        "tool": tool,
        "args": args_summary[:5000],
        "result": result_summary[:5000],
        "error": is_error,
    }
    try:
        task_event_logger.info(json.dumps(record, default=str))
    except Exception:  # noqa: BLE001
        pass


# ── File-based logging helpers (for orchestrator/activity/worker) ─


def get_orchestrator_log_path() -> str:
    """Return the path to the orchestrator log file."""
    return os.path.join(get_log_dir(), "orchestrator.log")


def get_activity_log_path() -> str:
    """Return the path to the unified activity log file."""
    return os.path.join(get_log_dir(), "activity.log")


def get_worker_log_dir(task_id: str) -> str:
    """Return the directory for per-task worker/supervisor logs."""
    d = os.path.join(get_log_dir(), "workers", task_id)
    os.makedirs(d, exist_ok=True)
    return d


def get_mcp_log_path() -> str:
    """Return the path to the MCP calls log file."""
    return os.path.join(get_log_dir(), "mcp-calls.log")

def get_audit_log_path() -> str:
    """Return the path to the centralized audit JSONL log."""
    return os.path.join(get_log_dir(), "audit.jsonl")


def append_to_file(path: str, line: str) -> None:
    """Append a timestamped line to a log file, flushing immediately."""
    try:
        ts = time.strftime("%Y-%m-%dT%H:%M:%S")
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{ts} {line}\n")
            f.flush()
    except Exception:  # noqa: BLE001
        pass
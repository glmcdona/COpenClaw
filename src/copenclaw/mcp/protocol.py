"""MCP JSON-RPC protocol handler.

Implements the Model Context Protocol over HTTP (Streamable HTTP transport).
Copilot CLI POSTs JSON-RPC requests to a single endpoint and expects
JSON-RPC responses back.

    Includes infrastructure tools (jobs, messaging, file access) and the
    task dispatch / ITC protocol tools (tasks_*, task_report, task_check_inbox).
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta
from typing import Any, Optional

from copenclaw.core.audit import log_event
from copenclaw.core.mcp_registry import (
    add_server as registry_add_server,
    get_user_servers_for_merge,
    list_servers as registry_list_servers,
    remove_server as registry_remove_server,
    run_install_command,
)
from copenclaw.core.logging_config import (
    append_to_file,
    get_mcp_log_path,
    get_activity_log_path,
    log_mcp_call,
    log_task_event_central,
)
from copenclaw.core.names import generate_name
from copenclaw.core.policy import ExecutionPolicy, load_execution_policy
from copenclaw.core.scheduler import Scheduler
from copenclaw.core.task_events import TaskEventRegistry
from copenclaw.core.tasks import TaskManager, _now
from copenclaw.core.worker import WorkerPool
from copenclaw.integrations.telegram import TelegramAdapter
from copenclaw.integrations.teams import TeamsAdapter
from copenclaw.integrations.whatsapp import WhatsAppAdapter
from copenclaw.integrations.signal import SignalAdapter
from copenclaw.integrations.slack import SlackAdapter

logger = logging.getLogger("copenclaw.mcp.protocol")

_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".tif", ".tiff"}


def _is_image_path(path: str) -> bool:
    return os.path.splitext(path.lower())[1] in _IMAGE_EXTENSIONS

# ── Tool definitions (returned by tools/list) ────────────────────

INFRA_TOOLS = [
    {
        "name": "jobs_schedule",
        "description": "Schedule a one-shot or recurring job. The job will execute a prompt via Copilot CLI and deliver the result to a chat channel.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable job name"},
                "run_at": {"type": "string", "description": "ISO-8601 datetime when the job should first run"},
                "prompt": {"type": "string", "description": "The prompt to execute when the job fires"},
                "channel": {"type": "string", "enum": ["telegram", "teams", "whatsapp", "signal", "slack"], "description": "Delivery channel"},
                "target": {"type": "string", "description": "Chat ID (Telegram) or conversation ID (Teams)"},
                "cron_expr": {"type": "string", "description": "Optional cron expression for recurring jobs"},
                "service_url": {"type": "string", "description": "Required for Teams channel"},
            },
            "required": ["name", "run_at", "prompt", "channel", "target"],
        },
    },
    {
        "name": "jobs_list",
        "description": "List all scheduled jobs with their status.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "jobs_cancel",
        "description": "Cancel a scheduled job by its ID.",
        "inputSchema": {
            "type": "object",
            "properties": {"job_id": {"type": "string"}},
            "required": ["job_id"],
        },
    },
    {
        "name": "jobs_runs",
        "description": "List job run history.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "job_id": {"type": "string", "description": "Filter by job ID (optional)"},
                "limit": {"type": "integer", "default": 50},
            },
        },
    },
    {
        "name": "jobs_clear_all",
        "description": "Remove all scheduled jobs (both pending and completed). Returns the count of jobs cleared.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "send_message",
        "description": "Send a message to a chat channel (Telegram, Teams, WhatsApp, Signal, or Slack).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "channel": {"type": "string", "enum": ["telegram", "teams", "whatsapp", "signal", "slack"]},
                "target": {"type": "string", "description": "Chat ID, conversation ID, phone number, or Slack channel ID"},
                "text": {"type": "string", "description": "Message text or image caption (optional)"},
                "image_path": {"type": "string", "description": "Local image path to send (Telegram/Signal/Slack)"},
                "service_url": {"type": "string", "description": "Required for Teams"},
            },
            "required": ["channel", "target"],
        },
    },
    {
        "name": "files_read",
        "description": "Read a file from the data directory.",
        "inputSchema": {
            "type": "object",
            "properties": {"path": {"type": "string"}},
            "required": ["path"],
        },
    },
    {
        "name": "files_write",
        "description": "Write content to a file within a task's workspace or the data directory. Creates parent directories as needed.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "File path (relative to data_dir, or absolute within allowed dirs)"},
                "content": {"type": "string", "description": "File content to write"},
            },
            "required": ["path", "content"],
        },
    },
    {
        "name": "audit_read",
        "description": "Read recent audit log events.",
        "inputSchema": {
            "type": "object",
            "properties": {"limit": {"type": "integer", "default": 100}},
        },
    },
    # ── MCP server management tools ──
    {
        "name": "mcp_server_add",
        "description": "Install and register an MCP server so it becomes available to the brain and all future tasks (workers/supervisors). For stdio servers, optionally runs an install command first (e.g. 'npm install -g @playwright/mcp'). The server is written to ~/.copilot/mcp-config.json — the same config Copilot CLI uses natively.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Unique server name (e.g. 'playwright', 'fetch', 'github')"},
                "type": {"type": "string", "enum": ["http", "sse", "stdio"], "description": "Server transport type"},
                "url": {"type": "string", "description": "Server URL (required for http/sse type)"},
                "command": {"type": "string", "description": "Executable command (required for stdio type, e.g. 'npx')"},
                "args": {"type": "array", "items": {"type": "string"}, "description": "Arguments for stdio command (e.g. ['playwright-mcp'])"},
                "install_command": {"type": "string", "description": "Package install command to run first (e.g. 'npm install -g @playwright/mcp'). Only for stdio servers."},
                "env": {"type": "object", "description": "Environment variables for the server"},
                "headers": {"type": "object", "description": "HTTP headers (for http/sse servers, e.g. auth tokens)"},
                "tools": {"type": "array", "items": {"type": "string"}, "description": "Tool filter list (default: all tools)"},
            },
            "required": ["name", "type"],
        },
    },
    {
        "name": "mcp_server_list",
        "description": "List all MCP servers configured in ~/.copilot/mcp-config.json. Shows servers available to the brain and all tasks.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "mcp_server_remove",
        "description": "Remove an MCP server from ~/.copilot/mcp-config.json by name.",
        "inputSchema": {
            "type": "object",
            "properties": {"name": {"type": "string", "description": "Server name to remove"}},
            "required": ["name"],
        },
    },
    {
        "name": "app_restart",
        "description": "Restart the entire COpenClaw application. Use this when the app needs a fresh start, e.g. after configuration changes, code updates, or to recover from a bad state. The process will gracefully shut down all workers and supervisors, then re-launch itself.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "reason": {"type": "string", "description": "Why the restart is needed (logged for audit)"},
            },
        },
    },
]

TASK_TOOLS = [
    # ── Orchestrator-level tools ──
    {
        "name": "tasks_propose",
        "description": "Propose a task plan for user approval. Creates a proposal that the user must approve (Yes) or reject (No) before workers are spawned. Use this for any complex multi-step work.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable task name (auto-generated if omitted)"},
                "prompt": {"type": "string", "description": "The detailed instructions for the worker"},
                "plan": {"type": "string", "description": "A clear bullet-point plan of what the worker will do"},
                "channel": {"type": "string", "enum": ["telegram", "teams", "whatsapp", "signal", "slack"], "description": "Where to report results"},
                "target": {"type": "string", "description": "Chat ID, conversation ID, phone number, or Slack channel ID for notifications"},
                "service_url": {"type": "string", "description": "Required for Teams"},
                "check_interval": {"type": "integer", "default": 300, "description": "Supervisor check interval in seconds"},
                "auto_supervise": {"type": "boolean", "default": True, "description": "Whether to auto-start a supervisor"},
                "supervisor_instructions": {"type": "string", "description": "What the supervisor should watch for"},
                "task_type": {"type": "string", "enum": ["standard", "continuous_improvement"], "default": "standard"},
                "continuous": {"type": "object", "description": "Continuous improvement configuration when task_type=continuous_improvement"},
                "on_complete": {"type": "string", "description": "A prompt to feed to the orchestrator when this task finishes (success, failure, or cancellation). Use this for chaining tasks or retrying on failure. The hook prompt includes the terminal reason so the orchestrator can react appropriately."},
            },
            "required": ["prompt", "plan"],
        },
    },
    {
        "name": "tasks_approve",
        "description": "Approve a proposed task, spawning the worker (and optionally supervisor). Called automatically when the user replies 'Yes' to a proposal.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "tasks_create",
        "description": "Create and immediately dispatch a task WITHOUT requiring approval. Only use for simple automated tasks or when the user has explicitly pre-approved. For complex work, use tasks_propose instead.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "name": {"type": "string", "description": "Human-readable task name (auto-generated if omitted)"},
                "prompt": {"type": "string", "description": "The task to execute autonomously"},
                "channel": {"type": "string", "enum": ["telegram", "teams", "whatsapp", "signal", "slack"], "description": "Where to report results"},
                "target": {"type": "string", "description": "Chat ID, conversation ID, phone number, or Slack channel ID for notifications"},
                "service_url": {"type": "string", "description": "Required for Teams"},
                "check_interval": {"type": "integer", "default": 300, "description": "Supervisor check interval in seconds"},
                "auto_supervise": {"type": "boolean", "default": True, "description": "Whether to auto-start a supervisor"},
                "task_type": {"type": "string", "enum": ["standard", "continuous_improvement"], "default": "standard"},
                "continuous": {"type": "object", "description": "Continuous improvement configuration when task_type=continuous_improvement"},
                "on_complete": {"type": "string", "description": "A prompt to feed to the orchestrator when this task finishes (success, failure, or cancellation)."},
            },
            "required": ["prompt"],
        },
    },
    {
        "name": "tasks_list",
        "description": "List all dispatched tasks with their current status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "status": {"type": "string", "description": "Filter by status (optional)"},
            },
        },
    },
    {
        "name": "tasks_status",
        "description": "Get detailed status of a task including its concise timeline.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "timeline_limit": {"type": "integer", "default": 20},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "tasks_logs",
        "description": "Get logs from a task. Use log_type to choose: 'combined' (default, from TaskManager), 'worker' (worker.log), 'supervisor' (supervisor.log), or 'activity' (unified activity.log).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "tail": {"type": "integer", "default": 100, "description": "Number of lines from end"},
                "log_type": {"type": "string", "enum": ["combined", "worker", "supervisor", "activity"], "default": "combined", "description": "Which log file to read"},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "tasks_send",
        "description": "Send a message to a task's worker or supervisor. If the task has stopped (completed/failed/cancelled), sending an 'instruction' or 'redirect' will auto-resume it with a new worker using the message as updated instructions. Use this to continue or redirect existing tasks.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "msg_type": {"type": "string", "enum": ["instruction", "input", "pause", "resume", "redirect", "cancel", "priority"]},
                "content": {"type": "string", "description": "The message content — for stopped tasks, this becomes the continuation prompt"},
                "supervisor_instructions": {"type": "string", "description": "Updated supervisor instructions (optional, applied on resume)"},
            },
            "required": ["task_id", "msg_type", "content"],
        },
    },
    {
        "name": "tasks_cancel",
        "description": "Cancel a running task and stop its worker/supervisor.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "tasks_clear_all",
        "description": "Cancel all active tasks (stopping their workers/supervisors) and remove all tasks from the store. Returns the count of tasks cleared.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    # ── Worker/Supervisor-level tools (called by sub-tier sessions) ──
    {
        "name": "task_report",
        "description": "Report progress, completion, failure, or other status upward from a worker or supervisor session.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "type": {"type": "string", "enum": [
                    "progress", "completed", "failed", "needs_input",
                    "question", "artifact", "assessment", "intervention", "escalation",
                ]},
                "summary": {"type": "string", "description": "One-line concise summary"},
                "detail": {"type": "string", "description": "Longer detail (optional)"},
                "artifact_url": {"type": "string", "description": "URL/path if type=artifact"},
                "continuous": {"type": "object", "description": "Optional structured continuous-improvement iteration payload"},
                "from_tier": {"type": "string", "enum": ["worker", "supervisor"], "default": "worker"},
                "notify_user": {"type": "boolean", "default": False},
            },
            "required": ["task_id", "type", "summary"],
        },
    },
    {
        "name": "task_check_inbox",
        "description": "Check for new messages/instructions from the orchestrator or supervisor. Workers should call this periodically.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "task_set_status",
        "description": "Update a task's status (for worker/supervisor use).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "status": {"type": "string", "enum": ["running", "paused", "needs_input", "completed", "failed"]},
            },
            "required": ["task_id", "status"],
        },
    },
    {
        "name": "task_get_context",
        "description": "Read the original task prompt, configuration, and any recent messages.",
        "inputSchema": {
            "type": "object",
            "properties": {"task_id": {"type": "string"}},
            "required": ["task_id"],
        },
    },
    {
        "name": "task_read_peer",
        "description": "Read a task's worker logs (for supervisors to review worker output).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "tail": {"type": "integer", "default": 50},
            },
            "required": ["task_id"],
        },
    },
    {
        "name": "task_send_input",
        "description": "Send guidance/input from supervisor to worker (adds to worker's inbox).",
        "inputSchema": {
            "type": "object",
            "properties": {
                "task_id": {"type": "string"},
                "content": {"type": "string", "description": "Guidance or input for the worker"},
            },
            "required": ["task_id", "content"],
        },
    },
]

ALL_TOOLS = INFRA_TOOLS + TASK_TOOLS


class MCPProtocolHandler:
    """Handles MCP JSON-RPC requests and dispatches tool calls."""

    def __init__(
        self,
        scheduler: Scheduler,
        data_dir: str | None = None,
        telegram_token: str | None = None,
        msteams_creds: dict | None = None,
        task_manager: TaskManager | None = None,
        worker_pool: WorkerPool | None = None,
        notify_callback: Any = None,
        owner_chat_id: str | None = None,
        execution_policy: ExecutionPolicy | None = None,
    ) -> None:
        self.scheduler = scheduler
        self.data_dir = data_dir
        self.telegram_token = telegram_token
        self.msteams_creds = msteams_creds
        self.task_manager = task_manager
        self.worker_pool = worker_pool
        self.notify_callback = notify_callback  # callable(channel, target, text, service_url=None)
        self.owner_chat_id = owner_chat_id  # Telegram chat ID for the owner, used as fallback
        # Cache execution policy at init time (after dotenv is loaded)
        self._execution_policy = execution_policy or load_execution_policy()
        # Per-task event stream registry
        self.event_registry = TaskEventRegistry()
        # Callback fired when a task reaches a terminal state (includes on_complete hook if provided).
        # Signature: on_complete_callback(prompt: str, channel: str, target: str, service_url: str, source_task_name: str) -> None
        self.on_complete_callback: Any = None
        # Callback to restart the entire app process.
        # Signature: restart_callback(reason: str) -> None
        self.restart_callback: Any = None

    def handle_request(
        self,
        body: dict[str, Any],
        task_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        """Process a single JSON-RPC request and return a JSON-RPC response.

        Parameters
        ----------
        body : dict
            The JSON-RPC request body.
        task_id : str, optional
            If provided (from query params), all tool calls are logged to
            the per-task event stream.
        role : str, optional
            "worker" or "supervisor" — identifies the caller for event logging.
        """
        jsonrpc = body.get("jsonrpc", "2.0")
        method = body.get("method", "")
        params = body.get("params", {})
        req_id = body.get("id")

        logger.info("MCP request: method=%s id=%s task=%s role=%s", method, req_id, task_id, role)
        # Log every inbound MCP request to the centralized MCP log (plain text)
        append_to_file(
            get_mcp_log_path(),
            f"REQUEST method={method} id={req_id} task={task_id} role={role}",
        )
        # Also log structured JSONL for MCP requests
        log_mcp_call(
            method=method,
            params=params,
            task_id=task_id,
            role=role,
        )

        try:
            result = self._dispatch(method, params, task_id=task_id, role=role)
        except Exception as exc:  # noqa: BLE001
            logger.error("MCP method %s failed: %s", method, exc)
            return self._error_response(req_id, -32603, str(exc))

        if req_id is None:
            return {}

        return {"jsonrpc": jsonrpc, "id": req_id, "result": result}

    def _dispatch(
        self,
        method: str,
        params: dict[str, Any],
        task_id: str | None = None,
        role: str | None = None,
    ) -> Any:
        if method == "initialize":
            return self._handle_initialize(params)
        if method in ("initialized", "notifications/initialized"):
            return {}
        if method == "tools/list":
            return self._handle_tools_list(params, role=role)
        if method == "tools/call":
            return self._handle_tools_call(params, task_id=task_id, role=role)
        if method == "ping":
            return {}
        raise ValueError(f"Unknown method: {method}")

    def _handle_initialize(self, params: dict[str, Any]) -> dict[str, Any]:
        return {
            "protocolVersion": "2025-03-26",
            "capabilities": {"tools": {"listChanged": False}},
            "serverInfo": {"name": "COpenClaw", "version": "0.2.0"},
        }

    def _handle_tools_list(self, params: dict[str, Any], role: str | None = None) -> dict[str, Any]:
        # All tools visible to all roles — orchestrator uses tasks_create
        # for automated follow-ups (on_complete hooks, scheduled tasks)
        return {"tools": ALL_TOOLS}

    def _handle_tools_call(
        self,
        params: dict[str, Any],
        task_id: str | None = None,
        role: str | None = None,
    ) -> dict[str, Any]:
        name = params.get("name", "")
        arguments = params.get("arguments", {})
        args_str = json.dumps(arguments)[:2000]
        event_args_summary = json.dumps(arguments, default=str)[:4000]
        send_message_summary = ""
        if name == "send_message":
            send_message_summary, _ = self._summarize_send_message_args(arguments)
            event_args_summary = send_message_summary
        logger.info("MCP tools/call: %s args=%s (task=%s role=%s)", name, args_str[:200], task_id, role)
        # Detailed MCP tool call log
        append_to_file(
            get_mcp_log_path(),
            f"TOOL_CALL tool={name} task={task_id} role={role} args={args_str}",
        )

        # Store caller context so individual tool methods can use it for audit
        self._current_task_id = task_id
        self._current_role = role or "orchestrator"

        import time as _time
        _t0 = _time.monotonic()

        # Track last worker activity timestamp for stuck-detection
        if task_id and role == "worker" and self.task_manager:
            task_obj = self.task_manager.get(task_id)
            if task_obj:
                task_obj.last_worker_activity_at = _now()
                if task_obj.watchdog_state in {"warned", "restarted"}:
                    task_obj.watchdog_state = "none"
                    task_obj.watchdog_last_action_at = None
                self.task_manager._save()

        try:
            result = self._call_tool(name, arguments)
            _duration_ms = (_time.monotonic() - _t0) * 1000
            result_str = json.dumps(result, default=str)

            if name == "send_message" and task_id and role == "worker" and self.task_manager:
                task = self.task_manager.get(task_id)
                if task:
                    summary = send_message_summary or self._summarize_send_message_args(arguments)[0]
                    task.add_timeline("message_sent", f"Sent user message ({summary})")
                    task.updated_at = _now()
                    self.task_manager._save()

            # Log tool call details — wrapped in its own try/except so
            # logging failures never turn a successful tool call into an error.
            try:
                self._log_task_event(
                    task_id, role or "unknown", name,
                    event_args_summary,
                    result_str[:8000],
                    is_error=False,
                )
                append_to_file(
                    get_mcp_log_path(),
                    f"TOOL_RESULT tool={name} task={task_id} ok=true result={result_str[:4000]}",
                )
                log_mcp_call(
                    method="tools/call",
                    params={"name": name},
                    result=result,
                    task_id=task_id,
                    role=role,
                    duration_ms=_duration_ms,
                    tool_name=name,
                    tool_args=arguments,
                )
                log_task_event_central(
                    task_id=task_id or "",
                    role=role or "orchestrator",
                    tool=name,
                    args_summary=event_args_summary,
                    result_summary=result_str[:4000],
                    is_error=False,
                )
            except Exception:  # noqa: BLE001
                logger.warning("Failed to log successful tool call %s (result still returned)", name, exc_info=True)

            return {
                "content": [{"type": "text", "text": result_str}],
                "isError": False,
            }
        except Exception as exc:  # noqa: BLE001
            _duration_ms = (_time.monotonic() - _t0) * 1000
            logger.error("Tool %s failed: %s", name, exc)
            err_str = str(exc)
            # Log error to per-task event stream
            self._log_task_event(
                task_id, role or "unknown", name,
                event_args_summary,
                err_str[:4000],
                is_error=True,
            )
            # Log tool error to centralized MCP log (plain text)
            append_to_file(
                get_mcp_log_path(),
                f"TOOL_RESULT tool={name} task={task_id} ok=false error={err_str[:4000]}",
            )
            # Structured JSONL MCP call log with error
            log_mcp_call(
                method="tools/call",
                params={"name": name},
                error=err_str,
                task_id=task_id,
                role=role,
                duration_ms=_duration_ms,
                tool_name=name,
                tool_args=arguments,
            )
            # Centralized task-events JSONL log (error)
            log_task_event_central(
                task_id=task_id or "",
                role=role or "orchestrator",
                tool=name,
                args_summary=event_args_summary,
                result_summary=err_str[:4000],
                is_error=True,
            )
            return {
                "content": [{"type": "text", "text": f"Error: {exc}"}],
                "isError": True,
            }

    def _call_tool(self, name: str, args: dict[str, Any]) -> Any:
        # Infrastructure tools
        if name == "jobs_schedule":
            return self._tool_jobs_schedule(args)
        if name == "jobs_list":
            return self._tool_jobs_list(args)
        if name == "jobs_cancel":
            return self._tool_jobs_cancel(args)
        if name == "jobs_runs":
            return self._tool_jobs_runs(args)
        if name == "jobs_clear_all":
            return self._tool_jobs_clear_all(args)
        if name == "send_message":
            return self._tool_send_message(args)
        if name == "files_read":
            return self._tool_files_read(args)
        if name == "files_write":
            return self._tool_files_write(args)
        if name == "audit_read":
            return self._tool_audit_read(args)
        # MCP server management tools
        if name == "mcp_server_add":
            return self._tool_mcp_server_add(args)
        if name == "mcp_server_list":
            return self._tool_mcp_server_list(args)
        if name == "mcp_server_remove":
            return self._tool_mcp_server_remove(args)
        # App lifecycle tools
        if name == "app_restart":
            return self._tool_app_restart(args)
        # Task dispatch tools (orchestrator level)
        if name == "tasks_propose":
            return self._tool_tasks_propose(args)
        if name == "tasks_approve":
            return self._tool_tasks_approve(args)
        if name == "tasks_create":
            return self._tool_tasks_create(args)
        if name == "tasks_list":
            return self._tool_tasks_list(args)
        if name == "tasks_status":
            return self._tool_tasks_status(args)
        if name == "tasks_logs":
            return self._tool_tasks_logs(args)
        if name == "tasks_send":
            return self._tool_tasks_send(args)
        if name == "tasks_cancel":
            return self._tool_tasks_cancel(args)
        if name == "tasks_clear_all":
            return self._tool_tasks_clear_all(args)
        # Task ITC tools (worker/supervisor level)
        if name == "task_report":
            return self._tool_task_report(args)
        if name == "task_check_inbox":
            return self._tool_task_check_inbox(args)
        if name == "task_set_status":
            return self._tool_task_set_status(args)
        if name == "task_get_context":
            return self._tool_task_get_context(args)
        if name == "task_read_peer":
            return self._tool_task_read_peer(args)
        if name == "task_send_input":
            return self._tool_task_send_input(args)
        raise ValueError(f"Unknown tool: {name}")

    # ── Infrastructure tool implementations ───────────────

    def _tool_jobs_schedule(self, args: dict[str, Any]) -> dict:
        run_at_str = args["run_at"]
        try:
            run_at = datetime.fromisoformat(run_at_str.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError(f"Invalid run_at: {exc}") from exc
        cron_expr = args.get("cron_expr")
        if cron_expr and not Scheduler.validate_cron(cron_expr):
            raise ValueError(f"Invalid cron expression: {cron_expr}")
        payload = {"prompt": args["prompt"], "channel": args["channel"], "target": args["target"]}
        if args.get("service_url"):
            payload["service_url"] = args["service_url"]
        errors = Scheduler.validate_payload(payload)
        if errors:
            raise ValueError("; ".join(errors))
        job = self.scheduler.schedule(args["name"], run_at, payload, cron_expr=cron_expr)
        if self.data_dir:
            log_event(self.data_dir, "mcp.jobs.schedule", {"job_id": job.job_id, "name": job.name})
        return {
            "job_id": job.job_id,
            "name": job.name,
            "run_at": job.run_at.isoformat(),
            "status": "scheduled",
            "cron_expr": job.cron_expr,
        }

    def _tool_jobs_list(self, args: dict[str, Any]) -> dict:
        jobs = self.scheduler.list()
        return {"jobs": [{"job_id": j.job_id, "name": j.name, "run_at": j.run_at.isoformat(),
                          "completed": j.completed_at is not None, "cancelled": j.cancelled,
                          "cron_expr": j.cron_expr} for j in jobs]}

    def _tool_jobs_cancel(self, args: dict[str, Any]) -> dict:
        if self.scheduler.cancel(args["job_id"]):
            if self.data_dir:
                log_event(self.data_dir, "mcp.jobs.cancel", {"job_id": args["job_id"]})
            return {"status": "cancelled", "job_id": args["job_id"]}
        raise ValueError(f"Job not found: {args['job_id']}")

    def _tool_jobs_runs(self, args: dict[str, Any]) -> dict:
        return {"runs": self.scheduler.list_runs(job_id=args.get("job_id"), limit=args.get("limit", 50))}

    def _tool_jobs_clear_all(self, args: dict[str, Any]) -> dict:
        count = self.scheduler.clear_all()
        if self.data_dir:
            log_event(self.data_dir, "mcp.jobs.clear_all", {"cleared": count})
        return {"status": "cleared", "cleared": count}

    def _audit(self, action: str, payload: dict[str, Any]) -> None:
        """Write an audit log entry with role/task context automatically included."""
        if not self.data_dir:
            return
        task_id = getattr(self, "_current_task_id", None)
        role = getattr(self, "_current_role", "orchestrator")
        # Prefix the event type with the role for easy filtering
        event_type = f"{role}.{action}"
        # Always include task_id and task_name if available
        if task_id:
            payload["task_id"] = task_id
            if self.task_manager:
                task = self.task_manager.get(task_id)
                if task:
                    payload["task_name"] = task.name
        log_event(self.data_dir, event_type, payload)

    @staticmethod
    def _summarize_send_message_args(args: dict[str, Any]) -> tuple[str, dict[str, Any]]:
        channel = args.get("channel", "")
        target = str(args.get("target", ""))
        image_path = args.get("image_path")
        message_type = "image" if image_path else "text"
        summary = (
            f"message_type={message_type} channel={channel} target={target} "
            f"image_path={'yes' if image_path else 'no'}"
        )
        payload = {
            "channel": channel,
            "target": target,
            "message_type": message_type,
            "image_path_used": bool(image_path),
        }
        return summary, payload

    def _tool_send_message(self, args: dict[str, Any]) -> dict:
        channel = args["channel"]
        text = args.get("text") or ""
        image_path = args.get("image_path")
        _, audit_payload = self._summarize_send_message_args(args)
        if channel == "telegram":
            if not self.telegram_token:
                raise ValueError("Telegram not configured")
            adapter = TelegramAdapter(self.telegram_token)
            if image_path:
                caption = text or None
                if caption and len(caption) > 1024:
                    adapter.send_photo(chat_id=int(args["target"]), photo_path=image_path, caption=caption[:1024])
                    adapter.send_message(chat_id=int(args["target"]), text=caption)
                else:
                    adapter.send_photo(chat_id=int(args["target"]), photo_path=image_path, caption=caption)
                self._audit("send_message", audit_payload)
                return {"status": "sent", "channel": "telegram"}
            adapter.send_message(chat_id=int(args["target"]), text=text)
            self._audit("send_message", audit_payload)
            return {"status": "sent", "channel": "telegram"}
        if channel == "teams":
            if not self.msteams_creds:
                raise ValueError(
                    "Teams not configured. Set MSTEAMS_APP_ID, MSTEAMS_APP_PASSWORD, MSTEAMS_TENANT_ID."
                )
            service_url = args.get("service_url") or self.msteams_creds.get("service_url")
            if not service_url:
                raise ValueError(
                    "service_url required for Teams (from webhook payload). "
                    "Pass service_url or send a Teams message to capture it."
                )
            TeamsAdapter(
                app_id=self.msteams_creds["app_id"],
                app_password=self.msteams_creds["app_password"],
                tenant_id=self.msteams_creds["tenant_id"],
            ).send_message(service_url=service_url, conversation_id=args["target"], text=text)
            self._audit("send_message", audit_payload)
            return {"status": "sent", "channel": "teams"}
        if channel == "whatsapp":
            wa_phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
            wa_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
            if not wa_phone_id or not wa_token:
                raise ValueError("WhatsApp not configured")
            adapter = WhatsAppAdapter(phone_number_id=wa_phone_id, access_token=wa_token)
            if image_path:
                adapter.send_image(to=args["target"], image_url=image_path, caption=text or None)
            else:
                adapter.send_message(to=args["target"], text=text)
            self._audit("send_message", audit_payload)
            return {"status": "sent", "channel": "whatsapp"}
        if channel == "signal":
            sig_url = os.getenv("SIGNAL_API_URL")
            sig_phone = os.getenv("SIGNAL_PHONE_NUMBER")
            if not sig_url or not sig_phone:
                raise ValueError(
                    "Signal not configured. Set SIGNAL_API_URL and SIGNAL_PHONE_NUMBER, "
                    "and ensure signal-cli-rest-api is running."
                )
            adapter = SignalAdapter(api_url=sig_url, phone_number=sig_phone)
            if image_path:
                adapter.send_image(recipient=args["target"], image_path=image_path, caption=text or None)
            else:
                adapter.send_message(recipient=args["target"], text=text)
            self._audit("send_message", audit_payload)
            return {"status": "sent", "channel": "signal"}
        if channel == "slack":
            slack_token = os.getenv("SLACK_BOT_TOKEN")
            if not slack_token:
                raise ValueError("Slack not configured")
            adapter = SlackAdapter(bot_token=slack_token)
            if image_path:
                adapter.send_image(channel=args["target"], image_path=image_path, caption=text or None)
            else:
                adapter.send_message(channel=args["target"], text=text)
            self._audit("send_message", audit_payload)
            return {"status": "sent", "channel": "slack"}
        raise ValueError(f"Unsupported channel: {channel}")

    def _tool_files_read(self, args: dict[str, Any]) -> dict:
        if not self.data_dir:
            raise ValueError("data_dir not configured")
        base = os.path.abspath(self.data_dir)
        path = args["path"]
        if not os.path.isabs(path):
            path = os.path.join(base, path)
        target = os.path.abspath(path)
        if not target.startswith(base):
            raise PermissionError("Path is outside allowed data_dir")
        if not os.path.exists(target):
            raise FileNotFoundError(f"File not found: {path}")
        with open(target, "r", encoding="utf-8") as f:
            return {"content": f.read()}

    def _tool_files_write(self, args: dict[str, Any]) -> dict:
        """Write content to a file. Relative paths resolve against data_dir."""
        if not self.data_dir:
            raise ValueError("data_dir not configured")
        base = os.path.abspath(self.data_dir)
        path = args["path"]
        if not os.path.isabs(path):
            path = os.path.join(base, path)
        target = os.path.abspath(path)
        # Warn (but allow) writes outside data_dir to preserve backward compatibility.
        if not target.startswith(base):
            logger.warning("files_write: path outside data_dir: %s", target)
        # Create parent directories
        parent = os.path.dirname(target)
        if parent:
            os.makedirs(parent, exist_ok=True)
        with open(target, "w", encoding="utf-8") as f:
            f.write(args["content"])
        self._audit("files.write", {"path": target, "size": len(args["content"])})
        return {"status": "written", "path": target, "size": len(args["content"])}

    def _tool_audit_read(self, args: dict[str, Any]) -> dict:
        if not self.data_dir:
            raise ValueError("data_dir not configured")
        path = os.path.join(self.data_dir, "audit.jsonl")
        if not os.path.exists(path):
            return {"events": []}
        events: list[dict] = []
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    events.append(json.loads(line))
        return {"events": events[-args.get("limit", 100):]}

    def _tool_mcp_server_add(self, args: dict[str, Any]) -> dict:
        """Add an MCP server to Copilot CLI's config."""
        name = args["name"]
        server_type = args["type"]

        # Run install command first if provided (for stdio servers)
        install_output = ""
        if args.get("install_command"):
            install_output = run_install_command(args["install_command"])

        entry = registry_add_server(
            name=name,
            server_type=server_type,
            url=args.get("url"),
            command=args.get("command"),
            args=args.get("args"),
            env=args.get("env"),
            headers=args.get("headers"),
            tools=args.get("tools"),
        )

        self._audit("mcp_server.add", {
            "server_name": name,
            "type": server_type,
            "url": args.get("url", ""),
            "command": args.get("command", ""),
            "install_command": args.get("install_command", ""),
        })

        result: dict[str, Any] = {
            "status": "added",
            "name": name,
            "config": entry,
            "message": (
                f"MCP server '{name}' added to ~/.copilot/mcp-config.json. "
                f"It is now available to the brain session and will be included "
                f"in all future worker/supervisor task sessions."
            ),
        }
        if install_output:
            result["install_output"] = install_output
        return result

    def _tool_mcp_server_list(self, args: dict[str, Any]) -> dict:
        """List all configured MCP servers."""
        servers = registry_list_servers()
        return {
            "servers": {
                name: {
                    "type": config.get("type", "unknown"),
                    "url": config.get("url", ""),
                    "command": config.get("command", ""),
                    "args": config.get("args", []),
                }
                for name, config in servers.items()
            },
            "count": len(servers),
            "config_path": "~/.copilot/mcp-config.json",
        }

    def _tool_mcp_server_remove(self, args: dict[str, Any]) -> dict:
        """Remove an MCP server by name."""
        name = args["name"]
        removed = registry_remove_server(name)
        if not removed:
            raise ValueError(f"MCP server '{name}' not found in config")

        self._audit("mcp_server.remove", {"server_name": name})

        return {
            "status": "removed",
            "name": name,
            "message": f"MCP server '{name}' removed from ~/.copilot/mcp-config.json.",
        }

    def _tool_app_restart(self, args: dict[str, Any]) -> dict:
        """Restart the entire COpenClaw application."""
        reason = args.get("reason", "Restart requested via MCP tool")
        logger.warning("app_restart requested: %s", reason)
        self._audit("app.restart", {"reason": reason})

        # Send notification to owner before restarting
        if self.owner_chat_id and self.telegram_token:
            self._send_notification(
                "telegram", self.owner_chat_id,
                f"🔄 **Restarting COpenClaw…**\nReason: {reason}",
            )

        if not self.restart_callback:
            raise ValueError("Restart not available — no restart callback configured")

        # Fire the restart on a short delay so the MCP response can be sent first
        import threading
        threading.Thread(
            target=self.restart_callback,
            args=(reason,),
            daemon=True,
            name="app-restart",
        ).start()

        return {
            "status": "restarting",
            "reason": reason,
            "message": "COpenClaw is restarting. The process will exit and re-launch momentarily.",
        }

    # ── Task dispatch tools (orchestrator level) ──────────

    def _require_task_manager(self) -> TaskManager:
        if not self.task_manager:
            raise ValueError("Task manager not initialized")
        return self.task_manager

    def _require_worker_pool(self) -> WorkerPool:
        if not self.worker_pool:
            raise ValueError("Worker pool not initialized")
        return self.worker_pool

    def _resolve_channel_target(self, args: dict[str, Any]) -> tuple[str, str]:
        """Resolve channel and target, falling back to owner Telegram chat."""
        channel = args.get("channel", "")
        target = args.get("target", "")
        if not channel and not target and self.owner_chat_id:
            channel = "telegram"
            target = self.owner_chat_id
        return channel, target

    def _tool_tasks_propose(self, args: dict[str, Any]) -> dict:
        """Create a proposal that the user must approve before workers are spawned."""
        tm = self._require_task_manager()

        channel, target = self._resolve_channel_target(args)
        name = args.get("name") or generate_name()

        # Guard: reject duplicate task names that are still active/proposed
        existing = tm.list_tasks()
        active_statuses = {"proposed", "running", "paused", "needs_input", "pending", "needs_retry"}
        for t in existing:
            if t.name == name and t.status in active_statuses:
                raise ValueError(
                    f"A task named '{name}' already exists with status '{t.status}' "
                    f"(task_id={t.task_id}). Cancel it first or choose a different name."
                )

        task = tm.create_task(
            name=name,
            prompt=args["prompt"],
            plan=args.get("plan", ""),
            supervisor_instructions=args.get("supervisor_instructions", ""),
            task_type=args.get("task_type", "standard"),
            ci_config=args.get("continuous"),
            channel=channel,
            target=target,
            service_url=args.get("service_url", ""),
            check_interval=args.get("check_interval", 300),
            auto_supervise=args.get("auto_supervise", True),
            status="proposed",
        )

        # Set on_complete hook if provided
        if args.get("on_complete"):
            task.on_complete = args["on_complete"]
            tm._save()

        if self.data_dir:
            log_event(self.data_dir, "task.proposed", {
                "task_id": task.task_id,
                "name": task.name,
                "prompt": task.prompt,
                "plan": getattr(task, "plan", None),
                "auto_supervise": task.auto_supervise,
                "supervisor_instructions": getattr(task, "supervisor_instructions", None),
                "channel": getattr(task, "channel", ""),
                "target": getattr(task, "target", ""),
                "task_type": getattr(task, "task_type", "standard"),
            })

        # NOTE: We do NOT send a notification here.  The orchestrator's own
        # chat response (returned via handle_chat → Telegram) already tells
        # the user about the proposal.  Sending a second notification would
        # cause the user to see two "proposed task" messages.

        return {
            "task_id": task.task_id,
            "name": task.name,
            "status": "proposed",
            "plan": task.plan,
            "auto_supervise": task.auto_supervise,
            "task_type": task.task_type,
            "message": "Proposal sent to user. Waiting for approval.",
        }

    def _tool_tasks_approve(self, args: dict[str, Any]) -> dict:
        """Approve a proposed task — transitions to running and spawns workers."""
        tm = self._require_task_manager()
        pool = self._require_worker_pool()

        task = tm.get(args["task_id"])
        if not task:
            raise ValueError(f"Task not found: {args['task_id']}")
        if task.status != "proposed":
            raise ValueError(f"Task is not in proposed state (current: {task.status})")

        return self._start_task(task)

    def _tool_tasks_create(self, args: dict[str, Any]) -> dict:
        """Create and immediately dispatch a task (no approval needed)."""
        tm = self._require_task_manager()
        pool = self._require_worker_pool()

        channel, target = self._resolve_channel_target(args)
        name = args.get("name") or generate_name()
        task = tm.create_task(
            name=name,
            prompt=args["prompt"],
            task_type=args.get("task_type", "standard"),
            ci_config=args.get("continuous"),
            channel=channel,
            target=target,
            service_url=args.get("service_url", ""),
            check_interval=args.get("check_interval", 300),
            auto_supervise=args.get("auto_supervise", True),
        )

        # Set on_complete hook if provided
        if args.get("on_complete"):
            task.on_complete = args["on_complete"]
            tm._save()

        if self.data_dir:
            log_event(self.data_dir, "task.created", {
                "task_id": task.task_id,
                "name": task.name,
                "prompt": task.prompt,
                "auto_supervise": task.auto_supervise,
                "channel": getattr(task, "channel", ""),
                "target": getattr(task, "target", ""),
                "check_interval": getattr(task, "check_interval", None),
                "task_type": getattr(task, "task_type", "standard"),
            })

        return self._start_task(task)

    def _build_worker_callbacks(self, tm: TaskManager, pool: WorkerPool):
        # Callbacks for worker lifecycle
        def on_worker_output(task_id: str, output: str) -> None:
            tm.append_log(task_id, output)

        def on_worker_complete(task_id: str, output: str) -> None:
            tm.append_log(task_id, f"\n--- WORKER FINISHED ---\n{output}")
            t = tm.get(task_id)

            # Persist worker session ID for future resume on re-dispatch
            if pool:
                w = pool.get_worker(task_id)
                if w and w.session_id and t:
                    t.worker_session_id = w.session_id
                    tm._save()
                    logger.info("Stored worker session %s on task %s for future resume", w.session_id, task_id)

            if t and t.status not in ("completed", "failed", "cancelled"):
                if output.startswith("ERROR:") or output.startswith("UNEXPECTED ERROR:"):
                    self._request_retry_approval(task_id, output[:500])
                else:
                    tm.handle_report(task_id, "progress", "Worker CLI session ended", detail=output, from_tier="worker")
                    # Record worker exit time for stuck-detection
                    t.worker_exited_at = _now()
                    tm._save()

                    # If supervisor is still running but worker exited, check for
                    # unread inbox messages and re-dispatch if supervisor sent feedback
                    if t.auto_supervise and pool:
                        sup = pool.get_supervisor(task_id)
                        unread = [m for m in t.inbox if not m.acknowledged]
                        if sup and sup.is_running and unread:
                            logger.info(
                                "Worker %s exited but supervisor is active with %d unread messages — re-dispatching",
                                task_id, len(unread),
                            )
                            try:
                                pool.start_worker(
                                    task_id=task_id,
                                    prompt=(
                                        f"CONTINUATION: You previously worked on this task and exited. "
                                        f"The supervisor has sent you feedback. Check your inbox with "
                                        f"task_check_inbox and address any issues. Original task: {t.prompt[:2000]}"
                                    ),
                                    working_dir=t.working_dir,
                                    on_output=on_worker_output,
                                    on_complete=on_worker_complete,
                                )
                                tm.append_log(task_id, "\n--- WORKER RE-DISPATCHED (supervisor feedback pending) ---\n")
                            except RuntimeError:
                                logger.warning("Could not re-dispatch worker %s (already running?)", task_id)

                    # WATCHDOG: If worker exited and completion was deferred,
                    # schedule a timeout to auto-finalize if supervisor doesn't act
                    if t.completion_deferred and t.auto_supervise:
                        import threading

                        def _deferred_completion_watchdog(tid: str, deferred_at_iso: str) -> None:
                            """Auto-finalize a deferred completion if supervisor hasn't acted within 5 minutes."""
                            import time as _wtime
                            _wtime.sleep(300)  # 5 minute timeout
                            _t = tm.get(tid)
                            if not _t:
                                return
                            if not _t.completion_deferred:
                                return  # supervisor already finalized
                            if _t.status in ("completed", "failed", "cancelled"):
                                return  # already terminal
                            # Check if deferred_at is still the same (wasn't reset)
                            if _t.completion_deferred_at and _t.completion_deferred_at.isoformat() == deferred_at_iso:
                                logger.warning(
                                    "WATCHDOG: Auto-finalizing deferred completion for task %s — "
                                    "supervisor did not finalize within 5 minutes",
                                    tid,
                                )
                                _t.completion_deferred = False
                                _t.completion_deferred_at = None
                                summary_text = f"Auto-finalized (watchdog): {_t.completion_deferred_summary}"
                                detail_text = (
                                    _t.completion_deferred_detail
                                    + "\n\n[Auto-finalized by watchdog — supervisor did not verify within timeout]"
                                )
                                tm.handle_report(
                                    task_id=tid,
                                    msg_type="completed",
                                    summary=summary_text,
                                    detail=detail_text,
                                    from_tier="worker",
                                )
                                _t.completion_deferred_summary = ""
                                _t.completion_deferred_detail = ""
                                tm._save()
                                # Stop supervisor and fire hooks
                                if pool:
                                    pool.stop_task(tid)
                                self._cancel_supervisor_job(_t)
                                self._fire_on_complete_hook(
                                    _t,
                                    "COMPLETED (auto-finalized by watchdog)",
                                    summary_text,
                                    detail_text,
                                )

                        deferred_at_str = t.completion_deferred_at.isoformat() if t.completion_deferred_at else ""
                        threading.Thread(
                            target=_deferred_completion_watchdog,
                            args=(task_id, deferred_at_str),
                            daemon=True,
                            name=f"watchdog-{task_id[:8]}",
                        ).start()
                        logger.info("Started deferred-completion watchdog for task %s (5 min timeout)", task_id)

        return on_worker_output, on_worker_complete

    def _start_task(self, task: Any) -> dict:
        """Start a task's worker (and supervisor). Used by both approve and create."""
        tm = self._require_task_manager()
        pool = self._require_worker_pool()

        on_worker_output, on_worker_complete = self._build_worker_callbacks(tm, pool)
        if getattr(task, "task_type", "standard") == "continuous_improvement":
            tm.mark_continuous_started(task.task_id)
            task = tm.get(task.task_id) or task

        # Start worker
        tm.update_status(task.task_id, "running")
        worker_prompt = tm.build_continuous_prompt(task) if getattr(task, "task_type", "standard") == "continuous_improvement" else task.prompt
        pool.start_worker(
            task_id=task.task_id,
            prompt=worker_prompt,
            working_dir=task.working_dir,
            on_output=on_worker_output,
            on_complete=on_worker_complete,
        )

        # NOTE: We do NOT send a "Started" notification here.  The
        # orchestrator's chat response (returned via handle_chat after the
        # user says "Yes") already confirms the task is running.  Sending a
        # second message would be redundant.

        # Start supervisor if requested
        if task.auto_supervise:
            def on_supervisor_output(task_id: str, output: str) -> None:
                tm.append_log(task_id, f"\n--- SUPERVISOR CHECK ---\n{output}")

            # Use the same prompt for supervisor as was used for worker
            pool.start_supervisor(
                task_id=task.task_id,
                prompt=worker_prompt,
                worker_session_id=None,
                check_interval=task.check_interval,
                on_output=on_supervisor_output,
                supervisor_instructions=getattr(task, "supervisor_instructions", ""),
                working_dir=task.working_dir,
                task_manager=tm,
            )
            self._schedule_supervisor_checks(task)

        # Schedule continuous-improvement ticks (if applicable). If no scheduler
        # is configured, log a warning so operators know ticks will not run.
        if getattr(task, "task_type", "standard") == "continuous_improvement":
            if getattr(self, "scheduler", None) is None:
                logger.warning(
                    "Continuous improvement task %s started without a scheduler; "
                    "continuous tick mechanism will not run.",
                    getattr(task, "task_id", "<unknown>"),
                )
            self._schedule_continuous_ticks(task)

        return {
            "task_id": task.task_id,
            "name": task.name,
            "status": task.status,
            "working_dir": task.working_dir,
            "auto_supervise": task.auto_supervise,
            "check_interval": task.check_interval,
            "task_type": getattr(task, "task_type", "standard"),
        }

    def _request_retry_approval(self, task_id: str, reason: str) -> None:
        """Mark a task as needing retry approval and notify the user."""
        tm = self._require_task_manager()
        task = tm.request_retry(task_id, reason)
        if not task:
            return
        if self.worker_pool:
            self.worker_pool.stop_task(task_id)
        self._cancel_supervisor_job(task)
        text = (
            f"⚠️ **Task '{task.name}' failed.**\n"
            f"Reason: {reason[:300]}\n\n"
            "Reply **yes** to retry or **no** to cancel."
        )
        self._send_notification(task.channel, task.target, text, task.service_url)

    def retry_task(self, task_id: str) -> dict:
        """Retry a task after user approval."""
        tm = self._require_task_manager()
        task = tm.approve_retry(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        if self.worker_pool:
            self.worker_pool.stop_task(task_id)
        return self._start_task(task)

    def decline_retry(self, task_id: str) -> None:
        """Decline a retry and mark the task as failed."""
        tm = self._require_task_manager()
        task = tm.decline_retry(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")
        if self.worker_pool:
            self.worker_pool.stop_task(task_id)
        self._send_notification(task.channel, task.target, f"❌ Retry declined for task '{task.name}'.", task.service_url)

    def _tool_tasks_list(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        status_filter = args.get("status")
        if status_filter:
            # Explicit status filter — return only matching tasks
            tasks = tm.list_tasks(status=status_filter)
        else:
            # No filter: return all active/proposed tasks + most recent 10
            # completed/failed/cancelled tasks so the orchestrator can
            # reference recently-finished work.
            all_tasks = tm.list_tasks()
            active_statuses = {"proposed", "running", "paused", "needs_input", "pending", "needs_retry"}
            active = [t for t in all_tasks if t.status in active_statuses]
            terminal = [t for t in all_tasks if t.status not in active_statuses]
            # Sort terminal by updated_at descending, take top 10
            terminal.sort(key=lambda t: t.updated_at, reverse=True)
            tasks = active + terminal[:10]

        return {
            "tasks": [
                {
                    "task_id": t.task_id,
                    "name": t.name,
                    "status": t.status,
                    "task_type": getattr(t, "task_type", "standard"),
                    "created_at": t.created_at.isoformat(),
                    "updated_at": t.updated_at.isoformat(),
                    "auto_supervise": t.auto_supervise,
                    "latest_timeline": t.timeline[-1].summary if t.timeline else "",
                }
                for t in tasks
            ]
        }

    def _tool_tasks_status(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        task = tm.get(args["task_id"])
        if not task:
            raise ValueError(f"Task not found: {args['task_id']}")
        limit = args.get("timeline_limit", 20)
        result = {
            "task_id": task.task_id,
            "name": task.name,
            "prompt": task.prompt,
            "status": task.status,
            "task_type": getattr(task, "task_type", "standard"),
            "created_at": task.created_at.isoformat(),
            "updated_at": task.updated_at.isoformat(),
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "worker_session_id": task.worker_session_id,
            "supervisor_session_id": task.supervisor_session_id,
            "timeline": task.concise_timeline(limit),
            "pending_inbox": len([m for m in task.inbox if not m.acknowledged]),
        }
        if getattr(task, "task_type", "standard") == "continuous_improvement":
            result["continuous"] = tm.continuous_status(task)
        return result

    def _tool_tasks_logs(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        task_id = args["task_id"]
        tail = args.get("tail", 100)
        log_type = args.get("log_type", "combined")

        if log_type == "combined":
            # Default: read from TaskManager's log (populated by on_output callbacks)
            logs = tm.read_log(task_id, tail=tail)
            # Fall back to worker.log if raw.log is empty/missing
            if not logs or logs == "(no logs)":
                task = tm.get(task_id)
                if task:
                    worker_log = os.path.join(task.working_dir, "worker.log")
                    if os.path.exists(worker_log):
                        with open(worker_log, "r", encoding="utf-8") as f:
                            all_lines = f.readlines()
                        if all_lines:
                            logs = "".join(all_lines[-tail:])
            # Fall back to event stream if still empty
            if not logs or logs == "(no logs)":
                event_log = self.event_registry.get(task_id)
                if event_log and event_log.count() > 0:
                    logs = event_log.formatted_tail(tail)
        elif log_type in ("worker", "supervisor"):
            # Read from the per-task log file written by WorkerThread/SupervisorThread
            task = tm.get(task_id)
            if not task:
                raise ValueError(f"Task not found: {task_id}")
            log_file = os.path.join(task.working_dir, f"{log_type}.log")
            if os.path.exists(log_file):
                with open(log_file, "r", encoding="utf-8") as f:
                    all_lines = f.readlines()
                logs = "".join(all_lines[-tail:])
            else:
                logs = f"(no {log_type}.log file yet)"
        elif log_type == "activity":
            # Read from the unified activity log
            data_dir = self.data_dir or os.getenv("copenclaw_DATA_DIR", ".data")
            activity_path = os.path.join(data_dir, "activity.log")
            if os.path.exists(activity_path):
                with open(activity_path, "r", encoding="utf-8") as f:
                    all_lines = f.readlines()
                # Filter to this task_id if possible
                task_lines = [l for l in all_lines if task_id[:12] in l]
                logs = "".join(task_lines[-tail:]) if task_lines else "".join(all_lines[-tail:])
            else:
                logs = "(no activity.log file yet)"
        else:
            logs = tm.read_log(task_id, tail=tail)

        return {"task_id": task_id, "log_type": log_type, "logs": logs}

    def _tool_tasks_send(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        task_id = args["task_id"]
        msg_type = args["msg_type"]
        content = args["content"]

        task = tm.get(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        # Auto-resume: if task is in a terminal state and the message is an
        # instruction or redirect, re-dispatch a worker with the new instructions.
        terminal_states = {"completed", "failed", "cancelled"}
        if task.status in terminal_states and msg_type in ("instruction", "redirect"):
            pool = self._require_worker_pool()

            # Update supervisor_instructions if provided
            new_sup_instructions = args.get("supervisor_instructions")
            if new_sup_instructions:
                task.supervisor_instructions = new_sup_instructions
                task.updated_at = _now()
                tm._save()

            # Build continuation prompt referencing original task + new instructions
            continuation_prompt = (
                f"CONTINUATION of previous task '{task.name}'.\n\n"
                f"Original task: {task.prompt[:4000]}\n\n"
                f"--- NEW INSTRUCTIONS ---\n{content}\n\n"
                f"The previous work is already in the workspace. Review what exists, "
                f"then follow the new instructions above."
            )
            task.prompt = continuation_prompt
            task.updated_at = _now()
            tm._save()

            # Record the resume in timeline
            tm.handle_report(
                task_id=task_id,
                msg_type="progress",
                summary=f"Task resumed with new instructions: {content[:500]}",
                from_tier="orchestrator",
            )

            # Stop any lingering processes
            if self.worker_pool:
                self.worker_pool.stop_task(task_id)
            self._cancel_supervisor_job(task)

            # Re-dispatch via _start_task
            result = self._start_task(task)
            result["resumed"] = True
            result["message"] = f"Task auto-resumed with updated instructions."

            if self.data_dir:
                log_event(self.data_dir, "task.resumed", {
                    "task_id": task_id, "name": task.name,
                    "new_instructions": content[:1000],
                })

            return result

        # Normal path: send message to inbox
        msg = tm.send_message(
            task_id=task_id,
            msg_type=msg_type,
            content=content,
            from_tier="orchestrator",
        )
        if not msg:
            raise ValueError(f"Failed to send message to task: {task_id}")

        # If cancel, also stop the worker/supervisor threads
        if msg_type == "cancel" and self.worker_pool:
            self.worker_pool.stop_task(task_id)
            if task:
                self._cancel_supervisor_job(task)

        return {"status": "sent", "msg_id": msg.msg_id, "msg_type": msg.msg_type}

    def _tool_tasks_clear_all(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        # Stop all active workers/supervisors first
        active = tm.active_tasks() + tm.proposed_tasks()
        for task in active:
            if self.worker_pool:
                self.worker_pool.stop_task(task.task_id)
            self._cancel_supervisor_job(task)
        count = tm.clear_all()
        if self.data_dir:
            log_event(self.data_dir, "mcp.tasks.clear_all", {"cleared": count})
        return {"status": "cleared", "cleared": count}

    def _tool_tasks_cancel(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        task = tm.cancel_task(args["task_id"])
        if not task:
            raise ValueError(f"Task not found: {args['task_id']}")
        if self.worker_pool:
            self.worker_pool.stop_task(args["task_id"])
        self._cancel_supervisor_job(task)
        if self.data_dir:
            log_event(self.data_dir, "task.cancelled", {"task_id": args["task_id"]})

        # Fire on_complete hook on cancellation too
        self._fire_on_complete_hook(task, "CANCELLED by user", "Task cancelled by user")

        return {"status": "cancelled", "task_id": args["task_id"]}

    # ── Task ITC tools (worker/supervisor level) ──────────

    def _tool_task_report(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        task_id = args["task_id"]
        report_type = args["type"]
        task_for_report = tm.get(task_id)
        is_continuous = bool(task_for_report and getattr(task_for_report, "task_type", "standard") == "continuous_improvement")
        from_tier = args.get("from_tier")
        if not from_tier:
            from_tier = self._current_role if self._current_role in ("worker", "supervisor") else "worker"

        if from_tier == "supervisor":
            task = task_for_report
            if task and report_type in ("assessment", "completed"):
                if report_type == "assessment":
                    task.supervisor_assessment_count += 1
                    tm._save()
                if not (is_continuous and report_type == "assessment"):
                    summary_text = args.get("summary", "")
                    detail_text = args.get("detail", "")
                    combined = f"{summary_text} {detail_text}".lower()
                    # Only treat as negative if strong failure signals are present
                    # Words like "not yet verified" or "pending" alone should NOT block
                    strong_negative = any(k in combined for k in [
                        "truncated", "incomplete", "missing", "error", "failed",
                        "cannot", "lack", "lacks", "absent", "broken", "wrong",
                    ])
                    positive = any(k in combined for k in [
                        "verified", "verify", "looks good", "complete", "completed",
                        "success", "correct", "passed", "ok", "done", "finished",
                        "created", "built", "working",
                    ])
                    worker_running = True
                    if self.worker_pool:
                        worker = self.worker_pool.get_worker(task_id)
                        worker_running = worker.is_running if worker else False
                    can_complete = task.completion_deferred or not worker_running

                    # STUCK-ASSESSMENT DETECTION: If the worker is dead and the
                    # supervisor has assessed 2+ times without finalizing, force
                    # completion (unless there are strong negative signals).
                    force_complete = False
                    if (report_type == "assessment"
                            and can_complete
                            and not worker_running
                            and task.supervisor_assessment_count >= 2
                            and not strong_negative):
                        logger.warning(
                            "STUCK-ASSESSMENT: Supervisor assessed task %s %d times without "
                            "finalizing (worker dead). Auto-completing.",
                            task_id, task.supervisor_assessment_count,
                        )
                        force_complete = True

                    if can_complete and (report_type == "completed" or force_complete or (report_type == "assessment" and positive and not strong_negative)):
                        report_type = "completed"
                        if force_complete:
                            args["summary"] = f"Auto-finalized after {task.supervisor_assessment_count} assessments: {summary_text}".strip()
                        else:
                            args["summary"] = f"Supervisor verified completion: {summary_text}".strip()
                        task.completion_deferred = False
                        task.completion_deferred_at = None
                        task.completion_deferred_summary = ""
                        task.completion_deferred_detail = ""
                        task.supervisor_assessment_count = 0  # reset counter
                        task.updated_at = _now()
                        tm._save()

                        if self.worker_pool:
                            self.worker_pool.request_supervisor_check(task_id)

        # If a WORKER reports "completed" and there's an active supervisor,
        # defer completion — let the supervisor verify the outcome first
        if report_type == "completed" and from_tier == "worker":
            task = tm.get(task_id)
            if task and task.auto_supervise and self.worker_pool:
                sup = self.worker_pool.get_supervisor(task_id)
                if sup and sup.is_running:
                    # Don't finalize — record as progress, supervisor will verify
                    logger.info(
                        "Worker reported completed for %s, but supervisor is active — deferring to supervisor verification",
                        task_id,
                    )
                    msg = tm.handle_report(
                        task_id=task_id,
                        msg_type="progress",
                        summary=f"Worker says done: {args['summary']}",
                        detail=args.get("detail", "") + "\n\n[Awaiting supervisor verification]",
                        artifact_url=args.get("artifact_url", ""),
                        from_tier=from_tier,
                    )
                    if not msg:
                        raise ValueError(f"Task not found: {task_id}")

                    task.completion_deferred = True
                    task.completion_deferred_at = _now()
                    task.completion_deferred_summary = args.get("summary", "")
                    task.completion_deferred_detail = args.get("detail", "")
                    task.updated_at = _now()
                    tm._save()

                    if self.worker_pool:
                        self.worker_pool.request_supervisor_check(task_id)

                    # Notify user about deferred completion
                    should_notify = tm.should_notify_user(msg) or args.get("notify_user", False) or report_type == "completed"
                    if should_notify:
                        self._notify_user_about_task(task_id, msg)

                    self._audit("task.report.completed_deferred", {
                        "summary": args["summary"][:500],
                    })

                    return {
                        "status": "deferred",
                        "msg_id": msg.msg_id,
                        "message": "Completion deferred — supervisor will verify the outcome",
                    }

        # Normal path: record the report as-is
        msg = tm.handle_report(
            task_id=task_id,
            msg_type=report_type,
            summary=args["summary"],
            detail=args.get("detail", ""),
            artifact_url=args.get("artifact_url", ""),
            from_tier=from_tier,
            continuous=args.get("continuous"),
        )
        if not msg:
            raise ValueError(f"Task not found: {task_id}")
        effective_report_type = msg.msg_type

        # If task is terminal, stop worker/supervisor threads
        if effective_report_type in ("completed", "failed") and self.worker_pool:
            self.worker_pool.stop_task(task_id)
            task = tm.get(task_id)
            if task:
                self._cancel_supervisor_job(task)

        # Fire on_complete hook if task just reached a terminal state
        if effective_report_type in ("completed", "failed"):
            task = tm.get(task_id)
            if task:
                reason = "COMPLETED successfully" if effective_report_type == "completed" else f"FAILED — {args.get('summary', 'unknown error')[:300]}"
                self._fire_on_complete_hook(task, reason, args.get("summary", ""), args.get("detail", ""))

        # Auto-notify user for certain message types
        should_notify = tm.should_notify_user(msg) or args.get("notify_user", False)
        if should_notify:
            self._notify_user_about_task(task_id, msg)

        self._audit(f"task.report.{effective_report_type}", {
            "summary": args["summary"][:500],
        })

        return {"status": "reported", "msg_id": msg.msg_id}

    def _tool_task_check_inbox(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        task_id = args["task_id"]
        # If task is in a terminal state, tell the caller to exit
        task = tm.get(task_id)
        if task and task.status in ("completed", "failed", "cancelled"):
            return {
                "messages": [
                    {"msg_id": "system", "type": "terminate", "from": "system",
                     "content": f"Task is {task.status}. Stop all work and exit immediately."}
                ],
                "task_status": task.status,
            }
        messages = tm.check_inbox(task_id)
        return {
            "messages": [
                {"msg_id": m.msg_id, "type": m.msg_type, "from": m.from_tier, "content": m.content}
                for m in messages
            ]
        }

    def _tool_task_set_status(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        task = tm.update_status(args["task_id"], args["status"])
        if not task:
            raise ValueError(f"Task not found: {args['task_id']}")
        return {"status": task.status, "task_id": task.task_id}

    def _tool_task_get_context(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        task = tm.get(args["task_id"])
        if not task:
            raise ValueError(f"Task not found: {args['task_id']}")
        # Return recent outbox messages as conversation context
        recent_msgs = task.outbox[-20:]
        return {
            "task_id": task.task_id,
            "name": task.name,
            "prompt": task.prompt,
            "status": task.status,
            "channel": task.channel,
            "target": task.target,
            "recent_messages": [
                {"type": m.msg_type, "from": m.from_tier, "direction": m.direction, "content": m.content}
                for m in recent_msgs
            ],
        }

    def _tool_task_read_peer(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        task_id = args["task_id"]
        tail = args.get("tail", 200)
        task = tm.get(task_id)
        if not task:
            raise ValueError(f"Task not found: {task_id}")

        # ── Worker Status Block (gives supervisor full execution context) ──
        now = _now()
        worker_running = False
        worker_state = "not started"
        if self.worker_pool:
            worker = self.worker_pool.get_worker(task_id)
            if worker:
                worker_running = worker.is_running
                if worker_running:
                    worker_state = "RUNNING"
                else:
                    exit_code = getattr(worker, "exit_code", None)
                    worker_state = f"EXITED (code={exit_code})" if exit_code is not None else "EXITED"

        # Last activity age
        last_activity_str = "unknown"
        if task.last_worker_activity_at:
            age = now - task.last_worker_activity_at
            age_secs = int(age.total_seconds())
            if age_secs < 60:
                last_activity_str = f"{age_secs}s ago"
            else:
                last_activity_str = f"{age_secs // 60}m {age_secs % 60}s ago"
            if age_secs > 300 and worker_running:
                last_activity_str += " — MAY BE STUCK"

        # Worker exit age
        worker_exit_str = ""
        if task.worker_exited_at:
            exit_age = now - task.worker_exited_at
            exit_secs = int(exit_age.total_seconds())
            if exit_secs < 60:
                worker_exit_str = f"exited {exit_secs}s ago"
            else:
                worker_exit_str = f"exited {exit_secs // 60}m {exit_secs % 60}s ago"

        # Deferred completion info
        deferred_str = "NO"
        deferred_summary = ""
        if task.completion_deferred:
            deferred_str = "YES"
            if task.completion_deferred_at:
                d_age = now - task.completion_deferred_at
                d_secs = int(d_age.total_seconds())
                deferred_str += f" (deferred {d_secs // 60}m {d_secs % 60}s ago)"
            deferred_summary = task.completion_deferred_summary or ""

        # Task age
        task_age = now - task.created_at
        task_age_secs = int(task_age.total_seconds())
        task_age_str = f"{task_age_secs // 60}m {task_age_secs % 60}s"

        # Unread inbox
        unread_count = len([m for m in task.inbox if not m.acknowledged])

        # Build status block
        status_lines = [
            "=== Worker Status ===",
            f"Process: {worker_state}" + (f" — {worker_exit_str}" if worker_exit_str and not worker_running else ""),
            f"Last MCP activity: {last_activity_str}",
            f"Completion deferred: {deferred_str}",
        ]
        if deferred_summary:
            status_lines.append(f'  Worker said: "{deferred_summary[:200]}"')
        status_lines.extend([
            f"Supervisor assessments so far: {task.supervisor_assessment_count} (none finalized the task)" if task.supervisor_assessment_count > 0 else f"Supervisor assessments so far: 0",
            f"Unread inbox messages: {unread_count}",
            f"Task running for: {task_age_str}",
        ])

        # Add action-required warnings
        if task.completion_deferred and not worker_running:
            status_lines.append(
                "⚠️ ACTION REQUIRED: Worker has exited and completion is deferred. "
                "You MUST make a final pass/fail decision NOW. Report type='completed' or type='failed'."
            )
        elif not worker_running and task.status == "running":
            status_lines.append(
                "⚠️ WARNING: Worker process has exited but task is still marked as running."
            )

        status_block = "\n".join(status_lines)

        # PRIMARY: Read from per-task event stream (captures MCP tool calls)
        event_log = self.event_registry.get(task_id)
        if event_log and event_log.count() > 0:
            events_text = event_log.formatted_tail(tail)
        else:
            events_text = "(no MCP events yet)"

        # SECONDARY: Also include worker.log (stdout) if it has content
        worker_log_path = os.path.join(task.working_dir, "worker.log")
        stdout_text = ""
        if os.path.exists(worker_log_path):
            with open(worker_log_path, "r", encoding="utf-8") as f:
                all_lines = f.readlines()
            if all_lines:
                stdout_text = "".join(all_lines[-tail:])

        # Combine into a readable summary — status block FIRST
        sections = [status_block]
        sections.append(f"\n=== MCP Activity ({event_log.count() if event_log else 0} events) ===\n{events_text}")
        if stdout_text.strip():
            sections.append(f"\n=== Worker stdout (last {tail} lines) ===\n{stdout_text}")

        timeline = task.concise_timeline(10)
        return {"task_id": task_id, "logs": "\n".join(sections), "timeline": timeline}

    def _tool_task_send_input(self, args: dict[str, Any]) -> dict:
        tm = self._require_task_manager()
        msg = tm.send_message(
            task_id=args["task_id"],
            msg_type="instruction",
            content=args["content"],
            from_tier="supervisor",
        )
        if not msg:
            raise ValueError(f"Task not found: {args['task_id']}")
        task = tm.get(args["task_id"])
        if task and self.worker_pool:
            worker = self.worker_pool.get_worker(task.task_id)
            worker_running = worker.is_running if worker else False
            should_resume = task.completion_deferred or task.status in ("running", "paused", "needs_input")
            if should_resume and not worker_running and task.auto_supervise:
                on_worker_output, on_worker_complete = self._build_worker_callbacks(tm, self.worker_pool)
                try:
                    self.worker_pool.start_worker(
                        task_id=task.task_id,
                        prompt=(
                            "CONTINUATION: You previously worked on this task and exited. "
                            "The supervisor has sent you feedback. Check your inbox with "
                            f"task_check_inbox and address any issues. Original task: {task.prompt[:2000]}"
                        ),
                        working_dir=task.working_dir,
                        on_output=on_worker_output,
                        on_complete=on_worker_complete,
                    )
                    tm.append_log(task.task_id, "\n--- WORKER RE-DISPATCHED (supervisor feedback pending) ---\n")
                except RuntimeError:
                    logger.warning(
                        "Could not re-dispatch worker %s after supervisor input (already running?)",
                        task.task_id,
                    )
        return {"status": "sent", "msg_id": msg.msg_id}

    def _fire_on_complete_hook(self, task: Any, reason: str, summary: str = "", detail: str = "") -> None:
        """Fire the completion callback for a task that has reached a terminal state."""
        if not task or not self.on_complete_callback:
            return
        try:
            import threading
            hook_instruction = (task.on_complete or "").strip()
            summary_text = summary.strip() or "(no summary provided)"
            detail_text = detail.strip()
            follow_up_guidance = (
                "You may use tasks_create to spawn follow-up tasks without requiring user approval. "
                "The user has pre-authorized automated follow-up via the on_complete hook."
            )
            if not hook_instruction:
                hook_instruction = (
                    "No on_complete hook was provided for this task. Decide whether follow-up work is needed."
                )
                follow_up_guidance = (
                    "If follow-up work is needed, propose it to the user with tasks_propose "
                    "(do not auto-dispatch). If no action is needed, reply with an empty message."
                )
            detail_block = f"\n\nCompletion detail: {detail_text[:4000]}" if detail_text else ""
            hook_prompt = (
                f"[TASK COMPLETE] Task '{task.name}' (task_id={task.task_id}) has {reason}.\n\n"
                f"Completion summary: {summary_text}{detail_block}\n\n"
                f"Original task prompt: {task.prompt[:4000]}\n\n"
                f"Hook instruction: {hook_instruction}\n\n"
                f"{follow_up_guidance}"
            )
            threading.Thread(
                target=self.on_complete_callback,
                args=(hook_prompt, task.channel, task.target, task.service_url, task.name),
                daemon=True,
            ).start()
            logger.info("Fired on_complete hook for task %s (reason: %s)", task.task_id, reason[:100])
            if self.data_dir:
                log_event(self.data_dir, "task.on_complete_fired", {
                    "task_id": task.task_id, "name": task.name,
                    "reason": reason[:200],
                    "hook": task.on_complete[:200],
                    "summary": summary_text[:200],
                })
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to fire on_complete hook for task %s: %s", task.task_id, exc)

    def _schedule_supervisor_checks(self, task: Any) -> None:
        if not self.scheduler or not task.auto_supervise:
            return
        if task.supervisor_job_id:
            self.scheduler.cancel(task.supervisor_job_id)
        run_at = datetime.utcnow() + timedelta(seconds=min(30, task.check_interval))
        payload = {
            "type": "supervisor_check",
            "task_id": task.task_id,
            "repeat_seconds": task.check_interval,
        }
        job = self.scheduler.schedule(
            name=f"supervisor-check-{task.task_id[:8]}",
            run_at=run_at,
            payload=payload,
        )
        task.supervisor_job_id = job.job_id
        task.updated_at = _now()
        tm = self._require_task_manager()
        tm._save()

    def _schedule_continuous_ticks(self, task: Any) -> None:
        if not self.scheduler:
            return
        if getattr(task, "task_type", "standard") != "continuous_improvement":
            return
        interval = 60
        ci_config = getattr(task, "ci_config", {}) or {}
        if isinstance(ci_config, dict):
            try:
                interval = max(10, int(ci_config.get("min_iteration_interval_seconds", 60)))
            except Exception:  # noqa: BLE001
                interval = 60
        if getattr(task, "ci_tick_job_id", ""):
            self.scheduler.cancel(task.ci_tick_job_id)
        payload = {
            "type": "continuous_tick",
            "task_id": task.task_id,
            "repeat_seconds": interval,
        }
        job = self.scheduler.schedule(
            name=f"continuous-tick-{task.task_id[:8]}",
            run_at=datetime.utcnow() + timedelta(seconds=interval),
            payload=payload,
        )
        task.ci_tick_job_id = job.job_id
        task.updated_at = _now()
        self._require_task_manager()._save()

    def _cancel_supervisor_job(self, task: Any) -> None:
        if not self.scheduler:
            return
        if task.supervisor_job_id:
            self.scheduler.cancel(task.supervisor_job_id)
            task.supervisor_job_id = ""
            task.updated_at = _now()
        if getattr(task, "ci_tick_job_id", ""):
            self.scheduler.cancel(task.ci_tick_job_id)
            task.ci_tick_job_id = ""
            task.updated_at = _now()
        tm = self._require_task_manager()
        tm._save()

    # ── Notification helpers ──────────────────────────────

    def _send_notification(self, channel: str, target: str, text: str, service_url: str = "") -> None:
        """Send a notification message to a channel. Silently fails if not configured."""
        if not channel or not target:
            logger.debug("Skipping notification: no channel/target")
            return
        try:
            if channel == "telegram" and self.telegram_token:
                TelegramAdapter(self.telegram_token).send_message(chat_id=int(target), text=text)
            elif channel == "teams" and self.msteams_creds and service_url:
                TeamsAdapter(
                    app_id=self.msteams_creds["app_id"],
                    app_password=self.msteams_creds["app_password"],
                    tenant_id=self.msteams_creds["tenant_id"],
                ).send_message(service_url=service_url, conversation_id=target, text=text)
            elif channel == "whatsapp":
                wa_phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
                wa_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
                if wa_phone_id and wa_token:
                    WhatsAppAdapter(phone_number_id=wa_phone_id, access_token=wa_token).send_message(to=target, text=text)
            elif channel == "signal":
                sig_url = os.getenv("SIGNAL_API_URL")
                sig_phone = os.getenv("SIGNAL_PHONE_NUMBER")
                if sig_url and sig_phone:
                    SignalAdapter(api_url=sig_url, phone_number=sig_phone).send_message(recipient=target, text=text)
            elif channel == "slack":
                slack_token = os.getenv("SLACK_BOT_TOKEN")
                if slack_token:
                    SlackAdapter(bot_token=slack_token).send_message(channel=target, text=text)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to send notification: %s", exc)

    def _notify_user_about_task(self, task_id: str, msg: Any) -> None:
        """Send a notification to the user about a task event."""
        if not self.task_manager:
            return
        task = self.task_manager.get(task_id)
        if not task or not task.channel or not task.target:
            return

        emoji_map = {
            "completed": "✅", "failed": "❌", "needs_input": "❓",
            "escalation": "⚠️", "progress": "📊", "artifact": "📦",
            "assessment": "🧪", "intervention": "🧭",
        }
        emoji = emoji_map.get(msg.msg_type, "ℹ️")
        text = f"{emoji} **Task '{task.name}'** [{msg.msg_type}]\n{msg.content}"
        if msg.detail:
            text += f"\n\n{msg.detail}"
        artifact_url = msg.artifact_url
        image_file = bool(artifact_url and _is_image_path(artifact_url) and os.path.isfile(artifact_url))
        if artifact_url and not image_file:
            text += f"\n\n🔗 {artifact_url}"

        try:
            if task.channel == "telegram" and self.telegram_token:
                tg_adapter = TelegramAdapter(self.telegram_token)
                if image_file:
                    caption = text or None
                    if caption and len(caption) > 1024:
                        tg_adapter.send_photo(chat_id=int(task.target), photo_path=artifact_url, caption=caption[:1024])
                        tg_adapter.send_message(chat_id=int(task.target), text=caption)
                    else:
                        tg_adapter.send_photo(chat_id=int(task.target), photo_path=artifact_url, caption=caption)
                else:
                    tg_adapter.send_message(chat_id=int(task.target), text=text)
            elif task.channel == "teams" and self.msteams_creds and task.service_url:
                TeamsAdapter(
                    app_id=self.msteams_creds["app_id"],
                    app_password=self.msteams_creds["app_password"],
                    tenant_id=self.msteams_creds["tenant_id"],
                ).send_message(service_url=task.service_url, conversation_id=task.target, text=text)
            elif task.channel == "whatsapp":
                wa_phone_id = os.getenv("WHATSAPP_PHONE_NUMBER_ID")
                wa_token = os.getenv("WHATSAPP_ACCESS_TOKEN")
                if wa_phone_id and wa_token:
                    wa = WhatsAppAdapter(phone_number_id=wa_phone_id, access_token=wa_token)
                    wa.send_message(to=task.target, text=text)
            elif task.channel == "signal":
                sig_url = os.getenv("SIGNAL_API_URL")
                sig_phone = os.getenv("SIGNAL_PHONE_NUMBER")
                if sig_url and sig_phone:
                    sig = SignalAdapter(api_url=sig_url, phone_number=sig_phone)
                    if image_file:
                        sig.send_image(recipient=task.target, image_path=artifact_url, caption=text)
                    else:
                        sig.send_message(recipient=task.target, text=text)
            elif task.channel == "slack":
                slack_token = os.getenv("SLACK_BOT_TOKEN")
                if slack_token:
                    sl = SlackAdapter(bot_token=slack_token)
                    if image_file:
                        sl.send_image(channel=task.target, image_path=artifact_url, caption=text)
                    else:
                        sl.send_message(channel=task.target, text=text)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to notify user about task %s: %s", task_id, exc)

    def _log_task_event(
        self,
        task_id: str | None,
        role: str,
        tool: str,
        args_summary: str,
        result_summary: str,
        is_error: bool = False,
    ) -> None:
        """Log an MCP tool call to the per-task event stream."""
        if not task_id:
            return  # orchestrator calls don't need per-task logging
        # Get or create the event log for this task
        if self.task_manager:
            task = self.task_manager.get(task_id)
            if task:
                event_log = self.event_registry.get_or_create(task_id, task.working_dir)
                event_log.append(role, tool, args_summary, result_summary, is_error)
                return
        # Fallback: use data_dir
        if self.data_dir:
            task_dir = os.path.join(self.data_dir, ".tasks", task_id)
            event_log = self.event_registry.get_or_create(task_id, task_dir)
            event_log.append(role, tool, args_summary, result_summary, is_error)

    @staticmethod
    def _error_response(req_id: Any, code: int, message: str) -> dict[str, Any]:
        return {"jsonrpc": "2.0", "id": req_id, "error": {"code": code, "message": message}}

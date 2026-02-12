"""Task dispatch and orchestration system.

Manages multi-session task execution with bidirectional inter-tier
communication (ITC) between orchestrator, workers, and supervisors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from typing import Dict, List, Optional
import uuid
import logging

logger = logging.getLogger("copenclaw.tasks")


def _now() -> datetime:
    return datetime.now(timezone.utc)


# ── Data models ──────────────────────────────────────────────

@dataclass
class TaskMessage:
    """A message in the bidirectional inter-tier communication protocol."""
    msg_id: str
    ts: datetime
    direction: str          # "up" | "down"
    msg_type: str           # progress, completed, failed, needs_input, question,
                            # artifact, assessment, intervention, escalation,
                            # instruction, input, pause, resume, redirect, cancel, priority
    from_tier: str          # "orchestrator" | "worker" | "supervisor" | "user"
    content: str
    detail: str = ""
    artifact_url: str = ""
    acknowledged: bool = False

    def to_dict(self) -> dict:
        return {
            "msg_id": self.msg_id,
            "ts": self.ts.isoformat(),
            "direction": self.direction,
            "msg_type": self.msg_type,
            "from_tier": self.from_tier,
            "content": self.content,
            "detail": self.detail,
            "artifact_url": self.artifact_url,
            "acknowledged": self.acknowledged,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TaskMessage:
        return cls(
            msg_id=d["msg_id"],
            ts=datetime.fromisoformat(d["ts"]),
            direction=d["direction"],
            msg_type=d["msg_type"],
            from_tier=d["from_tier"],
            content=d["content"],
            detail=d.get("detail", ""),
            artifact_url=d.get("artifact_url", ""),
            acknowledged=d.get("acknowledged", False),
        )


@dataclass
class TimelineEntry:
    """A concise summary entry in the task timeline."""
    ts: datetime
    event: str              # created, started, checkpoint, needs_input, supervised,
                            # completed, failed, cancelled, user_input, redirected
    summary: str
    detail: str = ""

    def to_dict(self) -> dict:
        return {
            "ts": self.ts.isoformat(),
            "event": self.event,
            "summary": self.summary,
            "detail": self.detail,
        }

    @classmethod
    def from_dict(cls, d: dict) -> TimelineEntry:
        return cls(
            ts=datetime.fromisoformat(d["ts"]),
            event=d["event"],
            summary=d["summary"],
            detail=d.get("detail", ""),
        )


# Valid task statuses
TASK_STATUSES = {"proposed", "pending", "running", "paused", "needs_input", "completed", "failed", "cancelled"}

# Message types that always notify the user
AUTO_NOTIFY_TYPES = {"completed", "failed", "needs_input", "escalation"}

# Upward message types (worker/supervisor → orchestrator)
UP_MSG_TYPES = {
    "progress", "completed", "failed", "needs_input",
    "question", "artifact", "assessment", "intervention", "escalation",
}

# Downward message types (orchestrator → worker/supervisor)
DOWN_MSG_TYPES = {
    "instruction", "input", "pause", "resume",
    "redirect", "cancel", "priority",
}


@dataclass
class Task:
    """A dispatched task with worker and optional supervisor sessions."""
    task_id: str
    name: str
    prompt: str
    status: str = "pending"             # proposed|pending|running|paused|needs_input|completed|failed|cancelled

    created_at: datetime = field(default_factory=_now)
    updated_at: datetime = field(default_factory=_now)
    completed_at: Optional[datetime] = None

    # Session tracking
    worker_session_id: Optional[str] = None
    supervisor_session_id: Optional[str] = None

    # Execution config
    working_dir: str = ""
    channel: str = ""                   # telegram | teams
    target: str = ""                    # chat_id or conversation_id
    service_url: str = ""               # for Teams

    # Plan (for proposed tasks awaiting approval)
    plan: str = ""                      # what the worker will do
    supervisor_instructions: str = ""   # what the supervisor should watch for

    # Supervision
    check_interval: int = 120           # seconds between supervisor checks
    auto_supervise: bool = True

    # Communication
    timeline: List[TimelineEntry] = field(default_factory=list)
    inbox: List[TaskMessage] = field(default_factory=list)       # pending downward messages
    outbox: List[TaskMessage] = field(default_factory=list)      # all messages (history)

    # Log file path
    log_file: str = ""

    # Retry approval tracking
    retry_pending: bool = False
    retry_reason: str = ""
    retry_attempts: int = 0
    completion_deferred: bool = False
    completion_deferred_at: Optional[datetime] = None
    completion_deferred_summary: str = ""
    completion_deferred_detail: str = ""
    supervisor_job_id: str = ""

    # Completion hook — prompt to feed to the orchestrator when this task completes
    on_complete: str = ""

    # Supervisor tracking — detect stuck assessment loops
    supervisor_assessment_count: int = 0        # consecutive assessments without finalization
    last_worker_activity_at: Optional[datetime] = None  # last MCP tool call from worker
    worker_exited_at: Optional[datetime] = None  # when worker process exited

    # Recovery tracking — tasks that were in-progress when the app restarted
    recovery_pending: bool = False

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "prompt": self.prompt,
            "status": self.status,
            "created_at": self.created_at.isoformat(),
            "updated_at": self.updated_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "worker_session_id": self.worker_session_id,
            "supervisor_session_id": self.supervisor_session_id,
            "working_dir": self.working_dir,
            "channel": self.channel,
            "target": self.target,
            "service_url": self.service_url,
            "plan": self.plan,
            "supervisor_instructions": self.supervisor_instructions,
            "check_interval": self.check_interval,
            "auto_supervise": self.auto_supervise,
            "timeline": [e.to_dict() for e in self.timeline],
            "inbox": [m.to_dict() for m in self.inbox],
            "outbox": [m.to_dict() for m in self.outbox],
            "log_file": self.log_file,
            "retry_pending": self.retry_pending,
            "retry_reason": self.retry_reason,
            "retry_attempts": self.retry_attempts,
            "completion_deferred": self.completion_deferred,
            "completion_deferred_at": self.completion_deferred_at.isoformat() if self.completion_deferred_at else None,
            "completion_deferred_summary": self.completion_deferred_summary,
            "completion_deferred_detail": self.completion_deferred_detail,
            "supervisor_job_id": self.supervisor_job_id,
            "on_complete": self.on_complete,
            "supervisor_assessment_count": self.supervisor_assessment_count,
            "last_worker_activity_at": self.last_worker_activity_at.isoformat() if self.last_worker_activity_at else None,
            "worker_exited_at": self.worker_exited_at.isoformat() if self.worker_exited_at else None,
            "recovery_pending": self.recovery_pending,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        return cls(
            task_id=d["task_id"],
            name=d["name"],
            prompt=d["prompt"],
            status=d.get("status", "pending"),
            created_at=datetime.fromisoformat(d["created_at"]),
            updated_at=datetime.fromisoformat(d["updated_at"]),
            completed_at=datetime.fromisoformat(d["completed_at"]) if d.get("completed_at") else None,
            worker_session_id=d.get("worker_session_id"),
            supervisor_session_id=d.get("supervisor_session_id"),
            working_dir=d.get("working_dir", ""),
            channel=d.get("channel", ""),
            target=d.get("target", ""),
            service_url=d.get("service_url", ""),
            plan=d.get("plan", ""),
            supervisor_instructions=d.get("supervisor_instructions", ""),
            check_interval=d.get("check_interval", 120),
            auto_supervise=d.get("auto_supervise", True),
            timeline=[TimelineEntry.from_dict(e) for e in d.get("timeline", [])],
            inbox=[TaskMessage.from_dict(m) for m in d.get("inbox", [])],
            outbox=[TaskMessage.from_dict(m) for m in d.get("outbox", [])],
            log_file=d.get("log_file", ""),
            retry_pending=d.get("retry_pending", False),
            retry_reason=d.get("retry_reason", ""),
            retry_attempts=d.get("retry_attempts", 0),
            completion_deferred=d.get("completion_deferred", False),
            completion_deferred_at=datetime.fromisoformat(d["completion_deferred_at"]) if d.get("completion_deferred_at") else None,
            completion_deferred_summary=d.get("completion_deferred_summary", ""),
            completion_deferred_detail=d.get("completion_deferred_detail", ""),
            supervisor_job_id=d.get("supervisor_job_id", ""),
            on_complete=d.get("on_complete", ""),
            supervisor_assessment_count=d.get("supervisor_assessment_count", 0),
            last_worker_activity_at=datetime.fromisoformat(d["last_worker_activity_at"]) if d.get("last_worker_activity_at") else None,
            worker_exited_at=datetime.fromisoformat(d["worker_exited_at"]) if d.get("worker_exited_at") else None,
            recovery_pending=d.get("recovery_pending", False),
        )

    def add_timeline(self, event: str, summary: str, detail: str = "") -> TimelineEntry:
        entry = TimelineEntry(ts=_now(), event=event, summary=summary, detail=detail)
        self.timeline.append(entry)
        self.updated_at = _now()
        return entry

    def concise_timeline(self, limit: int = 20) -> str:
        """Return a formatted concise timeline string."""
        entries = self.timeline[-limit:]
        lines = []
        for e in entries:
            ts_str = e.ts.strftime("%H:%M:%S")
            lines.append(f"[{ts_str}] {e.event}: {e.summary}")
        return "\n".join(lines) if lines else "(no timeline entries)"


# ── TaskManager ──────────────────────────────────────────────

class TaskManager:
    """Manages the lifecycle of dispatched tasks."""

    def __init__(self, data_dir: str, workspace_dir: str | None = None) -> None:
        self.data_dir = data_dir
        self.tasks_dir = os.path.join(workspace_dir, ".tasks") if workspace_dir else os.path.join(data_dir, ".tasks")
        self._tasks: Dict[str, Task] = {}
        self._store_path = os.path.join(data_dir, "tasks.json")
        os.makedirs(self.tasks_dir, exist_ok=True)
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._store_path):
            return
        try:
            with open(self._store_path, "r", encoding="utf-8") as f:
                raw = json.load(f)
            for item in raw.get("tasks", []):
                task = Task.from_dict(item)
                self._tasks[task.task_id] = task
        except Exception as exc:
            logger.error("Failed to load tasks: %s", exc)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._store_path), exist_ok=True)
        payload = {"tasks": [t.to_dict() for t in self._tasks.values()]}
        with open(self._store_path, "w", encoding="utf-8") as f:
            json.dump(payload, f, indent=2)

    def create_task(
        self,
        name: str,
        prompt: str,
        channel: str = "",
        target: str = "",
        service_url: str = "",
        check_interval: int = 120,
        auto_supervise: bool = True,
        plan: str = "",
        supervisor_instructions: str = "",
        status: str = "pending",
    ) -> Task:
        """Create a new task. Does NOT start execution (that's the worker's job)."""
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        working_dir = os.path.join(self.tasks_dir, task_id)
        os.makedirs(working_dir, exist_ok=True)
        log_file = os.path.join(working_dir, "raw.log")

        task = Task(
            task_id=task_id,
            name=name,
            prompt=prompt,
            status=status,
            channel=channel,
            target=target,
            service_url=service_url,
            working_dir=working_dir,
            log_file=log_file,
            check_interval=check_interval,
            auto_supervise=auto_supervise,
            plan=plan,
            supervisor_instructions=supervisor_instructions,
        )
        event = "proposed" if status == "proposed" else "created"
        task.add_timeline(event, f"Task {event}: {name}")
        self._tasks[task_id] = task
        self._save()
        logger.info("Task %s: %s (%s)", event, task_id, name)
        return task

    def get(self, task_id: str) -> Optional[Task]:
        return self._tasks.get(task_id)

    def list_tasks(self, status: Optional[str] = None) -> List[Task]:
        tasks = list(self._tasks.values())
        if status:
            tasks = [t for t in tasks if t.status == status]
        return tasks

    def active_tasks(self) -> List[Task]:
        """Return tasks that are currently running or paused."""
        return [t for t in self._tasks.values() if t.status in ("running", "paused", "needs_input", "pending")]

    def proposed_tasks(self) -> List[Task]:
        """Return tasks awaiting user approval."""
        return [t for t in self._tasks.values() if t.status == "proposed"]

    def pending_retry_tasks(self) -> List[Task]:
        """Return tasks awaiting retry approval."""
        return [t for t in self._tasks.values() if t.retry_pending]

    def latest_pending_retry(self, channel: str = "", target: str = "") -> Optional[Task]:
        """Get the most recent retry request, optionally filtered by channel/target."""
        pending = self.pending_retry_tasks()
        if channel:
            pending = [t for t in pending if t.channel == channel]
        if target:
            pending = [t for t in pending if t.target == target]
        if not pending:
            return None
        return max(pending, key=lambda t: t.updated_at)

    def latest_proposed(self, channel: str = "", target: str = "") -> Optional[Task]:
        """Get the most recent proposed task, optionally filtered by channel/target."""
        proposed = self.proposed_tasks()
        if channel:
            proposed = [t for t in proposed if t.channel == channel]
        if target:
            proposed = [t for t in proposed if t.target == target]
        if not proposed:
            return None
        return max(proposed, key=lambda t: t.created_at)

    def update_status(self, task_id: str, status: str) -> Optional[Task]:
        """Update a task's status."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        if status not in TASK_STATUSES:
            raise ValueError(f"Invalid status: {status}")
        old = task.status
        task.status = status
        task.updated_at = _now()
        if status in ("completed", "failed", "cancelled"):
            task.completed_at = _now()
        task.add_timeline("status_change", f"{old} → {status}")
        self._save()
        return task

    def request_retry(self, task_id: str, reason: str) -> Optional[Task]:
        """Mark a task as needing retry approval from the user."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.retry_pending = True
        task.retry_reason = reason
        task.status = "needs_input"
        task.completed_at = None
        task.add_timeline("retry_requested", reason[:500])
        task.updated_at = _now()
        self._save()
        return task

    def approve_retry(self, task_id: str) -> Optional[Task]:
        """Approve a retry for a failed task."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.retry_pending = False
        task.retry_reason = ""
        task.retry_attempts += 1
        task.add_timeline("retry_approved", f"Retry approved (attempt {task.retry_attempts})")
        task.updated_at = _now()
        self._save()
        return task

    def decline_retry(self, task_id: str) -> Optional[Task]:
        """Decline a retry and mark the task as failed."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.retry_pending = False
        task.retry_reason = ""
        task.status = "failed"
        task.completed_at = _now()
        task.add_timeline("retry_declined", "Retry declined by user")
        task.updated_at = _now()
        self._save()
        return task

    def cancel_task(self, task_id: str) -> Optional[Task]:
        """Cancel a task."""
        return self.update_status(task_id, "cancelled")

    def clear_all(self) -> int:
        """Remove all tasks. Returns the number of tasks cleared."""
        count = len(self._tasks)
        self._tasks.clear()
        self._save()
        return count

    # ── Upward messages (worker/supervisor → orchestrator) ────

    def handle_report(
        self,
        task_id: str,
        msg_type: str,
        summary: str,
        detail: str = "",
        artifact_url: str = "",
        from_tier: str = "worker",
    ) -> Optional[TaskMessage]:
        """Process an upward report from a worker or supervisor."""
        task = self._tasks.get(task_id)
        if not task:
            logger.warning("Report for unknown task: %s", task_id)
            return None

        if msg_type not in UP_MSG_TYPES:
            raise ValueError(f"Invalid upward message type: {msg_type}")

        msg = TaskMessage(
            msg_id=f"msg-{uuid.uuid4().hex[:8]}",
            ts=_now(),
            direction="up",
            msg_type=msg_type,
            from_tier=from_tier,
            content=summary,
            detail=detail,
            artifact_url=artifact_url,
        )
        task.outbox.append(msg)

        # Map report type to timeline event
        event_map = {
            "progress": "checkpoint",
            "completed": "completed",
            "failed": "failed",
            "needs_input": "needs_input",
            "question": "question",
            "artifact": "artifact",
            "assessment": "supervised",
            "intervention": "supervised",
            "escalation": "escalation",
        }
        event = event_map.get(msg_type, msg_type)
        task.add_timeline(event, summary, detail)

        # Update task status based on report type
        if msg_type == "completed":
            task.status = "completed"
            task.completed_at = _now()
        elif msg_type == "failed":
            task.status = "failed"
            task.completed_at = _now()
        elif msg_type == "needs_input":
            task.status = "needs_input"

        task.updated_at = _now()
        self._save()

        logger.info("Task %s report [%s]: %s", task_id, msg_type, summary)
        return msg

    def should_notify_user(self, msg: TaskMessage) -> bool:
        """Check if a message should trigger user notification."""
        if msg.msg_type in AUTO_NOTIFY_TYPES:
            return True
        if msg.from_tier == "supervisor" and msg.msg_type in {"assessment", "intervention"}:
            return True
        return False

    # ── Downward messages (orchestrator → worker/supervisor) ──

    def send_message(
        self,
        task_id: str,
        msg_type: str,
        content: str,
        from_tier: str = "orchestrator",
    ) -> Optional[TaskMessage]:
        """Send a downward message to a task's worker/supervisor."""
        task = self._tasks.get(task_id)
        if not task:
            logger.warning("Send to unknown task: %s", task_id)
            return None

        if msg_type not in DOWN_MSG_TYPES:
            raise ValueError(f"Invalid downward message type: {msg_type}")

        msg = TaskMessage(
            msg_id=f"msg-{uuid.uuid4().hex[:8]}",
            ts=_now(),
            direction="down",
            msg_type=msg_type,
            from_tier=from_tier,
            content=content,
        )
        task.inbox.append(msg)
        task.outbox.append(msg)  # Also in full history

        # Timeline
        event_map = {
            "instruction": "user_input",
            "input": "user_input",
            "pause": "paused",
            "resume": "resumed",
            "redirect": "redirected",
            "cancel": "cancelled",
            "priority": "priority_change",
        }
        event = event_map.get(msg_type, msg_type)
        task.add_timeline(event, f"[{from_tier}] {content}")

        # Status side effects
        if msg_type == "pause":
            task.status = "paused"
        elif msg_type == "resume" and task.status == "paused":
            task.status = "running"
        elif msg_type == "cancel":
            task.status = "cancelled"
            task.completed_at = _now()

        task.updated_at = _now()
        self._save()

        logger.info("Task %s message [%s] from %s: %s", task_id, msg_type, from_tier, content[:80])
        return msg

    # ── Inbox management (for workers/supervisors to poll) ────

    def check_inbox(self, task_id: str, acknowledge: bool = True) -> List[TaskMessage]:
        """Get unacknowledged inbox messages for a task."""
        task = self._tasks.get(task_id)
        if not task:
            return []

        unread = [m for m in task.inbox if not m.acknowledged]
        if acknowledge and unread:
            for m in unread:
                m.acknowledged = True
            self._save()
        return unread

    # ── Log management ────────────────────────────────────────

    def append_log(self, task_id: str, text: str) -> None:
        """Append raw output to a task's log file."""
        task = self._tasks.get(task_id)
        if not task or not task.log_file:
            return
        os.makedirs(os.path.dirname(task.log_file), exist_ok=True)
        with open(task.log_file, "a", encoding="utf-8") as f:
            f.write(text + "\n")

    def read_log(self, task_id: str, tail: int = 200) -> str:
        """Read the last N lines of a task's log."""
        task = self._tasks.get(task_id)
        if not task or not task.log_file or not os.path.exists(task.log_file):
            return "(no logs)"
        with open(task.log_file, "r", encoding="utf-8") as f:
            lines = f.readlines()
        return "".join(lines[-tail:])

    def set_worker_session(self, task_id: str, session_id: str) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.worker_session_id = session_id
            self._save()

    def set_supervisor_session(self, task_id: str, session_id: str) -> None:
        task = self._tasks.get(task_id)
        if task:
            task.supervisor_session_id = session_id
            self._save()

    # ── Recovery management (stale tasks from prior run) ──────

    def stale_active_tasks(self) -> List[Task]:
        """Return tasks with in-progress statuses that are not already flagged for recovery.

        These are tasks that were running/pending/paused/needs_input when
        the app last shut down and have no live worker process.
        """
        return [
            t for t in self._tasks.values()
            if t.status in ("running", "paused", "needs_input", "pending")
            and not t.recovery_pending
        ]

    def recovery_pending_tasks(self, channel: str = "", target: str = "") -> List[Task]:
        """Return tasks awaiting the user's resume/cancel decision."""
        tasks = [t for t in self._tasks.values() if t.recovery_pending]
        if channel:
            tasks = [t for t in tasks if t.channel == channel]
        if target:
            tasks = [t for t in tasks if t.target == target]
        return tasks

    def mark_recovery_pending(self, task_id: str) -> Optional[Task]:
        """Flag a task as awaiting the user's recovery decision."""
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.recovery_pending = True
        task.add_timeline("recovery_pending", "App restarted — awaiting user decision to resume or cancel")
        task.updated_at = _now()
        self._save()
        return task

    def resolve_recovery(self, task_id: str, resume: bool) -> Optional[Task]:
        """Resolve a recovery-pending task.

        If *resume* is True, set the task back to ``pending`` so it can
        be re-dispatched.  If False, cancel the task.
        """
        task = self._tasks.get(task_id)
        if not task:
            return None
        task.recovery_pending = False
        if resume:
            task.status = "pending"
            task.completed_at = None
            task.add_timeline("recovery_resumed", "User chose to resume task")
        else:
            task.status = "cancelled"
            task.completed_at = _now()
            task.add_timeline("recovery_cancelled", "User chose to cancel stale task")
        task.updated_at = _now()
        self._save()
        logger.info("Task %s recovery resolved: %s", task_id, "resumed" if resume else "cancelled")
        return task

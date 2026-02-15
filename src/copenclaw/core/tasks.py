"""Task dispatch and orchestration system.

Manages multi-session task execution with bidirectional inter-tier
communication (ITC) between orchestrator, workers, and supervisors.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
import copy
import json
import os
import threading
from typing import Any, Dict, List, Optional
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
TASK_TYPES = {"standard", "continuous_improvement"}

_CI_DEFAULT_CONFIG: dict[str, Any] = {
    "objective": "",
    "max_wall_clock_seconds": 172800,
    "max_iterations": 120,
    "iteration_timeout_seconds": 1800,
    "min_iteration_interval_seconds": 60,
    "max_consecutive_failures": 5,
    "max_no_improvement_iterations": 8,
    "quality_gate": {
        "metric": "composite_score",
        "min_delta": 0.0,
        "target_score": None,
        "required_evidence": [],
    },
    "retry_policy": {
        "max_attempts_per_iteration": 3,
        "initial_backoff_seconds": 10,
        "backoff_multiplier": 2.0,
        "max_backoff_seconds": 300,
        "jitter": True,
    },
    "safety": {
        "require_supervisor_gate": True,
        "require_human_approval_on": ["scope_expansion", "destructive_change", "budget_exceeded"],
        "max_files_changed_per_iteration": 40,
        "max_commits_per_iteration": 3,
        "allowed_paths": [],
    },
    "resume_policy": "checkpoint_only",
}

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
    task_type: str = "standard"         # standard|continuous_improvement

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
    ci_tick_job_id: str = ""

    # Supervisor tracking — detect stuck assessment loops
    supervisor_assessment_count: int = 0        # consecutive assessments without finalization
    last_worker_activity_at: Optional[datetime] = None  # last MCP tool call from worker
    worker_exited_at: Optional[datetime] = None  # when worker process exited

    # Recovery tracking — tasks that were in-progress when the app restarted
    recovery_pending: bool = False

    # Watchdog tracking — used for auto-recovery of stuck workers
    watchdog_state: str = "none"  # none | warned | restarted | needs_input
    watchdog_last_action_at: Optional[datetime] = None
    watchdog_restart_count: int = 0
    ci_config: Dict[str, Any] = field(default_factory=dict)
    ci_state: Dict[str, Any] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "task_id": self.task_id,
            "name": self.name,
            "prompt": self.prompt,
            "status": self.status,
            "task_type": self.task_type,
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
            "ci_tick_job_id": self.ci_tick_job_id,
            "supervisor_assessment_count": self.supervisor_assessment_count,
            "last_worker_activity_at": self.last_worker_activity_at.isoformat() if self.last_worker_activity_at else None,
            "worker_exited_at": self.worker_exited_at.isoformat() if self.worker_exited_at else None,
            "recovery_pending": self.recovery_pending,
            "watchdog_state": self.watchdog_state,
            "watchdog_last_action_at": self.watchdog_last_action_at.isoformat() if self.watchdog_last_action_at else None,
            "watchdog_restart_count": self.watchdog_restart_count,
            "ci_config": self.ci_config,
            "ci_state": self.ci_state,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Task:
        return cls(
            task_id=d["task_id"],
            name=d["name"],
            prompt=d["prompt"],
            status=d.get("status", "pending"),
            task_type=d.get("task_type", "standard"),
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
            ci_tick_job_id=d.get("ci_tick_job_id", ""),
            supervisor_assessment_count=d.get("supervisor_assessment_count", 0),
            last_worker_activity_at=datetime.fromisoformat(d["last_worker_activity_at"]) if d.get("last_worker_activity_at") else None,
            worker_exited_at=datetime.fromisoformat(d["worker_exited_at"]) if d.get("worker_exited_at") else None,
            recovery_pending=d.get("recovery_pending", False),
            watchdog_state=d.get("watchdog_state", "none"),
            watchdog_last_action_at=datetime.fromisoformat(d["watchdog_last_action_at"]) if d.get("watchdog_last_action_at") else None,
            watchdog_restart_count=d.get("watchdog_restart_count", 0),
            ci_config=d.get("ci_config", {}),
            ci_state=d.get("ci_state", {}),
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
        self._save_lock = threading.RLock()
        self._ci_locks: Dict[str, threading.Lock] = {}
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
                if task.task_type not in TASK_TYPES:
                    task.task_type = "standard"
                self._ensure_continuous_defaults(task)
                self._tasks[task.task_id] = task
        except Exception as exc:
            logger.error("Failed to load tasks: %s", exc)

    def _save(self) -> None:
        os.makedirs(os.path.dirname(self._store_path), exist_ok=True)
        payload = {"tasks": [t.to_dict() for t in self._tasks.values()]}
        tmp_path = f"{self._store_path}.tmp"
        with self._save_lock:
            with open(tmp_path, "w", encoding="utf-8") as f:
                json.dump(payload, f, indent=2)
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, self._store_path)

    def _ci_lock(self, task_id: str) -> threading.Lock:
        with self._save_lock:
            lock = self._ci_locks.get(task_id)
            if lock is None:
                lock = threading.Lock()
                self._ci_locks[task_id] = lock
            return lock

    @staticmethod
    def _normalize_positive_int(value: Any, default: int) -> int:
        try:
            parsed = int(value)
            return parsed if parsed > 0 else default
        except Exception:  # noqa: BLE001
            return default

    def _normalize_ci_config(self, raw: dict[str, Any]) -> dict[str, Any]:
        cfg = copy.deepcopy(_CI_DEFAULT_CONFIG)
        source = raw if isinstance(raw, dict) else {}
        for key in ("objective", "resume_policy"):
            if key in source and isinstance(source[key], str):
                cfg[key] = source[key]
        for key in (
            "max_wall_clock_seconds",
            "max_iterations",
            "iteration_timeout_seconds",
            "min_iteration_interval_seconds",
            "max_consecutive_failures",
            "max_no_improvement_iterations",
        ):
            cfg[key] = self._normalize_positive_int(source.get(key), int(cfg[key]))

        for section in ("quality_gate", "retry_policy", "safety"):
            src_section = source.get(section)
            if isinstance(src_section, dict):
                cfg_section = cfg[section]
                if isinstance(cfg_section, dict):
                    cfg_section.update(src_section)

        if not isinstance(cfg.get("safety", {}).get("require_human_approval_on"), list):
            cfg["safety"]["require_human_approval_on"] = list(_CI_DEFAULT_CONFIG["safety"]["require_human_approval_on"])
        if not isinstance(cfg.get("quality_gate", {}).get("required_evidence"), list):
            cfg["quality_gate"]["required_evidence"] = list(_CI_DEFAULT_CONFIG["quality_gate"]["required_evidence"])
        return cfg

    @staticmethod
    def _initial_ci_state() -> dict[str, Any]:
        ts = _now().isoformat()
        return {
            "phase": "plan",
            "started_at": ts,
            "last_iteration_started_at": None,
            "last_iteration_finished_at": None,
            "iteration": 0,
            "consecutive_failures": 0,
            "no_improvement_iterations": 0,
            "best_score": None,
            "last_score": None,
            "last_checkpoint_id": "",
            "last_checkpoint_at": None,
            "circuit_open_until": None,
            "stop_reason": "",
            "checkpoint_seq": 0,
        }

    def _ensure_continuous_defaults(self, task: Task) -> None:
        if task.task_type != "continuous_improvement":
            return
        task.ci_config = self._normalize_ci_config(task.ci_config)
        if not isinstance(task.ci_state, dict) or not task.ci_state:
            task.ci_state = self._initial_ci_state()
        state = task.ci_state
        defaults = self._initial_ci_state()
        for key, value in defaults.items():
            state.setdefault(key, value)
        if not isinstance(state.get("checkpoint_seq"), int):
            state["checkpoint_seq"] = 0

    @staticmethod
    def _ci_paths(task: Task) -> dict[str, str]:
        return {
            "checkpoints": os.path.join(task.working_dir, "ci-checkpoints.jsonl"),
            "latest": os.path.join(task.working_dir, "ci-latest-checkpoint.json"),
            "iterations": os.path.join(task.working_dir, "ci-iterations.jsonl"),
        }

    @staticmethod
    def _append_jsonl(path: str, record: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")
            handle.flush()
            os.fsync(handle.fileno())

    @staticmethod
    def _write_json_atomic(path: str, payload: dict[str, Any]) -> None:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    def _record_ci_checkpoint(self, task: Task, reason: str, extra: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        self._ensure_continuous_defaults(task)
        lock = self._ci_lock(task.task_id)
        with lock:
            state = task.ci_state
            seq = int(state.get("checkpoint_seq", 0)) + 1
            state["checkpoint_seq"] = seq
            checkpoint_id = f"chk-{seq:06d}"
            now_iso = _now().isoformat()
            record = {
                "checkpoint_id": checkpoint_id,
                "task_id": task.task_id,
                "iteration": int(state.get("iteration", 0)),
                "phase": state.get("phase", "execute"),
                "ts": now_iso,
                "reason": reason,
                "resume_hint": f"continue_from_iteration_{int(state.get('iteration', 0)) + 1}",
                "ci_state_snapshot": copy.deepcopy(state),
                "extra": extra or {},
            }
            paths = self._ci_paths(task)
            self._append_jsonl(paths["checkpoints"], record)
            self._write_json_atomic(paths["latest"], record)
            state["last_checkpoint_id"] = checkpoint_id
            state["last_checkpoint_at"] = now_iso
            task.updated_at = _now()
            return record

    def _load_latest_ci_checkpoint(self, task: Task) -> Optional[dict[str, Any]]:
        paths = self._ci_paths(task)
        latest_path = paths["latest"]
        if os.path.exists(latest_path):
            try:
                with open(latest_path, "r", encoding="utf-8") as handle:
                    payload = json.load(handle)
                if isinstance(payload, dict):
                    return payload
            except Exception:  # noqa: BLE001
                logger.warning("Failed reading latest checkpoint for %s", task.task_id)
        checkpoints_path = paths["checkpoints"]
        if not os.path.exists(checkpoints_path):
            return None
        try:
            with open(checkpoints_path, "r", encoding="utf-8") as handle:
                lines = [line.strip() for line in handle if line.strip()]
            if not lines:
                return None
            payload = json.loads(lines[-1])
            return payload if isinstance(payload, dict) else None
        except Exception:  # noqa: BLE001
            logger.warning("Failed parsing checkpoint log for %s", task.task_id)
            return None

    def _restore_ci_state_from_checkpoint(self, task: Task, checkpoint: dict[str, Any]) -> bool:
        if checkpoint.get("task_id") != task.task_id:
            return False
        snapshot = checkpoint.get("ci_state_snapshot")
        if not isinstance(snapshot, dict):
            return False
        task.ci_state.update(snapshot)
        task.ci_state["last_checkpoint_id"] = checkpoint.get("checkpoint_id", task.ci_state.get("last_checkpoint_id", ""))
        task.ci_state["last_checkpoint_at"] = checkpoint.get("ts", task.ci_state.get("last_checkpoint_at"))
        return True

    @staticmethod
    def _coerce_float(value: Any) -> Optional[float]:
        try:
            return float(value)
        except Exception:  # noqa: BLE001
            return None

    def _elapsed_ci_seconds(self, task: Task) -> int:
        started_at = task.ci_state.get("started_at")
        if not started_at:
            return 0
        try:
            started = datetime.fromisoformat(str(started_at))
            if started.tzinfo is None:
                started = started.replace(tzinfo=timezone.utc)
            return max(0, int((_now() - started).total_seconds()))
        except Exception:  # noqa: BLE001
            return 0

    def _set_ci_terminal(self, task: Task, status: str, phase: str, reason: str) -> str:
        task.status = status
        if status in ("completed", "failed", "cancelled"):
            task.completed_at = _now()
        task.ci_state["phase"] = phase
        task.ci_state["stop_reason"] = reason
        task.updated_at = _now()
        return status if status in UP_MSG_TYPES else "progress"

    def _apply_ci_limits(self, task: Task, continuous: dict[str, Any]) -> Optional[str]:
        cfg = task.ci_config
        state = task.ci_state

        if continuous.get("safety_violation"):
            return self._set_ci_terminal(task, "needs_input", "halted_by_safety", "safety_violation")

        max_iterations = int(cfg.get("max_iterations", 0) or 0)
        if max_iterations > 0 and int(state.get("iteration", 0)) >= max_iterations:
            return self._set_ci_terminal(task, "completed", "budget_exhausted", "max_iterations_reached")

        max_wall = int(cfg.get("max_wall_clock_seconds", 0) or 0)
        if max_wall > 0 and self._elapsed_ci_seconds(task) >= max_wall:
            return self._set_ci_terminal(task, "completed", "budget_exhausted", "max_wall_clock_seconds_reached")

        max_fail = int(cfg.get("max_consecutive_failures", 0) or 0)
        if max_fail > 0 and int(state.get("consecutive_failures", 0)) >= max_fail:
            return self._set_ci_terminal(task, "failed", "failed_unrecoverable", "max_consecutive_failures_reached")

        max_no_improve = int(cfg.get("max_no_improvement_iterations", 0) or 0)
        if max_no_improve > 0 and int(state.get("no_improvement_iterations", 0)) >= max_no_improve:
            return self._set_ci_terminal(task, "completed", "budget_exhausted", "max_no_improvement_iterations_reached")

        quality_gate = cfg.get("quality_gate", {})
        target_score = None
        if isinstance(quality_gate, dict):
            target_score = self._coerce_float(quality_gate.get("target_score"))
        last_score = self._coerce_float(state.get("last_score"))
        if target_score is not None and last_score is not None and last_score >= target_score:
            return self._set_ci_terminal(task, "completed", "succeeded", "target_score_reached")
        return None

    def _update_continuous_state(
        self,
        task: Task,
        msg_type: str,
        summary: str,
        detail: str,
        from_tier: str,
        continuous: Optional[dict[str, Any]],
    ) -> str:
        self._ensure_continuous_defaults(task)
        data: dict[str, Any] = continuous if isinstance(continuous, dict) else {}

        if not data and isinstance(detail, str) and summary.startswith(("ITERATION_RESULT:", "ITERATION_SCORE:")):
            try:
                parsed = json.loads(detail)
                if isinstance(parsed, dict):
                    data = parsed
            except Exception:  # noqa: BLE001
                data = {}

        state = task.ci_state
        now_iso = _now().isoformat()
        state.setdefault("started_at", now_iso)

        if "phase" in data and isinstance(data["phase"], str):
            state["phase"] = data["phase"]
        elif msg_type == "progress":
            state["phase"] = "execute"
        elif msg_type == "assessment":
            state["phase"] = "evaluate"
        elif msg_type == "needs_input":
            state["phase"] = "gate"

        iteration_hint = data.get("iteration")
        if isinstance(iteration_hint, int) and iteration_hint >= 0:
            state["iteration"] = max(int(state.get("iteration", 0)), iteration_hint)
        elif msg_type == "progress" and from_tier == "worker":
            state["iteration"] = int(state.get("iteration", 0)) + 1

        if msg_type == "progress" and from_tier == "worker":
            state["last_iteration_started_at"] = state.get("last_iteration_started_at") or now_iso
            state["last_iteration_finished_at"] = now_iso
            state["consecutive_failures"] = 0
        elif msg_type == "failed":
            state["last_iteration_finished_at"] = now_iso
            state["consecutive_failures"] = int(state.get("consecutive_failures", 0)) + 1

        score = self._coerce_float(data.get("score"))
        if score is not None:
            best_score = self._coerce_float(state.get("best_score"))
            state["last_score"] = score
            if best_score is None or score > best_score:
                state["best_score"] = score
                state["no_improvement_iterations"] = 0
            else:
                state["no_improvement_iterations"] = int(state.get("no_improvement_iterations", 0)) + 1

        if msg_type == "completed":
            state["phase"] = "succeeded"
            state["stop_reason"] = data.get("stop_reason", "completed")
        elif msg_type == "failed":
            state["phase"] = "failed_unrecoverable"
            state["stop_reason"] = data.get("stop_reason", "failed")

        iter_record = {
            "ts": now_iso,
            "task_id": task.task_id,
            "iteration": int(state.get("iteration", 0)),
            "msg_type": msg_type,
            "from_tier": from_tier,
            "summary": summary[:500],
            "phase": state.get("phase", ""),
            "continuous": data,
        }
        paths = self._ci_paths(task)
        lock = self._ci_lock(task.task_id)
        with lock:
            self._append_jsonl(paths["iterations"], iter_record)

        force_type = self._apply_ci_limits(task, data)

        should_checkpoint = bool(data.get("checkpoint")) or msg_type in {"completed", "failed"} or force_type in {"completed", "failed", "needs_input"}
        if should_checkpoint:
            reason = data.get("checkpoint_reason", summary[:160] or msg_type)
            self._record_ci_checkpoint(task, reason=str(reason), extra={"msg_type": msg_type, "from_tier": from_tier})

        if force_type:
            return force_type
        return msg_type

    def mark_continuous_started(self, task_id: str) -> None:
        task = self._tasks.get(task_id)
        if not task or task.task_type != "continuous_improvement":
            return
        self._ensure_continuous_defaults(task)
        now_iso = _now().isoformat()
        task.ci_state["phase"] = "execute"
        if not task.ci_state.get("started_at"):
            task.ci_state["started_at"] = now_iso
        task.ci_state["last_iteration_started_at"] = now_iso
        if not task.ci_state.get("last_checkpoint_id"):
            self._record_ci_checkpoint(task, reason="task_started")
        task.updated_at = _now()
        self._save()

    def continuous_status(self, task: Task) -> dict[str, Any]:
        if task.task_type != "continuous_improvement":
            return {}
        self._ensure_continuous_defaults(task)
        state = task.ci_state
        cfg = task.ci_config
        elapsed = self._elapsed_ci_seconds(task)
        max_iterations = int(cfg.get("max_iterations", 0) or 0)
        max_wall = int(cfg.get("max_wall_clock_seconds", 0) or 0)
        max_fail = int(cfg.get("max_consecutive_failures", 0) or 0)
        max_no_improve = int(cfg.get("max_no_improvement_iterations", 0) or 0)
        return {
            "phase": state.get("phase"),
            "iteration": int(state.get("iteration", 0)),
            "best_score": state.get("best_score"),
            "last_score": state.get("last_score"),
            "consecutive_failures": int(state.get("consecutive_failures", 0)),
            "no_improvement_iterations": int(state.get("no_improvement_iterations", 0)),
            "last_checkpoint_id": state.get("last_checkpoint_id", ""),
            "last_checkpoint_at": state.get("last_checkpoint_at"),
            "stop_reason": state.get("stop_reason", ""),
            "elapsed_seconds": elapsed,
            "budgets_remaining": {
                "iterations": max(0, max_iterations - int(state.get("iteration", 0))) if max_iterations > 0 else None,
                "wall_clock_seconds": max(0, max_wall - elapsed) if max_wall > 0 else None,
                "consecutive_failures": max(0, max_fail - int(state.get("consecutive_failures", 0))) if max_fail > 0 else None,
                "no_improvement_iterations": max(0, max_no_improve - int(state.get("no_improvement_iterations", 0))) if max_no_improve > 0 else None,
            },
            "config": cfg,
        }

    def build_continuous_prompt(self, task: Task) -> str:
        if task.task_type != "continuous_improvement":
            return task.prompt
        status = self.continuous_status(task)
        objective = str(task.ci_config.get("objective", "")).strip()
        objective_line = f"Objective: {objective}" if objective else "Objective: iterative continuous improvement"
        resume_hint = (
            f"Resume from checkpoint {status.get('last_checkpoint_id')} at iteration {status.get('iteration')}."
            if status.get("last_checkpoint_id")
            else "No checkpoint yet; start from iteration 1."
        )
        ci_context = (
            "\n\n[CONTINUOUS_IMPROVEMENT_CONTEXT]\n"
            f"{objective_line}\n"
            f"Phase: {status.get('phase')}\n"
            f"Iteration: {status.get('iteration')}\n"
            f"Best score: {status.get('best_score')}\n"
            f"Last score: {status.get('last_score')}\n"
            f"{resume_hint}\n"
            "Report each loop with task_report(type='progress', summary='ITERATION_RESULT: ...', continuous={...}) "
            "and include checkpoint=true when a durable checkpoint should be committed.\n"
            "[/CONTINUOUS_IMPROVEMENT_CONTEXT]\n"
        )
        return f"{task.prompt}{ci_context}"

    def _apply_ci_priority_patch(self, task: Task, content: str) -> None:
        self._ensure_continuous_defaults(task)
        try:
            payload = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ValueError("priority content for continuous_improvement tasks must be valid JSON") from exc
        if not isinstance(payload, dict):
            raise ValueError("priority content for continuous_improvement tasks must be a JSON object")
        patch = payload.get("budget_patch", payload)
        if not isinstance(patch, dict):
            raise ValueError("budget_patch must be a JSON object")

        updated = False
        for key in (
            "max_wall_clock_seconds",
            "max_iterations",
            "iteration_timeout_seconds",
            "min_iteration_interval_seconds",
            "max_consecutive_failures",
            "max_no_improvement_iterations",
        ):
            if key in patch:
                current_default = self._normalize_positive_int(task.ci_config.get(key), 1)
                task.ci_config[key] = self._normalize_positive_int(patch[key], current_default)
                updated = True
        if not updated:
            raise ValueError("priority budget patch did not include any supported budget keys")
        task.updated_at = _now()

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
        task_type: str = "standard",
        ci_config: Optional[dict[str, Any]] = None,
    ) -> Task:
        """Create a new task. Does NOT start execution (that's the worker's job)."""
        if task_type not in TASK_TYPES:
            raise ValueError(f"Invalid task_type: {task_type}")
        task_id = f"task-{uuid.uuid4().hex[:12]}"
        working_dir = os.path.join(self.tasks_dir, task_id)
        os.makedirs(working_dir, exist_ok=True)
        log_file = os.path.join(working_dir, "raw.log")

        normalized_ci_config: dict[str, Any] = {}
        ci_state: dict[str, Any] = {}
        if task_type == "continuous_improvement":
            normalized_ci_config = self._normalize_ci_config(ci_config or {})
            ci_state = self._initial_ci_state()

        task = Task(
            task_id=task_id,
            name=name,
            prompt=prompt,
            status=status,
            task_type=task_type,
            channel=channel,
            target=target,
            service_url=service_url,
            working_dir=working_dir,
            log_file=log_file,
            check_interval=check_interval,
            auto_supervise=auto_supervise,
            plan=plan,
            supervisor_instructions=supervisor_instructions,
            ci_config=normalized_ci_config,
            ci_state=ci_state,
        )
        event = "proposed" if status == "proposed" else "created"
        task.add_timeline(event, f"Task {event}: {name}")
        self._tasks[task_id] = task
        if task.task_type == "continuous_improvement":
            self._record_ci_checkpoint(task, reason="task_created")
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
        continuous: Optional[dict[str, Any]] = None,
    ) -> Optional[TaskMessage]:
        """Process an upward report from a worker or supervisor."""
        task = self._tasks.get(task_id)
        if not task:
            logger.warning("Report for unknown task: %s", task_id)
            return None

        if msg_type not in UP_MSG_TYPES:
            raise ValueError(f"Invalid upward message type: {msg_type}")

        effective_msg_type = msg_type
        if task.task_type == "continuous_improvement":
            effective_msg_type = self._update_continuous_state(
                task=task,
                msg_type=msg_type,
                summary=summary,
                detail=detail,
                from_tier=from_tier,
                continuous=continuous,
            )
            if effective_msg_type not in UP_MSG_TYPES:
                effective_msg_type = msg_type
        message_summary = summary
        if effective_msg_type != msg_type and task.task_type == "continuous_improvement":
            stop_reason = str(task.ci_state.get("stop_reason", "")).strip()
            if stop_reason:
                message_summary = f"{summary} ({effective_msg_type}: {stop_reason})"

        msg = TaskMessage(
            msg_id=f"msg-{uuid.uuid4().hex[:8]}",
            ts=_now(),
            direction="up",
            msg_type=effective_msg_type,
            from_tier=from_tier,
            content=message_summary,
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
        event = event_map.get(effective_msg_type, effective_msg_type)
        task.add_timeline(event, message_summary, detail)

        # Update task status based on report type
        if effective_msg_type == "completed":
            task.status = "completed"
            task.completed_at = _now()
        elif effective_msg_type == "failed":
            task.status = "failed"
            task.completed_at = _now()
        elif effective_msg_type == "needs_input":
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

        if msg_type == "priority" and task.task_type == "continuous_improvement":
            self._apply_ci_priority_patch(task, content)

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
        stale: list[Task] = []
        for task in self._tasks.values():
            if task.status not in ("running", "paused", "needs_input", "pending"):
                continue
            if task.recovery_pending:
                continue
            if task.task_type == "continuous_improvement":
                self._ensure_continuous_defaults(task)
                resume_policy = str(task.ci_config.get("resume_policy", "checkpoint_only"))
                if resume_policy == "checkpoint_only":
                    checkpoint = self._load_latest_ci_checkpoint(task)
                    if not checkpoint or not self._restore_ci_state_from_checkpoint(task, checkpoint):
                        if task.status != "needs_input":
                            task.status = "needs_input"
                            task.ci_state["phase"] = "halted_by_safety"
                            task.ci_state["stop_reason"] = "checkpoint_missing_or_invalid"
                            task.add_timeline(
                                "needs_input",
                                "Continuous task cannot resume automatically (checkpoint missing/invalid)",
                            )
                            task.updated_at = _now()
                            self._save()
                        continue
            stale.append(task)
        return stale

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

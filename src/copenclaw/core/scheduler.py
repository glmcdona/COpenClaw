from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import os
from typing import Dict, Optional
import uuid

from croniter import croniter

@dataclass
class ScheduledJob:
    job_id: str
    name: str
    run_at: datetime
    payload: dict
    created_at: datetime = field(default_factory=datetime.utcnow)
    completed_at: Optional[datetime] = None
    cancelled: bool = False
    cron_expr: Optional[str] = None  # If set, job recurs on this cron schedule

class Scheduler:
    def __init__(self, store_path: Optional[str] = None, run_log_path: Optional[str] = None) -> None:
        self._jobs: Dict[str, ScheduledJob] = {}
        self._store_path = store_path
        self._run_log_path = run_log_path
        if store_path:
            self._load()

    def _load(self) -> None:
        if not self._store_path or not os.path.exists(self._store_path):
            return
        with open(self._store_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        for item in raw.get("jobs", []):
            job = ScheduledJob(
                job_id=item["job_id"],
                name=item["name"],
                run_at=datetime.fromisoformat(item["run_at"]),
                payload=item.get("payload", {}),
                created_at=datetime.fromisoformat(item["created_at"]),
                completed_at=datetime.fromisoformat(item["completed_at"]) if item.get("completed_at") else None,
                cancelled=item.get("cancelled", False),
                cron_expr=item.get("cron_expr"),
            )
            self._jobs[job.job_id] = job

    def _save(self) -> None:
        if not self._store_path:
            return
        dir_path = os.path.dirname(self._store_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        payload = {
            "jobs": [
                {
                    "job_id": job.job_id,
                    "name": job.name,
                    "run_at": job.run_at.isoformat(),
                    "payload": job.payload,
                    "created_at": job.created_at.isoformat(),
                    "completed_at": job.completed_at.isoformat() if job.completed_at else None,
                    "cancelled": job.cancelled,
                    "cron_expr": job.cron_expr,
                }
                for job in self._jobs.values()
            ]
        }
        with open(self._store_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    @staticmethod
    def validate_payload(payload: dict) -> list[str]:
        """Validate a job payload. Returns list of error messages (empty = valid)."""
        errors: list[str] = []
        job_type = payload.get("type")
        if job_type == "supervisor_check":
            if not payload.get("task_id"):
                errors.append("task_id is required for supervisor_check")
            return errors
        channel = payload.get("channel")
        if channel and channel not in ("telegram", "teams"):
            errors.append(f"unsupported channel: {channel}")
        if channel and not payload.get("target"):
            errors.append("target is required when channel is set")
        if channel == "teams" and not payload.get("service_url"):
            errors.append("service_url is required for teams channel")
        if not payload.get("prompt"):
            errors.append("prompt is required in payload")
        return errors

    @staticmethod
    def validate_cron(expr: str) -> bool:
        """Check whether a cron expression is valid."""
        try:
            croniter(expr)
            return True
        except (ValueError, KeyError):
            return False

    def schedule(
        self,
        name: str,
        run_at: datetime,
        payload: dict,
        cron_expr: Optional[str] = None,
    ) -> ScheduledJob:
        job_id = f"job-{uuid.uuid4().hex}"
        job = ScheduledJob(
            job_id=job_id,
            name=name,
            run_at=run_at,
            payload=payload,
            cron_expr=cron_expr,
        )
        self._jobs[job_id] = job
        self._save()
        return job

    def get(self, job_id: str) -> Optional[ScheduledJob]:
        return self._jobs.get(job_id)

    def list(self) -> list[ScheduledJob]:
        return list(self._jobs.values())

    def cancel(self, job_id: str) -> bool:
        """Cancel a job. Returns True if found and cancelled."""
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.cancelled = True
        job.completed_at = job.completed_at or datetime.utcnow()
        self._save()
        return True

    def clear_all(self) -> int:
        """Remove all jobs. Returns the number of jobs cleared."""
        count = len(self._jobs)
        self._jobs.clear()
        self._save()
        return count

    def due(self, now: Optional[datetime] = None) -> list[ScheduledJob]:
        now = now or datetime.utcnow()
        result = []
        for job in self._jobs.values():
            if job.cancelled or job.completed_at is not None:
                continue
            # Normalize timezone awareness for comparison
            run_at = job.run_at
            cmp_now = now
            if run_at.tzinfo is not None and cmp_now.tzinfo is None:
                run_at = run_at.replace(tzinfo=None)
            elif run_at.tzinfo is None and cmp_now.tzinfo is not None:
                cmp_now = cmp_now.replace(tzinfo=None)
            if run_at <= cmp_now:
                result.append(job)
        return result

    def mark_completed(self, job_id: str) -> None:
        job = self._jobs.get(job_id)
        if not job:
            return
        if job.cron_expr:
            # Recurring: advance run_at to next occurrence instead of completing
            try:
                cron = croniter(job.cron_expr, job.run_at)
                job.run_at = cron.get_next(datetime)
                # Don't set completed_at so it fires again
            except (ValueError, KeyError):
                job.completed_at = datetime.utcnow()
        else:
            job.completed_at = datetime.utcnow()
        self._save()

    def reschedule(self, job_id: str, run_at: datetime) -> bool:
        job = self._jobs.get(job_id)
        if not job:
            return False
        job.run_at = run_at
        job.completed_at = None
        self._save()
        return True

    def log_run(self, job_id: str, status: str, detail: Optional[str] = None) -> None:
        if not self._run_log_path:
            return
        dir_path = os.path.dirname(self._run_log_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        record = {
            "ts": datetime.utcnow().isoformat(),
            "job_id": job_id,
            "status": status,
            "detail": detail,
        }
        with open(self._run_log_path, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record) + "\n")

    def list_runs(self, job_id: Optional[str] = None, limit: int = 50) -> list[dict]:
        if not self._run_log_path or not os.path.exists(self._run_log_path):
            return []
        runs: list[dict] = []
        with open(self._run_log_path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                item = json.loads(line)
                if job_id and item.get("job_id") != job_id:
                    continue
                runs.append(item)
        return runs[-limit:]
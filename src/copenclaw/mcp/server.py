from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi import APIRouter, Depends, Header, HTTPException
from pydantic import BaseModel, Field

import json
import os

from copenclaw.core.audit import log_event
from copenclaw.core.scheduler import Scheduler
from copenclaw.integrations.telegram import TelegramAdapter
from copenclaw.integrations.teams import TeamsAdapter

# ---------- request / response models (module-level for FastAPI) ----------

class JobRequest(BaseModel):
    name: str
    run_at: str
    payload: dict[str, Any] = Field(default_factory=dict)
    cron_expr: str | None = None

class JobResponse(BaseModel):
    job_id: str
    name: str
    run_at: str
    payload: dict[str, Any]
    completed_at: str | None = None
    cancelled: bool = False
    cron_expr: str | None = None

class ReadFileRequest(BaseModel):
    path: str

class ReadFileResponse(BaseModel):
    content: str

class RunsResponse(BaseModel):
    runs: list[dict[str, Any]]

class SendRequest(BaseModel):
    channel: str
    target: str
    text: str = ""
    image_path: str | None = None
    service_url: str | None = None

class AuditResponse(BaseModel):
    events: list[dict[str, Any]]

class CancelRequest(BaseModel):
    job_id: str

# ---------- router factory ----------

def get_router(
    scheduler: Scheduler,
    data_dir: str | None = None,
    telegram_token: str | None = None,
    msteams_creds: dict | None = None,
    mcp_token: str | None = None,
) -> APIRouter:
    def _auth(x_mcp_token: str | None = Header(default=None), authorization: str | None = Header(default=None)) -> None:
        if not mcp_token:
            return
        token = x_mcp_token
        if not token and authorization and authorization.lower().startswith("bearer "):
            token = authorization.split(" ", 1)[1]
        if token != mcp_token:
            raise HTTPException(status_code=401, detail="Invalid MCP token")

    router = APIRouter(dependencies=[Depends(_auth)])

    # ---------- routes ----------

    @router.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @router.get("/tools")
    def tools() -> dict[str, list[dict[str, str]]]:
        return {
            "tools": [
                {"name": "jobs.schedule", "description": "Schedule a one-shot or recurring job"},
                {"name": "jobs.list", "description": "List all jobs"},
                {"name": "jobs.runs", "description": "List job run history"},
                {"name": "jobs.cancel", "description": "Cancel a job by ID"},
                {"name": "jobs.clear_all", "description": "Remove all scheduled jobs"},
                {"name": "send.message", "description": "Send a message to a channel"},
                {"name": "files.read", "description": "Read a file under data_dir"},
                {"name": "audit.read", "description": "Read audit log events"},
                {"name": "tasks.clear_all", "description": "Cancel and remove all tasks"},
                {"name": "app.restart", "description": "Restart the COpenClaw application"},
            ]
        }

    @router.post("/jobs/schedule", response_model=JobResponse)
    def schedule_job(req: JobRequest) -> JobResponse:
        try:
            run_at = datetime.fromisoformat(req.run_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise HTTPException(status_code=400, detail="Invalid run_at timestamp") from exc

        if req.cron_expr and not Scheduler.validate_cron(req.cron_expr):
            raise HTTPException(status_code=400, detail="Invalid cron expression")

        errors = Scheduler.validate_payload(req.payload)
        if errors:
            raise HTTPException(status_code=400, detail="; ".join(errors))

        job = scheduler.schedule(req.name, run_at, req.payload, cron_expr=req.cron_expr)
        return JobResponse(
            job_id=job.job_id,
            name=job.name,
            run_at=job.run_at.isoformat(),
            payload=job.payload,
            completed_at=job.completed_at.isoformat() if job.completed_at else None,
            cancelled=job.cancelled,
            cron_expr=job.cron_expr,
        )

    @router.get("/jobs")
    def list_jobs() -> dict[str, list[JobResponse]]:
        jobs = [
            JobResponse(
                job_id=j.job_id,
                name=j.name,
                run_at=j.run_at.isoformat(),
                payload=j.payload,
                completed_at=j.completed_at.isoformat() if j.completed_at else None,
                cancelled=j.cancelled,
                cron_expr=j.cron_expr,
            )
            for j in scheduler.list()
        ]
        return {"jobs": jobs}

    @router.post("/jobs/cancel")
    def cancel_job(req: CancelRequest) -> dict[str, str]:
        if scheduler.cancel(req.job_id):
            if data_dir:
                log_event(data_dir, "job.cancel", {"job_id": req.job_id})
            return {"status": "cancelled", "job_id": req.job_id}
        raise HTTPException(status_code=404, detail="Job not found")

    @router.get("/jobs/runs", response_model=RunsResponse)
    def list_runs(job_id: str | None = None, limit: int = 50) -> RunsResponse:
        return RunsResponse(runs=scheduler.list_runs(job_id=job_id, limit=limit))

    @router.post("/files/read", response_model=ReadFileResponse)
    def files_read(req: ReadFileRequest) -> ReadFileResponse:
        if not data_dir:
            raise HTTPException(status_code=400, detail="data_dir not configured")
        base = os.path.abspath(data_dir)
        path = req.path
        if not os.path.isabs(path):
            path = os.path.join(base, path)
        target = os.path.abspath(path)
        if not target.startswith(base):
            raise HTTPException(status_code=403, detail="path is outside allowed data_dir")
        if not os.path.exists(target):
            raise HTTPException(status_code=404, detail="file not found")
        with open(target, "r", encoding="utf-8") as handle:
            content = handle.read()
        return ReadFileResponse(content=content)

    @router.get("/audit", response_model=AuditResponse)
    def audit_read(limit: int = 100) -> AuditResponse:
        if not data_dir:
            raise HTTPException(status_code=400, detail="data_dir not configured")
        path = os.path.join(data_dir, "audit.jsonl")
        if not os.path.exists(path):
            return AuditResponse(events=[])
        events: list[dict[str, Any]] = []
        with open(path, "r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                events.append(json.loads(line))
        return AuditResponse(events=events[-limit:])

    @router.post("/send", response_model=dict)
    def send_message(req: SendRequest) -> dict[str, str]:
        if req.channel == "telegram":
            if not telegram_token:
                raise HTTPException(status_code=400, detail="Telegram not configured")
            adapter = TelegramAdapter(telegram_token)
            if req.image_path:
                caption = req.text or None
                if caption and len(caption) > 1024:
                    adapter.send_photo(chat_id=int(req.target), photo_path=req.image_path, caption=caption[:1024])
                    adapter.send_message(chat_id=int(req.target), text=caption)
                else:
                    adapter.send_photo(chat_id=int(req.target), photo_path=req.image_path, caption=caption)
            else:
                adapter.send_message(chat_id=int(req.target), text=req.text)
            if data_dir:
                log_event(data_dir, "send.telegram", {"target": req.target})
            return {"status": "ok"}
        if req.channel == "teams":
            if not msteams_creds:
                raise HTTPException(status_code=400, detail="Teams not configured")
            service_url = req.service_url or msteams_creds.get("service_url")
            if not service_url:
                raise HTTPException(status_code=400, detail="service_url required for Teams")
            TeamsAdapter(
                app_id=msteams_creds["app_id"],
                app_password=msteams_creds["app_password"],
                tenant_id=msteams_creds["tenant_id"],
            ).send_message(service_url=service_url, conversation_id=req.target, text=req.text)
            if data_dir:
                log_event(data_dir, "send.teams", {"target": req.target})
            return {"status": "ok"}
        raise HTTPException(status_code=400, detail="unsupported channel")

    @router.get("/schema")
    def schema() -> dict[str, Any]:
        return {
            "tools": {
                "jobs.schedule": {
                    "input": {
                        "name": "string",
                        "run_at": "iso8601",
                        "payload": "object",
                        "cron_expr": "string? (cron expression for recurring jobs)",
                    }
                },
                "jobs.list": {"input": {}},
                "jobs.runs": {"input": {"job_id": "string?", "limit": "int?"}},
                "jobs.cancel": {"input": {"job_id": "string"}},
                "jobs.clear_all": {"input": {}},
                "send.message": {
                    "input": {
                        "channel": "string",
                        "target": "string",
                        "text": "string?",
                        "image_path": "string? (Telegram only)",
                        "service_url": "string?",
                    }
                },
                "files.read": {"input": {"path": "string"}},
                "audit.read": {"input": {"limit": "int?"}},
                "tasks.clear_all": {"input": {}},
                "app.restart": {"input": {"reason": "string?"}},
            }
        }

    @router.get("/config")
    def mcp_config() -> dict[str, Any]:
        """Return an MCP server configuration block that Copilot CLI can consume."""
        host = os.getenv("copenclaw_HOST", "127.0.0.1")
        port = os.getenv("copenclaw_PORT", "18790")
        base = f"http://{host}:{port}/mcp"
        headers: dict[str, str] = {}
        if mcp_token:
            headers["x-mcp-token"] = mcp_token
        return {
            "mcpServers": {
                "copenclaw": {
                    "type": "http",
                    "url": base,
                    "headers": headers,
                    "tools": [
                        "jobs.schedule",
                        "jobs.list",
                        "jobs.runs",
                        "jobs.cancel",
                        "jobs.clear_all",
                        "send.message",
                        "files.read",
                        "audit.read",
                        "tasks.clear_all",
                        "app.restart",
                    ],
                }
            }
        }

    return router

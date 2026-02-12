from datetime import datetime, timedelta

from fastapi.testclient import TestClient

from copenclaw.core.gateway import create_app
import json

def _client() -> TestClient:
    return TestClient(create_app())

def _jsonrpc(client: TestClient, method: str, params: dict | None = None, req_id: int = 1):
    return client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params or {},
    })

def _tool_call(client: TestClient, name: str, arguments: dict, req_id: int = 1) -> dict:
    resp = _jsonrpc(client, "tools/call", {"name": name, "arguments": arguments}, req_id=req_id)
    payload = resp.json()["result"]
    assert payload["content"]
    return {
        "isError": payload.get("isError", False),
        "text": payload["content"][0]["text"],
    }

def test_schedule_and_list() -> None:
    client = _client()
    run_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    result = _tool_call(client, "jobs_schedule", {
        "name": "test-job",
        "run_at": run_at,
        "prompt": "say hi",
        "channel": "telegram",
        "target": "123",
    })
    assert result["isError"] is False
    data = json.loads(result["text"])
    assert data["name"] == "test-job"
    assert data["job_id"].startswith("job-")

    list_result = _tool_call(client, "jobs_list", {})
    assert list_result["isError"] is False
    jobs = json.loads(list_result["text"])["jobs"]
    assert len(jobs) >= 1

def test_schedule_with_cron() -> None:
    client = _client()
    run_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    result = _tool_call(client, "jobs_schedule", {
        "name": "cron-job",
        "run_at": run_at,
        "prompt": "check",
        "channel": "telegram",
        "target": "1",
        "cron_expr": "*/10 * * * *",
    })
    assert result["isError"] is False
    assert json.loads(result["text"])["cron_expr"] == "*/10 * * * *"

def test_schedule_bad_cron() -> None:
    client = _client()
    run_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    result = _tool_call(client, "jobs_schedule", {
        "name": "bad-cron",
        "run_at": run_at,
        "prompt": "x",
        "channel": "telegram",
        "target": "1",
        "cron_expr": "invalid",
    })
    assert result["isError"] is True

def test_schedule_invalid_payload() -> None:
    client = _client()
    run_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    result = _tool_call(client, "jobs_schedule", {
        "name": "no-prompt",
        "run_at": run_at,
        "channel": "telegram",
        "target": "1",
    })
    assert result["isError"] is True

def test_cancel_job() -> None:
    client = _client()
    run_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    created = _tool_call(client, "jobs_schedule", {
        "name": "to-cancel",
        "run_at": run_at,
        "prompt": "x",
        "channel": "telegram",
        "target": "1",
    })
    job_id = json.loads(created["text"])["job_id"]
    cancelled = _tool_call(client, "jobs_cancel", {"job_id": job_id})
    assert cancelled["isError"] is False
    assert json.loads(cancelled["text"])["status"] == "cancelled"

def test_cancel_nonexistent() -> None:
    client = _client()
    result = _tool_call(client, "jobs_cancel", {"job_id": "nope"})
    assert result["isError"] is True

def test_runs_empty() -> None:
    client = _client()
    result = _tool_call(client, "jobs_runs", {})
    assert result["isError"] is False
    runs = json.loads(result["text"])["runs"]
    assert runs == [] or isinstance(runs, list)

def test_schedule_bad_run_at() -> None:
    client = _client()
    result = _tool_call(client, "jobs_schedule", {
        "name": "bad-time",
        "run_at": "not-a-date",
        "prompt": "x",
        "channel": "telegram",
        "target": "1",
    })
    assert result["isError"] is True

def test_clear_all_jobs() -> None:
    client = _client()
    run_at = (datetime.utcnow() + timedelta(hours=1)).isoformat()
    # Schedule a few jobs
    _tool_call(client, "jobs_schedule", {
        "name": "j1", "run_at": run_at, "prompt": "a",
        "channel": "telegram", "target": "1",
    })
    _tool_call(client, "jobs_schedule", {
        "name": "j2", "run_at": run_at, "prompt": "b",
        "channel": "telegram", "target": "1",
    })
    # Verify they exist
    before = json.loads(_tool_call(client, "jobs_list", {})["text"])
    assert len(before["jobs"]) >= 2

    # Clear all
    result = _tool_call(client, "jobs_clear_all", {})
    assert result["isError"] is False
    data = json.loads(result["text"])
    assert data["status"] == "cleared"
    assert data["cleared"] >= 2

    # Verify list is empty
    after = json.loads(_tool_call(client, "jobs_list", {})["text"])
    assert len(after["jobs"]) == 0

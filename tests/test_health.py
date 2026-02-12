from fastapi.testclient import TestClient

from copenclaw.core.gateway import create_app


def _jsonrpc(client: TestClient, method: str, params: dict | None = None, req_id: int = 1):
    return client.post("/mcp", json={
        "jsonrpc": "2.0",
        "id": req_id,
        "method": method,
        "params": params or {},
    })

def test_health() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok"}

def test_control_status() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get("/control/status")
    assert response.status_code == 200
    data = response.json()
    assert "sessions" in data
    assert "jobs" in data
    assert "brain_session_id" in data
    assert "brain_initialized" in data

def test_control_metrics() -> None:
    app = create_app()
    client = TestClient(app)
    response = client.get("/control/metrics")
    assert response.status_code == 200
    data = response.json()
    assert "total_jobs" in data
    assert "pending_jobs" in data
    assert "cancelled_jobs" in data
    assert "recurring_jobs" in data
    assert "sessions" in data

def test_mcp_health() -> None:
    app = create_app()
    client = TestClient(app)
    response = _jsonrpc(client, "ping")
    assert response.status_code == 200
    assert response.json()["result"] == {}

def test_mcp_tools() -> None:
    app = create_app()
    client = TestClient(app)
    response = _jsonrpc(client, "tools/list")
    assert response.status_code == 200
    tools = response.json()["result"]["tools"]
    names = [t["name"] for t in tools]
    assert "jobs_schedule" in names
    assert "jobs_cancel" in names
import json
import os
import tempfile

from copenclaw.core.audit import generate_request_id, log_event

def test_generate_request_id() -> None:
    rid = generate_request_id()
    assert len(rid) == 12
    assert rid.isalnum()

def test_log_event_creates_file() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_event(tmpdir, "test.event", {"key": "val"})
        path = os.path.join(tmpdir, "audit.jsonl")
        assert os.path.exists(path)
        with open(path, "r") as f:
            line = f.readline()
        record = json.loads(line)
        assert record["type"] == "test.event"
        assert record["payload"]["key"] == "val"
        assert "ts" in record

def test_log_event_with_request_id() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_event(tmpdir, "test.event", {"k": "v"}, request_id="abc123")
        path = os.path.join(tmpdir, "audit.jsonl")
        with open(path, "r") as f:
            record = json.loads(f.readline())
        assert record["request_id"] == "abc123"

def test_log_event_appends() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        log_event(tmpdir, "a", {})
        log_event(tmpdir, "b", {})
        path = os.path.join(tmpdir, "audit.jsonl")
        with open(path, "r") as f:
            lines = [l.strip() for l in f if l.strip()]
        assert len(lines) == 2
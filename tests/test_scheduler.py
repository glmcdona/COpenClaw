import os
import json
import tempfile
from datetime import datetime, timedelta

from copenclaw.core.scheduler import Scheduler, ScheduledJob

def test_schedule_and_list() -> None:
    sched = Scheduler()
    job = sched.schedule("test", datetime.utcnow() + timedelta(hours=1), {"prompt": "hello", "channel": "telegram", "target": "123"})
    assert job.job_id.startswith("job-")
    assert len(sched.list()) == 1

def test_due_returns_past_jobs() -> None:
    sched = Scheduler()
    past = datetime.utcnow() - timedelta(minutes=5)
    sched.schedule("past-job", past, {"prompt": "hi", "channel": "telegram", "target": "1"})
    future = datetime.utcnow() + timedelta(hours=1)
    sched.schedule("future-job", future, {"prompt": "hi", "channel": "telegram", "target": "1"})
    due = sched.due()
    assert len(due) == 1
    assert due[0].name == "past-job"

def test_mark_completed() -> None:
    sched = Scheduler()
    job = sched.schedule("j", datetime.utcnow() - timedelta(minutes=1), {"prompt": "x", "channel": "telegram", "target": "1"})
    sched.mark_completed(job.job_id)
    assert sched.get(job.job_id).completed_at is not None
    assert len(sched.due()) == 0

def test_cancel() -> None:
    sched = Scheduler()
    job = sched.schedule("j", datetime.utcnow() - timedelta(minutes=1), {"prompt": "x", "channel": "telegram", "target": "1"})
    assert sched.cancel(job.job_id) is True
    assert sched.get(job.job_id).cancelled is True
    assert len(sched.due()) == 0

def test_cancel_nonexistent() -> None:
    sched = Scheduler()
    assert sched.cancel("nonexistent") is False

def test_cron_recurring() -> None:
    sched = Scheduler()
    past = datetime.utcnow() - timedelta(minutes=1)
    job = sched.schedule("cron-job", past, {"prompt": "x", "channel": "telegram", "target": "1"}, cron_expr="*/5 * * * *")
    assert job.cron_expr == "*/5 * * * *"
    sched.mark_completed(job.job_id)
    # Should NOT be completed — should advance to next run
    updated = sched.get(job.job_id)
    assert updated.completed_at is None
    assert updated.run_at > past

def test_validate_payload_ok() -> None:
    errors = Scheduler.validate_payload({"prompt": "hello", "channel": "telegram", "target": "123"})
    assert errors == []

def test_validate_payload_missing_prompt() -> None:
    errors = Scheduler.validate_payload({"channel": "telegram", "target": "123"})
    assert any("prompt" in e for e in errors)

def test_validate_payload_bad_channel() -> None:
    errors = Scheduler.validate_payload({"prompt": "x", "channel": "slack", "target": "1"})
    assert any("unsupported" in e for e in errors)

def test_validate_payload_missing_target() -> None:
    errors = Scheduler.validate_payload({"prompt": "x", "channel": "telegram"})
    assert any("target" in e for e in errors)

def test_validate_payload_teams_missing_service_url() -> None:
    errors = Scheduler.validate_payload({"prompt": "x", "channel": "teams", "target": "1"})
    assert any("service_url" in e for e in errors)

def test_validate_cron() -> None:
    assert Scheduler.validate_cron("*/5 * * * *") is True
    assert Scheduler.validate_cron("bad") is False

def test_persistence() -> None:
    with tempfile.TemporaryDirectory() as tmpdir:
        store = os.path.join(tmpdir, "jobs.json")
        log = os.path.join(tmpdir, "runs.jsonl")
        s1 = Scheduler(store_path=store, run_log_path=log)
        job = s1.schedule("p", datetime.utcnow(), {"prompt": "x", "channel": "telegram", "target": "1"})
        s1.log_run(job.job_id, "ok")
        # Reload from disk
        s2 = Scheduler(store_path=store, run_log_path=log)
        assert len(s2.list()) == 1
        assert s2.list()[0].job_id == job.job_id
        runs = s2.list_runs()
        assert len(runs) == 1
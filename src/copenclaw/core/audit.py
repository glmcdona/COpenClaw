from __future__ import annotations

from datetime import datetime
import json
import os
import uuid
from typing import Any, Dict, Optional

from copenclaw.core.logging_config import append_to_file, get_audit_log_path

def generate_request_id() -> str:
    """Generate a short unique request ID."""
    return uuid.uuid4().hex[:12]

def log_event(
    data_dir: str,
    event_type: str,
    payload: Dict[str, Any],
    request_id: Optional[str] = None,
) -> None:
    os.makedirs(data_dir, exist_ok=True)
    path = os.path.join(data_dir, "audit.jsonl")
    record: Dict[str, Any] = {
        "ts": datetime.utcnow().isoformat() + "Z",
        "type": event_type,
        "payload": payload,
    }
    if request_id:
        record["request_id"] = request_id
    line = json.dumps(record)
    with open(path, "a", encoding="utf-8") as handle:
        handle.write(line + "\n")
    # Mirror to centralized audit log
    try:
        central = get_audit_log_path()
        if central != path:
            append_to_file(central, line)
    except Exception:  # noqa: BLE001
        pass

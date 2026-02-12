from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
import json
import os
import random
import string
from typing import Dict, List, Optional


@dataclass
class PairingRequest:
    code: str
    channel: str
    sender_id: str
    created_at: datetime = field(default_factory=datetime.utcnow)


class PairingStore:
    def __init__(self, store_path: str, code_length: int = 6) -> None:
        self._store_path = store_path
        self._code_length = max(4, code_length)
        self._allowlist: Dict[str, List[str]] = {}
        self._pending: Dict[str, PairingRequest] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._store_path):
            return
        with open(self._store_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self._allowlist = raw.get("allowlist", {})
        pending = raw.get("pending", {})
        for key, item in pending.items():
            self._pending[key] = PairingRequest(
                code=item["code"],
                channel=item["channel"],
                sender_id=item["sender_id"],
                created_at=datetime.fromisoformat(item["created_at"]),
            )

    def _save(self) -> None:
        dir_path = os.path.dirname(self._store_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        payload = {
            "allowlist": self._allowlist,
            "pending": {
                key: {
                    "code": req.code,
                    "channel": req.channel,
                    "sender_id": req.sender_id,
                    "created_at": req.created_at.isoformat(),
                }
                for key, req in self._pending.items()
            },
        }
        with open(self._store_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def is_allowed(self, channel: str, sender_id: str) -> bool:
        return sender_id in self._allowlist.get(channel, [])

    def add_allowed(self, channel: str, sender_id: str) -> None:
        current = set(self._allowlist.get(channel, []))
        current.add(sender_id)
        self._allowlist[channel] = sorted(current)
        self._save()

    def list_pending(self) -> list[PairingRequest]:
        self._prune_expired()
        return list(self._pending.values())

    def request_code(self, channel: str, sender_id: str) -> str:
        self._prune_expired()
        key = f"{channel}:{sender_id}"
        if key in self._pending:
            return self._pending[key].code
        code = "".join(random.choices(string.digits, k=self._code_length))
        self._pending[key] = PairingRequest(code=code, channel=channel, sender_id=sender_id)
        self._save()
        return code

    def approve(self, code: str) -> Optional[PairingRequest]:
        self._prune_expired()
        for key, req in list(self._pending.items()):
            if req.code == code:
                self.add_allowed(req.channel, req.sender_id)
                self._pending.pop(key, None)
                self._save()
                return req
        return None

    def _prune_expired(self) -> None:
        cutoff = datetime.utcnow() - timedelta(hours=1)
        removed = False
        for key, req in list(self._pending.items()):
            if req.created_at < cutoff:
                self._pending.pop(key, None)
                removed = True
        if removed:
            self._save()
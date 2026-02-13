"""Simple runtime allowlist for authorized users.

Persists to a JSON file so that owner auto-authorization (and any
future runtime additions) survive restarts without requiring .env edits.
"""
from __future__ import annotations

import json
import os
from typing import Dict, List


class PairingStore:
    """Persistent per-channel allowlist of authorized sender IDs."""

    def __init__(self, store_path: str, **_kwargs) -> None:
        self._store_path = store_path
        self._allowlist: Dict[str, List[str]] = {}
        self._load()

    def _load(self) -> None:
        if not os.path.exists(self._store_path):
            return
        with open(self._store_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        self._allowlist = raw.get("allowlist", {})

    def _save(self) -> None:
        dir_path = os.path.dirname(self._store_path)
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)
        payload = {"allowlist": self._allowlist}
        with open(self._store_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def is_allowed(self, channel: str, sender_id: str) -> bool:
        return sender_id in self._allowlist.get(channel, [])

    def add_allowed(self, channel: str, sender_id: str) -> None:
        current = set(self._allowlist.get(channel, []))
        current.add(sender_id)
        self._allowlist[channel] = sorted(current)
        self._save()
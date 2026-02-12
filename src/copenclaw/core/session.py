from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import json
import logging
import os
from typing import Any, Dict, List, Optional

logger = logging.getLogger("copenclaw.session")

# Conversation history defaults
DEFAULT_MAX_TURNS = 20          # number of user/assistant exchange pairs to keep
DEFAULT_MAX_MSG_CHARS = 2000    # max chars stored per individual message
DEFAULT_MAX_CONTEXT_CHARS = 8000  # soft cap on total context prefix length

@dataclass
class Session:
    key: str
    updated_at: datetime = field(default_factory=datetime.utcnow)
    data: Dict[str, Any] = field(default_factory=dict)

class SessionStore:
    def __init__(
        self,
        store_path: Optional[str] = None,
        max_turns: int = DEFAULT_MAX_TURNS,
        max_msg_chars: int = DEFAULT_MAX_MSG_CHARS,
        max_context_chars: int = DEFAULT_MAX_CONTEXT_CHARS,
    ) -> None:
        self._sessions: Dict[str, Session] = {}
        self._store_path = store_path
        self.max_turns = max_turns
        self.max_msg_chars = max_msg_chars
        self.max_context_chars = max_context_chars
        if store_path:
            self._load()

    def _load(self) -> None:
        if not self._store_path or not os.path.exists(self._store_path):
            return
        with open(self._store_path, "r", encoding="utf-8") as handle:
            raw = json.load(handle)
        for item in raw.get("sessions", []):
            self._sessions[item["key"]] = Session(
                key=item["key"],
                updated_at=datetime.fromisoformat(item["updated_at"]),
                data=item.get("data", {}),
            )

    def _save(self) -> None:
        if not self._store_path:
            return
        os.makedirs(os.path.dirname(self._store_path), exist_ok=True)
        payload = {
            "sessions": [
                {
                    "key": session.key,
                    "updated_at": session.updated_at.isoformat(),
                    "data": session.data,
                }
                for session in self._sessions.values()
            ]
        }
        with open(self._store_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)

    def get(self, key: str) -> Optional[Session]:
        return self._sessions.get(key)

    def upsert(self, key: str) -> Session:
        session = self._sessions.get(key)
        if session is None:
            session = Session(key=key)
            self._sessions[key] = session
        session.updated_at = datetime.utcnow()
        self._save()
        return session

    def list_keys(self) -> list[str]:
        return list(self._sessions.keys())

    # ── Conversation history ──────────────────────────────────

    def append_message(self, key: str, role: str, text: str) -> None:
        """Append a message to the conversation history for *key*.

        Messages are stored in ``session.data["messages"]`` as a list of
        ``{"role": "user"|"assistant", "text": "...", "ts": "..."}``.
        The list is trimmed to ``max_turns * 2`` entries (each turn is
        one user + one assistant message).
        """
        session = self.upsert(key)
        messages: List[dict] = session.data.get("messages", [])

        # Truncate individual message if excessively long
        stored_text = text[:self.max_msg_chars] if len(text) > self.max_msg_chars else text

        messages.append({
            "role": role,
            "text": stored_text,
            "ts": datetime.utcnow().isoformat(),
        })

        # Keep only the last max_turns * 2 messages
        max_messages = self.max_turns * 2
        if len(messages) > max_messages:
            messages = messages[-max_messages:]

        session.data["messages"] = messages
        self._save()

    def get_history(self, key: str, max_turns: Optional[int] = None) -> List[dict]:
        """Return recent conversation messages for *key*.

        Returns up to ``max_turns * 2`` messages (user + assistant pairs).
        """
        session = self._sessions.get(key)
        if not session:
            return []
        messages: List[dict] = session.data.get("messages", [])
        limit = (max_turns or self.max_turns) * 2
        return messages[-limit:]

    def build_context_prompt(self, key: str, current_message: str) -> str:
        """Build a prompt with conversation history prepended.

        If there is no history, returns *current_message* unchanged.
        If the total context exceeds ``max_context_chars``, older
        messages are dropped until it fits.
        """
        history = self.get_history(key)
        if not history:
            return current_message

        # Build context lines, oldest first
        context_lines: list[str] = []
        for msg in history:
            prefix = "User" if msg["role"] == "user" else "Assistant"
            context_lines.append(f"{prefix}: {msg['text']}")

        # Trim from the front if total context is too long
        context = "\n".join(context_lines)
        while len(context) > self.max_context_chars and context_lines:
            context_lines.pop(0)
            context = "\n".join(context_lines)

        if not context_lines:
            return current_message

        return (
            f"[Conversation history — reply to the current message only]\n"
            f"{context}\n\n"
            f"[Current message]\n"
            f"{current_message}"
        )

    def clear_history(self, key: str) -> None:
        """Clear conversation history for *key*."""
        session = self._sessions.get(key)
        if session and "messages" in session.data:
            session.data["messages"] = []
            self._save()

    # ── Copilot CLI session ID ────────────────────────────────

    def get_copilot_session_id(self, key: str) -> Optional[str]:
        """Return the stored Copilot CLI session ID for *key*, or None."""
        session = self._sessions.get(key)
        if not session:
            return None
        return session.data.get("copilot_session_id")

    def set_copilot_session_id(self, key: str, copilot_session_id: str) -> None:
        """Store the Copilot CLI session ID for *key*."""
        session = self.upsert(key)
        session.data["copilot_session_id"] = copilot_session_id
        self._save()

    def clear_copilot_session_id(self, key: str) -> None:
        """Remove the stored Copilot CLI session ID for *key*."""
        session = self._sessions.get(key)
        if session and "copilot_session_id" in session.data:
            del session.data["copilot_session_id"]
            self._save()

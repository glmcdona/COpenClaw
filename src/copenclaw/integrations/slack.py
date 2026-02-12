"""Slack adapter using the Slack Web API and Events API.

Sends messages via ``chat.postMessage``, receives messages via the
Events API webhook.  Request signature verification uses the
``SLACK_SIGNING_SECRET``.

Required env vars:
    SLACK_BOT_TOKEN      – Bot User OAuth Token (xoxb-...)
    SLACK_SIGNING_SECRET – Used to verify incoming event payloads
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger("copenclaw.slack")

_API_BASE = "https://slack.com/api"
_MAX_TEXT_LENGTH = 4000
_CHUNK_MARGIN = 200

def _split_text(text: str, max_len: int) -> list[str]:
    if len(text) <= max_len:
        return [text]
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + max_len, len(text))
        chunks.append(text[start:end])
        start = end
    return chunks

@dataclass
class SlackAdapter:
    bot_token: str
    signing_secret: str = ""

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.bot_token}",
            "Content-Type": "application/json; charset=utf-8",
        }

    # ── Outbound ──────────────────────────────────────────

    def send_message(self, channel: str, text: str, thread_ts: str | None = None) -> None:
        """Send a text message to a Slack channel or DM."""
        if not text:
            text = "(empty response)"
        max_len = max(1, _MAX_TEXT_LENGTH - _CHUNK_MARGIN)
        chunks = _split_text(text, max_len)
        url = f"{_API_BASE}/chat.postMessage"
        with httpx.Client(timeout=15.0) as client:
            for chunk in chunks:
                payload: dict[str, Any] = {
                    "channel": channel,
                    "text": chunk,
                }
                if thread_ts:
                    payload["thread_ts"] = thread_ts
                resp = client.post(url, json=payload, headers=self._headers())
                if resp.status_code != 200:
                    logger.error(
                        "Slack chat.postMessage failed: %s %s",
                        resp.status_code,
                        resp.text[:500],
                    )
                    return
                data = resp.json()
                if not data.get("ok"):
                    logger.error(
                        "Slack chat.postMessage error: %s",
                        data.get("error", "unknown"),
                    )
                    return

    def send_image(self, channel: str, image_path: str, caption: str | None = None) -> None:
        """Upload and send an image to a Slack channel."""
        if not os.path.isfile(image_path):
            logger.error("Slack sendImage failed: file not found %s", image_path)
            return
        url = f"{_API_BASE}/files.uploadV2"
        with httpx.Client(timeout=30.0) as client:
            with open(image_path, "rb") as f:
                resp = client.post(
                    url,
                    headers={"Authorization": f"Bearer {self.bot_token}"},
                    data={
                        "channel_id": channel,
                        "initial_comment": caption or "",
                        "filename": os.path.basename(image_path),
                    },
                    files={"file": (os.path.basename(image_path), f)},
                )
                if resp.status_code != 200:
                    logger.error(
                        "Slack files.uploadV2 failed: %s %s",
                        resp.status_code,
                        resp.text[:500],
                    )
                    return
                data = resp.json()
                if not data.get("ok"):
                    logger.error(
                        "Slack files.uploadV2 error: %s",
                        data.get("error", "unknown"),
                    )

    def send_typing(self, channel: str) -> None:
        """Indicate typing in a channel (ephemeral, best-effort)."""
        # Slack doesn't have a public typing indicator API for bots.
        # This is a no-op placeholder for interface consistency.
        pass

    # ── Request signature verification ───────────────────

    def verify_signature(
        self,
        body: bytes,
        timestamp: str,
        signature: str,
    ) -> bool:
        """Verify a Slack request signature.

        See https://api.slack.com/authentication/verifying-requests-from-slack
        """
        if not self.signing_secret:
            logger.warning("Slack signing secret not configured — skipping verification")
            return True
        # Reject requests older than 5 minutes (replay protection)
        try:
            if abs(time.time() - float(timestamp)) > 300:
                logger.warning("Slack request timestamp too old")
                return False
        except ValueError:
            return False
        sig_basestring = f"v0:{timestamp}:{body.decode('utf-8')}"
        computed = "v0=" + hmac.new(
            self.signing_secret.encode("utf-8"),
            sig_basestring.encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()
        return hmac.compare_digest(computed, signature)

    # ── Inbound parsing ──────────────────────────────────

    @staticmethod
    def parse_event(payload: dict[str, Any]) -> dict[str, Any] | None:
        """Parse a Slack Events API payload.

        Handles url_verification challenge and message events.
        Returns a dict with keys: sender, text, channel, thread_ts, event_type
        or None if not a relevant message.
        """
        # URL verification challenge
        if payload.get("type") == "url_verification":
            return {"type": "url_verification", "challenge": payload.get("challenge", "")}

        event = payload.get("event", {})
        event_type = event.get("type", "")

        # Only handle message events (not bot messages, not edits, not deletes)
        if event_type != "message":
            return None
        # Skip bot messages to avoid loops
        if event.get("bot_id") or event.get("subtype"):
            return None

        return {
            "type": "message",
            "sender": event.get("user", ""),
            "text": event.get("text", ""),
            "channel": event.get("channel", ""),
            "thread_ts": event.get("thread_ts", ""),
            "ts": event.get("ts", ""),
            "team": payload.get("team_id", ""),
        }

    # ── User info / DM helpers ───────────────────────────

    def open_dm(self, user_id: str) -> str | None:
        """Open a DM channel with a user. Returns the channel ID."""
        url = f"{_API_BASE}/conversations.open"
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(
                url,
                json={"users": user_id},
                headers=self._headers(),
            )
            if resp.status_code != 200:
                return None
            data = resp.json()
            if not data.get("ok"):
                return None
            return data.get("channel", {}).get("id")

    # ── Lifecycle stubs ──────────────────────────────────

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None
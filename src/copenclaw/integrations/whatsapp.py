"""WhatsApp Business Cloud API adapter.

Uses Meta's Cloud API (graph.facebook.com) for sending and receiving
messages.  Incoming messages arrive via a webhook that Meta calls;
outgoing messages are sent via the REST API.

Required env vars:
    WHATSAPP_PHONE_NUMBER_ID  – The phone number ID from Meta dashboard
    WHATSAPP_ACCESS_TOKEN     – Permanent or long-lived access token
    WHATSAPP_VERIFY_TOKEN     – Arbitrary string used during webhook verification
"""
from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from typing import Any, Optional

import httpx

logger = logging.getLogger("copenclaw.whatsapp")

_API_BASE = "https://graph.facebook.com/v21.0"
_MAX_TEXT_LENGTH = 4096
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
class WhatsAppAdapter:
    phone_number_id: str
    access_token: str
    verify_token: str = ""

    def _headers(self) -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
        }

    def _messages_url(self) -> str:
        return f"{_API_BASE}/{self.phone_number_id}/messages"

    # ── Outbound ──────────────────────────────────────────

    def send_message(self, to: str, text: str) -> None:
        """Send a text message to a WhatsApp number (E.164 format, no +)."""
        if not text:
            text = "(empty response)"
        max_len = max(1, _MAX_TEXT_LENGTH - _CHUNK_MARGIN)
        chunks = _split_text(text, max_len)
        with httpx.Client(timeout=15.0) as client:
            for chunk in chunks:
                payload = {
                    "messaging_product": "whatsapp",
                    "to": to,
                    "type": "text",
                    "text": {"body": chunk},
                }
                resp = client.post(
                    self._messages_url(),
                    json=payload,
                    headers=self._headers(),
                )
                if resp.status_code not in (200, 201):
                    logger.error(
                        "WhatsApp sendMessage failed: %s %s",
                        resp.status_code,
                        resp.text[:500],
                    )
                    return

    def send_image(self, to: str, image_url: str, caption: str | None = None) -> None:
        """Send an image message via URL."""
        payload: dict[str, Any] = {
            "messaging_product": "whatsapp",
            "to": to,
            "type": "image",
            "image": {"link": image_url},
        }
        if caption:
            payload["image"]["caption"] = caption[:1024]
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(
                self._messages_url(),
                json=payload,
                headers=self._headers(),
            )
            if resp.status_code not in (200, 201):
                logger.error(
                    "WhatsApp sendImage failed: %s %s",
                    resp.status_code,
                    resp.text[:500],
                )

    def mark_read(self, message_id: str) -> None:
        """Mark a message as read (sends blue ticks)."""
        payload = {
            "messaging_product": "whatsapp",
            "status": "read",
            "message_id": message_id,
        }
        try:
            with httpx.Client(timeout=5.0) as client:
                client.post(
                    self._messages_url(),
                    json=payload,
                    headers=self._headers(),
                )
        except Exception as exc:  # noqa: BLE001
            logger.debug("mark_read failed (non-critical): %s", exc)

    # ── Webhook verification ─────────────────────────────

    def verify_webhook(self, params: dict[str, str]) -> tuple[bool, str]:
        """Handle GET /whatsapp/webhook verification from Meta.

        Returns (ok, response_body).  If ok is True, return the
        challenge string with HTTP 200.  Otherwise return an error.
        """
        mode = params.get("hub.mode", "")
        token = params.get("hub.verify_token", "")
        challenge = params.get("hub.challenge", "")
        if mode == "subscribe" and token == self.verify_token:
            logger.info("WhatsApp webhook verified")
            return True, challenge
        logger.warning("WhatsApp webhook verification failed")
        return False, "Verification failed"

    # ── Inbound parsing ──────────────────────────────────

    @staticmethod
    def parse_webhook(body: dict[str, Any]) -> list[dict[str, Any]]:
        """Parse an incoming webhook payload and return a list of messages.

        Each returned dict has keys: sender, text, message_id, timestamp.
        Returns an empty list if no messages are present.
        """
        messages: list[dict[str, Any]] = []
        for entry in body.get("entry", []):
            for change in entry.get("changes", []):
                value = change.get("value", {})
                if value.get("messaging_product") != "whatsapp":
                    continue
                for msg in value.get("messages", []):
                    parsed: dict[str, Any] = {
                        "sender": msg.get("from", ""),
                        "message_id": msg.get("id", ""),
                        "timestamp": msg.get("timestamp", ""),
                        "text": "",
                    }
                    msg_type = msg.get("type", "")
                    if msg_type == "text":
                        parsed["text"] = msg.get("text", {}).get("body", "")
                    elif msg_type == "image":
                        caption = msg.get("image", {}).get("caption", "")
                        parsed["text"] = caption or "[image]"
                    elif msg_type == "document":
                        caption = msg.get("document", {}).get("caption", "")
                        parsed["text"] = caption or "[document]"
                    elif msg_type == "audio":
                        parsed["text"] = "[audio message]"
                    elif msg_type == "video":
                        caption = msg.get("video", {}).get("caption", "")
                        parsed["text"] = caption or "[video]"
                    else:
                        parsed["text"] = f"[{msg_type}]"
                    if parsed["text"]:
                        messages.append(parsed)
        return messages

    # ── Lifecycle stubs ──────────────────────────────────

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None
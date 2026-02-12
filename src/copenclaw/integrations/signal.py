"""Signal adapter using signal-cli-rest-api.

Connects to a self-hosted signal-cli-rest-api instance for sending and
receiving Signal messages.  Incoming messages are received via polling
the ``/v1/receive`` endpoint (similar to telegram polling).

Required env vars:
    SIGNAL_API_URL       – Base URL of signal-cli-rest-api (e.g. http://localhost:8080)
    SIGNAL_PHONE_NUMBER  – The registered Signal phone number (E.164 format)
"""
from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger("copenclaw.signal")

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
class SignalAdapter:
    api_url: str
    phone_number: str
    _polling_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def _base_url(self) -> str:
        return self.api_url.rstrip("/")

    # ── Outbound ──────────────────────────────────────────

    def send_message(self, recipient: str, text: str) -> None:
        """Send a text message to a Signal recipient (phone number or group ID)."""
        if not text:
            text = "(empty response)"
        max_len = max(1, _MAX_TEXT_LENGTH - _CHUNK_MARGIN)
        chunks = _split_text(text, max_len)
        url = f"{self._base_url()}/v2/send"
        with httpx.Client(timeout=15.0) as client:
            for chunk in chunks:
                payload: dict[str, Any] = {
                    "message": chunk,
                    "number": self.phone_number,
                    "recipients": [recipient],
                }
                resp = client.post(url, json=payload)
                if resp.status_code not in (200, 201):
                    logger.error(
                        "Signal sendMessage failed: %s %s",
                        resp.status_code,
                        resp.text[:500],
                    )
                    return

    def send_image(self, recipient: str, image_path: str, caption: str | None = None) -> None:
        """Send an image as a base64-encoded attachment."""
        import base64

        if not os.path.isfile(image_path):
            logger.error("Signal sendImage failed: file not found %s", image_path)
            return

        with open(image_path, "rb") as f:
            image_data = base64.b64encode(f.read()).decode("utf-8")

        # Determine content type from extension
        ext = os.path.splitext(image_path)[1].lower()
        content_type_map = {
            ".png": "image/png",
            ".jpg": "image/jpeg",
            ".jpeg": "image/jpeg",
            ".gif": "image/gif",
            ".webp": "image/webp",
        }
        content_type = content_type_map.get(ext, "application/octet-stream")

        url = f"{self._base_url()}/v2/send"
        payload: dict[str, Any] = {
            "message": caption or "",
            "number": self.phone_number,
            "recipients": [recipient],
            "base64_attachments": [f"data:{content_type};base64,{image_data}"],
        }
        with httpx.Client(timeout=30.0) as client:
            resp = client.post(url, json=payload)
            if resp.status_code not in (200, 201):
                logger.error(
                    "Signal sendImage failed: %s %s",
                    resp.status_code,
                    resp.text[:500],
                )

    def send_typing(self, recipient: str) -> None:
        """Send a typing indicator (if supported by the API version)."""
        url = f"{self._base_url()}/v1/typing-indicator/{self.phone_number}"
        payload = {"recipient": recipient}
        try:
            with httpx.Client(timeout=5.0) as client:
                client.put(url, json=payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Signal typing indicator failed (non-critical): %s", exc)

    # ── Inbound polling ──────────────────────────────────

    def receive_messages(self) -> list[dict[str, Any]]:
        """Poll the signal-cli-rest-api for new messages."""
        url = f"{self._base_url()}/v1/receive/{self.phone_number}"
        try:
            with httpx.Client(timeout=30.0) as client:
                resp = client.get(url)
                if resp.status_code != 200:
                    logger.error(
                        "Signal receive failed: %s %s",
                        resp.status_code,
                        resp.text[:300],
                    )
                    return []
                return resp.json()
        except Exception as exc:  # noqa: BLE001
            logger.error("Signal receive error: %s", exc)
            return []

    @staticmethod
    def parse_message(envelope: dict[str, Any]) -> dict[str, Any] | None:
        """Parse a single envelope from the receive endpoint.

        Returns a dict with keys: sender, text, timestamp, group_id
        or None if not a text message.
        """
        data = envelope.get("envelope", envelope)
        data_msg = data.get("dataMessage")
        if not data_msg:
            return None
        text = data_msg.get("message", "")
        if not text:
            return None
        sender = data.get("source", "") or data.get("sourceNumber", "")
        timestamp = data_msg.get("timestamp", "")
        group_info = data_msg.get("groupInfo", {})
        group_id = group_info.get("groupId", "") if group_info else ""
        return {
            "sender": sender,
            "text": text,
            "timestamp": str(timestamp),
            "group_id": group_id,
        }

    def start_polling(self, on_update: Callable[[dict[str, Any]], None]) -> None:
        """Start a background thread that polls signal-cli for messages."""

        def _poll_loop() -> None:
            logger.info("Signal polling started")
            while not self._stop_event.is_set():
                try:
                    envelopes = self.receive_messages()
                    for envelope in envelopes:
                        parsed = self.parse_message(envelope)
                        if parsed:
                            try:
                                on_update(parsed)
                            except Exception as exc:  # noqa: BLE001
                                logger.error("Error processing Signal message: %s", exc)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Signal polling error: %s", exc)
                    time.sleep(5)
                # Small delay between polls
                self._stop_event.wait(2.0)

        self._polling_thread = threading.Thread(
            target=_poll_loop, daemon=True, name="signal-poller"
        )
        self._polling_thread.start()

    def stop_polling(self) -> None:
        self._stop_event.set()

    # ── Lifecycle ─────────────────────────────────────────

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stop_polling()
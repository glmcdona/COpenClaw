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

    def _request(
        self,
        method: str,
        path: str,
        *,
        timeout: float,
        log_errors: bool = True,
        **kwargs: Any,
    ) -> httpx.Response | None:
        url = f"{self._base_url()}/{path.lstrip('/')}"
        try:
            with httpx.Client(timeout=timeout) as client:
                return client.request(method, url, **kwargs)
        except httpx.RequestError as exc:
            if log_errors:
                logger.error(
                    "Signal API request failed: %s. Check SIGNAL_API_URL and that signal-cli-rest-api is running.",
                    exc,
                )
            else:
                logger.debug("Signal API request failed (non-critical): %s", exc)
            return None

    def check_connection(self) -> bool:
        """Best-effort check that the Signal REST API is reachable."""
        if not self.api_url:
            logger.error("Signal API URL is missing (set SIGNAL_API_URL).")
            return False
        resp = self._request("GET", "v1/about", timeout=5.0)
        if not resp:
            return False
        if resp.status_code >= 400:
            logger.warning(
                "Signal API check returned %s for /v1/about. Verify SIGNAL_API_URL points to signal-cli-rest-api.",
                resp.status_code,
            )
        return True

    # ── Outbound ──────────────────────────────────────────

    def send_message(self, recipient: str, text: str) -> None:
        """Send a text message to a Signal recipient (phone number or group ID)."""
        if not text:
            text = "(empty response)"
        max_len = max(1, _MAX_TEXT_LENGTH - _CHUNK_MARGIN)
        chunks = _split_text(text, max_len)
        for chunk in chunks:
            payload: dict[str, Any] = {
                "message": chunk,
                "number": self.phone_number,
                "recipients": [recipient],
            }
            resp = self._request("POST", "v2/send", timeout=15.0, json=payload)
            if not resp:
                return
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

        payload: dict[str, Any] = {
            "message": caption or "",
            "number": self.phone_number,
            "recipients": [recipient],
            "base64_attachments": [f"data:{content_type};base64,{image_data}"],
        }
        resp = self._request("POST", "v2/send", timeout=30.0, json=payload)
        if not resp:
            return
        if resp.status_code not in (200, 201):
            logger.error(
                "Signal sendImage failed: %s %s",
                resp.status_code,
                resp.text[:500],
            )

    def send_typing(self, recipient: str) -> None:
        """Send a typing indicator (if supported by the API version)."""
        payload = {"recipient": recipient}
        self._request(
            "PUT",
            f"v1/typing-indicator/{self.phone_number}",
            timeout=5.0,
            log_errors=False,
            json=payload,
        )

    # ── Inbound polling ──────────────────────────────────

    def receive_messages(self) -> list[dict[str, Any]]:
        """Poll the signal-cli-rest-api for new messages."""
        resp = self._request("GET", f"v1/receive/{self.phone_number}", timeout=30.0)
        if not resp:
            return []
        if resp.status_code != 200:
            logger.error(
                "Signal receive failed: %s %s",
                resp.status_code,
                resp.text[:300],
            )
            return []
        return resp.json()

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

from __future__ import annotations

import logging
import os
import time
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

import httpx

logger = logging.getLogger("copenclaw.telegram")

# Telegram API limit for a single message
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


def _unique_path(path: str) -> str:
    if not os.path.exists(path):
        return path
    base, ext = os.path.splitext(path)
    for idx in range(1, 1000):
        candidate = f"{base}-{idx}{ext}"
        if not os.path.exists(candidate):
            return candidate
    return f"{base}-{int(time.time())}{ext}"

@dataclass
class TelegramAdapter:
    token: str
    _polling_thread: Optional[threading.Thread] = field(default=None, init=False, repr=False)
    _stop_event: threading.Event = field(default_factory=threading.Event, init=False, repr=False)

    def _base_url(self) -> str:
        return f"https://api.telegram.org/bot{self.token}"

    def send_typing(self, chat_id: int) -> None:
        """Send 'typing...' chat action indicator."""
        url = f"{self._base_url()}/sendChatAction"
        payload = {"chat_id": chat_id, "action": "typing"}
        try:
            with httpx.Client(timeout=5.0) as client:
                client.post(url, json=payload)
        except Exception as exc:  # noqa: BLE001
            logger.debug("sendChatAction failed (non-critical): %s", exc)

    def start_typing_loop(self, chat_id: int) -> threading.Event:
        """Start a background thread that sends typing indicator every 4s.

        Returns a threading.Event — set it to stop the loop.
        Usage:
            stop = adapter.start_typing_loop(chat_id)
            # ... do long-running work ...
            stop.set()  # stops the typing indicator
        """
        stop_event = threading.Event()

        def _loop() -> None:
            while not stop_event.is_set():
                self.send_typing(chat_id)
                stop_event.wait(4.0)

        t = threading.Thread(target=_loop, daemon=True, name=f"typing-{chat_id}")
        t.start()
        return stop_event

    def send_message(self, chat_id: int, text: str) -> None:
        if not text:
            text = "(empty response)"
        url = f"{self._base_url()}/sendMessage"
        max_len = max(1, _MAX_TEXT_LENGTH - _CHUNK_MARGIN)
        chunks = _split_text(text, max_len)
        with httpx.Client(timeout=15.0) as client:
            for chunk in chunks:
                payload = {"chat_id": chat_id, "text": chunk}
                resp = client.post(url, json=payload)
                if resp.status_code != 200:
                    logger.error(
                        "Telegram sendMessage failed: %s %s",
                        resp.status_code,
                        resp.text[:500],
                    )
                    return  # Don't raise — callers handle gracefully

    def send_photo(self, chat_id: int, photo_path: str, caption: str | None = None) -> None:
        if not os.path.isfile(photo_path):
            logger.error("Telegram sendPhoto failed: file not found %s", photo_path)
            return
        url = f"{self._base_url()}/sendPhoto"
        payload: dict[str, Any] = {"chat_id": chat_id}
        if caption:
            payload["caption"] = caption
        with httpx.Client(timeout=30.0) as client:
            with open(photo_path, "rb") as handle:
                files = {"photo": (os.path.basename(photo_path), handle)}
                resp = client.post(url, data=payload, files=files)
                if resp.status_code != 200:
                    logger.error(
                        "Telegram sendPhoto failed: %s %s",
                        resp.status_code,
                        resp.text[:500],
                    )

    def _get_file_path(self, file_id: str) -> str | None:
        url = f"{self._base_url()}/getFile"
        with httpx.Client(timeout=10.0) as client:
            resp = client.get(url, params={"file_id": file_id})
            if resp.status_code != 200:
                logger.error("Telegram getFile failed: %s %s", resp.status_code, resp.text[:300])
                return None
            data = resp.json()
            if not data.get("ok"):
                logger.error("Telegram getFile not ok: %s", data)
                return None
            result = data.get("result") or {}
            file_path = result.get("file_path")
            if not file_path:
                logger.error("Telegram getFile missing file_path: %s", result)
                return None
            return file_path

    def download_file(self, file_id: str, dest_dir: str, filename_hint: str | None = None) -> str | None:
        file_path = self._get_file_path(file_id)
        if not file_path:
            return None
        os.makedirs(dest_dir, exist_ok=True)
        filename = filename_hint or os.path.basename(file_path)
        dest_path = _unique_path(os.path.join(dest_dir, filename))
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        with httpx.Client(timeout=30.0) as client:
            resp = client.get(url)
            if resp.status_code != 200:
                logger.error("Telegram file download failed: %s %s", resp.status_code, resp.text[:300])
                return None
            with open(dest_path, "wb") as handle:
                handle.write(resp.content)
        return dest_path

    def delete_webhook(self, drop_pending: bool = True) -> None:
        """Remove any existing webhook so polling works.

        When *drop_pending* is True (default), Telegram discards all
        updates that accumulated while the bot was offline.  This
        prevents stale messages from a previous crashed session from
        being replayed on restart.
        """
        url = f"{self._base_url()}/deleteWebhook"
        with httpx.Client(timeout=10.0) as client:
            resp = client.post(url, json={"drop_pending_updates": drop_pending})
            logger.info("deleteWebhook (drop_pending=%s): %s %s", drop_pending, resp.status_code, resp.text[:200])

    def get_updates(self, offset: int = 0, timeout: int = 30) -> list[dict[str, Any]]:
        """Long-poll Telegram for updates."""
        url = f"{self._base_url()}/getUpdates"
        params = {"timeout": timeout, "allowed_updates": ["message"]}
        if offset:
            params["offset"] = offset  # type: ignore[assignment]
        with httpx.Client(timeout=timeout + 10) as client:
            resp = client.get(url, params=params)
            if resp.status_code != 200:
                logger.error("getUpdates failed: %s %s", resp.status_code, resp.text[:300])
                return []
            data = resp.json()
            if not data.get("ok"):
                logger.error("getUpdates not ok: %s", data)
                return []
            return data.get("result", [])

    def start_polling(self, on_update: Callable[[dict[str, Any]], None]) -> None:
        """Start a background thread that polls Telegram for messages."""
        self.delete_webhook()

        def _poll_loop() -> None:
            offset = 0
            logger.info("Telegram polling started")
            while not self._stop_event.is_set():
                try:
                    updates = self.get_updates(offset=offset, timeout=25)
                    for update in updates:
                        update_id = update.get("update_id", 0)
                        offset = update_id + 1
                        try:
                            on_update(update)
                        except Exception as exc:  # noqa: BLE001
                            logger.error("Error processing Telegram update %s: %s", update_id, exc)
                except Exception as exc:  # noqa: BLE001
                    logger.error("Telegram polling error: %s", exc)
                    time.sleep(5)  # back off on errors

        self._polling_thread = threading.Thread(target=_poll_loop, daemon=True, name="telegram-poller")
        self._polling_thread.start()

    def stop_polling(self) -> None:
        self._stop_event.set()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        self.stop_polling()

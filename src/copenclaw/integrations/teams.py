from __future__ import annotations

from dataclasses import dataclass
import httpx


_BOTFRAMEWORK_SCOPE = "https://api.botframework.com/.default"


def _token_url(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


@dataclass
class TeamsAdapter:
    app_id: str
    app_password: str
    tenant_id: str

    def _get_access_token(self) -> str:
        data = {
            "grant_type": "client_credentials",
            "client_id": self.app_id,
            "client_secret": self.app_password,
            "scope": _BOTFRAMEWORK_SCOPE,
        }
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(_token_url(self.tenant_id), data=data)
            resp.raise_for_status()
            return resp.json()["access_token"]

    def send_message(self, service_url: str, conversation_id: str, text: str) -> None:
        token = self._get_access_token()
        url = f"{service_url.rstrip('/')}/v3/conversations/{conversation_id}/activities"
        payload = {"type": "message", "text": text}
        headers = {"Authorization": f"Bearer {token}"}
        with httpx.Client(timeout=15.0) as client:
            resp = client.post(url, json=payload, headers=headers)
            resp.raise_for_status()

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None

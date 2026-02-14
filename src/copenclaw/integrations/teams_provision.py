from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import json
import logging
from pathlib import Path
import re
import struct
from typing import Any
from urllib.parse import urlparse
import zipfile
import zlib

import httpx

logger = logging.getLogger("copenclaw.teams_provision")

_GRAPH_SCOPE = "https://graph.microsoft.com/.default"
_ARM_SCOPE = "https://management.azure.com/.default"
_GRAPH_BASE = "https://graph.microsoft.com/v1.0"
_ARM_BASE = "https://management.azure.com"


class TeamsProvisioningError(RuntimeError):
    pass


@dataclass
class TeamsProvisioningConfig:
    tenant_id: str
    admin_client_id: str
    admin_client_secret: str
    subscription_id: str
    resource_group: str
    resource_group_location: str
    bot_name: str
    messaging_endpoint: str
    package_dir: Path
    create_resource_group: bool = True
    publish: bool = False


@dataclass
class TeamsProvisioningResult:
    app_id: str
    app_password: str
    tenant_id: str
    app_object_id: str
    bot_resource_id: str
    app_package_path: Path
    teams_channel_enabled: bool
    teams_channel_error: str | None = None
    published: bool = False


def _token_url(tenant_id: str) -> str:
    return f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/token"


def _request(
    method: str,
    url: str,
    *,
    token: str | None = None,
    json_body: dict[str, Any] | None = None,
    content: bytes | None = None,
) -> dict[str, Any]:
    headers = {"Accept": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    if content is not None:
        headers["Content-Type"] = "application/zip"
    with httpx.Client(timeout=30.0) as client:
        resp = client.request(method, url, json=json_body, content=content, headers=headers)
    if resp.status_code >= 400:
        detail = resp.text[:800]
        raise TeamsProvisioningError(f"{method} {url} failed ({resp.status_code}): {detail}")
    if not resp.text:
        return {}
    try:
        return resp.json()
    except ValueError:
        return {"raw": resp.text}


def _get_token(tenant_id: str, client_id: str, client_secret: str, scope: str) -> str:
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "client_secret": client_secret,
        "scope": scope,
    }
    with httpx.Client(timeout=20.0) as client:
        resp = client.post(_token_url(tenant_id), data=data)
    if resp.status_code >= 400:
        raise TeamsProvisioningError(f"Token request failed ({resp.status_code}): {resp.text[:500]}")
    return resp.json().get("access_token", "")


def _png_chunk(chunk_type: bytes, data: bytes) -> bytes:
    length = struct.pack(">I", len(data))
    crc = zlib.crc32(chunk_type + data) & 0xFFFFFFFF
    return length + chunk_type + data + struct.pack(">I", crc)


def _solid_png(width: int, height: int, rgba: tuple[int, int, int, int]) -> bytes:
    row = bytes([0]) + bytes(rgba) * width
    raw = row * height
    compressed = zlib.compress(raw)
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 6, 0, 0, 0)
    return b"".join([
        b"\x89PNG\r\n\x1a\n",
        _png_chunk(b"IHDR", ihdr),
        _png_chunk(b"IDAT", compressed),
        _png_chunk(b"IEND", b""),
    ])


def _safe_package_name(bot_name: str) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", bot_name.lower()).strip("-")
    return slug or "copenclaw-bot"


def _extract_host(endpoint: str) -> str:
    parsed = urlparse(endpoint)
    if parsed.scheme != "https" or not parsed.hostname:
        raise TeamsProvisioningError("messaging_endpoint must be a valid https:// URL")
    return parsed.hostname


def _ensure_resource_group(config: TeamsProvisioningConfig, token: str) -> None:
    if not config.create_resource_group:
        return
    url = (
        f"{_ARM_BASE}/subscriptions/{config.subscription_id}/resourcegroups/"
        f"{config.resource_group}?api-version=2021-04-01"
    )
    _request("PUT", url, token=token, json_body={"location": config.resource_group_location})


def _create_app_registration(config: TeamsProvisioningConfig, token: str) -> tuple[str, str]:
    payload = {
        "displayName": config.bot_name,
        "signInAudience": "AzureADMyOrg",
    }
    data = _request("POST", f"{_GRAPH_BASE}/applications", token=token, json_body=payload)
    app_id = data.get("appId")
    obj_id = data.get("id")
    if not app_id or not obj_id:
        raise TeamsProvisioningError("Graph create application returned no appId/id")
    return app_id, obj_id


def _add_app_password(app_object_id: str, token: str) -> str:
    expires = datetime.now(timezone.utc) + timedelta(days=365 * 2)
    payload = {
        "passwordCredential": {
            "displayName": "copenclaw",
            "endDateTime": expires.isoformat(),
        }
    }
    data = _request(
        "POST",
        f"{_GRAPH_BASE}/applications/{app_object_id}/addPassword",
        token=token,
        json_body=payload,
    )
    secret = data.get("secretText")
    if not secret:
        raise TeamsProvisioningError("Graph addPassword returned no secretText")
    return secret


def _create_service_principal(app_id: str, token: str) -> None:
    payload = {"appId": app_id}
    try:
        _request("POST", f"{_GRAPH_BASE}/servicePrincipals", token=token, json_body=payload)
    except TeamsProvisioningError as exc:
        if "already exists" in str(exc).lower():
            return
        raise


def _create_bot_registration(
    config: TeamsProvisioningConfig,
    token: str,
    app_id: str,
) -> str:
    url = (
        f"{_ARM_BASE}/subscriptions/{config.subscription_id}/resourceGroups/"
        f"{config.resource_group}/providers/Microsoft.BotService/botServices/"
        f"{config.bot_name}?api-version=2022-09-15"
    )
    payload = {
        "location": "global",
        "kind": "registration",
        "properties": {
            "displayName": config.bot_name,
            "endpoint": config.messaging_endpoint,
            "msaAppId": app_id,
            "description": "COpenClaw Teams bot",
        },
    }
    data = _request("PUT", url, token=token, json_body=payload)
    return data.get("id", "")


def _enable_teams_channel(config: TeamsProvisioningConfig, token: str) -> None:
    url = (
        f"{_ARM_BASE}/subscriptions/{config.subscription_id}/resourceGroups/"
        f"{config.resource_group}/providers/Microsoft.BotService/botServices/"
        f"{config.bot_name}/channels/MicrosoftTeamsChannel?api-version=2022-09-15"
    )
    payload = {
        "location": "global",
        "properties": {
            "enableCalling": False,
            "enableVideo": False,
            "enableMessaging": True,
        },
    }
    _request("PUT", url, token=token, json_body=payload)


def _create_app_package(config: TeamsProvisioningConfig, app_id: str) -> Path:
    host = _extract_host(config.messaging_endpoint)
    package_dir = config.package_dir
    package_dir.mkdir(parents=True, exist_ok=True)
    safe_name = _safe_package_name(config.bot_name)
    work_dir = package_dir / f"{safe_name}-teams-app"
    work_dir.mkdir(parents=True, exist_ok=True)

    manifest = {
        "$schema": "https://developer.microsoft.com/en-us/json-schemas/teams/v1.16/MicrosoftTeams.schema.json",
        "manifestVersion": "1.16",
        "version": "1.0.0",
        "id": app_id,
        "packageName": f"com.copenclaw.{safe_name}",
        "developer": {
            "name": "COpenClaw",
            "websiteUrl": "https://github.com/glmcdona/copenclaw",
            "privacyUrl": "https://github.com/glmcdona/copenclaw",
            "termsOfUseUrl": "https://github.com/glmcdona/copenclaw",
        },
        "name": {"short": config.bot_name[:30], "full": config.bot_name},
        "description": {
            "short": "COpenClaw bot for Microsoft Teams",
            "full": "COpenClaw bot for Microsoft Teams automation and Copilot CLI access.",
        },
        "icons": {"color": "color.png", "outline": "outline.png"},
        "accentColor": "#4A3AFF",
        "bots": [
            {
                "botId": app_id,
                "scopes": ["personal", "team", "groupchat"],
                "supportsFiles": False,
                "isNotificationOnly": False,
            }
        ],
        "validDomains": [host],
    }

    (work_dir / "manifest.json").write_text(
        json.dumps(manifest, indent=2),
        encoding="utf-8",
    )
    color_png = _solid_png(192, 192, (74, 58, 255, 255))
    outline_png = _solid_png(32, 32, (16, 16, 16, 255))
    (work_dir / "color.png").write_bytes(color_png)
    (work_dir / "outline.png").write_bytes(outline_png)

    zip_path = package_dir / f"{safe_name}-teams-app.zip"
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.write(work_dir / "manifest.json", arcname="manifest.json")
        zf.write(work_dir / "color.png", arcname="color.png")
        zf.write(work_dir / "outline.png", arcname="outline.png")
    return zip_path


def provision_teams_bot(config: TeamsProvisioningConfig) -> TeamsProvisioningResult:
    graph_token = _get_token(config.tenant_id, config.admin_client_id, config.admin_client_secret, _GRAPH_SCOPE)
    arm_token = _get_token(config.tenant_id, config.admin_client_id, config.admin_client_secret, _ARM_SCOPE)

    _ensure_resource_group(config, arm_token)
    app_id, app_object_id = _create_app_registration(config, graph_token)
    app_secret = _add_app_password(app_object_id, graph_token)
    _create_service_principal(app_id, graph_token)
    bot_resource_id = _create_bot_registration(config, arm_token, app_id)

    teams_channel_enabled = True
    teams_channel_error = None
    try:
        _enable_teams_channel(config, arm_token)
    except TeamsProvisioningError as exc:
        teams_channel_enabled = False
        teams_channel_error = str(exc)
        logger.warning("Teams channel enable failed: %s", exc)

    app_package = _create_app_package(config, app_id)

    published = False
    if config.publish:
        _request(
            "POST",
            f"{_GRAPH_BASE}/appCatalogs/teamsApps",
            token=graph_token,
            content=app_package.read_bytes(),
        )
        published = True

    return TeamsProvisioningResult(
        app_id=app_id,
        app_password=app_secret,
        tenant_id=config.tenant_id,
        app_object_id=app_object_id,
        bot_resource_id=bot_resource_id,
        app_package_path=app_package,
        teams_channel_enabled=teams_channel_enabled,
        teams_channel_error=teams_channel_error,
        published=published,
    )


def update_env_file(path: Path, values: dict[str, str]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
    seen: set[str] = set()
    new_lines: list[str] = []
    for line in existing:
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in line:
            new_lines.append(line)
            continue
        key, _, _ = line.partition("=")
        key = key.strip()
        if key in values:
            new_lines.append(f"{key}={values[key]}")
            seen.add(key)
        else:
            new_lines.append(line)
    for key, value in values.items():
        if key not in seen:
            new_lines.append(f"{key}={value}")
    path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

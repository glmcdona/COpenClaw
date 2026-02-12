from __future__ import annotations

import time
from typing import Any, Dict

import httpx
import jwt


_OPENID_CONFIG = "https://login.botframework.com/v1/.well-known/openidconfiguration"
_CACHE: Dict[str, Any] = {}


def _get_openid_config() -> Dict[str, Any]:
    cached = _CACHE.get("openid")
    if cached and cached["expires_at"] > time.time():
        return cached["value"]
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(_OPENID_CONFIG)
        resp.raise_for_status()
        value = resp.json()
    _CACHE["openid"] = {"value": value, "expires_at": time.time() + 3600}
    return value


def _get_jwks() -> Dict[str, Any]:
    cached = _CACHE.get("jwks")
    if cached and cached["expires_at"] > time.time():
        return cached["value"]
    config = _get_openid_config()
    jwks_uri = config["jwks_uri"]
    with httpx.Client(timeout=15.0) as client:
        resp = client.get(jwks_uri)
        resp.raise_for_status()
        value = resp.json()
    _CACHE["jwks"] = {"value": value, "expires_at": time.time() + 3600}
    return value


def validate_bearer_token(token: str, app_id: str) -> bool:
    try:
        jwks = _get_jwks()
        unverified = jwt.get_unverified_header(token)
        kid = unverified.get("kid")
        key = None
        for jwk in jwks.get("keys", []):
            if jwk.get("kid") == kid:
                key = jwt.algorithms.RSAAlgorithm.from_jwk(jwk)
                break
        if not key:
            return False
        decoded = jwt.decode(
            token,
            key=key,
            algorithms=["RS256"],
            audience=app_id,
            issuer="https://api.botframework.com",
        )
        return bool(decoded)
    except Exception:  # noqa: BLE001
        return False
"""Validates platform Heartbeat identity before any scheduled side effect."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import time
from urllib.request import Request, urlopen
from typing import Callable


class HeartbeatAuthError(ValueError):
    pass


def _decode_segment(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def verify_platform_jwt(token: str, *, secret: str | None = None, project_id: str | None = None, now: int | None = None) -> dict:
    key = secret if secret is not None else os.environ.get("MANUS_JWT_SECRET", "")
    expected_project = project_id if project_id is not None else os.environ.get("MANUS_PROJECT_ID", "")
    if not token or not key or not expected_project:
        raise HeartbeatAuthError("missing_credentials")
    try:
        header_part, payload_part, signature_part = token.split(".")
        header = json.loads(_decode_segment(header_part))
        claims = json.loads(_decode_segment(payload_part))
        supplied = _decode_segment(signature_part)
    except Exception as exc:
        raise HeartbeatAuthError("invalid_token") from exc
    if not isinstance(header, dict) or header.get("alg") != "HS256":
        raise HeartbeatAuthError("invalid_algorithm")
    expected = hmac.new(key.encode("utf-8"), f"{header_part}.{payload_part}".encode("ascii"), hashlib.sha256).digest()
    if not hmac.compare_digest(supplied, expected):
        raise HeartbeatAuthError("invalid_signature")
    current = int(time.time() if now is None else now)
    if not isinstance(claims, dict) or int(claims.get("exp") or 0) < current:
        raise HeartbeatAuthError("expired_token")
    if claims.get("appId") != expected_project or not str(claims.get("openId") or "").startswith("cron_"):
        raise HeartbeatAuthError("wrong_identity")
    return claims


def resolve_platform_task(token: str, *, transport: Callable = urlopen) -> dict:
    base = os.environ.get("MANUS_OAUTH_API_URL", "").rstrip("/")
    project_id = os.environ.get("MANUS_PROJECT_ID", "")
    if not base or not project_id:
        raise HeartbeatAuthError("identity_service_unavailable")
    url = base + "/webdev.v1.WebDevAuthPublicService/GetUserInfoWithJwt"
    request = Request(
        url,
        data=json.dumps({"jwt_token": token, "project_id": project_id}).encode("utf-8"),
        headers={"Content-Type": "application/json", "Accept": "application/json"},
        method="POST",
    )
    try:
        with transport(request, timeout=8) as response:
            identity = json.loads(response.read().decode("utf-8"))
    except Exception as exc:
        raise HeartbeatAuthError("identity_service_unavailable") from exc
    if not isinstance(identity, dict) or identity.get("projectId") != project_id:
        raise HeartbeatAuthError("identity_mismatch")
    if not str(identity.get("openId") or "").startswith("cron_") or not identity.get("taskUid"):
        raise HeartbeatAuthError("task_identity_missing")
    return identity

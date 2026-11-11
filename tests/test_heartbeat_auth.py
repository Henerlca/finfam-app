import base64
import hashlib
import hmac
import json

import pytest

from app.services.heartbeat_auth import HeartbeatAuthError, verify_platform_jwt


def _token(claims, secret="unit-test-secret", alg="HS256"):
    enc = lambda value: base64.urlsafe_b64encode(json.dumps(value, separators=(",", ":")).encode()).rstrip(b"=").decode()
    header = enc({"alg": alg, "typ": "JWT"})
    payload = enc(claims)
    signature = base64.urlsafe_b64encode(hmac.new(secret.encode(), f"{header}.{payload}".encode(), hashlib.sha256).digest()).rstrip(b"=").decode()
    return f"{header}.{payload}.{signature}"


def test_platform_jwt_requires_correct_project_cron_identity_and_expiration():
    claims = {"openId": "cron_daily", "appId": "project-test", "exp": 2000}
    assert verify_platform_jwt(_token(claims), secret="unit-test-secret", project_id="project-test", now=1500) == claims
    with pytest.raises(HeartbeatAuthError):
        verify_platform_jwt(_token({**claims, "appId": "other"}), secret="unit-test-secret", project_id="project-test", now=1500)
    with pytest.raises(HeartbeatAuthError):
        verify_platform_jwt(_token({**claims, "openId": "family-user"}), secret="unit-test-secret", project_id="project-test", now=1500)
    with pytest.raises(HeartbeatAuthError):
        verify_platform_jwt(_token({**claims, "exp": 1000}), secret="unit-test-secret", project_id="project-test", now=1500)

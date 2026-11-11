from __future__ import annotations

import hashlib
import hmac
import os
import secrets
import time
from collections import defaultdict, deque
from typing import Any, Literal

from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer

SESSION_COOKIE = "finfam_session"
LOGIN_CSRF_COOKIE = "finfam_login_csrf"
SESSION_MAX_AGE = 12 * 60 * 60
LOGIN_CSRF_MAX_AGE = 15 * 60
MAX_LOGIN_ATTEMPTS = 8
LOGIN_WINDOW_SECONDS = 15 * 60
_attempts: dict[str, deque[float]] = defaultdict(deque)


def family_password() -> str:
    return os.environ.get("FINANCE_APP_PASSWORD", "")


def cookies_secure() -> bool:
    return os.environ.get("APP_COOKIE_SECURE", "1").strip().lower() not in {"0", "false", "no"}


def cookie_samesite() -> Literal["none", "lax"]:
    return "none" if cookies_secure() else "lax"


def _serializer() -> URLSafeTimedSerializer:
    password = family_password()
    if not password:
        raise RuntimeError("A senha familiar não foi configurada.")
    key = hashlib.sha256(b"finfam-demo-session-v1\0" + password.encode("utf-8")).digest()
    return URLSafeTimedSerializer(key, salt="finfam-session-v1")


def new_csrf_token() -> str:
    return secrets.token_urlsafe(32)


def make_login_csrf(token: str) -> str:
    return _serializer().dumps({"kind": "login", "csrf": token})


def read_login_csrf(cookie: str | None) -> str | None:
    if not cookie:
        return None
    try:
        data = _serializer().loads(cookie, max_age=LOGIN_CSRF_MAX_AGE)
        return data.get("csrf") if data.get("kind") == "login" else None
    except (BadSignature, SignatureExpired, TypeError):
        return None


def make_session(csrf: str) -> str:
    return _serializer().dumps({"kind": "session", "csrf": csrf, "issued": int(time.time())})


def read_session(cookie: str | None) -> dict[str, Any] | None:
    if not cookie:
        return None
    try:
        data = _serializer().loads(cookie, max_age=SESSION_MAX_AGE)
        if data.get("kind") != "session" or not isinstance(data.get("csrf"), str):
            return None
        return data
    except (BadSignature, SignatureExpired, TypeError):
        return None


def password_matches(candidate: str) -> bool:
    expected = family_password()
    return bool(expected) and hmac.compare_digest(candidate.encode("utf-8"), expected.encode("utf-8"))


def verify_csrf(expected: str | None, supplied: str | None) -> bool:
    return bool(expected and supplied and hmac.compare_digest(expected, supplied))


def login_allowed(client_key: str) -> bool:
    now = time.time()
    q = _attempts[client_key]
    while q and now - q[0] > LOGIN_WINDOW_SECONDS:
        q.popleft()
    return len(q) < MAX_LOGIN_ATTEMPTS


def record_login_failure(client_key: str) -> None:
    now = time.time()
    q = _attempts[client_key]
    while q and now - q[0] > LOGIN_WINDOW_SECONDS:
        q.popleft()
    q.append(now)


CONTEXT_COOKIE = "finfam_context"


def make_context_cookie(context: dict[str, str]) -> str:
    return _serializer().dumps({"kind": "context", **context})


def read_context_cookie(cookie: str | None) -> dict[str, Any]:
    if not cookie:
        return {}
    try:
        value = _serializer().loads(cookie, max_age=SESSION_MAX_AGE)
        return value if isinstance(value, dict) and value.get("kind") == "context" else {}
    except (BadSignature, SignatureExpired, TypeError, RuntimeError):
        return {}

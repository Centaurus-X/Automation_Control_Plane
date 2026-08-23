"""File-backed scoped tokens and signed stateless browser sessions."""

import base64
import hashlib
import hmac
import json
import os
import pathlib
import secrets
import time

from automation_fleet.canonical import canonical_bytes


COOKIE_NAME = "automation_fleet_session"
TOKEN_ROLES = ("admin", "operator", "approver", "auditor", "viewer")
ROLE_PERMISSIONS = {
    "admin": {"read", "plan", "approve", "execute", "pause", "audit", "evidence"},
    "operator": {"read", "plan", "execute", "pause"},
    "approver": {"read", "approve", "audit", "evidence"},
    "auditor": {"read", "audit", "evidence"},
    "viewer": {"read"},
}


def _read_token(path, label):
    target = pathlib.Path(path)
    stat_result = target.stat()
    if stat_result.st_mode & 0o077:
        raise ValueError(label + " token file must not be group/world accessible")
    token = target.read_text(encoding="utf-8").strip()
    if not 32 <= len(token) <= 4096 or any(character.isspace() for character in token):
        raise ValueError(label + " token is invalid")
    if token.startswith("CHANGE_ME_"):
        raise ValueError(label + " token is a placeholder")
    return token


def load_tokens(config):
    tokens = {}
    for role in TOKEN_ROLES:
        path = config.get(role + "_token_file")
        if path:
            tokens[role] = _read_token(path, role)
    if "admin" not in tokens:
        raise ValueError("admin token file is required")
    values = list(tokens.values())
    if len(values) != len(set(values)):
        raise ValueError("token_identity: scoped token values must be unique")
    return tokens


def _signing_key(tokens):
    material = tokens["admin"].encode("utf-8")
    return hmac.new(material, b"automation-fleet-session-v1", hashlib.sha256).digest()


def _encode(data):
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _decode(value):
    padding = "=" * ((4 - len(value) % 4) % 4)
    return base64.urlsafe_b64decode((value + padding).encode("ascii"))


def create_session(role, tokens, ttl_s=3600):
    payload = {
        "role": role,
        "subject": "token:" + role,
        "expires": int(time.time()) + int(ttl_s),
        "nonce": secrets.token_urlsafe(18),
    }
    encoded = _encode(canonical_bytes(payload))
    signature = _encode(hmac.new(
        _signing_key(tokens), encoded.encode("ascii"), hashlib.sha256).digest())
    session = encoded + "." + signature
    csrf = _encode(hmac.new(
        _signing_key(tokens), ("csrf:" + session).encode("ascii"), hashlib.sha256).digest())
    return {
        "session": session,
        "csrf": csrf,
        "expires": payload["expires"],
        "role": role,
        "subject": payload["subject"],
    }


def _verify_session(value, tokens):
    parts = value.split(".")
    if len(parts) != 2:
        return None
    expected = _encode(hmac.new(
        _signing_key(tokens), parts[0].encode("ascii"), hashlib.sha256).digest())
    if not hmac.compare_digest(expected, parts[1]):
        return None
    try:
        payload = json.loads(_decode(parts[0]).decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError):
        return None
    if payload.get("role") not in ROLE_PERMISSIONS:
        return None
    if payload.get("subject") != "token:" + payload["role"]:
        return None
    if not isinstance(payload.get("expires"), int) or payload["expires"] < int(time.time()):
        return None
    csrf = _encode(hmac.new(
        _signing_key(tokens), ("csrf:" + value).encode("ascii"), hashlib.sha256).digest())
    return {
        "role": payload["role"],
        "subject": payload["subject"],
        "csrf": csrf,
        "method": "cookie",
    }


def _cookies(environ):
    result = {}
    for item in environ.get("HTTP_COOKIE", "").split(";"):
        if "=" not in item:
            continue
        name, value = item.strip().split("=", 1)
        result[name] = value
    return result


def authenticate(environ, tokens):
    authorization = environ.get("HTTP_AUTHORIZATION", "")
    if authorization.startswith("Bearer "):
        supplied = authorization[7:]
        for role in TOKEN_ROLES:
            token = tokens.get(role)
            if token and hmac.compare_digest(supplied, token):
                return {
                    "role": role,
                    "subject": "token:" + role,
                    "csrf": None,
                    "method": "bearer",
                }
        return None
    session = _cookies(environ).get(COOKIE_NAME)
    return _verify_session(session, tokens) if session else None


def authenticate_token(supplied, tokens):
    if not isinstance(supplied, str):
        return None
    for role in TOKEN_ROLES:
        token = tokens.get(role)
        if token and hmac.compare_digest(supplied, token):
            return role
    return None


def require_permission(principal, permission):
    if principal is None:
        raise PermissionError("authentication_required")
    permissions = ROLE_PERMISSIONS.get(principal.get("role"), set())
    if permission not in permissions:
        raise PermissionError(permission + "_permission_required")
    return principal


def permissions_for_role(role):
    return sorted(ROLE_PERMISSIONS.get(role, set()))


def require_role(principal, role):
    """Retain the v0027-r2 role helper for compatible callers."""
    permission = "read" if role == "viewer" else "execute"
    if role == "admin" and principal is not None and principal.get("role") != "admin":
        raise PermissionError("admin_role_required")
    return require_permission(principal, permission)


def require_csrf(environ, principal):
    if principal["method"] == "bearer":
        return
    supplied = environ.get("HTTP_X_CSRF_TOKEN", "")
    if not supplied or not hmac.compare_digest(supplied, principal["csrf"]):
        raise PermissionError("csrf_validation_failed")
    origin = environ.get("HTTP_ORIGIN")
    host = environ.get("HTTP_HOST")
    if not origin or not host:
        raise PermissionError("origin_validation_failed")
    allowed_http = "http://" + host
    allowed_https = "https://" + host
    if origin not in {allowed_http, allowed_https}:
        raise PermissionError("origin_validation_failed")


def session_cookie(session, secure):
    parts = [
        COOKIE_NAME + "=" + session["session"],
        "Path=/",
        "HttpOnly",
        "SameSite=Strict",
        "Max-Age=" + str(max(0, session["expires"] - int(time.time()))),
    ]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)


def expired_cookie(secure):
    parts = [COOKIE_NAME + "=", "Path=/", "HttpOnly", "SameSite=Strict", "Max-Age=0"]
    if secure:
        parts.append("Secure")
    return "; ".join(parts)

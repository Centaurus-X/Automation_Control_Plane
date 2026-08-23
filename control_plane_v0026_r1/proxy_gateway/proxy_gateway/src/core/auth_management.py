# -*- coding: utf-8 -*-

# proxy_gateway/src/core/auth_management.py

"""Local authentication, rotating sessions and scoped API-token principals."""

import copy
import datetime
import hmac

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library.rate_limit_store import bucket_key
from src.library.token_security import (
    binding_hash,
    generate_token,
    hash_password,
    token_hash,
    token_prefix,
    verify_password,
)


def _now():
    return datetime.datetime.now(datetime.timezone.utc)


def _login_rate(context, username, source_ip):
    key = bucket_key("login", source_ip, str(username).casefold())
    return context["rate_limit"]["consume"](
        key,
        limit=context["options"]["login_rate_limit"],
        window_s=context["options"]["login_rate_window_s"],
    )


def login(context, request):
    """Verify credentials and create one shared server-side session."""
    username = str(request.get("username") or "")
    limited = _login_rate(context, username, request.get("source_ip"))
    if not limited.get("allowed"):
        return make_error("rate_limited", "login rate limit exceeded", reset_at=limited.get("reset_at"))
    try:
        credentials = context["identity"]["get_credentials"](username)
    except ValueError:
        credentials = None
    now = _now()
    locked = credentials and credentials.get("locked_until") and credentials["locked_until"] > now
    candidate_hash = credentials.get("password_hash") if credentials else context["dummy_hash"]
    password_matches = verify_password(
        request.get("password"), candidate_hash, context["password_pepper"])
    valid = bool(
        credentials
        and credentials.get("enabled")
        and not locked
        and password_matches
    )
    if not valid:
        context["identity"]["record_login_failure"]({
            "username": username or "unknown",
            "source_ip": request.get("source_ip"),
            "reason_code": "invalid_credentials",
            "max_failures": context["options"]["max_login_failures"],
            "lock_seconds": context["options"]["lock_seconds"],
        })
        return make_error("authentication_required", "credentials are invalid")
    access = context["identity"]["effective_access"](credentials["user_id"])
    session = _new_session_material(context, credentials, request)
    stored = context["identity"]["create_session"](session["record"])
    if not stored.get("ok"):
        return make_error("database_unavailable", "session could not be created")
    context["identity"]["record_login_success"]({
        "username": username,
        "source_ip": request.get("source_ip"),
    })
    return make_success(
        user=_public_principal(credentials, access, stored["session"]["session_id"]),
        session_token=session["session_token"],
        csrf_token=session["csrf_token"],
        expires_at=stored["session"]["expires_at"],
    )


def _new_session_material(context, credentials, request):
    now = _now()
    token = generate_token()
    csrf = generate_token(entropy_bytes=32)
    absolute = now + datetime.timedelta(seconds=context["options"]["absolute_session_s"])
    expires = now + datetime.timedelta(seconds=context["options"]["sliding_session_s"])
    return {
        "session_token": token,
        "csrf_token": csrf,
        "record": {
            "user_id": credentials["user_id"],
            "token_hash": token_hash(token, context["token_pepper"]),
            "csrf_hash": token_hash(csrf, context["token_pepper"]),
            "client_binding_hash": binding_hash(
                request.get("source_ip"), request.get("user_agent"), context["binding_pepper"]),
            "expires_at": expires,
            "absolute_expires_at": absolute,
        },
    }


def _public_principal(record, access, session_id=None):
    return {
        "subject": str(record["user_id"]),
        "username": record.get("username"),
        "display_name": record.get("display_name"),
        "session_id": str(session_id) if session_id else None,
        "permissions": list(access.get("permissions") or []),
        "site_scopes": list(access.get("site_scopes") or []),
        "resource_scopes": copy.deepcopy(access.get("resource_scopes") or []),
        "auth_type": "session" if session_id else "api_token",
    }


def authenticate_session(context, request):
    """Resolve a cookie session and optionally enforce its CSRF token."""
    raw = str(request.get("session_token") or "")
    if not raw:
        return make_error("authentication_required", "session cookie is missing")
    digest = token_hash(raw, context["token_pepper"])
    record = context["identity"]["get_session"]({
        "token_hash": digest,
        "client_binding_hash": binding_hash(
            request.get("source_ip"), request.get("user_agent"), context["binding_pepper"]),
        "sliding_seconds": context["options"]["sliding_session_s"],
    })
    if not record:
        return make_error("authentication_required", "session is invalid or expired")
    if request.get("csrf_required"):
        supplied = token_hash(request.get("csrf_token"), context["token_pepper"])
        if not hmac.compare_digest(supplied, str(record["csrf_hash"])):
            return make_error("authorization_denied", "CSRF token is invalid")
    access = context["identity"]["effective_access"](record["user_id"])
    return make_success(principal=_public_principal(record, access, record["session_id"]), session=record)


def rotate_session(context, request):
    """Replace a valid session and CSRF token atomically."""
    authenticated = authenticate_session(context, request)
    if not authenticated.get("ok"):
        return authenticated
    record = authenticated["session"]
    material = _new_session_material(context, record, request)
    material["record"]["old_token_hash"] = token_hash(
        request["session_token"], context["token_pepper"])
    stored = context["identity"]["rotate_session"](material["record"])
    if not stored.get("ok"):
        return stored
    principal = copy.deepcopy(authenticated["principal"])
    principal["session_id"] = str(stored["session"]["session_id"])
    return make_success(
        principal=principal,
        session_token=material["session_token"],
        csrf_token=material["csrf_token"],
        expires_at=stored["session"]["expires_at"],
    )


def authenticate_api_token(context, token):
    """Resolve one token and constrain it to the user's current access."""
    if not token:
        return make_error("authentication_required", "API token is missing")
    record = context["identity"]["get_api_token"](token_hash(token, context["token_pepper"]))
    if not record:
        return make_error("authentication_required", "API token is invalid or expired")
    current = context["identity"]["effective_access"](record["user_id"])
    current = current if isinstance(current, dict) else {}
    token_permissions = {str(value) for value in record.get("permissions") or []}
    current_permissions = {str(value) for value in current.get("permissions") or []}
    token_sites = {str(value) for value in record.get("site_scopes") or []}
    current_sites = {str(value) for value in current.get("site_scopes") or []}
    access = {
        "permissions": sorted(token_permissions.intersection(current_permissions)),
        "site_scopes": _intersect_site_scopes(token_sites, current_sites),
        "resource_scopes": copy.deepcopy(current.get("resource_scopes") or []),
    }
    return make_success(principal=_public_principal(record, access))


def _intersect_site_scopes(token_sites, current_sites):
    """Intersect site sets while preserving the wildcard's set semantics."""
    if "*" in token_sites:
        result = current_sites
    elif "*" in current_sites:
        result = token_sites
    else:
        result = token_sites.intersection(current_sites)
    return sorted(result)


def logout(context, request):
    """Revoke the current shared session."""
    raw = str(request.get("session_token") or "")
    if not raw:
        return make_success(revoked=False)
    revoked = context["identity"]["revoke_session"]({
        "token_hash": token_hash(raw, context["token_pepper"]),
        "reason": "logout",
    })
    return make_success(revoked=bool(revoked))


def create_local_user(context, request):
    """Hash and create one user through the identity capability."""
    prepared = copy.deepcopy(request)
    try:
        prepared["password_hash"] = hash_password(
            prepared.pop("password"), context["password_pepper"],
            minimum_length=context["options"]["minimum_password_length"],
        )
    except (KeyError, ValueError) as exc:
        return make_error("invalid_payload", str(exc))
    return context["identity"]["create_user"](prepared)


def issue_api_token(context, request):
    """Create one raw token while persisting only its keyed hash."""
    expires_at = request.get("expires_at")
    if expires_at is not None:
        try:
            parsed = datetime.datetime.fromisoformat(str(expires_at).replace("Z", "+00:00"))
        except ValueError:
            return make_error("invalid_payload", "expires_at must be RFC3339")
        if parsed.tzinfo is None or parsed <= _now():
            return make_error("invalid_payload", "expires_at must be a future aware timestamp")
        if parsed > _now() + datetime.timedelta(days=366):
            return make_error("invalid_payload", "API token lifetime exceeds one year")
        expires_at = parsed.astimezone(datetime.timezone.utc)
    raw = generate_token()
    prepared = copy.deepcopy(request)
    prepared.update({
        "token_prefix": token_prefix(raw),
        "token_hash": token_hash(raw, context["token_pepper"]),
        "expires_at": expires_at,
    })
    stored = context["identity"]["create_api_token"](prepared)
    if not stored.get("ok"):
        return stored
    return make_success(token=raw, metadata=stored["token"])


def create_auth_management(**kwargs):
    """Create explicit authentication capabilities."""
    options = copy.deepcopy(kwargs.get("options") or {})
    defaults = {
        "minimum_password_length": 14,
        "sliding_session_s": 1800,
        "absolute_session_s": 43200,
        "max_login_failures": 5,
        "lock_seconds": 300,
        "login_rate_limit": 10,
        "login_rate_window_s": 300,
    }
    for key, value in defaults.items():
        options.setdefault(key, value)
    context = {
        "identity": kwargs["identity"],
        "rate_limit": kwargs["rate_limit"],
        "password_pepper": kwargs["password_pepper"],
        "token_pepper": kwargs["token_pepper"],
        "binding_pepper": kwargs["binding_pepper"],
        "options": options,
        "dummy_hash": hash_password(
            generate_token(), kwargs["password_pepper"],
            minimum_length=options["minimum_password_length"],
        ),
    }
    return {
        "context": context,
        "login": partial(login, context),
        "authenticate_session": partial(authenticate_session, context),
        "authenticate_api_token": partial(authenticate_api_token, context),
        "rotate_session": partial(rotate_session, context),
        "logout": partial(logout, context),
        "create_user": partial(create_local_user, context),
        "issue_api_token": partial(issue_api_token, context),
    }

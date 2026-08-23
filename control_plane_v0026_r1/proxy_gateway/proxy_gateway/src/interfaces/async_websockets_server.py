# -*- coding: utf-8 -*-

# proxy_gateway/proxy_gateway/src/interfaces/async_websockets_server.py

"""Primary asynchronous WSS transport for Proxy events and control commands.

What it is:
    A functional transport adapter built directly on the public
    ``websockets`` asyncio server API.  It owns TLS, exact path routing,
    authenticated connection lifecycle and WebSocket framing.

Why it is needed:
    WSS is the primary v0017 client ingress.  Starlette and Uvicorn remain an
    HTTP-only secondary interface and must not implement WebSocket framing.

How it works:
    One listener exposes ``/ws/v1/events`` and ``/ws/v1/control``.  Injected
    async callbacks bind the transport to the existing Proxy authentication,
    shared command dispatcher and event-stream core.  The event core retains
    ownership of authorization, filters, bounded queues, coalescing, durable
    replay and subscription generations.  Replay uses an atomic high-water
    mark and pagination before the transport switches to buffered live data.
"""


# =============================================================================
# MODULE IMPORTS
# =============================================================================

import asyncio
import collections.abc
import contextlib
import copy
import datetime
import decimal
import http
import ipaddress
import inspect
import json
import logging
import math
import re
import threading
import time
import urllib.parse
import uuid

from functools import partial
from http.cookies import SimpleCookie

import websockets

from src.library.generate_ssl_certs import build_server_ssl_context


# =============================================================================
# CONSTANTS
# =============================================================================

PROTOCOL = "automation.proxy.websocket.v1"
EVENTS_PATH = "/ws/v1/events"
CONTROL_PATH = "/ws/v1/control"
REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")
ACTION_PATTERN = re.compile(r"^[a-z][a-z0-9._-]{1,95}$")
TOKEN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
HANDSHAKE_REJECTION_REASONS = (
    "authentication", "busy", "connection_limit", "internal", "invalid_principal",
    "not_ready", "origin", "path", "principal_limit", "site", "source_limit",
    "upgrade",
)
COMMAND_RESULT_CODES = frozenset((
    "accepted", "authentication_required", "authorization_denied", "completed",
    "conflict", "database_unavailable", "expired", "internal_error",
    "invalid_payload", "not_config_authority", "not_found", "not_ready", "ok",
    "pending", "rate_limited", "replay_detected", "stale_revision",
))
IPADDRESS_TYPES = (
    ipaddress.IPv4Address,
    ipaddress.IPv6Address,
    ipaddress.IPv4Interface,
    ipaddress.IPv6Interface,
    ipaddress.IPv4Network,
    ipaddress.IPv6Network,
)


# =============================================================================
# JSON AND REQUEST HELPERS
# =============================================================================

def _reject_constant(value):
    raise ValueError("non-finite JSON number is forbidden: " + str(value))


def _decode_json(message):
    if not isinstance(message, str):
        raise ValueError("binary WebSocket messages are forbidden")
    value = json.loads(message, parse_constant=_reject_constant)
    if not isinstance(value, dict):
        raise ValueError("WebSocket JSON root must be an object")
    return value


def _json_type_name(value):
    value_type = type(value)
    return "{}.{}".format(value_type.__module__, value_type.__qualname__)


def _json_child_path(path, key):
    if isinstance(key, int):
        return "{}[{}]".format(path, key)
    if isinstance(key, str) and re.fullmatch(r"[A-Za-z_][A-Za-z0-9_.-]{0,63}", key):
        return "{}.{}".format(path, key)
    return path + ".<key>"


def _json_ready(value, **kwargs):
    """Detach one outbound value into a bounded, deterministic JSON tree.

    PostgreSQL adapters intentionally return native Python scalar types.  The
    transport boundary converts the supported, deterministic subset instead
    of relying on ``json.dumps(default=str)``, which could hide contract bugs
    or expose arbitrary object representations.
    """
    active = kwargs.get("active")
    if active is None:
        active = set()
    depth = int(kwargs.get("depth", 1))
    maximum_depth = int(kwargs.get("maximum_depth", 16))
    maximum_items = int(kwargs.get("maximum_items", 1024))
    maximum_string = int(kwargs.get("maximum_string", 65536))
    path = str(kwargs.get("path", "$"))
    if depth > maximum_depth:
        raise ValueError("JSON nesting exceeds its limit at " + path)

    recursive = {
        "active": active,
        "depth": depth + 1,
        "maximum_depth": maximum_depth,
        "maximum_items": maximum_items,
        "maximum_string": maximum_string,
    }
    if isinstance(value, collections.abc.Mapping):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic JSON object is forbidden at " + path)
        if len(value) > maximum_items:
            raise ValueError("JSON object exceeds its item limit at " + path)
        active.add(identity)
        try:
            result = {}
            for key, item in value.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings at " + path)
                if len(key) > maximum_string:
                    raise ValueError(
                        "JSON object key exceeds its string limit at " + path)
                child_kwargs = dict(recursive)
                child_kwargs["path"] = _json_child_path(path, key)
                result[key] = _json_ready(item, **child_kwargs)
            return result
        finally:
            active.discard(identity)
    if isinstance(value, (list, tuple)):
        identity = id(value)
        if identity in active:
            raise ValueError("cyclic JSON array is forbidden at " + path)
        if len(value) > maximum_items:
            raise ValueError("JSON array exceeds its item limit at " + path)
        active.add(identity)
        try:
            result = []
            for index, item in enumerate(value):
                child_kwargs = dict(recursive)
                child_kwargs["path"] = _json_child_path(path, index)
                result.append(_json_ready(item, **child_kwargs))
            return result
        finally:
            active.discard(identity)
    if isinstance(value, (set, frozenset)):
        raise ValueError("unordered JSON containers are forbidden at " + path)
    if isinstance(value, decimal.Decimal):
        if not value.is_finite():
            raise ValueError("non-finite JSON number is forbidden at " + path)
        result = str(value)
    elif isinstance(value, (
            datetime.datetime, datetime.date, datetime.time,
            datetime.timedelta, uuid.UUID) + IPADDRESS_TYPES):
        result = str(value)
    elif isinstance(value, (bytes, bytearray, memoryview)):
        result = bytes(value).decode("utf-8", errors="replace")
    elif isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError("non-finite JSON number is forbidden at " + path)
        result = value
    elif value is None or isinstance(value, (str, int, bool)):
        result = value
    else:
        raise ValueError(
            "unsupported JSON value type at {}: {}".format(
                path, _json_type_name(value)))
    if isinstance(result, str) and len(result) > maximum_string:
        raise ValueError("JSON string exceeds its limit at " + path)
    return result


def _prepare_json(value, **kwargs):
    prepared = _json_ready(value, **kwargs)
    return _validate_json_limits(prepared, **kwargs)


def _dump_json(value):
    return json.dumps(
        value, ensure_ascii=False, separators=(",", ":"),
        allow_nan=False, sort_keys=True)


def _encode_json(value):
    return _dump_json(_prepare_json(value))


def _validate_json_limits(value, **kwargs):
    maximum_depth = int(kwargs.get("maximum_depth", 16))
    maximum_items = int(kwargs.get("maximum_items", 1024))
    maximum_string = int(kwargs.get("maximum_string", 65536))
    stack = [(value, 1, False, "$")]
    active_containers = set()
    while stack:
        current, depth, leaving, path = stack.pop()
        identity = id(current)
        if leaving:
            active_containers.discard(identity)
            continue
        if depth > maximum_depth:
            raise ValueError("JSON nesting exceeds its limit at " + path)
        if isinstance(current, dict):
            if identity in active_containers:
                raise ValueError("cyclic JSON object is forbidden at " + path)
            active_containers.add(identity)
            stack.append((current, depth, True, path))
            if len(current) > maximum_items:
                raise ValueError("JSON object exceeds its item limit at " + path)
            for key, child in current.items():
                if not isinstance(key, str):
                    raise ValueError("JSON object keys must be strings at " + path)
                if len(key) > maximum_string:
                    raise ValueError(
                        "JSON object key exceeds its string limit at " + path)
                stack.append((
                    child, depth + 1, False, _json_child_path(path, key)))
        elif isinstance(current, list):
            if identity in active_containers:
                raise ValueError("cyclic JSON array is forbidden at " + path)
            active_containers.add(identity)
            stack.append((current, depth, True, path))
            if len(current) > maximum_items:
                raise ValueError("JSON array exceeds its item limit at " + path)
            stack.extend((
                child, depth + 1, False, _json_child_path(path, index))
                for index, child in enumerate(current))
        elif isinstance(current, (set, frozenset)):
            raise ValueError("unordered JSON containers are forbidden at " + path)
        elif isinstance(current, str) and len(current) > maximum_string:
            raise ValueError("JSON string exceeds its limit at " + path)
        elif isinstance(current, float) and not math.isfinite(current):
            raise ValueError("non-finite JSON number is forbidden at " + path)
        elif current is not None and not isinstance(
                current, (str, int, float, bool)):
            raise ValueError(
                "unsupported JSON value type at {}: {}".format(
                    path, _json_type_name(current)))
    return value


def _path_only(raw_path):
    parsed = urllib.parse.urlsplit(str(raw_path or ""))
    if parsed.query or parsed.fragment:
        raise ValueError("WebSocket query strings and fragments are forbidden")
    if parsed.path not in (EVENTS_PATH, CONTROL_PATH):
        raise ValueError("unknown WebSocket endpoint")
    return parsed.path


def _source_ip(connection):
    address = getattr(connection, "remote_address", None)
    return str(address[0]) if address else "unknown"


def _cookies(headers):
    parsed = SimpleCookie()
    parsed.load(str(headers.get("cookie") or ""))
    return {name: morsel.value for name, morsel in parsed.items()}


def _handshake_request(connection, request, path):
    headers = request.headers
    return {
        "path": path,
        "source_ip": _source_ip(connection),
        "authorization": str(headers.get("authorization") or ""),
        "origin": str(headers.get("origin") or ""),
        "user_agent": str(headers.get("user-agent") or ""),
        "cookies": _cookies(headers),
    }


async def _invoke(function, *args):
    result = function(*args)
    if inspect.isawaitable(result):
        return await result
    return result


async def _send_json(context, connection, envelope):
    encoded = _encode_json(envelope)
    if len(encoded.encode("utf-8")) > context["config"]["max_message_bytes"]:
        raise ValueError("outgoing WebSocket message exceeds its size limit")
    await asyncio.wait_for(
        connection.send(encoded),
        timeout=context["config"]["send_timeout_s"])


def _prepare_outgoing_envelope(context, envelope):
    prepared = _prepare_json(envelope)
    encoded = _dump_json(prepared)
    if len(encoded.encode("utf-8")) > context["config"]["max_message_bytes"]:
        raise ValueError("outgoing WebSocket message exceeds its size limit")
    return prepared


def _bounded_float(value, **kwargs):
    label = str(kwargs["label"])
    try:
        numeric = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(label + " must be numeric") from exc
    if not math.isfinite(numeric):
        raise ValueError(label + " must be finite")
    if not float(kwargs["minimum"]) <= numeric <= float(kwargs["maximum"]):
        raise ValueError(label + " is outside its allowed range")
    return numeric


def _has_explicit_callback_outcome(value):
    """Accept trust-boundary outcomes only when ``ok`` is a JSON boolean."""
    return isinstance(value, dict) and isinstance(value.get("ok"), bool)


def _callback_succeeded(value):
    return _has_explicit_callback_outcome(value) and value.get("ok") is True


def _record_handshake_rejection(context, reason):
    normalized = reason if reason in HANDSHAKE_REJECTION_REASONS else "internal"
    with context["lock"]:
        context["statistics"]["handshake_rejections"][normalized] += 1


def _command_metric_action(context, action):
    value = str(action or "")
    return value if value in context["allowed_actions"] else "other"


def _command_metric_code(result):
    if result.get("ok") is True:
        value = str(result.get("state") or "ok").lower()
    else:
        value = str(result.get("code") or "internal_error").lower()
    return value if value in COMMAND_RESULT_CODES else "other"


def _observe_command(context, action, result, started_monotonic):
    key = "{}|{}".format(
        _command_metric_action(context, action), _command_metric_code(result))
    elapsed = max(0.0, time.monotonic() - float(started_monotonic))
    with context["lock"]:
        counts = context["statistics"]["command_results"]
        durations = context["statistics"]["command_latency_seconds"]
        counts[key] = int(counts.get(key) or 0) + 1
        durations[key] = float(durations.get(key) or 0.0) + elapsed


# =============================================================================
# PROTOCOL VALIDATION
# =============================================================================

def validate_filters(filters):
    if not isinstance(filters, list) or len(filters) > 32:
        return {"ok": False, "code": "invalid_payload"}
    normalized = []
    for supplied in filters:
        if not isinstance(supplied, dict) or set(supplied) != {
                "resource_type", "resource_pattern"}:
            return {"ok": False, "code": "invalid_payload"}
        resource_type = supplied.get("resource_type")
        pattern = supplied.get("resource_pattern")
        if not isinstance(resource_type, str) or not isinstance(pattern, str):
            return {"ok": False, "code": "invalid_payload"}
        if not resource_type or not pattern or any(
                token in resource_type + pattern for token in ("/", "#", "+")):
            return {"ok": False, "code": "invalid_payload"}
        if len(resource_type) > 64 or len(pattern) > 256:
            return {"ok": False, "code": "invalid_payload"}
        normalized.append({"resource_type": resource_type, "resource_pattern": pattern})
    return {"ok": True, "filters": normalized}


def _validate_subscription(value, **kwargs):
    initial = bool(kwargs.get("initial"))
    allowed = {"protocol", "type", "request_id", "filters"}
    if initial:
        allowed.add("last_event_id")
    required = {"protocol", "type", "request_id", "filters"}
    if set(value) - allowed or not required.issubset(value):
        raise ValueError("subscription fields are invalid")
    if value.get("protocol") != PROTOCOL:
        raise ValueError("unsupported WebSocket protocol")
    if value.get("type") != "subscribe":
        raise ValueError("event endpoint accepts subscribe messages only")
    request_id = value.get("request_id")
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ValueError("request_id is invalid")
    checked = validate_filters(value.get("filters"))
    if not checked.get("ok"):
        raise ValueError("event subscription is invalid")
    after_sequence = value.get("last_event_id", 0)
    if isinstance(after_sequence, bool) or not isinstance(after_sequence, int):
        raise ValueError("last_event_id must be an integer")
    if after_sequence < 0:
        raise ValueError("last_event_id must not be negative")
    return {
        "request_id": request_id,
        "filters": checked["filters"],
        "after_sequence": after_sequence,
    }


def _validate_command_request(value):
    allowed = {
        "protocol", "type", "request_id", "action", "idempotency_key",
        "ttl_s", "payload", "csrf_token",
    }
    required = {"protocol", "type", "request_id", "action", "payload"}
    if set(value) - allowed or not required.issubset(value):
        raise ValueError("command fields are invalid")
    if value.get("protocol") != PROTOCOL:
        raise ValueError("unsupported WebSocket protocol")
    if value.get("type") != "command.request":
        raise ValueError("control endpoint accepts command.request only")
    request_id = value.get("request_id")
    action = value.get("action")
    if not isinstance(request_id, str) or not REQUEST_ID_PATTERN.fullmatch(request_id):
        raise ValueError("request_id is invalid")
    if not isinstance(action, str) or not ACTION_PATTERN.fullmatch(action):
        raise ValueError("action is invalid")
    payload = value.get("payload")
    if not isinstance(payload, dict):
        raise ValueError("payload must be an object")
    idempotency_key = value.get("idempotency_key")
    if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key
            or len(idempotency_key) > 128):
        raise ValueError("idempotency_key is invalid")
    ttl_value = value.get("ttl_s", 30.0)
    if isinstance(ttl_value, bool) or not isinstance(ttl_value, (int, float)):
        raise ValueError("ttl_s must be a number")
    ttl_s = float(ttl_value)
    if not math.isfinite(ttl_s) or ttl_s < 1.0 or ttl_s > 300.0:
        raise ValueError("ttl_s is outside its allowed range")
    csrf_token = value.get("csrf_token", "")
    if not isinstance(csrf_token, str) or len(csrf_token) > 512:
        raise ValueError("csrf_token is invalid")
    return {
        "request_id": request_id,
        "action": action,
        "idempotency_key": idempotency_key,
        "ttl_s": ttl_s,
        "payload": copy.deepcopy(payload),
        "csrf_token": csrf_token,
    }


# =============================================================================
# CONNECTION REGISTRY AND AUTH LIFECYCLE
# =============================================================================

def _prune_pending(context):
    cutoff = time.monotonic() - context["config"]["open_timeout_s"] * 2.0
    stale = [key for key, value in context["pending_principals"].items()
             if float(value.get("created_monotonic") or 0.0) < cutoff]
    for key in stale:
        context["pending_principals"].pop(key, None)
        context["reserved_connections"] = max(0, context["reserved_connections"] - 1)


def _release_pending(context, connection):
    with context["lock"]:
        removed = context["pending_principals"].pop(id(connection), None)
        if removed:
            context["reserved_connections"] = max(
                0, context["reserved_connections"] - 1)
            context["statistics"]["failed_upgrade_reservations_released"] += 1
        return bool(removed)


def _activate_client(context, connection, path):
    with context["lock"]:
        authenticated = context["pending_principals"].pop(id(connection), None)
        if authenticated:
            context["reserved_connections"] = max(0, context["reserved_connections"] - 1)
    if not authenticated:
        return None
    client_id = str(uuid.uuid4())
    client = {
        "client_id": client_id,
        "connection": connection,
        "path": path,
        "source_ip": authenticated["request"]["source_ip"],
        "request": authenticated["request"],
        "principal": copy.deepcopy(authenticated["principal"]),
        "response_queue": asyncio.Queue(
            maxsize=context["config"]["response_queue_maxsize"]),
        "principal_revalidation_lock": asyncio.Lock(),
        "event_transition_lock": asyncio.Lock(),
        "disconnect_event": asyncio.Event(),
        "event_subscription": None,
        "event_filters": [],
        "event_generation": 0,
        "event_high_watermark": 0,
        "last_revalidated_monotonic": time.monotonic(),
        "created_monotonic": time.monotonic(),
        "disconnect": False,
        "active_task_count": 0,
        "lock": threading.RLock(),
        "statistics": {
            "sent": 0,
            "stale_events_dropped": 0,
            "response_overflow": 0,
        },
    }
    with context["lock"]:
        context["clients"][client_id] = client
        context["statistics"]["connections_accepted"] += 1
    return client


def _unregister_client(context, client_id):
    with context["lock"]:
        return context["clients"].pop(str(client_id), None)


def _queue_response(client, envelope):
    with client["lock"]:
        if client["disconnect"]:
            return False
    try:
        client["response_queue"].put_nowait(copy.deepcopy(envelope))
        return True
    except asyncio.QueueFull:
        with client["lock"]:
            client["disconnect"] = True
            client["statistics"]["response_overflow"] += 1
        client["disconnect_event"].set()
        return False


def _principal_authorization_projection(principal):
    value = principal if isinstance(principal, dict) else {}
    resources = copy.deepcopy(value.get("resource_scopes") or [])
    resources.sort(key=_encode_json)
    return {
        "subject": str(value.get("subject") or ""),
        "auth_type": str(value.get("auth_type") or ""),
        "permissions": sorted(str(item) for item in value.get("permissions") or []),
        "site_scopes": sorted(str(item) for item in value.get("site_scopes") or []),
        "resource_scopes": resources,
    }


async def _refresh_event_principal(context, client, **kwargs):
    subscription = client.get("event_subscription")
    if not subscription:
        return {"ok": True, "generation": client["event_generation"]}
    candidate = kwargs["principal"]
    previous_generation = client["event_generation"]
    request = {
        "subscription_id": subscription["subscription_id"],
        "site_id": context["config"]["site_id"],
        "principal": copy.deepcopy(candidate),
        "filters": copy.deepcopy(client["event_filters"]),
        "generation": previous_generation,
        "reason": "principal_revalidation",
    }
    updated = await _invoke(context["update_event_subscription"], request)
    if not _has_explicit_callback_outcome(updated):
        return {"ok": False, "code": "invalid_event_stream_contract"}
    if updated.get("ok") is not True:
        return {
            "ok": False,
            "code": updated.get("code") or "authorization_denied",
        }
    generation = updated.get("generation")
    if isinstance(generation, bool) or not isinstance(generation, int) or \
            generation < previous_generation:
        return {"ok": False, "code": "invalid_event_stream_contract"}
    authorization_changed = bool(
        _principal_authorization_projection(candidate)
        != _principal_authorization_projection(client["principal"])
    )
    if authorization_changed and generation == previous_generation:
        return {"ok": False, "code": "invalid_event_stream_contract"}
    return {"ok": True, "generation": generation}


async def _ensure_principal_current_locked(context, client, **kwargs):
    now = time.monotonic()
    force = bool(kwargs.get("force"))
    if now - client["created_monotonic"] >= context["config"]["max_connection_age_s"]:
        return {"ok": False, "code": "authentication_expired"}
    elapsed = now - client["last_revalidated_monotonic"]
    if not force and elapsed < context["config"]["principal_revalidate_interval_s"]:
        return {"ok": True}
    request = {
        "principal": copy.deepcopy(client["principal"]),
        "handshake": copy.deepcopy(client["request"]),
        "site_id": context["config"]["site_id"],
        "client_id": client["client_id"],
    }
    checked = await _invoke(context["revalidate_principal"], request)
    if not _callback_succeeded(checked):
        return {"ok": False, "code": "authentication_expired"}
    principal = checked.get("principal")
    if not isinstance(principal, dict):
        return {"ok": False, "code": "authentication_expired"}
    current_subject = str(client["principal"].get("subject") or "")
    if not current_subject or str(principal.get("subject") or "") != current_subject:
        return {"ok": False, "code": "authentication_expired"}
    scopes = set(principal.get("site_scopes") or ())
    if context["config"]["site_id"] not in scopes and "*" not in scopes:
        return {"ok": False, "code": "authorization_denied"}
    refreshed = await _refresh_event_principal(
        context, client, principal=principal)
    if not refreshed.get("ok"):
        return refreshed
    previous_generation = client["event_generation"]
    client["principal"] = copy.deepcopy(principal)
    client["last_revalidated_monotonic"] = time.monotonic()
    client["event_generation"] = refreshed["generation"]
    if client["event_generation"] != previous_generation:
        return {"ok": False, "code": "replay_resync_required"}
    return {"ok": True}


async def _ensure_principal_current(context, client, **kwargs):
    """Serialize principal refresh and subscription generation CAS per client."""
    async with client["principal_revalidation_lock"]:
        return await _ensure_principal_current_locked(context, client, **kwargs)


async def _auth_watchdog(context, client):
    interval = context["config"]["principal_revalidate_interval_s"]
    while True:
        await asyncio.sleep(interval)
        checked = await _ensure_principal_current(context, client, force=True)
        if not checked.get("ok"):
            with client["lock"]:
                client["disconnect"] = True
            code = str(checked.get("code") or "authentication_expired")
            close_code = 1013 if code == "replay_resync_required" else 1008
            if code == "invalid_event_stream_contract":
                close_code = 1011
            await client["connection"].close(
                code=close_code, reason=code[:120])
            return


# =============================================================================
# HANDSHAKE
# =============================================================================

async def _process_request(context, connection, request):
    try:
        path = _path_only(request.path)
    except ValueError as exc:
        _record_handshake_rejection(context, "path")
        status = http.HTTPStatus.BAD_REQUEST if "query" in str(exc) else http.HTTPStatus.NOT_FOUND
        return connection.respond(status, str(exc) + "\n")
    origin = str(request.headers.get("origin") or "")
    if origin not in context["config"]["origin_allowlist"]:
        _record_handshake_rejection(context, "origin")
        return connection.respond(http.HTTPStatus.FORBIDDEN, "origin denied\n")
    with context["lock"]:
        accepting = context["ready"] and not context["shutting_down"]
    if not accepting:
        _record_handshake_rejection(context, "not_ready")
        return connection.respond(
            http.HTTPStatus.SERVICE_UNAVAILABLE, "server not ready\n")
    try:
        await asyncio.wait_for(
            context["handshake_semaphore"].acquire(),
            timeout=context["config"]["handshake_queue_timeout_s"])
    except TimeoutError:
        _record_handshake_rejection(context, "busy")
        return connection.respond(http.HTTPStatus.SERVICE_UNAVAILABLE, "server busy\n")
    reserved = False
    try:
        with context["lock"]:
            _prune_pending(context)
            current = len(context["clients"]) + context["reserved_connections"]
            if current >= context["config"]["max_connections"]:
                _record_handshake_rejection(context, "connection_limit")
                return connection.respond(
                    http.HTTPStatus.SERVICE_UNAVAILABLE, "connection limit\n")
            context["reserved_connections"] += 1
            reserved = True
        handshake = _handshake_request(connection, request, path)
        authenticated = await _invoke(context["authenticate"], handshake)
        if not _callback_succeeded(authenticated):
            with context["lock"]:
                context["statistics"]["authentication_rejected"] += 1
            _record_handshake_rejection(context, "authentication")
            return connection.respond(
                http.HTTPStatus.UNAUTHORIZED, "authentication required\n")
        principal = authenticated.get("principal")
        if not isinstance(principal, dict):
            _record_handshake_rejection(context, "invalid_principal")
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "invalid principal\n")
        subject = str(principal.get("subject") or "")
        if not subject:
            _record_handshake_rejection(context, "invalid_principal")
            return connection.respond(http.HTTPStatus.UNAUTHORIZED, "invalid principal\n")
        scopes = set(principal.get("site_scopes") or ())
        if context["config"]["site_id"] not in scopes and "*" not in scopes:
            _record_handshake_rejection(context, "site")
            return connection.respond(http.HTTPStatus.FORBIDDEN, "site denied\n")
        pending = {
            "principal": copy.deepcopy(principal),
            "request": handshake,
            "created_monotonic": time.monotonic(),
        }
        with context["lock"]:
            source_count = sum(
                1 for client in context["clients"].values()
                if client["source_ip"] == handshake["source_ip"])
            source_count += sum(
                1 for value in context["pending_principals"].values()
                if value["request"]["source_ip"] == handshake["source_ip"])
            principal_count = sum(
                1 for client in context["clients"].values()
                if str(client["principal"].get("subject") or "") == subject)
            principal_count += sum(
                1 for value in context["pending_principals"].values()
                if str(value["principal"].get("subject") or "") == subject)
            if source_count >= context["config"]["max_connections_per_source"]:
                _record_handshake_rejection(context, "source_limit")
                return connection.respond(
                    http.HTTPStatus.TOO_MANY_REQUESTS, "source connection limit\n")
            if principal_count >= context["config"]["max_connections_per_principal"]:
                _record_handshake_rejection(context, "principal_limit")
                return connection.respond(
                    http.HTTPStatus.TOO_MANY_REQUESTS, "principal connection limit\n")
            context["pending_principals"][id(connection)] = pending
        reserved = False
        return None
    except asyncio.CancelledError:
        raise
    except Exception:
        context["logger"].exception("WSS handshake authentication failed")
        _record_handshake_rejection(context, "internal")
        return connection.respond(http.HTTPStatus.INTERNAL_SERVER_ERROR, "handshake failed\n")
    finally:
        if reserved:
            with context["lock"]:
                context["reserved_connections"] = max(
                    0, context["reserved_connections"] - 1)
        context["handshake_semaphore"].release()


async def _process_response(context, *args, **kwargs):
    """Release authenticated reservations when RFC 6455 rejects the upgrade."""
    if len(args) != 3 or kwargs:
        raise ValueError("unexpected websockets process_response contract")
    connection, _request, response = args
    if response.status_code != http.HTTPStatus.SWITCHING_PROTOCOLS:
        if _release_pending(context, connection):
            _record_handshake_rejection(context, "upgrade")
    return response


# =============================================================================
# EVENT ENDPOINT AND ATOMIC REPLAY HANDOFF
# =============================================================================

async def _read_initial_subscription(context, connection):
    message = await asyncio.wait_for(
        connection.recv(), timeout=context["config"]["initial_message_timeout_s"])
    value = _validate_json_limits(_decode_json(message))
    return _validate_subscription(value, initial=True)


async def _send_replay_page_records(context, client, **kwargs):
    page = kwargs["page"]
    state = kwargs["state"]
    expected_generation = kwargs["generation"]
    records = page.get("records")
    if not isinstance(records, list):
        raise ValueError("event stream replay records must be a list")
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("envelope"), dict):
            raise ValueError("event stream replay record is invalid")
        sequence = record.get("event_sequence")
        if isinstance(sequence, bool) or not isinstance(sequence, int):
            raise ValueError("event stream replay sequence is invalid")
        if sequence <= state["last_record"] or sequence > state["high_watermark"]:
            raise ValueError("event stream replay sequence is not monotonic")
        envelope = copy.deepcopy(record["envelope"])
        envelope.setdefault("extensions", {})["replay_sequence"] = sequence
        async with client["principal_revalidation_lock"]:
            checked = await _ensure_principal_current_locked(context, client)
            if not checked.get("ok"):
                raise PermissionError(
                    checked.get("code") or "authentication_expired")
            if client["event_generation"] != expected_generation:
                raise PermissionError("replay_resync_required")
            await _send_json(context, client["connection"], envelope)
        with context["lock"]:
            context["statistics"]["replay_records"] += 1
        state["last_record"] = sequence
        state["records_sent"] += 1
        if state["records_sent"] > context["config"]["replay_record_limit"]:
            raise OverflowError("replay record safety limit exceeded")


async def _send_replay(context, client, **kwargs):
    initial = kwargs["initial"]
    opened = kwargs["opened"]
    high_watermark = opened.get("high_watermark")
    if isinstance(high_watermark, bool) or not isinstance(high_watermark, int):
        raise ValueError("event stream high_watermark is invalid")
    if high_watermark < initial["after_sequence"]:
        return {"ok": False, "code": "replay_resync_required"}
    replay_lag = high_watermark - initial["after_sequence"]
    with context["lock"]:
        context["statistics"]["replay_sessions"] += 1
        context["statistics"]["replay_lag_events_sum"] += replay_lag
        context["statistics"]["replay_lag_events_max"] = max(
            context["statistics"]["replay_lag_events_max"], replay_lag)
    state = {
        "cursor": initial["after_sequence"],
        "last_record": initial["after_sequence"],
        "high_watermark": high_watermark,
        "records_sent": 0,
        "pages": 0,
    }
    while state["cursor"] < high_watermark:
        checked = await _ensure_principal_current(context, client)
        if not checked.get("ok"):
            return {"ok": False, "code": checked.get("code") or "authentication_expired"}
        page_generation = client["event_generation"]
        page = await _invoke(context["read_replay_page"], {
            "subscription_id": opened["subscription_id"],
            "site_id": context["config"]["site_id"],
            "principal": copy.deepcopy(client["principal"]),
            "after_sequence": state["cursor"],
            "through_sequence": high_watermark,
            "limit": context["config"]["replay_page_size"],
            "generation": page_generation,
        })
        if not _has_explicit_callback_outcome(page):
            return {"ok": False, "code": "invalid_event_stream_contract"}
        if page.get("ok") is not True:
            return {
                "ok": False,
                "code": page.get("code") or "replay_resync_required",
            }
        if page.get("gap"):
            return {"ok": False, "code": "replay_resync_required"}
        try:
            await _send_replay_page_records(
                context, client, page=page, state=state,
                generation=page_generation)
        except PermissionError as exc:
            return {"ok": False, "code": str(exc) or "authentication_expired"}
        next_sequence = page.get("next_sequence")
        if isinstance(next_sequence, bool) or not isinstance(next_sequence, int):
            raise ValueError("event stream next_sequence is invalid")
        if next_sequence <= state["cursor"] or next_sequence > high_watermark:
            raise ValueError("event stream replay cursor did not advance")
        state["cursor"] = next_sequence
        state["pages"] += 1
        if state["pages"] > context["config"]["replay_page_limit"]:
            raise OverflowError("replay page safety limit exceeded")
        if page.get("complete") and state["cursor"] != high_watermark:
            raise ValueError("event stream completed before its high-water mark")
    return {
        "ok": True,
        "high_watermark": high_watermark,
        "records_sent": state["records_sent"],
    }


async def _take_event_outputs(client):
    response_task = asyncio.create_task(client["response_queue"].get())
    live_queue = client["event_subscription"]["live_queue"]
    live_task = asyncio.create_task(live_queue.get())
    core_disconnect = client["event_subscription"]["disconnect_event"]
    disconnect_task = asyncio.create_task(core_disconnect.wait())
    tasks = (response_task, live_task, disconnect_task)
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        outputs = []
        if response_task in done and not response_task.cancelled():
            outputs.append(("response", response_task.result()))
        if disconnect_task in done and not disconnect_task.cancelled():
            outputs.append(("disconnect", None))
        if disconnect_task in done and live_task in done and not live_task.cancelled():
            live_queue.task_done()
        elif live_task in done and not live_task.cancelled():
            outputs.append(("live", live_task.result()))
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        return outputs
    except BaseException:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        raise


async def _consume_live_event(context, client, record):
    subscription = client["event_subscription"]
    if not isinstance(record, dict) or not isinstance(record.get("envelope"), dict):
        raise ValueError("live event stream record is invalid")
    sequence = record.get("event_sequence")
    generation = record.get("generation")
    if isinstance(sequence, bool) or not isinstance(sequence, int):
        raise ValueError("live event sequence is invalid")
    if isinstance(generation, bool) or not isinstance(generation, int):
        raise ValueError("live subscription generation is invalid")
    delivered = False
    try:
        if generation != client["event_generation"] or \
                sequence <= client["event_high_watermark"]:
            client["statistics"]["stale_events_dropped"] += 1
            return
        envelope = copy.deepcopy(record["envelope"])
        envelope.setdefault("extensions", {})["live_sequence"] = sequence
        async with client["principal_revalidation_lock"]:
            checked = await _ensure_principal_current_locked(context, client)
            if not checked.get("ok"):
                raise PermissionError(
                    checked.get("code") or "authentication_expired")
            if generation != client["event_generation"]:
                raise PermissionError("replay_resync_required")
            await _send_json(context, client["connection"], envelope)
        delivered = True
        client["event_high_watermark"] = max(client["event_high_watermark"], sequence)
    finally:
        queued_monotonic = record.get("queued_monotonic")
        if delivered and isinstance(queued_monotonic, (int, float)) \
                and not isinstance(queued_monotonic, bool):
            latency = max(0.0, time.monotonic() - float(queued_monotonic))
            with context["lock"]:
                context["statistics"]["event_fanout_latency_seconds_sum"] += latency
                context["statistics"]["event_fanout_deliveries"] += 1
        await _invoke(context["event_consumed"], {
            "subscription_id": subscription["subscription_id"],
            "event_sequence": sequence,
            "generation": generation,
        })


async def _event_writer(context, client):
    live_queue = client["event_subscription"]["live_queue"]
    while True:
        outputs = await _take_event_outputs(client)
        for kind, item in outputs:
            if kind == "disconnect":
                await client["connection"].close(
                    code=1013, reason="event backpressure")
                return
            try:
                if kind == "response":
                    await _send_json(context, client["connection"], item)
                else:
                    async with client["event_transition_lock"]:
                        while not client["response_queue"].empty():
                            response = client["response_queue"].get_nowait()
                            try:
                                await _send_json(
                                    context, client["connection"], response)
                                client["statistics"]["sent"] += 1
                            finally:
                                client["response_queue"].task_done()
                        try:
                            await _consume_live_event(context, client, item)
                        except PermissionError as exc:
                            code = str(exc) or "authentication_expired"
                            if code == "replay_resync_required":
                                await _send_json(context, client["connection"], {
                                    "protocol": PROTOCOL,
                                    "type": "replay.resync_required",
                                    "code": "replay_resync_required",
                                })
                                await client["connection"].close(
                                    code=1013, reason="replay resync required")
                            else:
                                await client["connection"].close(
                                    code=1008, reason=code[:120])
                            return
                client["statistics"]["sent"] += 1
            finally:
                if kind == "response":
                    client["response_queue"].task_done()
                else:
                    live_queue.task_done()


async def _update_client_filters(context, client, update):
    async with client["event_transition_lock"]:
        async with client["principal_revalidation_lock"]:
            checked = await _ensure_principal_current_locked(
                context, client, force=True)
            if not checked.get("ok"):
                failed = dict(checked)
                failed["fatal"] = True
                return failed
            subscription = client["event_subscription"]
            result = await _invoke(context["update_event_subscription"], {
                "subscription_id": subscription["subscription_id"],
                "site_id": context["config"]["site_id"],
                "principal": copy.deepcopy(client["principal"]),
                "filters": copy.deepcopy(update["filters"]),
                "generation": client["event_generation"],
                "reason": "client_update",
            })
            if not _has_explicit_callback_outcome(result):
                return {
                    "ok": False, "code": "internal_error", "fatal": True}
            if result.get("ok") is not True:
                return result
            generation = result.get("generation")
            if isinstance(generation, bool) or not isinstance(generation, int) or \
                    generation <= client["event_generation"]:
                return {
                    "ok": False,
                    "code": "invalid_event_stream_contract",
                    "fatal": True,
                }
            client["event_filters"] = copy.deepcopy(update["filters"])
            client["event_generation"] = generation
            queued = _queue_response(client, {
                "protocol": PROTOCOL,
                "type": "subscription.accepted",
                "request_id": update["request_id"],
                "filters": copy.deepcopy(update["filters"]),
                "generation": generation,
            })
            return {"ok": bool(queued), "code": None if queued else "not_ready"}


async def _event_reader(context, client):
    async for message in client["connection"]:
        request_id = None
        try:
            value = _validate_json_limits(_decode_json(message))
            update = _validate_subscription(value, initial=False)
            request_id = update["request_id"]
            result = await _update_client_filters(context, client, update)
            if not isinstance(result, dict) or not result.get("ok"):
                code = result.get("code") if isinstance(result, dict) else None
                if isinstance(result, dict) and result.get("fatal"):
                    if code == "replay_resync_required":
                        _queue_response(client, {
                            "protocol": PROTOCOL,
                            "type": "replay.resync_required",
                            "code": "replay_resync_required",
                        })
                    else:
                        _queue_response(client, {
                            "protocol": PROTOCOL,
                            "type": "subscription.rejected",
                            "request_id": request_id,
                            "code": code or "authentication_expired",
                        })
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            client["response_queue"].join(),
                            timeout=context["config"]["send_timeout_s"] + 1.0)
                    close_code = 1013 if code == "replay_resync_required" else 1008
                    if code == "invalid_event_stream_contract":
                        close_code = 1011
                    await client["connection"].close(
                        code=close_code,
                        reason=str(code or "authentication expired")[:120])
                    return
                raise PermissionError(code or "authorization_denied")
        except PermissionError as exc:
            _queue_response(client, {
                "protocol": PROTOCOL,
                "type": "subscription.rejected",
                "request_id": request_id,
                "code": str(exc) or "authorization_denied",
            })
        except ValueError as exc:
            _queue_response(client, {
                "protocol": PROTOCOL,
                "type": "subscription.rejected",
                "request_id": request_id,
                "code": "invalid_payload",
                "message": str(exc),
            })
            with context["lock"]:
                context["statistics"]["commands_rejected"] += 1


def _validate_event_open_contract(context, opened, initial):
    live_queue = opened.get("live_queue")
    disconnect_event = opened.get("disconnect_event")
    generation = opened.get("generation")
    subscription_id = opened.get("subscription_id")
    high_watermark = opened.get("high_watermark")
    if not isinstance(subscription_id, str) or not subscription_id or \
            len(subscription_id) > 256:
        raise ValueError("event stream subscription_id is invalid")
    if not isinstance(live_queue, asyncio.Queue) or not 1 <= \
            live_queue.maxsize <= context["config"]["event_live_queue_maxsize"]:
        raise ValueError("event stream live queue is invalid")
    if not isinstance(disconnect_event, asyncio.Event):
        raise ValueError("event stream open contract is invalid")
    if isinstance(generation, bool) or not isinstance(generation, int) or generation < 0:
        raise ValueError("event stream generation is invalid")
    if isinstance(high_watermark, bool) or not isinstance(high_watermark, int) or \
            high_watermark < 0:
        raise ValueError("event stream high_watermark is invalid")
    return {
        "subscription_id": subscription_id,
        "live_queue": live_queue,
        "disconnect_event": disconnect_event,
        "generation": generation,
        "high_watermark": high_watermark,
        "cursor_ahead": high_watermark < initial["after_sequence"],
    }


async def _open_event_subscription_atomic(context, client, initial):
    async with client["principal_revalidation_lock"]:
        checked = await _ensure_principal_current_locked(
            context, client, force=True)
        if not checked.get("ok"):
            return checked
        opened = await _invoke(context["open_event_subscription"], {
            "site_id": context["config"]["site_id"],
            "client_id": client["client_id"],
            "principal": copy.deepcopy(client["principal"]),
            "filters": copy.deepcopy(initial["filters"]),
            "after_sequence": initial["after_sequence"],
            "live_queue_maxsize": context["config"]["event_live_queue_maxsize"],
        })
        if not _has_explicit_callback_outcome(opened):
            return {
                "ok": False,
                "code": "invalid_event_stream_contract",
            }
        if opened.get("ok") is not True:
            return opened
        subscription_id = opened.get("subscription_id")
        if isinstance(subscription_id, str) and subscription_id:
            client["event_subscription"] = {
                "subscription_id": subscription_id,
            }
        contract = _validate_event_open_contract(context, opened, initial)
        client["event_subscription"].update({
            "live_queue": contract["live_queue"],
            "disconnect_event": contract["disconnect_event"],
        })
        client["event_filters"] = copy.deepcopy(initial["filters"])
        client["event_generation"] = contract["generation"]
        return {"ok": True, "opened": opened, "contract": contract}


async def _serve_event_client(context, client):
    try:
        initial = await _read_initial_subscription(context, client["connection"])
    except (TimeoutError, ValueError) as exc:
        await _send_json(context, client["connection"], {
            "protocol": PROTOCOL,
            "type": "subscription.rejected",
            "request_id": None,
            "code": "invalid_payload",
            "message": str(exc),
        })
        await client["connection"].close(code=1008, reason="invalid subscription")
        return
    setup = await _open_event_subscription_atomic(context, client, initial)
    if not isinstance(setup, dict) or setup.get("ok") is not True:
        code = setup.get("code") if isinstance(setup, dict) else None
        code = code or "not_ready"
        if code == "replay_resync_required":
            await _send_json(context, client["connection"], {
                "protocol": PROTOCOL,
                "type": "replay.resync_required",
                "code": "replay_resync_required",
            })
            await client["connection"].close(
                code=1013, reason="replay resync required")
            return
        await _send_json(context, client["connection"], {
            "protocol": PROTOCOL,
            "type": "subscription.rejected",
            "request_id": initial["request_id"],
            "code": code,
        })
        close_code = 1008 if code in (
            "authentication_required", "authentication_expired",
            "authorization_denied", "invalid_payload") else 1013
        if code == "invalid_event_stream_contract":
            close_code = 1011
        await client["connection"].close(
            code=close_code, reason="subscription rejected")
        return
    opened = setup["opened"]
    contract = setup["contract"]
    if contract["cursor_ahead"]:
        await _send_json(context, client["connection"], {
            "protocol": PROTOCOL,
            "type": "replay.resync_required",
            "code": "replay_resync_required",
        })
        await client["connection"].close(
            code=1013, reason="replay cursor exceeds high-water mark")
        return
    await _send_json(context, client["connection"], {
        "protocol": PROTOCOL,
        "type": "subscription.accepted",
        "request_id": initial["request_id"],
        "client_id": client["client_id"],
        "filters": copy.deepcopy(initial["filters"]),
        "generation": contract["generation"],
        "replay_high_watermark": contract["high_watermark"],
    })
    try:
        replay = await asyncio.wait_for(
            _send_replay(context, client, initial=initial, opened=opened),
            timeout=context["config"]["replay_timeout_s"])
    except TimeoutError:
        replay = {"ok": False, "code": "replay_resync_required"}
    if not replay.get("ok"):
        code = replay.get("code") or "replay_resync_required"
        if code in ("authentication_expired", "authorization_denied"):
            await _send_json(context, client["connection"], {
                "protocol": PROTOCOL,
                "type": "subscription.rejected",
                "request_id": initial["request_id"],
                "code": code,
            })
            await client["connection"].close(code=1008, reason=code)
            return
        await _send_json(context, client["connection"], {
            "protocol": PROTOCOL,
            "type": "replay.resync_required",
            "code": "replay_resync_required",
        })
        await client["connection"].close(code=1013, reason="replay resync required")
        return
    client["event_high_watermark"] = replay["high_watermark"]
    await _run_client_tasks(
        context, client,
        reader=_event_reader(context, client),
        writer=_event_writer(context, client))


# =============================================================================
# CONTROL ENDPOINT AND SHARED DISPATCHER
# =============================================================================

async def _control_writer(context, client):
    while True:
        envelope = await client["response_queue"].get()
        try:
            try:
                await _send_json(context, client["connection"], envelope)
                client["statistics"]["sent"] += 1
            except ValueError as exc:
                # Command replies are preflighted by the reader.  Keep this
                # final guard so a future internal response path cannot turn
                # one serialization defect into a 1011 connection failure.
                context["logger"].error(
                    "WSS control response rejected by JSON boundary: %s", exc)
        finally:
            client["response_queue"].task_done()


def _command_completed(context, task):
    with context["lock"]:
        context["inflight_commands"].discard(task)


async def _run_dispatched_command(context, request):
    try:
        return await _invoke(context["dispatch_command"], request)
    finally:
        context["command_semaphore"].release()


def _control_response_envelopes(result, request_id, action):
    if result.get("ok") is True:
        responses = [{
            "protocol": PROTOCOL,
            "type": "command.accepted",
            "request_id": request_id,
            "action": action,
            "operation_id": result.get("operation_id"),
            "state": result.get("state") or "ACCEPTED",
        }]
        if "reply" in result:
            responses.append({
                "protocol": PROTOCOL,
                "type": "command.result",
                "request_id": request_id,
                "action": action,
                "result": result["reply"],
            })
        return responses
    rejected = {
        "protocol": PROTOCOL,
        "type": "command.rejected",
        "request_id": request_id,
        "action": action,
        "code": result.get("code") or "internal_error",
        "message": result.get("message"),
    }
    if isinstance(result.get("details"), dict):
        rejected["details"] = result["details"]
    return [rejected]


def _prepare_control_exchange(context, result, request_id, action):
    """Preflight an entire accepted/result exchange before queueing its head."""
    try:
        prepared_result = _prepare_json(result)
        responses = [
            _prepare_outgoing_envelope(context, envelope)
            for envelope in _control_response_envelopes(
                prepared_result, request_id, action)
        ]
        return prepared_result, responses
    except (TypeError, ValueError, OverflowError) as exc:
        context["logger"].error(
            "WSS command reply rejected by JSON boundary "
            "request_id=%s action=%s error=%s",
            request_id or "unknown", action or "unknown", exc,
        )
        rejected_result = {"ok": False, "code": "internal_error"}
        responses = [
            _prepare_outgoing_envelope(context, envelope)
            for envelope in _control_response_envelopes(
                rejected_result, request_id, action)
        ]
        return rejected_result, responses


async def _dispatch_command(context, client, request):
    with context["lock"]:
        if context["shutting_down"]:
            return {
                "ok": False, "code": "not_ready",
                "message": "server shutdown is in progress",
            }
    try:
        await asyncio.wait_for(
            context["command_semaphore"].acquire(),
            timeout=context["config"]["command_queue_timeout_s"])
    except TimeoutError:
        return {"ok": False, "code": "not_ready", "message": "dispatcher is busy"}
    dispatch_request = {
            "transport": "wss",
            "protocol": PROTOCOL,
            "site_id": context["config"]["site_id"],
            "proxy_instance_id": context["config"]["instance_id"],
            "client_id": client["client_id"],
            "source_ip": client["source_ip"],
            "user_agent": client["request"].get("user_agent"),
            "principal": copy.deepcopy(client["principal"]),
            "request_id": request["request_id"],
            "action": request["action"],
            "idempotency_key": request["idempotency_key"],
            "ttl_s": request["ttl_s"],
            "csrf_token": request["csrf_token"],
            "session_token": client["request"]["cookies"].get(
                context["config"]["session_cookie"]),
            "payload": copy.deepcopy(request["payload"]),
        }
    # Admission and registration form one shutdown barrier.  ``stop()`` may be
    # called from another thread, so a second check followed by an unlocked
    # ``create_task`` would leave a window in which shutdown could drain an
    # empty set before the durable mutation was registered.
    with context["lock"]:
        if context["shutting_down"]:
            context["command_semaphore"].release()
            return {
                "ok": False, "code": "not_ready",
                "message": "server shutdown is in progress",
            }
        try:
            task = asyncio.create_task(
                _run_dispatched_command(context, dispatch_request),
                name="WSSCommand-" + request["request_id"],
            )
        except Exception:
            context["command_semaphore"].release()
            raise
        context["inflight_commands"].add(task)
    task.add_done_callback(partial(_command_completed, context))
    return await asyncio.shield(task)


async def _control_reader(context, client):
    async for message in client["connection"]:
        started_monotonic = time.monotonic()
        request_id = None
        action = None
        try:
            value = _validate_json_limits(_decode_json(message))
            request = _validate_command_request(value)
            request_id = request["request_id"]
            action = request["action"]
            checked = await _ensure_principal_current(context, client, force=True)
            if not checked.get("ok"):
                result = {"ok": False, "code": checked.get("code")}
            else:
                result = await _dispatch_command(context, client, request)
            if not _has_explicit_callback_outcome(result):
                result = {"ok": False, "code": "internal_error"}
            result, responses = _prepare_control_exchange(
                context, result, request_id, action)
            _observe_command(context, action, result, started_monotonic)
            if result.get("ok") is True:
                with context["lock"]:
                    context["statistics"]["commands_accepted"] += 1
            else:
                with context["lock"]:
                    context["statistics"]["commands_rejected"] += 1
            for response in responses:
                if not _queue_response(client, response):
                    break
        except ValueError as exc:
            rejected_result = {"ok": False, "code": "invalid_payload"}
            _observe_command(
                context, action, rejected_result, started_monotonic)
            with context["lock"]:
                context["statistics"]["commands_rejected"] += 1
            _queue_response(client, {
                "protocol": PROTOCOL,
                "type": "command.rejected",
                "request_id": request_id,
                "action": action,
                "code": "invalid_payload",
                "message": str(exc),
            })
        except asyncio.CancelledError:
            raise
        except Exception:
            context["logger"].exception("WSS command dispatch failed")
            rejected_result = {"ok": False, "code": "internal_error"}
            _observe_command(
                context, action, rejected_result, started_monotonic)
            with context["lock"]:
                context["statistics"]["commands_rejected"] += 1
            _queue_response(client, {
                "protocol": PROTOCOL,
                "type": "command.rejected",
                "request_id": request_id,
                "action": action,
                "code": "internal_error",
            })


async def _serve_control_client(context, client):
    _queue_response(client, {
        "protocol": PROTOCOL,
        "type": "connection.ready",
        "client_id": client["client_id"],
        "endpoint": "control",
    })
    await _run_client_tasks(
        context, client,
        reader=_control_reader(context, client),
        writer=_control_writer(context, client))


# =============================================================================
# COMMON CONNECTION LIFECYCLE
# =============================================================================

async def _close_writer_timeout(context, client):
    context["logger"].warning(
        "WSS slow client send timed out; closing connection: client_id=%s path=%s",
        client["client_id"], client["path"],
    )
    with contextlib.suppress(Exception):
        await asyncio.wait_for(
            client["connection"].close(code=1013, reason="send timeout"),
            timeout=context["config"]["close_timeout_s"],
        )


async def _run_client_tasks(context, client, **kwargs):
    reader_task = asyncio.create_task(
        kwargs["reader"], name="WSSReader-" + client["client_id"])
    writer_task = asyncio.create_task(
        kwargs["writer"], name="WSSWriter-" + client["client_id"])
    watchdog_task = client["auth_watchdog_task"]
    disconnect_task = asyncio.create_task(
        _disconnect_watcher(client), name="WSSBackpressure-" + client["client_id"])
    tasks = (reader_task, writer_task, watchdog_task, disconnect_task)
    with client["lock"]:
        client["active_task_count"] = len(tasks)
    try:
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        for task in done:
            if task.cancelled():
                continue
            exception = task.exception()
            if not exception or isinstance(exception, websockets.ConnectionClosed):
                continue
            if task is writer_task and isinstance(exception, TimeoutError):
                await _close_writer_timeout(context, client)
                return
            raise exception
        for task in pending:
            task.cancel()
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with client["lock"]:
            client["active_task_count"] = 0


async def _disconnect_watcher(client):
    await client["disconnect_event"].wait()
    await client["connection"].close(code=1013, reason="response backpressure")


async def _close_event_subscription(context, client):
    subscription = client.get("event_subscription")
    if not subscription:
        return
    with contextlib.suppress(Exception):
        await _invoke(context["close_event_subscription"], {
            "subscription_id": subscription["subscription_id"],
            "client_id": client["client_id"],
        })


async def _connection_handler(context, connection):
    try:
        path = _path_only(connection.request.path)
    except ValueError as exc:
        await connection.close(code=1008, reason=str(exc)[:120])
        return
    client = _activate_client(context, connection, path)
    if not client:
        await connection.close(code=1008, reason="authentication state missing")
        return
    client["auth_watchdog_task"] = asyncio.create_task(
        _auth_watchdog(context, client), name="WSSAuth-" + client["client_id"])
    try:
        if path == EVENTS_PATH:
            await _serve_event_client(context, client)
        else:
            await _serve_control_client(context, client)
    except websockets.ConnectionClosed:
        pass
    except asyncio.CancelledError:
        raise
    except Exception:
        context["logger"].exception("WSS client handler failed")
        with contextlib.suppress(Exception):
            await connection.close(code=1011, reason="internal error")
    finally:
        watchdog = client.get("auth_watchdog_task")
        if watchdog and not watchdog.done():
            watchdog.cancel()
        if watchdog:
            await asyncio.gather(watchdog, return_exceptions=True)
        await _close_event_subscription(context, client)
        _unregister_client(context, client["client_id"])
        close_code = getattr(connection, "close_code", None)
        with context["lock"]:
            context["statistics"]["connections_closed"] += 1
            if close_code in (1000, 1001):
                context["statistics"]["connections_closed_cleanly"] += 1


async def _close_clients(context):
    with context["lock"]:
        clients = tuple(context["clients"].values())
    await asyncio.gather(*(
        client["connection"].close(code=1001, reason="server shutdown")
        for client in clients
    ), return_exceptions=True)


async def _drain_commands(context):
    with context["lock"]:
        tasks = tuple(context["inflight_commands"])
    if not tasks:
        return
    done, pending = await asyncio.wait(
        tasks,
        timeout=context["config"]["command_drain_timeout_s"],
    )
    if pending:
        context["logger"].error(
            "WSS shutdown command drain timed out: pending=%s", len(pending))
        # A coroutine awaiting ``asyncio.to_thread`` cannot stop the worker
        # thread when cancelled.  Cancelling here would let that thread mutate
        # durable state after the Proxy has closed its database pool.  Command
        # backends own their execution deadlines, so shutdown waits for the
        # already-admitted work to reach its real terminal outcome.
        await asyncio.gather(*pending, return_exceptions=True)
        done = done.union(pending)
    for task in done:
        if not task.cancelled() and task.exception():
            context["logger"].error(
                "WSS drained command failed: error_type=%s",
                type(task.exception()).__name__)


async def _drain_responses(context):
    with context["lock"]:
        queues = tuple(
            client["response_queue"] for client in context["clients"].values()
            if client["path"] == CONTROL_PATH)
    if not queues:
        return
    try:
        await asyncio.wait_for(
            asyncio.gather(*(queue.join() for queue in queues)),
            timeout=context["config"]["close_timeout_s"],
        )
    except TimeoutError:
        context["logger"].error("WSS shutdown response drain timed out")


# =============================================================================
# SERVER INTERFACE
# =============================================================================

async def serve_websocket_server(context):
    """Run the direct ``websockets`` listener until a stop is requested."""
    context["loop"] = asyncio.get_running_loop()
    context["stop_event"] = asyncio.Event()
    context["handshake_semaphore"] = asyncio.Semaphore(
        context["config"]["handshake_concurrency"])
    context["command_semaphore"] = asyncio.Semaphore(
        context["config"]["command_concurrency"])
    with context["lock"]:
        stop_requested = context["stop_requested"]
        context["shutting_down"] = stop_requested
    if stop_requested:
        context["stop_event"].set()
    tls_context = build_server_ssl_context(**context["config"]["tls"])
    handler = partial(_connection_handler, context)
    process_request = partial(_process_request, context)
    process_response = partial(_process_response, context)
    try:
        async with websockets.serve(
                handler,
                context["config"]["host"],
                context["config"]["port"],
                ssl=tls_context,
                origins=tuple(context["config"]["origin_allowlist"]),
                process_request=process_request,
                process_response=process_response,
                open_timeout=context["config"]["open_timeout_s"],
                close_timeout=context["config"]["close_timeout_s"],
                ping_interval=context["config"]["ping_interval_s"],
                ping_timeout=context["config"]["ping_timeout_s"],
                max_size=context["config"]["max_message_bytes"],
                max_queue=context["config"]["protocol_queue_maxsize"],
                write_limit=context["config"]["write_limit_bytes"],
                compression=None,
                server_header=None,
                logger=context["protocol_logger"]):
            with context["lock"]:
                context["ready"] = True
            await context["stop_event"].wait()
            with context["lock"]:
                context["ready"] = False
                context["shutting_down"] = True
            await _drain_commands(context)
            await asyncio.sleep(0)
            await _drain_responses(context)
            await _close_clients(context)
    finally:
        with context["lock"]:
            context["ready"] = False
            context["shutting_down"] = True
            context["pending_principals"].clear()
            context["reserved_connections"] = 0
        await _drain_commands(context)
        await _drain_responses(context)
        await _close_clients(context)


def request_websocket_stop(context):
    with context["lock"]:
        context["ready"] = False
        context["shutting_down"] = True
        context["stop_requested"] = True
    loop = context.get("loop")
    stop_event = context.get("stop_event")
    if loop and stop_event:
        loop.call_soon_threadsafe(stop_event.set)
        return True
    return False


def snapshot_websocket_server(context):
    with context["lock"]:
        clients = tuple(context["clients"].values())
        statistics = copy.deepcopy(context["statistics"])
        ready = context["ready"]
        shutting_down = context["shutting_down"]
        reserved = context["reserved_connections"]
        pending_principals = len(context["pending_principals"])
    return {
        "ready": ready,
        "shutting_down": shutting_down,
        "host": context["config"]["host"],
        "port": context["config"]["port"],
        "clients": len(clients),
        "reserved_connections": reserved,
        "pending_principal_connections": pending_principals,
        "event_clients": sum(1 for client in clients if client["path"] == EVENTS_PATH),
        "control_clients": sum(1 for client in clients if client["path"] == CONTROL_PATH),
        "response_queue_depth": sum(client["response_queue"].qsize() for client in clients),
        "task_count": len(context["inflight_commands"]) + sum(
            int(client.get("active_task_count") or 0) for client in clients),
        "inflight_commands": len(context["inflight_commands"]),
        "replay_page_size": context["config"]["replay_page_size"],
        "protocol_queue_maxsize": context["config"]["protocol_queue_maxsize"],
        "response_queue_maxsize": context["config"]["response_queue_maxsize"],
        "event_live_queue_maxsize": context["config"]["event_live_queue_maxsize"],
        "max_message_bytes": context["config"]["max_message_bytes"],
        "statistics": statistics,
    }


def create_async_websocket_server(**kwargs):
    """Create the transport and validate every mandatory injected callback."""
    callback_names = (
        "authenticate", "revalidate_principal", "dispatch_command",
        "open_event_subscription", "read_replay_page",
        "update_event_subscription", "close_event_subscription",
        "event_consumed",
    )
    for name in callback_names:
        if not callable(kwargs.get(name)):
            raise ValueError(name + " callback is required")
        callback = kwargs[name]
        if not inspect.iscoroutinefunction(callback) and not inspect.iscoroutinefunction(
                getattr(callback, "__call__", None)):
            raise ValueError(name + " callback must be asynchronous")
    allowed_actions = frozenset(
        str(value) for value in kwargs.get("allowed_actions") or ())
    if any(not ACTION_PATTERN.fullmatch(value) for value in allowed_actions):
        raise ValueError("allowed WSS command action is invalid")
    supplied = copy.deepcopy(kwargs.get("config") or {})
    origins = tuple(sorted(set(supplied.get("origin_allowlist") or ())))
    if not origins or any(origin in ("null", "*") for origin in origins):
        raise ValueError("an exact non-wildcard Origin allowlist is required")
    for origin in origins:
        parsed = urllib.parse.urlsplit(str(origin))
        if parsed.scheme != "https" or not parsed.netloc or parsed.username or \
                parsed.password or parsed.path or parsed.query or parsed.fragment:
            raise ValueError("each allowed Origin must be an exact HTTPS origin")
    tls = copy.deepcopy(supplied.get("tls") or {})
    if not tls.get("certfile") or not tls.get("keyfile"):
        raise ValueError("WSS TLS certfile and keyfile are required")
    site_id = str(supplied.get("site_id") or "")
    instance_id = str(supplied.get("instance_id") or "")
    if not TOKEN_PATTERN.fullmatch(site_id) or not TOKEN_PATTERN.fullmatch(instance_id):
        raise ValueError("site_id and instance_id must be normalized routing tokens")
    port = int(supplied.get("port", 8443))
    if not 1 <= port <= 65535:
        raise ValueError("WSS port is outside its allowed range")
    config = {
        "site_id": site_id,
        "instance_id": instance_id,
        "host": str(supplied.get("host") or "0.0.0.0"),
        "port": port,
        "origin_allowlist": origins,
        "session_cookie": str(supplied.get("session_cookie") or "automation_session"),
        "tls": tls,
        "max_connections": min(100000, max(1, int(
            supplied.get("max_connections", 4096)))),
        "max_connections_per_source": min(10000, max(1, int(
            supplied.get("max_connections_per_source", 256)))),
        "max_connections_per_principal": min(10000, max(1, int(
            supplied.get("max_connections_per_principal", 128)))),
        "max_message_bytes": min(4 * 1024 * 1024, max(4096, int(
            supplied.get("max_message_bytes", 1024 * 1024)))),
        "protocol_queue_maxsize": min(256, max(1, int(
            supplied.get("protocol_queue_maxsize", 16)))),
        "response_queue_maxsize": min(1024, max(8, int(
            supplied.get("response_queue_maxsize", 64)))),
        "event_live_queue_maxsize": min(4096, max(8, int(
            supplied.get("event_live_queue_maxsize", 256)))),
        "write_limit_bytes": min(1024 * 1024, max(4096, int(
            supplied.get("write_limit_bytes", 32768)))),
        "handshake_concurrency": min(1024, max(1, int(
            supplied.get("handshake_concurrency", 64)))),
        "command_concurrency": min(1024, max(1, int(
            supplied.get("command_concurrency", 128)))),
        "replay_page_size": min(500, max(1, int(
            supplied.get("replay_page_size", 200)))),
        "replay_page_limit": min(10000, max(1, int(
            supplied.get("replay_page_limit", 1000)))),
        "replay_record_limit": min(1000000, max(1, int(
            supplied.get("replay_record_limit", 100000)))),
        "open_timeout_s": _bounded_float(
            supplied.get("open_timeout_s", 5.0), label="open_timeout_s",
            minimum=1.0, maximum=60.0),
        "close_timeout_s": _bounded_float(
            supplied.get("close_timeout_s", 10.0), label="close_timeout_s",
            minimum=1.0, maximum=60.0),
        "ping_interval_s": _bounded_float(
            supplied.get("ping_interval_s", 20.0), label="ping_interval_s",
            minimum=5.0, maximum=300.0),
        "ping_timeout_s": _bounded_float(
            supplied.get("ping_timeout_s", 20.0), label="ping_timeout_s",
            minimum=5.0, maximum=300.0),
        "send_timeout_s": _bounded_float(
            supplied.get("send_timeout_s", 5.0), label="send_timeout_s",
            minimum=0.1, maximum=60.0),
        "initial_message_timeout_s": _bounded_float(
            supplied.get("initial_message_timeout_s", 5.0),
            label="initial_message_timeout_s", minimum=1.0, maximum=60.0),
        "handshake_queue_timeout_s": _bounded_float(
            supplied.get("handshake_queue_timeout_s", 1.0),
            label="handshake_queue_timeout_s", minimum=0.1, maximum=30.0),
        "command_queue_timeout_s": _bounded_float(
            supplied.get("command_queue_timeout_s", 1.0),
            label="command_queue_timeout_s", minimum=0.1, maximum=30.0),
        "principal_revalidate_interval_s": _bounded_float(
            supplied.get("principal_revalidate_interval_s", 30.0),
            label="principal_revalidate_interval_s", minimum=1.0, maximum=30.0),
        "max_connection_age_s": _bounded_float(
            supplied.get("max_connection_age_s", 1800.0),
            label="max_connection_age_s", minimum=60.0, maximum=1800.0),
        "replay_timeout_s": _bounded_float(
            supplied.get("replay_timeout_s", 60.0), label="replay_timeout_s",
            minimum=1.0, maximum=300.0),
        "command_drain_timeout_s": _bounded_float(
            supplied.get("command_drain_timeout_s", 30.0),
            label="command_drain_timeout_s", minimum=1.0, maximum=300.0),
    }
    protocol_logger = logging.getLogger("automation.proxy.wss.protocol")
    # websockets DEBUG traces include handshake headers and complete frames.
    # Keep protocol internals silent; sanitized application metrics and errors
    # are emitted through the separate context logger instead.
    protocol_logger.setLevel(logging.CRITICAL)
    context = {
        "config": config,
        "logger": kwargs.get("logger") or logging.getLogger("automation.proxy.wss"),
        "protocol_logger": protocol_logger,
        "lock": threading.RLock(),
        "clients": {},
        "inflight_commands": set(),
        "pending_principals": {},
        "reserved_connections": 0,
        "loop": None,
        "stop_event": None,
        "handshake_semaphore": None,
        "command_semaphore": None,
        "ready": False,
        "shutting_down": False,
        "stop_requested": False,
        "allowed_actions": allowed_actions,
        "statistics": {
            "connections_accepted": 0,
            "connections_closed": 0,
            "connections_closed_cleanly": 0,
            "authentication_rejected": 0,
            "handshake_rejections": {
                reason: 0 for reason in HANDSHAKE_REJECTION_REASONS
            },
            "commands_accepted": 0,
            "commands_rejected": 0,
            "command_results": {},
            "command_latency_seconds": {},
            "replay_sessions": 0,
            "replay_records": 0,
            "replay_lag_events_sum": 0,
            "replay_lag_events_max": 0,
            "event_fanout_deliveries": 0,
            "event_fanout_latency_seconds_sum": 0.0,
            "failed_upgrade_reservations_released": 0,
        },
    }
    for name in callback_names:
        context[name] = kwargs[name]
    return {
        "context": context,
        "serve": partial(serve_websocket_server, context),
        "stop": partial(request_websocket_stop, context),
        "snapshot": partial(snapshot_websocket_server, context),
    }

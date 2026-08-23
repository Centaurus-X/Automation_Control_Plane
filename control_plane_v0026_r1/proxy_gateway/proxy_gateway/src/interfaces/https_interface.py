# -*- coding: utf-8 -*-

# proxy_gateway/src/interfaces/https_interface.py

"""HTTPS JSON API with sessions, CSRF, RBAC, limits and fixed domain routes."""

import asyncio
import copy
import datetime
import decimal
import ipaddress
import json
import re
import uuid

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library.rate_limit_store import bucket_key


REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


async def _run_blocking(function, *args, **kwargs):
    return await asyncio.to_thread(partial(function, *args, **kwargs))


def _json_safe(value):
    if isinstance(value, dict):
        return {str(key): _json_safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_json_safe(item) for item in value]
    if isinstance(value, (
            datetime.datetime, datetime.date, uuid.UUID, decimal.Decimal,
            ipaddress.IPv4Address, ipaddress.IPv6Address,
            ipaddress.IPv4Interface, ipaddress.IPv6Interface,
            ipaddress.IPv4Network, ipaddress.IPv6Network)):
        return str(value)
    if isinstance(value, bytes):
        return value.decode("utf-8", errors="replace")
    return value


def _status_for(result, success=200):
    if result.get("ok"):
        return 202 if result.get("state") == "PENDING" else success
    codes = {
        "authentication_required": 401,
        "authorization_denied": 403,
        "not_found": 404,
        "conflict": 409,
        "stale_revision": 409,
        "rate_limited": 429,
        "not_ready": 503,
        "database_unavailable": 503,
        "not_config_authority": 503,
        "internal_error": 500,
        "replay_detected": 409,
    }
    return codes.get(result.get("code"), 400)


def _response(value, status=None):
    from starlette.responses import JSONResponse

    result = value if isinstance(value, dict) else make_success(result=value)
    return JSONResponse(_json_safe(result), status_code=status or _status_for(result))


def _request_id(request):
    scoped = str(request.scope.get("state", {}).get("request_id") or "")
    if REQUEST_ID_PATTERN.fullmatch(scoped):
        return scoped
    supplied = str(request.headers.get("x-request-id") or "")
    return supplied if REQUEST_ID_PATTERN.fullmatch(supplied) else str(uuid.uuid4())


def _source_ip(request):
    return request.client.host if request.client else None


def _bearer_token(request):
    value = str(request.headers.get("authorization") or "")
    return value[7:].strip() if value.lower().startswith("bearer ") else None


async def _json_body(context, request, **kwargs):
    maximum = int(kwargs.get("maximum_bytes", context["max_json_bytes"]))
    length = request.headers.get("content-length")
    if length and int(length) > maximum:
        raise ValueError("request payload exceeds its size limit")
    content_type = str(request.headers.get("content-type") or "").split(";", 1)[0].strip().lower()
    if content_type != "application/json":
        raise ValueError("Content-Type must be application/json")
    body = await request.body()
    if len(body) > maximum:
        raise ValueError("request payload exceeds its size limit")
    value = json.loads(body.decode("utf-8", errors="strict"))
    if not isinstance(value, dict):
        raise ValueError("request JSON must be an object")
    return value


def _validate_fields(value, allowed, required=()):
    missing = sorted(set(required) - set(value))
    unknown = sorted(set(value) - set(allowed))
    if missing:
        return make_error("invalid_payload", "required fields are missing", fields=missing)
    if unknown:
        return make_error("invalid_payload", "unknown fields are not allowed", fields=unknown)
    return make_success()


def _authenticate(context, request, **kwargs):
    bearer = _bearer_token(request)
    if bearer:
        return context["auth"]["authenticate_api_token"](bearer)
    return context["auth"]["authenticate_session"]({
        "session_token": request.cookies.get(context["session_cookie"]),
        "csrf_token": request.headers.get("x-csrf-token"),
        "csrf_required": bool(kwargs.get("csrf_required", False)),
        "source_ip": _source_ip(request),
        "user_agent": request.headers.get("user-agent"),
    })


def _api_rate(context, request, principal, action):
    subject = principal.get("subject") if isinstance(principal, dict) else "anonymous"
    key = bucket_key("api", _source_ip(request), subject, action)
    return context["rate_limit"]["consume"](
        key,
        limit=context["api_rate_limit"],
        window_s=context["api_rate_window_s"],
    )


async def _audit(context, request, principal, action, result, **kwargs):
    return await _run_blocking(context["audit"]["append"], {
        "request_id": kwargs.get("request_id") or _request_id(request),
        "correlation_id": kwargs.get("correlation_id"),
        "principal_subject": principal.get("subject") if isinstance(principal, dict) else None,
        "session_id": principal.get("session_id") if isinstance(principal, dict) else None,
        "source_ip": _source_ip(request),
        "proxy_instance_id": context["instance_id"],
        "site_id": context["site_id"],
        "resource": kwargs.get("resource") or action,
        "action": action,
        "decision": "ALLOW" if result.get("ok") else "DENY",
        "reason_code": result.get("code") or "ok",
        "operation_id": result.get("operation_id"),
        "details": {},
    })


def _set_session_cookies(context, response, result):
    max_age = context["session_max_age_s"]
    response.set_cookie(
        context["session_cookie"], result["session_token"],
        max_age=max_age, secure=True, httponly=True, samesite="strict", path="/",
    )
    response.set_cookie(
        context["csrf_cookie"], result["csrf_token"],
        max_age=max_age, secure=True, httponly=False, samesite="strict", path="/",
    )


def _clear_session_cookies(context, response):
    response.delete_cookie(context["session_cookie"], path="/", secure=True, httponly=True)
    response.delete_cookie(context["csrf_cookie"], path="/", secure=True, httponly=False)


async def auth_login(context, request):
    request_id = _request_id(request)
    try:
        body = await _json_body(context, request, maximum_bytes=16384)
    except Exception as exc:
        return _response(make_error("invalid_payload", str(exc)), 400)
    checked = _validate_fields(body, ("username", "password"), ("username", "password"))
    if not checked.get("ok"):
        return _response(checked)
    result = await _run_blocking(context["auth"]["login"], {
        "username": body["username"], "password": body["password"],
        "source_ip": _source_ip(request), "user_agent": request.headers.get("user-agent"),
    })
    principal = result.get("user") or {}
    await _audit(context, request, principal, "auth.login", result, request_id=request_id)
    if not result.get("ok"):
        return _response(result)
    response = _response({
        "ok": True, "user": result["user"], "expires_at": result["expires_at"]})
    _set_session_cookies(context, response, result)
    return response


async def auth_logout(context, request):
    authenticated = await _run_blocking(_authenticate, context, request, csrf_required=True)
    if not authenticated.get("ok"):
        return _response(authenticated)
    result = await _run_blocking(context["auth"]["logout"], {
        "session_token": request.cookies.get(context["session_cookie"])})
    await _audit(context, request, authenticated["principal"], "auth.logout", result)
    response = _response(result)
    _clear_session_cookies(context, response)
    return response


async def auth_session(context, request):
    csrf_required = request.method == "POST"
    authenticated = await _run_blocking(
        _authenticate, context, request, csrf_required=csrf_required)
    if not authenticated.get("ok"):
        return _response(authenticated)
    if request.method == "GET":
        return _response({"ok": True, "principal": authenticated["principal"]})
    result = await _run_blocking(context["auth"]["rotate_session"], {
        "session_token": request.cookies.get(context["session_cookie"]),
        "csrf_token": request.headers.get("x-csrf-token"),
        "csrf_required": True,
        "source_ip": _source_ip(request),
        "user_agent": request.headers.get("user-agent"),
    })
    response = _response({
        "ok": result.get("ok"),
        "principal": result.get("principal"),
        "expires_at": result.get("expires_at"),
        "code": result.get("code"),
        "message": result.get("message"),
    })
    if result.get("ok"):
        _set_session_cookies(context, response, result)
    return response


async def _authenticated_and_limited(context, request, action, **kwargs):
    authenticated = await _run_blocking(
        _authenticate, context, request,
        csrf_required=kwargs.get("csrf_required", False))
    if not authenticated.get("ok"):
        return None, authenticated
    limited = await _run_blocking(
        _api_rate, context, request, authenticated["principal"], action)
    if not limited.get("allowed"):
        return None, make_error("rate_limited", "API rate limit exceeded", reset_at=limited.get("reset_at"))
    return authenticated["principal"], None


async def _dispatch_http(context, request, action, payload, **kwargs):
    authenticated = await _run_blocking(
        _authenticate, context, request, csrf_required=False)
    if not authenticated.get("ok"):
        return authenticated
    ttl_value = request.headers.get("x-command-ttl-s", kwargs.get("ttl_s", 30.0))
    return await context["dispatcher"]["dispatch"]({
        "transport": "https",
        "site_id": context["site_id"],
        "proxy_instance_id": context["instance_id"],
        "source_ip": _source_ip(request),
        "user_agent": request.headers.get("user-agent"),
        "principal": authenticated["principal"],
        "request_id": _request_id(request),
        "action": action,
        "idempotency_key": request.headers.get("idempotency-key"),
        "ttl_s": ttl_value,
        "csrf_token": request.headers.get("x-csrf-token"),
        "session_token": request.cookies.get(context["session_cookie"]),
        "payload": copy.deepcopy(payload),
    })


def _dispatcher_result(result):
    if result.get("ok") and isinstance(result.get("reply"), dict):
        return result["reply"]
    return result


async def users_endpoint(context, request):
    if request.method == "GET":
        try:
            payload = {"limit": int(request.query_params.get("limit", "100"))}
        except ValueError:
            return _response(make_error("invalid_payload", "limit must be an integer"))
        result = await _dispatch_http(context, request, "users.list", payload)
        return _response(_dispatcher_result(result))
    try:
        body = await _json_body(context, request)
    except Exception as exc:
        return _response(make_error("invalid_payload", str(exc)))
    result = await _dispatch_http(context, request, "user.create", body)
    value = _dispatcher_result(result)
    return _response(value, 201 if value.get("ok") else None)


async def roles_endpoint(context, request):
    result = await _dispatch_http(context, request, "roles.list", {})
    return _response(_dispatcher_result(result))


async def permissions_endpoint(context, request):
    result = await _dispatch_http(context, request, "permissions.list", {})
    return _response(_dispatcher_result(result))


async def api_tokens_endpoint(context, request):
    if request.method == "GET":
        try:
            payload = {"limit": int(request.query_params.get("limit", "100"))}
        except ValueError:
            return _response(make_error("invalid_payload", "limit must be an integer"))
        result = await _dispatch_http(context, request, "api_tokens.list", payload)
        return _response(_dispatcher_result(result))
    try:
        body = await _json_body(context, request)
    except Exception as exc:
        return _response(make_error("invalid_payload", str(exc)))
    result = await _dispatch_http(context, request, "api_tokens.create", body)
    value = _dispatcher_result(result)
    return _response(value, 201 if value.get("ok") else None)


async def api_token_endpoint(context, request):
    result = await _dispatch_http(
        context, request, "api_tokens.revoke",
        {"token_id": request.path_params["token_id"]})
    return _response(_dispatcher_result(result))


async def sites_endpoint(context, request):
    result = await _dispatch_http(context, request, "sites.list", {})
    return _response(_dispatcher_result(result))


async def nodes_endpoint(context, request):
    result = await _dispatch_http(context, request, "nodes.list", {})
    return _response(_dispatcher_result(result))


async def node_endpoint(context, request):
    node_id = request.path_params["node_id"]
    if request.method == "GET":
        result = await _dispatch_http(
            context, request, "node.get", {"node_id": node_id})
        return _response(_dispatcher_result(result))
    try:
        body = await _json_body(context, request)
    except Exception as exc:
        return _response(make_error("invalid_payload", str(exc)))
    action = body.pop("action", None)
    prepared = dict(body)
    prepared["node_id"] = node_id
    if action == "drain":
        dispatch_action = "node.drain"
    elif action in ("resume", "maintenance", "quarantine"):
        states = {
            "resume": "ACTIVE",
            "maintenance": "MAINTENANCE",
            "quarantine": "QUARANTINED",
        }
        prepared["desired_state"] = states[action]
        dispatch_action = "node.state.set"
    elif action == "approve":
        dispatch_action = "node.admission.approve"
    else:
        return _response(make_error("invalid_payload", "node action is unsupported"))
    result = await _dispatch_http(context, request, dispatch_action, prepared)
    return _response(_dispatcher_result(result))


async def config_endpoint(context, request):
    revision = request.query_params.get("revision")
    scope = request.query_params.get("scope")
    if revision is not None and scope is not None:
        return _response(make_error(
            "invalid_payload", "revision and scope are mutually exclusive"))
    try:
        selected = int(revision) if revision is not None else None
    except ValueError:
        return _response(make_error("invalid_payload", "revision must be an integer"))
    if selected is not None:
        payload = {"revision": selected}
    elif scope is not None:
        payload = {"scope": scope}
    else:
        payload = {}
    result = await _dispatch_http(context, request, "config.get", payload)
    return _response(_dispatcher_result(result))

async def config_history_endpoint(context, request):
    try:
        limit = int(request.query_params.get("limit", "50"))
    except ValueError:
        return _response(make_error("invalid_payload", "limit must be an integer"))
    result = await _dispatch_http(
        context, request, "config.history", {"limit": limit})
    return _response(_dispatcher_result(result))


def _diff_values(before, after, path=""):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(set(before) | set(after)):
            child = key if not path else path + "." + key
            changes.extend(_diff_values(before.get(key), after.get(key), child))
        return changes
    return [{"path": path, "before": copy.deepcopy(before), "after": copy.deepcopy(after)}]


async def config_diff_endpoint(context, request):
    try:
        left = int(request.query_params["from_revision"])
        right = int(request.query_params["to_revision"])
    except Exception:
        return _response(make_error("invalid_payload", "from_revision and to_revision are required"))
    result = await _dispatch_http(
        context, request, "config.diff",
        {"from_revision": left, "to_revision": right})
    return _response(_dispatcher_result(result))


async def config_proposals_endpoint(context, request):
    try:
        body = await _json_body(context, request)
    except Exception as exc:
        return _response(make_error("invalid_payload", str(exc)))
    result = await _dispatch_http(context, request, "config.propose", body)
    return _response(_dispatcher_result(result))


async def config_rollback_endpoint(context, request):
    try:
        body = await _json_body(context, request)
    except Exception as exc:
        return _response(make_error("invalid_payload", str(exc)))
    result = await _dispatch_http(context, request, "config.rollback", body)
    return _response(_dispatcher_result(result))


async def actuator_requests_endpoint(context, request):
    try:
        body = await _json_body(context, request)
    except Exception as exc:
        return _response(make_error("invalid_payload", str(exc)))
    result = await _dispatch_http(context, request, "actuator.request", body)
    return _response(_dispatcher_result(result))


async def operation_endpoint(context, request):
    result = await _dispatch_http(
        context, request, "operation.get",
        {"operation_id": request.path_params["operation_id"]})
    return _response(_dispatcher_result(result))


async def audit_endpoint(context, request):
    try:
        limit = int(request.query_params.get("limit", "100"))
    except ValueError:
        return _response(make_error("invalid_payload", "limit must be an integer"))
    result = await _dispatch_http(
        context, request, "audit.list", {"limit": limit})
    return _response(_dispatcher_result(result))


async def terminal_endpoint(context, request):
    try:
        body = await _json_body(context, request, maximum_bytes=8192)
    except Exception as exc:
        return _response(make_error("invalid_payload", str(exc)))
    result = await _dispatch_http(context, request, "terminal.execute", body)
    return _response(_dispatcher_result(result))


async def runtime_status_endpoint(context, request):
    result = await _dispatch_http(context, request, "runtime.status", {})
    return _response(_dispatcher_result(result))


async def health_live(context, _request):
    return _response({"ok": True, "state": "live"})


async def health_ready(context, _request):
    snapshot = await _run_blocking(context["runtime_snapshot"])
    return _response({"ok": bool(snapshot.get("ready")), "runtime": snapshot},
                     200 if snapshot.get("ready") else 503)


def _metrics_lines(snapshot):
    """Build the non-sensitive Prometheus exposition from a runtime snapshot."""
    mqtt = snapshot.get("mqtt") or {}
    wss = snapshot.get("wss") or {}
    wss_statistics = wss.get("statistics") or {}
    stream = snapshot.get("stream") or {}
    lines = [
        "# TYPE automation_proxy_ready gauge",
        "automation_proxy_ready {}".format(1 if snapshot.get("ready") else 0),
        "# TYPE automation_proxy_mqtt_connected gauge",
        "automation_proxy_mqtt_connected {}".format(
            1 if mqtt.get("connected") else 0),
        "# TYPE automation_proxy_websocket_clients gauge",
        "automation_proxy_websocket_clients {}".format(
            int(stream.get("clients") or 0)),
        "# TYPE automation_proxy_websocket_queue_depth gauge",
        "automation_proxy_websocket_queue_depth {}".format(
            int(stream.get("queue_depth") or 0)),
        "# TYPE automation_proxy_wss_event_clients gauge",
        "automation_proxy_wss_event_clients {}".format(
            int(wss.get("event_clients") or 0)),
        "# TYPE automation_proxy_wss_control_clients gauge",
        "automation_proxy_wss_control_clients {}".format(
            int(wss.get("control_clients") or 0)),
        "# TYPE automation_proxy_wss_response_queue_depth gauge",
        "automation_proxy_wss_response_queue_depth {}".format(
            int(wss.get("response_queue_depth") or 0)),
        "# TYPE automation_proxy_event_queue_depth gauge",
        "automation_proxy_event_queue_depth {}".format(
            int(stream.get("queue_depth") or 0)),
        "# TYPE automation_proxy_event_coalesced_depth gauge",
        "automation_proxy_event_coalesced_depth {}".format(
            int(stream.get("coalesced") or 0)),
        "# TYPE automation_proxy_event_critical_disconnects_total counter",
        "automation_proxy_event_critical_disconnects_total {}".format(
            int(stream.get("critical_disconnects") or 0)),
        "# TYPE automation_proxy_event_telemetry_overflow_disconnects_total counter",
        "automation_proxy_event_telemetry_overflow_disconnects_total {}".format(
            int(stream.get("telemetry_overflow_disconnects") or 0)),
        "# TYPE automation_proxy_wss_commands_accepted_total counter",
        "automation_proxy_wss_commands_accepted_total {}".format(
            int(wss_statistics.get("commands_accepted") or 0)),
        "# TYPE automation_proxy_wss_commands_rejected_total counter",
        "automation_proxy_wss_commands_rejected_total {}".format(
            int(wss_statistics.get("commands_rejected") or 0)),
        "# TYPE automation_proxy_wss_handshakes_accepted_total counter",
        "automation_proxy_wss_handshakes_accepted_total {}".format(
            int(wss_statistics.get("connections_accepted") or 0)),
        "# TYPE automation_proxy_wss_connections_closed_total counter",
        "automation_proxy_wss_connections_closed_total {}".format(
            int(wss_statistics.get("connections_closed") or 0)),
        "# TYPE automation_proxy_wss_connections_closed_cleanly_total counter",
        "automation_proxy_wss_connections_closed_cleanly_total {}".format(
            int(wss_statistics.get("connections_closed_cleanly") or 0)),
        "# TYPE automation_proxy_wss_tasks gauge",
        "automation_proxy_wss_tasks {}".format(int(wss.get("task_count") or 0)),
        "# TYPE automation_proxy_wss_replay_sessions_total counter",
        "automation_proxy_wss_replay_sessions_total {}".format(
            int(wss_statistics.get("replay_sessions") or 0)),
        "# TYPE automation_proxy_wss_replay_records_total counter",
        "automation_proxy_wss_replay_records_total {}".format(
            int(wss_statistics.get("replay_records") or 0)),
        "# TYPE automation_proxy_wss_replay_lag_events_sum counter",
        "automation_proxy_wss_replay_lag_events_sum {}".format(
            int(wss_statistics.get("replay_lag_events_sum") or 0)),
        "# TYPE automation_proxy_wss_replay_lag_events_max gauge",
        "automation_proxy_wss_replay_lag_events_max {}".format(
            int(wss_statistics.get("replay_lag_events_max") or 0)),
        "# TYPE automation_proxy_wss_event_fanout_latency_seconds_sum counter",
        "automation_proxy_wss_event_fanout_latency_seconds_sum {:.9f}".format(
            float(wss_statistics.get("event_fanout_latency_seconds_sum") or 0.0)),
        "# TYPE automation_proxy_wss_event_fanout_latency_seconds_count counter",
        "automation_proxy_wss_event_fanout_latency_seconds_count {}".format(
            int(wss_statistics.get("event_fanout_deliveries") or 0)),
        "# TYPE automation_proxy_wss_handshake_rejections_total counter",
    ]
    handshake_rejections = wss_statistics.get("handshake_rejections") or {}
    for reason in sorted(handshake_rejections):
        lines.append(
            'automation_proxy_wss_handshake_rejections_total{{reason="{}"}} {}'.format(
                _prometheus_label(reason), int(handshake_rejections[reason] or 0)))
    lines.append("# TYPE automation_proxy_wss_command_results_total counter")
    command_results = wss_statistics.get("command_results") or {}
    durations = wss_statistics.get("command_latency_seconds") or {}
    for key in sorted(command_results):
        action, code = _command_metric_labels(key)
        labels = 'action="{}",code="{}"'.format(
            _prometheus_label(action), _prometheus_label(code))
        lines.append(
            "automation_proxy_wss_command_results_total{{{}}} {}".format(
                labels, int(command_results[key] or 0)))
    lines.append("# TYPE automation_proxy_wss_command_latency_seconds_sum counter")
    lines.append("# TYPE automation_proxy_wss_command_latency_seconds_count counter")
    for key in sorted(set(command_results) | set(durations)):
        action, code = _command_metric_labels(key)
        labels = 'action="{}",code="{}"'.format(
            _prometheus_label(action), _prometheus_label(code))
        lines.append(
            "automation_proxy_wss_command_latency_seconds_sum{{{}}} {:.9f}".format(
                labels, float(durations.get(key) or 0.0)))
        lines.append(
            "automation_proxy_wss_command_latency_seconds_count{{{}}} {}".format(
                labels, int(command_results.get(key) or 0)))
    return tuple(lines)


def _prometheus_label(value):
    return str(value).replace("\\", "\\\\").replace("\n", "\\n").replace('"', '\\"')


def _command_metric_labels(key):
    parts = str(key).split("|", 1)
    return (parts[0], parts[1]) if len(parts) == 2 else ("other", "other")


async def metrics_endpoint(context, _request):
    """Expose non-sensitive Prometheus metrics over the protected HTTPS listener."""
    from starlette.responses import PlainTextResponse

    snapshot = await _run_blocking(context["runtime_snapshot"])
    return PlainTextResponse(
        "\n".join(_metrics_lines(snapshot)) + "\n",
        media_type="text/plain; version=0.0.4",
    )


async def _security_send(send, context, scope, message):
    if message.get("type") == "http.response.start":
        headers = list(message.get("headers") or [])
        headers.extend([
            (b"strict-transport-security", b"max-age=31536000; includeSubDomains"),
            (b"content-security-policy", b"default-src 'none'; frame-ancestors 'none'"),
            (b"x-content-type-options", b"nosniff"),
            (b"referrer-policy", b"no-referrer"),
            (b"x-frame-options", b"DENY"),
            (b"cache-control", b"no-store"),
            (b"permissions-policy", b"camera=(), microphone=(), geolocation=()"),
            (b"x-request-id", str(scope.get("state", {}).get("request_id") or "").encode("ascii")),
        ])
        origin = _scope_origin(scope)
        if origin and origin in context["origin_allowlist"]:
            headers.extend([
                (b"access-control-allow-origin", origin.encode("ascii")),
                (b"access-control-allow-credentials", b"true"),
                (b"vary", b"Origin"),
            ])
        message = dict(message)
        message["headers"] = headers
    await send(message)


def _scope_origin(scope):
    for name, value in scope.get("headers") or []:
        if name.lower() == b"origin":
            return value.decode("ascii", errors="ignore")
    return ""


async def _send_asgi_response(send, status, body, headers=()):
    payload = json.dumps(body, separators=(",", ":")).encode("utf-8")
    base = [(b"content-type", b"application/json"), (b"content-length", str(len(payload)).encode("ascii"))]
    await send({"type": "http.response.start", "status": status, "headers": base + list(headers)})
    await send({"type": "http.response.body", "body": payload})


async def gateway_asgi(context, application, scope, receive, send):
    """Enforce Origin/CORS and security headers around the Starlette router."""
    if scope["type"] == "http":
        supplied_request_id = ""
        for name, value in scope.get("headers") or []:
            if name.lower() == b"x-request-id":
                supplied_request_id = value.decode("ascii", errors="ignore")
                break
        request_id = supplied_request_id if REQUEST_ID_PATTERN.fullmatch(
            supplied_request_id) else str(uuid.uuid4())
        scope = dict(scope)
        state = dict(scope.get("state") or {})
        state["request_id"] = request_id
        scope["state"] = state
        wrapped_send = partial(_security_send, send, context, scope)
        origin = _scope_origin(scope)
        if origin and origin not in context["origin_allowlist"]:
            await _send_asgi_response(
                wrapped_send, 403, {"ok": False, "code": "origin_denied"})
            return
        if scope.get("method") == "OPTIONS":
            headers = [
                (b"access-control-allow-methods", b"GET,POST,PATCH,DELETE,OPTIONS"),
                (b"access-control-allow-headers", b"Authorization,Content-Type,Idempotency-Key,X-Command-TTL-S,X-CSRF-Token,X-Request-ID"),
                (b"access-control-max-age", b"600"),
            ]
            await _send_asgi_response(wrapped_send, 200, {"ok": True}, headers=headers)
            return
    else:
        wrapped_send = partial(_security_send, send, context, scope)
    await application(scope, receive, wrapped_send)


def create_https_application(context):
    """Create the HTTP-only HTTPS ASGI route table without decorators."""
    from starlette.applications import Starlette
    from starlette.routing import Route

    routes = [
        Route("/api/v1/auth/login", partial(auth_login, context), methods=["POST"]),
        Route("/api/v1/auth/logout", partial(auth_logout, context), methods=["POST"]),
        Route("/api/v1/auth/session", partial(auth_session, context), methods=["GET", "POST"]),
        Route("/api/v1/users", partial(users_endpoint, context), methods=["GET", "POST"]),
        Route("/api/v1/roles", partial(roles_endpoint, context), methods=["GET"]),
        Route("/api/v1/permissions", partial(permissions_endpoint, context), methods=["GET"]),
        Route("/api/v1/api-tokens", partial(api_tokens_endpoint, context), methods=["GET", "POST"]),
        Route("/api/v1/api-tokens/{token_id}", partial(api_token_endpoint, context), methods=["DELETE"]),
        Route("/api/v1/sites", partial(sites_endpoint, context), methods=["GET"]),
        Route("/api/v1/runtime/status", partial(runtime_status_endpoint, context), methods=["GET"]),
        Route("/api/v1/nodes", partial(nodes_endpoint, context), methods=["GET"]),
        Route("/api/v1/nodes/{node_id}", partial(node_endpoint, context), methods=["GET", "PATCH"]),
        Route("/api/v1/config", partial(config_endpoint, context), methods=["GET"]),
        Route("/api/v1/config/history", partial(config_history_endpoint, context), methods=["GET"]),
        Route("/api/v1/config/diff", partial(config_diff_endpoint, context), methods=["GET"]),
        Route("/api/v1/config/proposals", partial(config_proposals_endpoint, context), methods=["POST"]),
        Route("/api/v1/config/rollback", partial(config_rollback_endpoint, context), methods=["POST"]),
        Route("/api/v1/actuators/requests", partial(actuator_requests_endpoint, context), methods=["POST"]),
        Route("/api/v1/operations/{operation_id}", partial(operation_endpoint, context), methods=["GET"]),
        Route("/api/v1/audit", partial(audit_endpoint, context), methods=["GET"]),
        Route("/api/v1/terminal/commands", partial(terminal_endpoint, context), methods=["POST"]),
        Route("/api/v1/health/live", partial(health_live, context), methods=["GET"]),
        Route("/api/v1/health/ready", partial(health_ready, context), methods=["GET"]),
        Route("/metrics", partial(metrics_endpoint, context), methods=["GET"]),
    ]
    application = Starlette(debug=False, routes=routes)
    return partial(gateway_asgi, context, application)

# -*- coding: utf-8 -*-

"""Authenticated ASGI Streamable HTTP transport with strict Origin checks."""

import base64
import json

from mcp_controller_server.auth import authenticate_bearer
from mcp_controller_server.canonical import canonical_bytes
from mcp_controller_server.config import SUPPORTED_PROTOCOL_VERSIONS, load_config
from mcp_controller_server.mcp_protocol import (
    MODERN_PROTOCOL_VERSION,
    handle_jsonrpc,
    is_modern_request,
    jsonrpc_error,
)
from mcp_controller_server.runtime import (
    create_runtime,
    runtime_ready,
    snapshot_runtime,
    start_runtime,
    stop_runtime,
)


APPLICATION_STATE = {"runtime": None}


def set_application_runtime(runtime):
    APPLICATION_STATE["runtime"] = runtime
    return runtime


def _headers(scope):
    result = {}
    protected = {"authorization", "content-type", "origin"}
    for name, value in scope.get("headers") or ():
        decoded_name = name.decode("latin-1").lower()
        if decoded_name in result:
            if decoded_name in protected or decoded_name.startswith("mcp-"):
                raise ValueError("duplicate protected HTTP headers are forbidden")
            result[decoded_name] += "," + value.decode("latin-1")
        else:
            result[decoded_name] = value.decode("latin-1")
    return result


def _origin_allowed(config, origin):
    if origin is None:
        return True
    return str(origin) in set(config["http"]["allowed_origins"])


def _decode_mcp_header(value):
    text = str(value or "")
    if text.startswith("=?base64?") and text.endswith("?="):
        encoded = text[len("=?base64?"):-2]
        try:
            return base64.b64decode(encoded, validate=True).decode("utf-8")
        except (ValueError, UnicodeDecodeError):
            raise ValueError("MCP header Base64 value is invalid")
    if not text or text != text.strip() or any(
            ord(character) > 126
            or ord(character) < 32 and ord(character) != 9
            for character in text):
        raise ValueError("MCP header value is invalid")
    return text


def validate_modern_headers(headers, message):
    supplied_version = headers.get("mcp-protocol-version")
    if supplied_version and supplied_version not in SUPPORTED_PROTOCOL_VERSIONS:
        return jsonrpc_error(
            message.get("id"), -32022, "Unsupported protocol version",
            {"supported": list(SUPPORTED_PROTOCOL_VERSIONS),
             "requested": supplied_version},
        )
    if not is_modern_request(message):
        if supplied_version == MODERN_PROTOCOL_VERSION:
            return jsonrpc_error(
                message.get("id"), -32020,
                "Modern HTTP header requires modern request metadata",
            )
        return None
    params = message.get("params") if isinstance(message.get("params"), dict) else {}
    metadata = params.get("_meta") if isinstance(params.get("_meta"), dict) else {}
    version = headers.get("mcp-protocol-version")
    method = headers.get("mcp-method")
    expected_version = metadata.get("io.modelcontextprotocol/protocolVersion")
    if version != expected_version or method != message.get("method"):
        return jsonrpc_error(
            message.get("id"), -32020,
            "Header mismatch: protocol version or method differs from the body",
        )
    name_methods = {"tools/call": "name", "prompts/get": "name", "resources/read": "uri"}
    field = name_methods.get(message.get("method"))
    if field:
        try:
            supplied = _decode_mcp_header(headers.get("mcp-name"))
        except ValueError as exc:
            return jsonrpc_error(message.get("id"), -32020, str(exc))
        if supplied != params.get(field):
            return jsonrpc_error(
                message.get("id"), -32020,
                "Header mismatch: Mcp-Name differs from the body",
            )
    return None


async def _read_body(receive, maximum):
    chunks = []
    size = 0
    while True:
        event = await receive()
        if event.get("type") == "http.disconnect":
            raise ConnectionError("HTTP client disconnected")
        if event.get("type") != "http.request":
            continue
        chunk = event.get("body") or b""
        size += len(chunk)
        if size > maximum:
            raise ValueError("HTTP request body exceeds its limit")
        chunks.append(chunk)
        if not event.get("more_body", False):
            return b"".join(chunks)


async def _send_response(send, status, body=b"", content_type="application/json", headers=None):
    response_headers = [
        (b"content-type", content_type.encode("ascii")),
        (b"content-length", str(len(body)).encode("ascii")),
        (b"cache-control", b"no-store"),
        (b"x-content-type-options", b"nosniff"),
    ]
    for name, value in headers or ():
        response_headers.append((name.encode("ascii"), value.encode("latin-1")))
    await send({"type": "http.response.start", "status": int(status), "headers": response_headers})
    await send({"type": "http.response.body", "body": bytes(body), "more_body": False})


async def _json_response(send, status, value, headers=None):
    await _send_response(send, status, canonical_bytes(value), headers=headers)


async def _lifespan(receive, send):
    while True:
        event = await receive()
        if event.get("type") == "lifespan.startup":
            try:
                if APPLICATION_STATE["runtime"] is None:
                    config = load_config(check_files=True)
                    set_application_runtime(create_runtime(config))
                start_runtime(APPLICATION_STATE["runtime"])
                await send({"type": "lifespan.startup.complete"})
            except Exception as exc:
                await send({"type": "lifespan.startup.failed", "message": str(exc)})
        elif event.get("type") == "lifespan.shutdown":
            runtime = APPLICATION_STATE.get("runtime")
            if runtime is not None:
                stop_runtime(runtime)
            await send({"type": "lifespan.shutdown.complete"})
            return


async def _health(scope, send, runtime):
    path = scope.get("path")
    live = runtime is not None and runtime.get("state") not in ("failed", "stopped")
    ready = bool(runtime and runtime_ready(runtime))
    accepted = live if path == "/health/live" else ready
    body = {
        "ok": accepted,
        "status": "live" if path == "/health/live" and accepted else (
            "ready" if accepted else "unavailable"),
    }
    if runtime is not None:
        body["component"] = "mcp_controller_server"
        body["version"] = runtime["version"]
    await _json_response(send, 200 if accepted else 503, body)


async def _http(scope, receive, send):
    runtime = APPLICATION_STATE.get("runtime")
    path = scope.get("path")
    method = str(scope.get("method") or "").upper()
    if path in ("/health/live", "/health/ready") and method == "GET":
        await _health(scope, send, runtime)
        return
    if runtime is None:
        await _json_response(send, 503, {"ok": False, "message": "runtime unavailable"})
        return
    config = runtime["config"]
    try:
        headers = _headers(scope)
    except ValueError:
        await _json_response(send, 400, jsonrpc_error(None, -32600, "Invalid HTTP headers"))
        return
    if path != config["http"]["path"]:
        await _json_response(send, 404, jsonrpc_error(None, -32601, "MCP endpoint not found"))
        return
    origin = headers.get("origin")
    if not _origin_allowed(config, origin):
        await _json_response(send, 403, jsonrpc_error(None, -32000, "Origin is not allowed"))
        return
    if method == "OPTIONS":
        response_headers = [
            ("allow", "POST, OPTIONS"),
            ("access-control-allow-methods", "POST, OPTIONS"),
        ]
        if origin:
            response_headers.append(("access-control-allow-origin", origin))
            response_headers.append(("vary", "Origin"))
            response_headers.append((
                "access-control-allow-headers",
                "Authorization, Content-Type, MCP-Protocol-Version, Mcp-Method, Mcp-Name",
            ))
        await _send_response(send, 204, b"", headers=response_headers)
        return
    if method != "POST":
        await _json_response(
            send, 405, jsonrpc_error(None, -32600, "Only POST is supported"),
            headers=[("allow", "POST, OPTIONS")],
        )
        return
    authenticated = authenticate_bearer(runtime["auth"], headers.get("authorization"))
    if not authenticated.get("ok"):
        status = 429 if authenticated.get("code") == "rate_limited" else 401
        await _json_response(
            send, status,
            jsonrpc_error(None, -32001, authenticated.get("message")),
            headers=[("www-authenticate", "Bearer")],
        )
        return
    content_type = headers.get("content-type", "").split(";", 1)[0].strip().lower()
    accept = headers.get("accept", "*/*").lower()
    if content_type != "application/json" or not any(
            value in accept for value in ("application/json", "text/event-stream", "*/*")):
        await _json_response(send, 415, jsonrpc_error(None, -32700, "JSON content negotiation failed"))
        return
    try:
        body = await _read_body(receive, config["http"]["max_body_bytes"])
        message = json.loads(body.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError, ConnectionError):
        await _json_response(send, 400, jsonrpc_error(None, -32700, "Parse error"))
        return
    if not isinstance(message, dict):
        await _json_response(send, 400, jsonrpc_error(None, -32600, "Batch requests are unsupported"))
        return
    header_error = validate_modern_headers(headers, message)
    if header_error:
        await _json_response(send, 400, header_error)
        return
    client_context = {
        "client_id": authenticated["client_id"],
        "principal": authenticated["principal"],
        "protocol_version": headers.get("mcp-protocol-version"),
    }
    response = handle_jsonrpc(runtime, message, client_context)
    if response is None:
        await _send_response(send, 202, b"", content_type="application/json")
        return
    status = 200
    error_code = (response.get("error") or {}).get("code")
    if is_modern_request(message) and error_code in (-32020, -32022, -32602):
        status = 400
    elif is_modern_request(message) and error_code == -32601:
        status = 404
    response_headers = []
    if origin:
        response_headers.append(("access-control-allow-origin", origin))
        response_headers.append(("vary", "Origin"))
    await _json_response(send, status, response, headers=response_headers)


async def application(scope, receive, send):
    if scope.get("type") == "lifespan":
        await _lifespan(receive, send)
        return
    if scope.get("type") == "http":
        await _http(scope, receive, send)
        return
    raise RuntimeError("unsupported ASGI scope")

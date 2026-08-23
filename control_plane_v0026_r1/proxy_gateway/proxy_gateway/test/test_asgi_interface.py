# -*- coding: utf-8 -*-

"""Regression tests for the strict HTTPS/WSS transport separation."""

import ast
import asyncio
import copy
import ipaddress
import json
import os

from functools import partial

from starlette.requests import Request

from src.interfaces.https_interface import (
    _json_safe,
    config_endpoint,
    gateway_asgi,
)


ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
GATEWAY_MAIN = os.path.join(ROOT, "src", "gateway_main.py")
HTTPS_INTERFACE = os.path.join(ROOT, "src", "interfaces", "https_interface.py")
WSS_INTERFACE = os.path.join(
    ROOT, "src", "interfaces", "async_websockets_server.py")


def _tree(path):
    with open(path, "r", encoding="utf-8") as source_file:
        return ast.parse(source_file.read(), filename=path)


def _constant_keyword(call, name):
    values = [
        keyword.value.value
        for keyword in call.keywords
        if keyword.arg == name and isinstance(keyword.value, ast.Constant)
    ]
    return values


def _qualified_name(node):
    parts = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
    return ".".join(reversed(parts))


def _imports(tree):
    imported = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(item.name for item in node.names)
        elif isinstance(node, ast.ImportFrom):
            imported.append(str(node.module or ""))
    return imported


def _string_constants(tree):
    return {
        node.value
        for node in ast.walk(tree)
        if isinstance(node, ast.Constant) and isinstance(node.value, str)
    }


def _test_uvicorn_is_http_only_asgi3():
    tree = _tree(GATEWAY_MAIN)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and _qualified_name(node.func) in ("uvicorn.Config", "uvicorn.run")
    ]
    assert len(calls) == 1
    call = calls[0]
    assert _constant_keyword(call, "interface") == ["asgi3"]
    assert _constant_keyword(call, "ws") == ["none"]
    assert "websockets-sansio" not in _string_constants(tree)
    assert not any(keyword.arg and keyword.arg.startswith("ws_")
                   for keyword in call.keywords)


def _test_starlette_has_no_websocket_surface():
    tree = _tree(HTTPS_INTERFACE)
    names = {
        node.id for node in ast.walk(tree) if isinstance(node, ast.Name)
    }
    imports = _imports(tree)
    strings = _string_constants(tree)
    assert "WebSocketRoute" not in names
    assert "interfaces.websocket_interface" not in imports
    assert not any(value.startswith("/ws/") for value in strings)
    assert not any("websocket" in name.lower() for name in imports)


def _test_direct_wss_adapter_owns_websockets_and_tls():
    tree = _tree(WSS_INTERFACE)
    calls = {
        _qualified_name(node.func)
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
    }
    imports = _imports(tree)
    assert "websockets" in imports
    assert "websockets.serve" in calls
    assert "build_server_ssl_context" in calls
    assert not any(name == "uvicorn" or name.startswith("uvicorn.")
                   for name in imports)
    assert not any(name == "starlette" or name.startswith("starlette.")
                   for name in imports)


def _test_https_json_boundary_covers_postgresql_inet():
    values = {
        "address": ipaddress.ip_address("192.168.0.82"),
        "interface": ipaddress.ip_interface("192.168.0.82/24"),
        "network": ipaddress.ip_network("2001:db8::/64"),
    }
    assert _json_safe(values) == {
        "address": "192.168.0.82",
        "interface": "192.168.0.82/24",
        "network": "2001:db8::/64",
    }


async def _exercise_asgi3_wrapper():
    sent = []

    async def downstream(scope, _receive, send):
        assert scope["type"] == "http"
        assert scope["state"]["request_id"] == "request-42"
        await send({"type": "http.response.start", "status": 200, "headers": []})
        await send({"type": "http.response.body", "body": b"ok"})

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    context = {"origin_allowlist": {"https://control.example:8444"}}
    application = partial(gateway_asgi, context, downstream)
    await application({
        "type": "http",
        "method": "GET",
        "headers": [(b"x-request-id", b"request-42")],
        "state": {},
    }, receive, send)
    assert sent[0]["type"] == "http.response.start"
    assert sent[0]["status"] == 200
    assert (b"x-content-type-options", b"nosniff") in sent[0]["headers"]
    assert sent[1] == {"type": "http.response.body", "body": b"ok"}


async def _exercise_origin_rejection():
    sent = []
    downstream_calls = []

    async def downstream(_scope, _receive, _send):
        downstream_calls.append(True)

    async def receive():
        return {"type": "http.request", "body": b"", "more_body": False}

    async def send(message):
        sent.append(message)

    context = {"origin_allowlist": {"https://control.example:8444"}}
    application = partial(gateway_asgi, context, downstream)
    await application({
        "type": "http",
        "method": "GET",
        "headers": [(b"origin", b"https://cross-site.invalid")],
        "state": {},
    }, receive, send)
    assert not downstream_calls
    assert sent[0]["status"] == 403


def _test_asgi3_wrapper_and_http_origin_policy():
    asyncio.run(_exercise_asgi3_wrapper())
    asyncio.run(_exercise_origin_rejection())


async def _exercise_scoped_config_endpoint():
    captured = []

    def authenticate_session(_request):
        return {
            "ok": True,
            "principal": {
                "subject": "user-01",
                "permissions": ["config.read"],
                "site_scopes": ["site-main"],
            },
        }

    async def dispatch(envelope):
        captured.append(copy.deepcopy(envelope))
        return {
            "ok": True,
            "reply": {
                "ok": True,
                "mirror": {
                    "scope": envelope["payload"].get("scope"),
                },
            },
        }

    context = {
        "site_id": "site-main",
        "instance_id": "proxy-01",
        "session_cookie": "automation_session",
        "auth": {"authenticate_session": authenticate_session},
        "dispatcher": {"dispatch": dispatch},
    }
    request = Request({
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": "/api/v1/config",
        "raw_path": b"/api/v1/config",
        "query_string": b"scope=node%3Afn-01",
        "headers": [(b"cookie", b"automation_session=session-01")],
        "client": ("192.0.2.10", 41000),
        "server": ("control.example", 8444),
        "state": {"request_id": "request-43"},
    })
    response = await config_endpoint(context, request)
    body = json.loads(response.body.decode("utf-8"))
    assert response.status_code == 200
    assert body["mirror"]["scope"] == "node:fn-01"
    assert len(captured) == 1
    assert captured[0]["action"] == "config.get"
    assert captured[0]["payload"] == {"scope": "node:fn-01"}

    ambiguous = Request({
        "type": "http",
        "method": "GET",
        "scheme": "https",
        "path": "/api/v1/config",
        "raw_path": b"/api/v1/config",
        "query_string": b"scope=node%3Afn-01&revision=7",
        "headers": [],
        "client": ("192.0.2.10", 41000),
        "server": ("control.example", 8444),
        "state": {"request_id": "request-44"},
    })
    rejected = await config_endpoint(context, ambiguous)
    rejected_body = json.loads(rejected.body.decode("utf-8"))
    assert rejected.status_code == 400
    assert rejected_body["code"] == "invalid_payload"
    assert len(captured) == 1


def _test_scoped_config_endpoint():
    asyncio.run(_exercise_scoped_config_endpoint())


def main():
    tests = (
        _test_uvicorn_is_http_only_asgi3,
        _test_starlette_has_no_websocket_surface,
        _test_direct_wss_adapter_owns_websockets_and_tls,
        _test_https_json_boundary_covers_postgresql_inet,
        _test_asgi3_wrapper_and_http_origin_policy,
        _test_scoped_config_endpoint,
    )
    for test in tests:
        test()
    print("PASS test_asgi_interface ({} checks)".format(len(tests)))


if __name__ == "__main__":
    main()

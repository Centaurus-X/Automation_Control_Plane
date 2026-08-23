# -*- coding: utf-8 -*-

"""ASGI authentication, Origin and modern header validation tests."""

import asyncio
import json
import tempfile

from mcp_controller_server.auth import load_auth_state
from mcp_controller_server.canonical import canonical_bytes
from mcp_controller_server.http_transport import application, set_application_runtime
from mcp_controller_server.runtime import create_runtime, start_runtime, stop_runtime
from tests.support import make_test_config, make_transport_factory, write_auth_file


async def _asgi_request(path, method, headers, body):
    events = [{"type": "http.request", "body": body, "more_body": False}]
    responses = []

    async def receive():
        return events.pop(0) if events else {"type": "http.disconnect"}

    async def send(message):
        responses.append(message)

    scope = {
        "type": "http", "path": path, "method": method,
        "headers": [(name.encode("ascii"), value.encode("latin-1"))
                    for name, value in headers.items()],
    }
    await application(scope, receive, send)
    status = responses[0]["status"]
    response_body = responses[1].get("body") or b""
    return status, response_body


def _modern_discover():
    return {
        "jsonrpc": "2.0", "id": 7, "method": "server/discover",
        "params": {"_meta": {
            "io.modelcontextprotocol/protocolVersion": "2026-07-28",
            "io.modelcontextprotocol/clientCapabilities": {},
            "io.modelcontextprotocol/clientInfo": {
                "name": "http-test", "version": "1.0",
            },
        }},
    }


def test_http_auth_origin_and_modern_headers():
    with tempfile.TemporaryDirectory() as directory:
        config = make_test_config(directory)
        token = write_auth_file(config["http"]["auth_file"])
        auth_state = load_auth_state(config["http"]["auth_file"], 20)
        runtime = create_runtime(
            config,
            transport_factory=make_transport_factory({}, config),
            auth_state=auth_state,
        )
        set_application_runtime(runtime)
        start_runtime(runtime)
        try:
            message = _modern_discover()
            body = canonical_bytes(message)
            base_headers = {
                "content-type": "application/json",
                "accept": "application/json",
                "authorization": "Bearer " + token,
                "origin": "https://agent.example",
                "mcp-protocol-version": "2026-07-28",
                "mcp-method": "server/discover",
            }
            status, response = asyncio.run(
                _asgi_request("/mcp", "POST", base_headers, body))
            assert status == 200
            assert json.loads(response)["result"]["resultType"] == "complete"
            denied_headers = dict(base_headers)
            denied_headers["origin"] = "https://evil.example"
            denied, _body = asyncio.run(
                _asgi_request("/mcp", "POST", denied_headers, body))
            assert denied == 403
            unauthenticated_headers = dict(base_headers)
            unauthenticated_headers.pop("authorization")
            unauthenticated, _body = asyncio.run(
                _asgi_request("/mcp", "POST", unauthenticated_headers, body))
            assert unauthenticated == 401
            mismatch_headers = dict(base_headers)
            mismatch_headers["mcp-method"] = "tools/list"
            mismatch, _body = asyncio.run(
                _asgi_request("/mcp", "POST", mismatch_headers, body))
            assert mismatch == 400
        finally:
            stop_runtime(runtime)
            set_application_runtime(None)

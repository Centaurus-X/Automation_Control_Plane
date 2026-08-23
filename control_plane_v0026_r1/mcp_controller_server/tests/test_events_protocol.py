# -*- coding: utf-8 -*-

"""Event cache ordering and dual-era MCP protocol tests."""

import tempfile
import uuid

from mcp_controller_server.event_cache import (
    create_event_cache,
    latest_sensors,
    record_event,
)
from mcp_controller_server.mcp_protocol import handle_jsonrpc
from mcp_controller_server.runtime import create_runtime
from tests.support import full_principal, make_test_config, make_transport_factory


def _sensor_envelope(controller_uuid, sensor_id, value, message_id):
    return {
        "message_id": message_id,
        "correlation_id": message_id,
        "event_type": "sensor.value",
        "issued_at": "2026-08-12T00:00:00Z",
        "principal": None,
        "payload": {
            "controller_uuid": controller_uuid,
            "sensor_id": sensor_id,
            "value": value,
            "quality": "good",
            "source_node_id": "fn-01",
        },
    }


def test_sensor_cache_tracks_latest_update_order():
    cache = create_event_cache(4, 2)
    first_controller = str(uuid.uuid4())
    second_controller = str(uuid.uuid4())
    record_event(cache, _sensor_envelope(first_controller, 1, 10, str(uuid.uuid4())))
    record_event(cache, _sensor_envelope(second_controller, 2, 20, str(uuid.uuid4())))
    record_event(cache, _sensor_envelope(first_controller, 1, 30, str(uuid.uuid4())))
    latest = latest_sensors(cache, limit=2)
    assert latest[0]["payload"]["value"] == 30
    assert latest[1]["payload"]["value"] == 20


def _modern_message(request_id, method, params=None):
    values = dict(params or {})
    values["_meta"] = {
        "io.modelcontextprotocol/protocolVersion": "2026-07-28",
        "io.modelcontextprotocol/clientCapabilities": {},
        "io.modelcontextprotocol/clientInfo": {
            "name": "test-agent", "version": "1.0",
        },
    }
    return {
        "jsonrpc": "2.0", "id": request_id,
        "method": method, "params": values,
    }


def test_modern_and_legacy_mcp_dispatch():
    with tempfile.TemporaryDirectory() as directory:
        config = make_test_config(directory)
        runtime = create_runtime(
            config,
            transport_factory=make_transport_factory({}, config),
            auth_state={"test": True},
        )
        context = {"client_id": "test-agent", "principal": full_principal()}
        discover = handle_jsonrpc(
            runtime, _modern_message(1, "server/discover"), context)
        assert discover["result"]["resultType"] == "complete"
        assert "2026-07-28" in discover["result"]["supportedVersions"]
        listed = handle_jsonrpc(runtime, _modern_message(2, "tools/list"), context)
        names = {tool["name"] for tool in listed["result"]["tools"]}
        assert "automation_change_prepare" in names
        assert "terminal.execute" not in names
        legacy_context = {"client_id": "legacy", "principal": full_principal()}
        initialized = handle_jsonrpc(runtime, {
            "jsonrpc": "2.0", "id": "init", "method": "initialize",
            "params": {
                "protocolVersion": "2025-11-25",
                "capabilities": {},
                "clientInfo": {"name": "legacy", "version": "1"},
            },
        }, legacy_context)
        assert initialized["result"]["protocolVersion"] == "2025-11-25"
        assert handle_jsonrpc(runtime, {
            "jsonrpc": "2.0", "method": "notifications/initialized",
        }, legacy_context) is None
        assert legacy_context["initialized"] is True

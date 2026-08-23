# -*- coding: utf-8 -*-

"""stdio framing, resources and safe local-principal tests."""

import io
import json
import tempfile

from mcp_controller_server.resources import read_resource
from mcp_controller_server.runtime import create_runtime
from mcp_controller_server.stdio_transport import run_stdio
from tests.support import (
    full_principal,
    make_test_config,
    make_transport_factory,
    write_auth_file,
)


def test_capability_resource_declares_hard_boundaries():
    with tempfile.TemporaryDirectory() as directory:
        config = make_test_config(directory)
        runtime = create_runtime(
            config,
            transport_factory=make_transport_factory({}, config),
            auth_state={"test": True},
        )
        result = read_resource(
            runtime, "automation://server/capabilities",
            {"client_id": "test", "principal": full_principal()},
        )
        assert result["ok"]
        content = json.loads(result["contents"][0]["text"])
        assert content["system_boundary"] == "mqtt_only"
        assert content["direct_database_access"] is False
        assert content["direct_worker_bypass"] is False
        assert content["terminal_execute"] is False


def test_stdio_legacy_initialize_and_read_tool_roundtrip():
    with tempfile.TemporaryDirectory() as directory:
        config = make_test_config(directory)
        write_auth_file(config["http"]["auth_file"])
        messages = [
            {
                "jsonrpc": "2.0", "id": 1, "method": "initialize",
                "params": {
                    "protocolVersion": "2025-11-25",
                    "capabilities": {},
                    "clientInfo": {"name": "stdio-test", "version": "1"},
                },
            },
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {
                "jsonrpc": "2.0", "id": 2, "method": "tools/call",
                "params": {"name": "automation_connection_status", "arguments": {}},
            },
        ]
        source = io.BytesIO(b"".join(
            (json.dumps(message, separators=(",", ":")) + "\n").encode("utf-8")
            for message in messages
        ))
        target = io.BytesIO()
        run_stdio(
            config, input_stream=source, output_stream=target,
            transport_factory=make_transport_factory({}, config),
        )
        responses = [json.loads(line) for line in target.getvalue().splitlines()]
        assert len(responses) == 2
        assert responses[0]["result"]["protocolVersion"] == "2025-11-25"
        assert responses[1]["result"]["structuredContent"]["ok"]

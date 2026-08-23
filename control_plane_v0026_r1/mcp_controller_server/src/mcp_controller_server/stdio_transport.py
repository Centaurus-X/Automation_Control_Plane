# -*- coding: utf-8 -*-

"""Newline-delimited MCP stdio transport for local agents and LLM hosts."""

import json
import sys

from mcp_controller_server.canonical import canonical_text
from mcp_controller_server.mcp_protocol import handle_jsonrpc, jsonrpc_error
from mcp_controller_server.runtime import create_runtime, start_runtime, stop_runtime


def _write_message(stream, message):
    stream.write((canonical_text(message) + "\n").encode("utf-8"))
    stream.flush()


def run_stdio(config, input_stream=None, output_stream=None, transport_factory=None):
    source = input_stream or sys.stdin.buffer
    target = output_stream or sys.stdout.buffer
    runtime = create_runtime(config, transport_factory=transport_factory)
    client_context = {
        "client_id": "stdio",
        "principal": config["stdio"]["principal"],
        "protocol_version": None,
        "initialized": False,
    }
    maximum = config["http"]["max_body_bytes"]
    start_runtime(runtime)
    try:
        while True:
            line = source.readline(maximum + 2)
            if not line:
                break
            if len(line) > maximum or not line.endswith(b"\n"):
                while line and not line.endswith(b"\n"):
                    line = source.readline(maximum + 2)
                _write_message(target, jsonrpc_error(None, -32700, "Message exceeds limit"))
                continue
            if not line.strip():
                continue
            try:
                message = json.loads(line.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError):
                _write_message(target, jsonrpc_error(None, -32700, "Parse error"))
                continue
            response = handle_jsonrpc(runtime, message, client_context)
            if response is not None:
                _write_message(target, response)
    finally:
        stop_runtime(runtime)
    return 0

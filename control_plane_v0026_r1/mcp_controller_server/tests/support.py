# -*- coding: utf-8 -*-

"""Shared functional test helpers."""

import copy
import json
import os
import threading

from functools import partial

from mcp_controller_server.automation_contract import (
    make_envelope,
    proxy_ack_topic,
)
from mcp_controller_server.canonical import canonical_bytes, decode_json_bytes
from mcp_controller_server.config import ALL_TOOL_NAMES


def make_test_config(directory):
    return {
        "schema_version": 1,
        "server": {
            "name": "automation-mcp-controller-server",
            "instance_id": "mcp-controller-test",
        },
        "http": {
            "host": "127.0.0.1",
            "port": 18080,
            "path": "/mcp",
            "allowed_origins": ["https://agent.example"],
            "max_body_bytes": 1024 * 1024,
            "auth_file": os.path.join(directory, "auth.json"),
            "request_limit_per_minute": 20,
            "ready_requires_mqtt": True,
        },
        "mqtt": {
            "enabled": True,
            "required": True,
            "host": "mqtt.test",
            "port": 1883,
            "keepalive_s": 30,
            "connect_timeout_s": 1.0,
            "socket_timeout_s": 1.0,
            "reconnect_min_s": 0.1,
            "reconnect_max_s": 1.0,
            "outbound_queue_maxsize": 64,
            "message_queue_maxsize": 64,
            "max_packet_size": 1024 * 1024,
            "max_pending_requests": 16,
            "command_timeout_s": 2.0,
            "retry_interval_s": 0.2,
            "site_id": "site-main",
            "source_service": "proxy",
            "source_instance_id": "proxy-01",
            "master_instance_id": "master-01",
            "allow_insecure_transport": True,
            "tls": {
                "enabled": False,
                "ca_file": "",
                "cert_file": "",
                "key_file": "",
                "server_hostname": "",
                "insecure_skip_verify": False,
            },
        },
        "policy": {
            "allowed_tools": list(ALL_TOOL_NAMES),
            "mutations_enabled": True,
            "mutation_actions": [
                "config.propose", "actuator.request",
                "node.drain", "node.state.set",
            ],
            "approval_mode": "external",
            "approval_ttl_s": 300,
            "max_approval_plans": 1000,
            "event_cache_size": 50,
            "sensor_cache_size": 50,
            "state_db": os.path.join(directory, "state", "controller.sqlite3"),
            "audit_log": os.path.join(directory, "state", "audit.jsonl"),
        },
        "stdio": {
            "principal": {
                "subject": "mcp:test-stdio",
                "permissions": ["node.read", "config.read", "operation.read"],
                "site_scopes": ["site-main"],
                "resource_scopes": [],
            },
        },
    }


def write_auth_file(path, token="a" * 48):
    document = {
        "schema_version": 1,
        "clients": [{
            "client_id": "test-agent",
            "token": token,
            "principal": full_principal(),
        }],
    }
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        json.dump(document, handle, sort_keys=True)
        handle.write("\n")
    return token


def full_principal():
    return {
        "subject": "mcp:test-agent",
        "permissions": [
            "node.read", "config.read", "operation.read",
            "actuator.request", "config.propose", "node.drain",
        ],
        "site_scopes": ["site-main"],
        "resource_scopes": [],
    }


def _transport_start(state):
    state["connected"] = True
    state["on_connect"]({"connected": True})


def _transport_stop(state, join_timeout_s=5.0):
    state["connected"] = False
    state["on_disconnect"]("stopped")
    return {"stopped": True, "join_timeout_s": float(join_timeout_s)}


def _transport_wait_connected(state, _timeout_s):
    return bool(state["connected"])


def _transport_snapshot(state):
    return {
        "connected": bool(state["connected"]),
        "published": len(state["published"]),
    }


def _ack_for_publish(state, envelope):
    options = state["options"]
    ack = make_envelope(
        "command.ack",
        {"service": "master", "instance_id": options["master_instance_id"]},
        {"service": "proxy", "instance_id": options["source_instance_id"]},
        site_id=options["site_id"],
        correlation_id=envelope["correlation_id"],
        causation_id=envelope["message_id"],
        payload={
            "command": envelope["event_type"],
            "result": {
                "ok": True,
                "accepted": True,
                "event_type": envelope["event_type"],
            },
        },
    )
    topic = proxy_ack_topic(
        options["site_id"], options["source_instance_id"],
        options["master_instance_id"], envelope["message_id"],
    )
    state["on_message"]({
        "topic": topic,
        "payload": canonical_bytes(ack),
        "qos": 1,
        "retain": False,
    })


def _transport_publish(state, topic, payload, qos=0, retain=False):
    envelope = decode_json_bytes(payload, maximum_bytes=1024 * 1024, require_object=True)
    state["published"].append({
        "topic": str(topic),
        "envelope": copy.deepcopy(envelope),
        "qos": int(qos),
        "retain": bool(retain),
    })
    if state.get("auto_ack"):
        _ack_for_publish(state, envelope)
    return True


def fake_transport_factory(state, **kwargs):
    state["on_message"] = kwargs["on_message"]
    state["on_connect"] = kwargs["on_connect"]
    state["on_disconnect"] = kwargs["on_disconnect"]
    state["connected"] = False
    state.setdefault("published", [])
    return {
        "start": partial(_transport_start, state),
        "stop": partial(_transport_stop, state),
        "publish": partial(_transport_publish, state),
        "wait_connected": partial(_transport_wait_connected, state),
        "snapshot": partial(_transport_snapshot, state),
    }


def make_transport_factory(state, config):
    state["options"] = copy.deepcopy(config["mqtt"])
    state.setdefault("auto_ack", True)
    state["lock"] = threading.RLock()
    return partial(fake_transport_factory, state)

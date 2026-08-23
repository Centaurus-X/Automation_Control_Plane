# -*- coding: utf-8 -*-

"""Component composition and lifecycle without module-global mutable state."""

import copy
import threading

from mcp_controller_server import __version__
from mcp_controller_server.approval_store import create_approval_store
from mcp_controller_server.audit import append_audit, create_audit_writer
from mcp_controller_server.auth import load_auth_state
from mcp_controller_server.event_cache import create_event_cache
from mcp_controller_server.mqtt_bridge import create_mqtt_bridge


def create_runtime(config, transport_factory=None, auth_state=None):
    policy = config["policy"]
    event_cache = create_event_cache(
        policy["event_cache_size"], policy["sensor_cache_size"])
    approvals = create_approval_store(
        policy["state_db"], policy["approval_ttl_s"],
        automatic=policy["approval_mode"] == "automatic",
        max_plans=policy["max_approval_plans"],
    )
    bridge = create_mqtt_bridge(
        config["mqtt"], config["server"]["instance_id"], event_cache,
        transport_factory=transport_factory,
    )
    authentication = auth_state or load_auth_state(
        config["http"]["auth_file"], config["http"]["request_limit_per_minute"])
    return {
        "version": __version__,
        "config": copy.deepcopy(config),
        "event_cache": event_cache,
        "approvals": approvals,
        "audit": create_audit_writer(policy["audit_log"]),
        "auth": authentication,
        "mqtt": bridge,
        "lock": threading.RLock(),
        "state": "created",
        "last_error": None,
    }


def start_runtime(runtime):
    with runtime["lock"]:
        if runtime["state"] == "running":
            return snapshot_runtime(runtime)
        runtime["state"] = "starting"
    try:
        if runtime["config"]["mqtt"]["enabled"]:
            runtime["mqtt"]["start"]()
        with runtime["lock"]:
            runtime["state"] = "running"
            runtime["last_error"] = None
    except Exception as exc:
        with runtime["lock"]:
            runtime["state"] = "failed"
            runtime["last_error"] = str(exc)
        raise
    return snapshot_runtime(runtime)


def stop_runtime(runtime, join_timeout_s=5.0):
    with runtime["lock"]:
        runtime["state"] = "stopping"
    if runtime["config"]["mqtt"]["enabled"]:
        runtime["mqtt"]["stop"](join_timeout_s=join_timeout_s)
    with runtime["lock"]:
        runtime["state"] = "stopped"
    return snapshot_runtime(runtime)


def runtime_ready(runtime):
    if runtime["state"] != "running":
        return False
    if not runtime["config"]["http"]["ready_requires_mqtt"]:
        return True
    if not runtime["config"]["mqtt"]["enabled"]:
        return not runtime["config"]["mqtt"]["required"]
    return bool(runtime["mqtt"]["snapshot"]().get("connected"))


def snapshot_runtime(runtime):
    with runtime["lock"]:
        return {
            "component": "mcp_controller_server",
            "version": runtime["version"],
            "instance_id": runtime["config"]["server"]["instance_id"],
            "state": runtime["state"],
            "ready": runtime_ready(runtime) if runtime["state"] == "running" else False,
            "mqtt": runtime["mqtt"]["snapshot"](),
            "last_error": runtime["last_error"],
        }


def audit_tool_call(runtime, client_context, request_id, tool_name, result):
    principal = client_context.get("principal") if isinstance(client_context, dict) else {}
    record = {
        "request_id": str(request_id),
        "client_id": client_context.get("client_id") if isinstance(client_context, dict) else None,
        "subject": principal.get("subject") if isinstance(principal, dict) else None,
        "tool": str(tool_name),
        "action": result.get("action") if isinstance(result, dict) else None,
        "decision": "ALLOW" if isinstance(result, dict) and result.get("ok") else "DENY",
        "reason_code": result.get("code", "ok") if isinstance(result, dict) else "internal_error",
        "plan_id": result.get("plan_id") if isinstance(result, dict) else None,
        "payload_sha256": result.get("payload_sha256") if isinstance(result, dict) else None,
        "mqtt_message_id": result.get("request_id") if isinstance(result, dict) else None,
    }
    try:
        return append_audit(runtime["audit"], record)
    except Exception:
        with runtime["lock"]:
            runtime["last_error"] = "audit append failed"
        return False


def audit_mutation_attempt(runtime, client_context, request_id, tool_name):
    principal = client_context.get("principal") if isinstance(client_context, dict) else {}
    record = {
        "request_id": str(request_id),
        "client_id": client_context.get("client_id") if isinstance(client_context, dict) else None,
        "subject": principal.get("subject") if isinstance(principal, dict) else None,
        "tool": str(tool_name),
        "decision": "ATTEMPT",
        "reason_code": "pre_execution",
    }
    try:
        return append_audit(runtime["audit"], record)
    except Exception:
        with runtime["lock"]:
            runtime["last_error"] = "audit preflight failed"
        return False

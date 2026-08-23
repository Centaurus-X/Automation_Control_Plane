# -*- coding: utf-8 -*-

"""Model-facing MCP tools mapped to fixed, policy-checked MQTT operations."""

import copy
import json
import uuid

from mcp_controller_server.approval_store import (
    finalize_plan,
    get_plan,
    prepare_plan,
    reserve_plan,
)
from mcp_controller_server.automation_contract import validate_payload
from mcp_controller_server.errors import make_error, make_success
from mcp_controller_server.event_cache import latest_sensors, recent_events


EVENT_PERMISSIONS = {
    "actuator.request": "actuator.request",
    "config.propose": "config.propose",
    "node.drain": "node.drain",
    "node.state.set": "node.drain",
    "nodes.list": "node.read",
    "config.get": "config.read",
    "config.history": "config.read",
    "operation.get": "operation.read",
    "runtime.status": "node.read",
}
READ_ONLY_ANNOTATIONS = {
    "readOnlyHint": True,
    "destructiveHint": False,
    "idempotentHint": True,
    "openWorldHint": False,
}
PREPARE_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": False,
    "idempotentHint": False,
    "openWorldHint": False,
}
COMMIT_ANNOTATIONS = {
    "readOnlyHint": False,
    "destructiveHint": True,
    "idempotentHint": True,
    "openWorldHint": False,
}
OUTPUT_SCHEMA = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "type": "object",
    "properties": {"ok": {"type": "boolean"}},
    "required": ["ok"],
    "additionalProperties": True,
}


def _object_schema(properties=None, required=None):
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "type": "object",
        "properties": copy.deepcopy(properties or {}),
        "required": list(required or ()),
        "additionalProperties": False,
    }


def _tool(name, title, description, input_schema, annotations):
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": input_schema,
        "outputSchema": copy.deepcopy(OUTPUT_SCHEMA),
        "annotations": copy.deepcopy(annotations),
    }


def tool_catalog():
    return (
        _tool(
            "automation_connection_status", "MQTT bridge status",
            "Inspect the MCP controller and MQTT transport without contacting the Master.",
            _object_schema(), READ_ONLY_ANNOTATIONS,
        ),
        _tool(
            "automation_runtime_status", "Automation runtime status",
            "Request the current secret-free Master runtime and readiness snapshot over MQTT.",
            _object_schema(), READ_ONLY_ANNOTATIONS,
        ),
        _tool(
            "automation_nodes_list", "List automation nodes",
            "List Master-registry Worker nodes, admission state, liveness and ownership metadata.",
            _object_schema(), READ_ONLY_ANNOTATIONS,
        ),
        _tool(
            "automation_config_get", "Read authoritative configuration",
            "Read all Worker mirror heads or one node/cross scope. This never reads PostgreSQL directly.",
            _object_schema({
                "scope": {
                    "type": "string", "maxLength": 256,
                    "pattern": "^(node|cross):[a-z0-9][a-z0-9._-]{0,127}$",
                    "description": "Optional node:<id> or cross:<id> scope.",
                },
            }), READ_ONLY_ANNOTATIONS,
        ),
        _tool(
            "automation_config_history", "Read configuration mirror history",
            "Read bounded current mirror-head history through the Master authority boundary.",
            _object_schema({
                "limit": {"type": "integer", "minimum": 1, "maximum": 200, "default": 50},
            }), READ_ONLY_ANNOTATIONS,
        ),
        _tool(
            "automation_operation_get", "Read operation state",
            "Read one durable actuator or orchestration operation by UUID through the Master.",
            _object_schema({
                "operation_id": {"type": "string", "format": "uuid"},
            }, ("operation_id",)), READ_ONLY_ANNOTATIONS,
        ),
        _tool(
            "automation_events_recent", "Read recent normalized events",
            "Read bounded Master-originated events cached by this controller instance.",
            _object_schema({
                "limit": {"type": "integer", "minimum": 1, "maximum": 1000, "default": 100},
                "event_types": {
                    "type": "array", "maxItems": 64, "uniqueItems": True,
                    "items": {"type": "string", "maxLength": 128},
                },
            }), READ_ONLY_ANNOTATIONS,
        ),
        _tool(
            "automation_sensors_latest", "Read latest sensor values",
            "Read latest normalized sensor.value events from the bounded local MQTT cache.",
            _object_schema({
                "controller_uuid": {"type": "string", "format": "uuid"},
                "sensor_id": {"type": ["string", "integer"]},
                "limit": {"type": "integer", "minimum": 1, "maximum": 5000, "default": 500},
            }), READ_ONLY_ANNOTATIONS,
        ),
        _tool(
            "automation_change_prepare", "Prepare an automation change",
            "Validate and persist an exact change plan. It does not publish MQTT or mutate automation state.",
            _object_schema({
                "action": {
                    "type": "string",
                    "enum": [
                        "config.propose", "actuator.request",
                        "node.drain", "node.state.set",
                    ],
                },
                "payload": {"type": "object"},
            }, ("action", "payload")), PREPARE_ANNOTATIONS,
        ),
        _tool(
            "automation_change_commit", "Commit an approved automation change",
            "Publish one externally approved exact plan over MQTT with an idempotent message identifier.",
            _object_schema({
                "plan_id": {"type": "string", "format": "uuid"},
                "payload_sha256": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
            }, ("plan_id", "payload_sha256")), COMMIT_ANNOTATIONS,
        ),
        _tool(
            "automation_approval_status", "Read change approval status",
            "Inspect one plan state and result without exposing its mutation payload.",
            _object_schema({
                "plan_id": {"type": "string", "format": "uuid"},
            }, ("plan_id",)), READ_ONLY_ANNOTATIONS,
        ),
    )


def list_tools(runtime):
    policy = runtime["config"]["policy"]
    allowed = set(policy["allowed_tools"])
    if not policy["mutations_enabled"] or policy["approval_mode"] == "disabled":
        allowed -= {
            "automation_change_prepare", "automation_change_commit",
            "automation_approval_status",
        }
    return [copy.deepcopy(item) for item in tool_catalog() if item["name"] in allowed]


def _validate_arguments(arguments, allowed, required=()):
    if not isinstance(arguments, dict):
        return make_error("invalid_arguments", "tool arguments must be an object")
    unknown = sorted(set(arguments) - set(allowed))
    missing = sorted(set(required) - set(arguments))
    if unknown:
        return make_error("invalid_arguments", "unknown tool arguments", fields=unknown)
    if missing:
        return make_error("invalid_arguments", "required tool arguments are missing", fields=missing)
    return make_success()


def _principal(client_context):
    principal = client_context.get("principal") if isinstance(client_context, dict) else None
    return copy.deepcopy(principal) if isinstance(principal, dict) else {}


def _authorize(runtime, principal, permission):
    if not isinstance(principal, dict) or not str(principal.get("subject") or ""):
        return make_error("authentication_required", "principal subject is required")
    permissions = {str(value) for value in principal.get("permissions") or ()}
    if permission not in permissions:
        return make_error("authorization_denied", "required permission is absent")
    site_id = runtime["config"]["mqtt"]["site_id"]
    site_scopes = {str(value) for value in principal.get("site_scopes") or ()}
    if site_id not in site_scopes and "*" not in site_scopes:
        return make_error("authorization_denied", "principal has no site scope")
    return make_success(permission=permission)


def _send(runtime, principal, event_type, payload, **kwargs):
    permission = EVENT_PERMISSIONS[event_type]
    authorized = _authorize(runtime, principal, permission)
    if not authorized.get("ok"):
        return authorized
    return runtime["mqtt"]["send_command"](
        event_type, payload, principal, **kwargs)


def _connection_status(runtime, _principal_value, arguments):
    checked = _validate_arguments(arguments, ())
    return make_success(bridge=runtime["mqtt"]["snapshot"]()) \
        if checked.get("ok") else checked


def _runtime_status(runtime, principal, arguments):
    checked = _validate_arguments(arguments, ())
    return _send(runtime, principal, "runtime.status", {}) \
        if checked.get("ok") else checked


def _nodes_list(runtime, principal, arguments):
    checked = _validate_arguments(arguments, ())
    return _send(runtime, principal, "nodes.list", {}) \
        if checked.get("ok") else checked


def _config_get(runtime, principal, arguments):
    checked = _validate_arguments(arguments, ("scope",))
    if not checked.get("ok"):
        return checked
    payload = {"scope": arguments["scope"]} if arguments.get("scope") is not None else {}
    return _send(runtime, principal, "config.get", payload)


def _config_history(runtime, principal, arguments):
    checked = _validate_arguments(arguments, ("limit",))
    if not checked.get("ok"):
        return checked
    limit = arguments.get("limit", 50)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        return make_error("invalid_arguments", "limit must be between 1 and 200")
    return _send(runtime, principal, "config.history", {"limit": limit})


def _operation_get(runtime, principal, arguments):
    checked = _validate_arguments(arguments, ("operation_id",), ("operation_id",))
    if not checked.get("ok"):
        return checked
    try:
        operation_id = str(uuid.UUID(str(arguments["operation_id"])))
    except (ValueError, TypeError, AttributeError):
        return make_error("invalid_arguments", "operation_id must be a UUID")
    return _send(runtime, principal, "operation.get", {"operation_id": operation_id})


def _events_recent(runtime, principal, arguments):
    authorized = _authorize(runtime, principal, "node.read")
    if not authorized.get("ok"):
        return authorized
    checked = _validate_arguments(arguments, ("limit", "event_types"))
    if not checked.get("ok"):
        return checked
    limit = arguments.get("limit", 100)
    event_types = arguments.get("event_types")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 1000:
        return make_error("invalid_arguments", "limit must be between 1 and 1000")
    if event_types is not None and (
            not isinstance(event_types, list) or len(event_types) > 64
            or len(event_types) != len(set(event_types))
            or not all(isinstance(value, str) and 0 < len(value) <= 128 for value in event_types)):
        return make_error("invalid_arguments", "event_types is invalid")
    return make_success(
        events=recent_events(runtime["event_cache"], limit, event_types),
        cache_scope="instance_local",
    )


def _sensors_latest(runtime, principal, arguments):
    authorized = _authorize(runtime, principal, "node.read")
    if not authorized.get("ok"):
        return authorized
    checked = _validate_arguments(arguments, ("controller_uuid", "sensor_id", "limit"))
    if not checked.get("ok"):
        return checked
    controller_uuid = arguments.get("controller_uuid")
    if controller_uuid is not None:
        try:
            controller_uuid = str(uuid.UUID(str(controller_uuid)))
        except (ValueError, TypeError, AttributeError):
            return make_error("invalid_arguments", "controller_uuid must be a UUID")
    sensor_id = arguments.get("sensor_id")
    if sensor_id is not None and (
            isinstance(sensor_id, bool)
            or not isinstance(sensor_id, (str, int))
            or not 1 <= len(str(sensor_id)) <= 128):
        return make_error("invalid_arguments", "sensor_id must be a string or integer")
    limit = arguments.get("limit", 500)
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 5000:
        return make_error("invalid_arguments", "limit must be between 1 and 5000")
    return make_success(
        sensors=latest_sensors(
            runtime["event_cache"], controller_uuid=controller_uuid,
            sensor_id=sensor_id, limit=limit,
        ),
        cache_scope="instance_local",
    )


def _change_prepare(runtime, principal, arguments):
    checked = _validate_arguments(arguments, ("action", "payload"), ("action", "payload"))
    if not checked.get("ok"):
        return checked
    policy = runtime["config"]["policy"]
    if not policy["mutations_enabled"] or policy["approval_mode"] == "disabled":
        return make_error("mutations_disabled", "mutation planning is disabled")
    action = str(arguments.get("action") or "")
    if action not in policy["mutation_actions"]:
        return make_error("authorization_denied", "mutation action is not enabled")
    permission = EVENT_PERMISSIONS.get(action)
    authorized = _authorize(runtime, principal, permission)
    if not authorized.get("ok"):
        return authorized
    payload = arguments.get("payload")
    validated = validate_payload(action, payload)
    if not validated.get("ok"):
        return validated
    result = prepare_plan(
        runtime["approvals"], principal["subject"], action, payload)
    result["next_step"] = (
        "commit_with_exact_digest" if not result.get("approval_required")
        else "external_operator_approval_required"
    )
    return result


def _change_commit(runtime, principal, arguments):
    checked = _validate_arguments(
        arguments, ("plan_id", "payload_sha256"), ("plan_id", "payload_sha256"))
    if not checked.get("ok"):
        return checked
    try:
        plan_id = str(uuid.UUID(str(arguments["plan_id"])))
    except (ValueError, TypeError, AttributeError):
        return make_error("invalid_arguments", "plan_id must be a UUID")
    reserved = reserve_plan(
        runtime["approvals"], plan_id, arguments["payload_sha256"],
        principal.get("subject"),
    )
    if not reserved.get("ok"):
        return reserved
    if reserved.get("terminal"):
        result = copy.deepcopy(reserved.get("result"))
        return result if isinstance(result, dict) else make_success(
            plan_id=plan_id, duplicate=True, state=reserved["plan"]["state"])
    permission = EVENT_PERMISSIONS.get(reserved["action"])
    authorized = _authorize(runtime, principal, permission)
    if not authorized.get("ok"):
        finalized = finalize_plan(runtime["approvals"], plan_id, authorized)
        return finalized.get("result") or authorized
    try:
        result = runtime["mqtt"]["send_command"](
            reserved["action"], reserved["payload"], principal,
            message_id=plan_id,
            idempotency_key="mcp-" + plan_id,
        )
    except Exception:
        result = make_error(
            "internal_error", "commit transport failed before a safe result",
            retryable=True,
        )
    finalized = finalize_plan(runtime["approvals"], plan_id, result)
    detached = copy.deepcopy(result)
    detached["plan_id"] = plan_id
    detached["plan_state"] = finalized.get("state")
    detached["payload_sha256"] = reserved["payload_sha256"]
    return detached


def _approval_status(runtime, principal, arguments):
    checked = _validate_arguments(arguments, ("plan_id",), ("plan_id",))
    if not checked.get("ok"):
        return checked
    try:
        plan_id = str(uuid.UUID(str(arguments["plan_id"])))
    except (ValueError, TypeError, AttributeError):
        return make_error("invalid_arguments", "plan_id must be a UUID")
    return get_plan(runtime["approvals"], plan_id, subject=principal.get("subject"))


def execute_tool(runtime, name, arguments, client_context):
    available = {item["name"] for item in list_tools(runtime)}
    if name not in available:
        return make_error("tool_not_found", "tool is unavailable: " + str(name))
    principal = _principal(client_context)
    handlers = {
        "automation_connection_status": _connection_status,
        "automation_runtime_status": _runtime_status,
        "automation_nodes_list": _nodes_list,
        "automation_config_get": _config_get,
        "automation_config_history": _config_history,
        "automation_operation_get": _operation_get,
        "automation_events_recent": _events_recent,
        "automation_sensors_latest": _sensors_latest,
        "automation_change_prepare": _change_prepare,
        "automation_change_commit": _change_commit,
        "automation_approval_status": _approval_status,
    }
    handler = handlers.get(name)
    try:
        return handler(runtime, principal, arguments or {})
    except Exception:
        return make_error(
            "internal_error", "tool failed before a safe result was produced",
            retryable=True,
        )


def mcp_tool_result(result):
    detached = copy.deepcopy(result)
    text = json.dumps(detached, sort_keys=True, ensure_ascii=False, indent=2)
    return {
        "content": [{"type": "text", "text": text}],
        "structuredContent": detached,
        "isError": not bool(detached.get("ok")),
    }

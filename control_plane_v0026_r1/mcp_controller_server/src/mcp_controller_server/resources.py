# -*- coding: utf-8 -*-

"""MCP resources backed by local snapshots or fixed MQTT read commands."""

import copy
import json
import urllib.parse
import uuid

from mcp_controller_server.errors import make_error, make_success
from mcp_controller_server.tools import execute_tool


def list_resources(_runtime):
    return [
        {
            "uri": "automation://bridge/status",
            "name": "bridge-status",
            "title": "MCP controller MQTT status",
            "description": "Secret-free local transport, queue and cache diagnostics.",
            "mimeType": "application/json",
        },
        {
            "uri": "automation://events/recent",
            "name": "recent-events",
            "title": "Recent normalized automation events",
            "description": "Bounded instance-local event cache; not durable history.",
            "mimeType": "application/json",
        },
        {
            "uri": "automation://sensors/latest",
            "name": "latest-sensors",
            "title": "Latest normalized sensor values",
            "description": "Latest sensor.value events observed after this instance connected.",
            "mimeType": "application/json",
        },
        {
            "uri": "automation://server/capabilities",
            "name": "server-capabilities",
            "title": "Automation MCP capability boundary",
            "description": "System boundary, supported protocols and mutation policy.",
            "mimeType": "application/json",
        },
    ]


def list_resource_templates(_runtime):
    return [
        {
            "uriTemplate": "automation://config/{scope}",
            "name": "config-scope",
            "title": "Authoritative node or cross configuration mirror",
            "description": "Use a URL-encoded node:<id> or cross:<id> scope.",
            "mimeType": "application/json",
        },
        {
            "uriTemplate": "automation://operations/{operation_id}",
            "name": "operation-state",
            "title": "Durable automation operation state",
            "description": "Read one Master operation by UUID.",
            "mimeType": "application/json",
        },
    ]


def _capabilities(runtime):
    policy = runtime["config"]["policy"]
    return make_success(
        component="mcp_controller_server",
        version=runtime["version"],
        system_boundary="mqtt_only",
        direct_database_access=False,
        direct_worker_bypass=False,
        raw_mqtt_publish=False,
        terminal_execute=False,
        authority_contract="automation.control.v1",
        mutation_actions=copy.deepcopy(policy["mutation_actions"]),
        approval_mode=policy["approval_mode"],
        event_cache="bounded_instance_local",
    )


def _dynamic_resource(runtime, parsed, client_context):
    value = urllib.parse.unquote(parsed.path.lstrip("/"))
    if parsed.netloc == "config" and value:
        return execute_tool(
            runtime, "automation_config_get", {"scope": value}, client_context)
    if parsed.netloc == "operations" and value:
        try:
            operation_id = str(uuid.UUID(value))
        except (ValueError, TypeError, AttributeError):
            return make_error("invalid_uri", "operation resource requires a UUID")
        return execute_tool(
            runtime, "automation_operation_get",
            {"operation_id": operation_id}, client_context,
        )
    return make_error("resource_not_found", "resource URI is unknown")


def read_resource(runtime, uri, client_context):
    text_uri = str(uri or "")
    fixed = {
        "automation://bridge/status": (
            "automation_connection_status", {},
        ),
        "automation://events/recent": (
            "automation_events_recent", {"limit": 200},
        ),
        "automation://sensors/latest": (
            "automation_sensors_latest", {"limit": 1000},
        ),
    }
    if text_uri == "automation://server/capabilities":
        result = _capabilities(runtime)
    elif text_uri in fixed:
        name, arguments = fixed[text_uri]
        result = execute_tool(runtime, name, arguments, client_context)
    else:
        parsed = urllib.parse.urlparse(text_uri)
        result = _dynamic_resource(runtime, parsed, client_context) \
            if parsed.scheme == "automation" else make_error(
                "resource_not_found", "resource URI scheme is unsupported")
    if not result.get("ok"):
        return result
    return make_success(contents=[{
        "uri": text_uri,
        "mimeType": "application/json",
        "text": json.dumps(result, sort_keys=True, ensure_ascii=False, indent=2),
    }])

# -*- coding: utf-8 -*-

"""Dual-era MCP JSON-RPC engine for 2026 stateless and legacy clients."""

import copy

from mcp_controller_server.config import SUPPORTED_PROTOCOL_VERSIONS
from mcp_controller_server.prompts import get_prompt, list_prompts
from mcp_controller_server.resources import (
    list_resource_templates,
    list_resources,
    read_resource,
)
from mcp_controller_server.errors import make_error
from mcp_controller_server.runtime import audit_mutation_attempt, audit_tool_call
from mcp_controller_server.tools import execute_tool, list_tools, mcp_tool_result


MODERN_PROTOCOL_VERSION = "2026-07-28"
LEGACY_PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26")
JSONRPC_VERSION = "2.0"


def server_info(runtime):
    return {
        "name": runtime["config"]["server"]["name"],
        "version": runtime["version"],
    }


def server_capabilities(modern=False):
    if modern:
        return {"tools": {}, "resources": {}, "prompts": {}}
    return {
        "tools": {"listChanged": False},
        "resources": {"subscribe": False, "listChanged": False},
        "prompts": {"listChanged": False},
    }


def server_instructions():
    return (
        "Use read-only status, node, configuration, operation and telemetry tools "
        "before planning a change. The telemetry cache is bounded and contains only "
        "events observed after this instance connected. Never infer durable history "
        "from it. Mutations require prepare, exact external approval and commit. "
        "All system access crosses MQTT; this server never connects to the platform DB."
    )


def jsonrpc_error(request_id, code, message, data=None):
    error = {"code": int(code), "message": str(message)}
    if data is not None:
        error["data"] = copy.deepcopy(data)
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "error": error}


def jsonrpc_result(request_id, result):
    return {"jsonrpc": JSONRPC_VERSION, "id": request_id, "result": result}


def _valid_request_id(value):
    return not isinstance(value, bool) and isinstance(value, (str, int))


def request_protocol_version(message, client_context=None):
    params = message.get("params") if isinstance(message, dict) else None
    metadata = params.get("_meta") if isinstance(params, dict) else None
    modern = metadata.get("io.modelcontextprotocol/protocolVersion") \
        if isinstance(metadata, dict) else None
    if modern is not None:
        return str(modern)
    context_version = client_context.get("protocol_version") \
        if isinstance(client_context, dict) else None
    return str(context_version) if context_version else None


def is_modern_request(message):
    params = message.get("params") if isinstance(message, dict) else None
    metadata = params.get("_meta") if isinstance(params, dict) else None
    return isinstance(metadata, dict) and \
        "io.modelcontextprotocol/protocolVersion" in metadata


def _validate_modern_metadata(message):
    params = message.get("params")
    if not isinstance(params, dict):
        return jsonrpc_error(message.get("id"), -32602, "Modern requests require params")
    metadata = params.get("_meta")
    if not isinstance(metadata, dict):
        return jsonrpc_error(message.get("id"), -32602, "Modern request metadata is required")
    version = metadata.get("io.modelcontextprotocol/protocolVersion")
    if version != MODERN_PROTOCOL_VERSION:
        return jsonrpc_error(
            message.get("id"), -32022, "Unsupported protocol version",
            {"supported": list(SUPPORTED_PROTOCOL_VERSIONS), "requested": version},
        )
    capabilities = metadata.get("io.modelcontextprotocol/clientCapabilities")
    if not isinstance(capabilities, dict):
        return jsonrpc_error(
            message.get("id"), -32602,
            "io.modelcontextprotocol/clientCapabilities must be an object",
        )
    client_info = metadata.get("io.modelcontextprotocol/clientInfo")
    if client_info is not None and (
            not isinstance(client_info, dict)
            or not isinstance(client_info.get("name"), str)
            or not isinstance(client_info.get("version"), str)):
        return jsonrpc_error(message.get("id"), -32602, "clientInfo is invalid")
    return None


def _result_metadata(runtime):
    return {"io.modelcontextprotocol/serverInfo": server_info(runtime)}


def _modern_result(runtime, result, cacheable=False, cache_scope="private", ttl_ms=0):
    detached = copy.deepcopy(result)
    detached["resultType"] = "complete"
    metadata = detached.get("_meta") if isinstance(detached.get("_meta"), dict) else {}
    metadata.update(_result_metadata(runtime))
    detached["_meta"] = metadata
    if cacheable:
        detached["ttlMs"] = max(0, int(ttl_ms))
        detached["cacheScope"] = str(cache_scope)
    return detached


def _list_params(message, modern):
    params = message.get("params")
    if params is None and not modern:
        return {}, None
    if not isinstance(params, dict):
        return None, jsonrpc_error(message.get("id"), -32602, "params must be an object")
    allowed = {"_meta", "cursor"} if modern else {"_meta", "cursor"}
    unknown = sorted(set(params) - allowed)
    if unknown or params.get("cursor") not in (None, ""):
        return None, jsonrpc_error(
            message.get("id"), -32602, "pagination parameters are invalid")
    return params, None


def _initialize(runtime, message, client_context):
    params = message.get("params")
    if not isinstance(params, dict):
        return jsonrpc_error(message.get("id"), -32602, "initialize params are required")
    requested = str(params.get("protocolVersion") or "")
    if requested in LEGACY_PROTOCOL_VERSIONS:
        selected = requested
    else:
        selected = LEGACY_PROTOCOL_VERSIONS[0]
    if isinstance(client_context, dict):
        client_context["protocol_version"] = selected
        client_context["initialized"] = False
    result = {
        "protocolVersion": selected,
        "capabilities": server_capabilities(modern=False),
        "serverInfo": server_info(runtime),
        "instructions": server_instructions(),
    }
    return jsonrpc_result(message.get("id"), result)


def _discover(runtime, message):
    _params, error = _list_params(message, True)
    if error:
        return error
    result = {
        "supportedVersions": list(SUPPORTED_PROTOCOL_VERSIONS),
        "capabilities": server_capabilities(modern=True),
        "instructions": server_instructions(),
    }
    return jsonrpc_result(
        message.get("id"),
        _modern_result(runtime, result, cacheable=True, cache_scope="private", ttl_ms=300000),
    )


def _tools_list(runtime, message, modern):
    _params, error = _list_params(message, modern)
    if error:
        return error
    result = {"tools": list_tools(runtime)}
    if modern:
        result = _modern_result(
            runtime, result, cacheable=True, cache_scope="private", ttl_ms=300000)
    return jsonrpc_result(message.get("id"), result)


def _tools_call(runtime, message, client_context, modern):
    params = message.get("params")
    if not isinstance(params, dict):
        return jsonrpc_error(message.get("id"), -32602, "tools/call params are required")
    allowed = {"name", "arguments", "_meta", "inputResponses", "requestState"} \
        if modern else {"name", "arguments", "_meta"}
    if set(params) - allowed or not isinstance(params.get("name"), str):
        return jsonrpc_error(message.get("id"), -32602, "tools/call params are invalid")
    arguments = params.get("arguments", {})
    if not isinstance(arguments, dict):
        return jsonrpc_error(message.get("id"), -32602, "tool arguments must be an object")
    available = {item["name"] for item in list_tools(runtime)}
    if params["name"] not in available:
        return jsonrpc_error(message.get("id"), -32602, "Unknown tool: " + params["name"])
    mutating = params["name"] in (
        "automation_change_prepare", "automation_change_commit")
    if mutating and not audit_mutation_attempt(
            runtime, client_context, message.get("id"), params["name"]):
        result = make_error(
            "audit_unavailable",
            "mutation was not executed because audit evidence is unavailable",
            retryable=True,
        )
    else:
        result = execute_tool(runtime, params["name"], arguments, client_context)
    audit_tool_call(runtime, client_context, message.get("id"), params["name"], result)
    mcp_result = mcp_tool_result(result)
    if modern:
        mcp_result = _modern_result(runtime, mcp_result)
    return jsonrpc_result(message.get("id"), mcp_result)


def _resources_list(runtime, message, modern):
    _params, error = _list_params(message, modern)
    if error:
        return error
    result = {"resources": list_resources(runtime)}
    if modern:
        result = _modern_result(
            runtime, result, cacheable=True, cache_scope="private", ttl_ms=60000)
    return jsonrpc_result(message.get("id"), result)


def _resource_templates_list(runtime, message, modern):
    _params, error = _list_params(message, modern)
    if error:
        return error
    result = {"resourceTemplates": list_resource_templates(runtime)}
    if modern:
        result = _modern_result(
            runtime, result, cacheable=True, cache_scope="private", ttl_ms=300000)
    return jsonrpc_result(message.get("id"), result)


def _resource_read(runtime, message, client_context, modern):
    params = message.get("params")
    allowed = {"uri", "_meta"}
    if not isinstance(params, dict) or set(params) - allowed \
            or not isinstance(params.get("uri"), str):
        return jsonrpc_error(message.get("id"), -32602, "resources/read params are invalid")
    result = read_resource(runtime, params["uri"], client_context)
    audit_tool_call(
        runtime, client_context, message.get("id"),
        "resource:" + params["uri"], result,
    )
    if not result.get("ok"):
        return jsonrpc_error(
            message.get("id"), -32002, result.get("message", "Resource unavailable"),
            {"code": result.get("code")},
        )
    payload = {"contents": result["contents"]}
    if modern:
        payload = _modern_result(
            runtime, payload, cacheable=True, cache_scope="private", ttl_ms=0)
    return jsonrpc_result(message.get("id"), payload)


def _prompts_list(runtime, message, modern):
    _params, error = _list_params(message, modern)
    if error:
        return error
    result = {"prompts": list_prompts()}
    if modern:
        result = _modern_result(
            runtime, result, cacheable=True, cache_scope="private", ttl_ms=300000)
    return jsonrpc_result(message.get("id"), result)


def _prompt_get(runtime, message, modern):
    params = message.get("params")
    allowed = {"name", "arguments", "_meta"}
    if not isinstance(params, dict) or set(params) - allowed \
            or not isinstance(params.get("name"), str):
        return jsonrpc_error(message.get("id"), -32602, "prompts/get params are invalid")
    result = get_prompt(params["name"], params.get("arguments") or {})
    if not result.get("ok"):
        return jsonrpc_error(message.get("id"), -32602, result.get("message"))
    payload = {
        "description": result.get("description"),
        "messages": result["messages"],
    }
    if modern:
        payload = _modern_result(runtime, payload)
    return jsonrpc_result(message.get("id"), payload)


def handle_jsonrpc(runtime, message, client_context=None):
    context = client_context if isinstance(client_context, dict) else {}
    if not isinstance(message, dict):
        return jsonrpc_error(None, -32600, "Invalid Request")
    request_id_present = "id" in message
    request_id = message.get("id")
    if message.get("jsonrpc") != JSONRPC_VERSION or not isinstance(message.get("method"), str):
        return None if not request_id_present else jsonrpc_error(request_id, -32600, "Invalid Request")
    if request_id_present and not _valid_request_id(request_id):
        return jsonrpc_error(None, -32600, "Request id must be a string or integer")
    modern = is_modern_request(message)
    if modern:
        metadata_error = _validate_modern_metadata(message)
        if metadata_error:
            return metadata_error
        context["protocol_version"] = MODERN_PROTOCOL_VERSION
    method = message["method"]
    if not request_id_present:
        if method == "notifications/initialized" and not modern:
            context["initialized"] = True
        return None
    if method == "initialize":
        return jsonrpc_error(
            request_id, -32601,
            "initialize is not available for modern MCP; supported versions: "
            + ", ".join(SUPPORTED_PROTOCOL_VERSIONS),
        ) if modern else _initialize(runtime, message, context)
    if method == "server/discover":
        if not modern:
            return jsonrpc_error(request_id, -32602, "Modern request metadata is required")
        return _discover(runtime, message)
    if method == "ping":
        return jsonrpc_error(request_id, -32601, "Method not found") \
            if modern else jsonrpc_result(request_id, {})
    handlers = {
        "tools/list": _tools_list,
        "resources/list": _resources_list,
        "resources/templates/list": _resource_templates_list,
        "prompts/list": _prompts_list,
    }
    handler = handlers.get(method)
    if callable(handler):
        return handler(runtime, message, modern)
    if method == "tools/call":
        return _tools_call(runtime, message, context, modern)
    if method == "resources/read":
        return _resource_read(runtime, message, context, modern)
    if method == "prompts/get":
        return _prompt_get(runtime, message, modern)
    return jsonrpc_error(request_id, -32601, "Method not found")

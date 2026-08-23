# -*- coding: utf-8 -*-

"""Fail-closed JSON configuration loading and environment overlays."""

import copy
import json
import os
import re

from mcp_controller_server.errors import make_error, make_success


IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
ALL_TOOL_NAMES = (
    "automation_connection_status",
    "automation_runtime_status",
    "automation_nodes_list",
    "automation_config_get",
    "automation_config_history",
    "automation_operation_get",
    "automation_events_recent",
    "automation_sensors_latest",
    "automation_change_prepare",
    "automation_change_commit",
    "automation_approval_status",
)
READ_TOOL_NAMES = ALL_TOOL_NAMES[:8]
MUTATION_TOOL_NAMES = ALL_TOOL_NAMES[8:]
MUTATION_ACTIONS = (
    "config.propose", "actuator.request", "node.drain", "node.state.set",
)
SUPPORTED_PROTOCOL_VERSIONS = (
    "2026-07-28", "2025-11-25", "2025-06-18", "2025-03-26",
)


def _read_json(path, maximum_bytes=1024 * 1024):
    absolute = os.path.abspath(str(path))
    if not os.path.isfile(absolute) or os.path.islink(absolute):
        raise ValueError("configuration file is missing or symbolic: " + absolute)
    if os.path.getsize(absolute) > maximum_bytes:
        raise ValueError("configuration file exceeds its size limit")
    with open(absolute, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    if not isinstance(value, dict):
        raise ValueError("configuration root must be an object")
    return value


def _unknown_fields(value, allowed, label):
    unknown = sorted(set(value) - set(allowed))
    if unknown:
        raise ValueError(label + " contains unknown fields: " + ", ".join(unknown))


def _identifier(value, label):
    text = str(value or "").strip().lower()
    if not IDENTIFIER_PATTERN.fullmatch(text):
        raise ValueError(label + " has an invalid format")
    return text


def _integer(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(label + " must be an integer")
    if value < minimum or value > maximum:
        raise ValueError(label + " is outside its bounds")
    return value


def _number(value, label, minimum, maximum):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(label + " must be numeric")
    numeric = float(value)
    if numeric < minimum or numeric > maximum:
        raise ValueError(label + " is outside its bounds")
    return numeric


def _string_list(value, label, maximum=256):
    if not isinstance(value, list) or len(value) > maximum:
        raise ValueError(label + " must be a bounded list")
    if not all(isinstance(item, str) and item for item in value):
        raise ValueError(label + " must contain non-empty strings")
    if len(value) != len(set(value)):
        raise ValueError(label + " must not contain duplicates")
    return list(value)


def _apply_environment(config, environment):
    result = copy.deepcopy(config)
    mappings = (
        ("MCP_HTTP_HOST", "http", "host", str),
        ("MCP_HTTP_PORT", "http", "port", int),
        ("MCP_AUTH_FILE", "http", "auth_file", str),
        ("MQTT_HOST", "mqtt", "host", str),
        ("MQTT_PORT", "mqtt", "port", int),
        ("AUTOMATION_SITE_ID", "mqtt", "site_id", str),
        ("MASTER_INSTANCE_ID", "mqtt", "master_instance_id", str),
        ("MCP_MQTT_SOURCE_INSTANCE_ID", "mqtt", "source_instance_id", str),
    )
    for env_name, section, key, converter in mappings:
        if environment.get(env_name) is not None:
            result.setdefault(section, {})[key] = converter(environment[env_name])
    tls_mappings = (
        ("MQTT_CA_FILE", "ca_file"),
        ("MCP_MQTT_CERT_FILE", "cert_file"),
        ("MCP_MQTT_KEY_FILE", "key_file"),
        ("MQTT_SERVER_HOSTNAME", "server_hostname"),
    )
    for env_name, key in tls_mappings:
        if environment.get(env_name) is not None:
            result.setdefault("mqtt", {}).setdefault("tls", {})[key] = environment[env_name]
    return result


def _validate_server(config):
    value = config.get("server")
    if not isinstance(value, dict):
        raise ValueError("server configuration is required")
    _unknown_fields(value, ("name", "instance_id"), "server")
    if not isinstance(value.get("name"), str) or not value["name"]:
        raise ValueError("server.name is required")
    value["instance_id"] = _identifier(value.get("instance_id"), "server.instance_id")


def _validate_http(config, check_files):
    value = config.get("http")
    if not isinstance(value, dict):
        raise ValueError("http configuration is required")
    _unknown_fields(value, (
        "host", "port", "path", "allowed_origins", "max_body_bytes",
        "auth_file", "request_limit_per_minute", "ready_requires_mqtt",
    ), "http")
    if not isinstance(value.get("host"), str) or not value["host"]:
        raise ValueError("http.host is required")
    _integer(value.get("port"), "http.port", 1, 65535)
    path = str(value.get("path") or "")
    if not path.startswith("/") or "?" in path or "#" in path:
        raise ValueError("http.path is invalid")
    _string_list(value.get("allowed_origins"), "http.allowed_origins", 128)
    _integer(value.get("max_body_bytes"), "http.max_body_bytes", 1024, 16 * 1024 * 1024)
    _integer(
        value.get("request_limit_per_minute"),
        "http.request_limit_per_minute", 1, 100000,
    )
    if not isinstance(value.get("ready_requires_mqtt"), bool):
        raise ValueError("http.ready_requires_mqtt must be boolean")
    auth_file = str(value.get("auth_file") or "")
    if not auth_file:
        raise ValueError("http.auth_file is required")
    if check_files and (not os.path.isfile(auth_file) or os.path.islink(auth_file)):
        raise ValueError("http.auth_file is missing or symbolic")


def _validate_mqtt(config, check_files):
    value = config.get("mqtt")
    if not isinstance(value, dict):
        raise ValueError("mqtt configuration is required")
    _unknown_fields(value, (
        "enabled", "required", "host", "port", "keepalive_s",
        "connect_timeout_s", "socket_timeout_s", "reconnect_min_s",
        "reconnect_max_s", "outbound_queue_maxsize", "message_queue_maxsize",
        "max_packet_size", "max_pending_requests", "command_timeout_s",
        "retry_interval_s", "site_id", "source_service",
        "source_instance_id", "master_instance_id",
        "allow_insecure_transport", "tls",
    ), "mqtt")
    for key in ("enabled", "required", "allow_insecure_transport"):
        if not isinstance(value.get(key), bool):
            raise ValueError("mqtt." + key + " must be boolean")
    if value.get("source_service") != "proxy":
        raise ValueError("mqtt.source_service must remain proxy for automation.control.v1")
    for key in ("site_id", "source_instance_id", "master_instance_id"):
        value[key] = _identifier(value.get(key), "mqtt." + key)
    if not isinstance(value.get("host"), str) or not value["host"]:
        raise ValueError("mqtt.host is required")
    _integer(value.get("port"), "mqtt.port", 1, 65535)
    for key, minimum, maximum in (
        ("keepalive_s", 5, 3600),
        ("outbound_queue_maxsize", 1, 100000),
        ("message_queue_maxsize", 1, 100000),
        ("max_packet_size", 1024, 32 * 1024 * 1024),
        ("max_pending_requests", 1, 10000),
    ):
        _integer(value.get(key), "mqtt." + key, minimum, maximum)
    for key, minimum, maximum in (
        ("connect_timeout_s", 0.1, 300.0),
        ("socket_timeout_s", 0.1, 300.0),
        ("reconnect_min_s", 0.1, 300.0),
        ("reconnect_max_s", 0.1, 3600.0),
        ("command_timeout_s", 0.1, 300.0),
        ("retry_interval_s", 0.1, 300.0),
    ):
        _number(value.get(key), "mqtt." + key, minimum, maximum)
    tls = value.get("tls")
    if not isinstance(tls, dict):
        raise ValueError("mqtt.tls is required")
    _unknown_fields(tls, (
        "enabled", "ca_file", "cert_file", "key_file",
        "server_hostname", "insecure_skip_verify",
    ), "mqtt.tls")
    if not isinstance(tls.get("enabled"), bool) or not isinstance(
            tls.get("insecure_skip_verify"), bool):
        raise ValueError("mqtt TLS flags must be boolean")
    if not tls["enabled"] and not value["allow_insecure_transport"]:
        raise ValueError("unencrypted MQTT requires an explicit unsafe test flag")
    for key in ("ca_file", "cert_file", "key_file", "server_hostname"):
        if tls["enabled"] and not str(tls.get(key) or ""):
            raise ValueError("mqtt.tls." + key + " is required")
    if check_files and tls["enabled"]:
        for key in ("ca_file", "cert_file", "key_file"):
            path = str(tls[key])
            if not os.path.isfile(path) or os.path.islink(path):
                raise ValueError("mqtt.tls." + key + " is missing or symbolic")


def _validate_policy(config):
    value = config.get("policy")
    if not isinstance(value, dict):
        raise ValueError("policy configuration is required")
    _unknown_fields(value, (
        "allowed_tools", "mutations_enabled", "mutation_actions",
        "approval_mode", "approval_ttl_s", "max_approval_plans", "event_cache_size",
        "sensor_cache_size", "state_db", "audit_log",
    ), "policy")
    tools = _string_list(value.get("allowed_tools"), "policy.allowed_tools", 128)
    if not tools:
        tools = list(ALL_TOOL_NAMES)
        value["allowed_tools"] = tools
    unknown_tools = sorted(set(tools) - set(ALL_TOOL_NAMES))
    if unknown_tools:
        raise ValueError("policy.allowed_tools contains unsupported tools")
    if not isinstance(value.get("mutations_enabled"), bool):
        raise ValueError("policy.mutations_enabled must be boolean")
    actions = _string_list(value.get("mutation_actions"), "policy.mutation_actions", 32)
    if set(actions) - set(MUTATION_ACTIONS):
        raise ValueError("policy.mutation_actions contains unsupported actions")
    if value.get("approval_mode") not in ("external", "automatic", "disabled"):
        raise ValueError("policy.approval_mode is invalid")
    if value["approval_mode"] == "automatic" and not value["mutations_enabled"]:
        raise ValueError("automatic approval requires enabled mutations")
    _integer(value.get("approval_ttl_s"), "policy.approval_ttl_s", 30, 86400)
    _integer(
        value.get("max_approval_plans"),
        "policy.max_approval_plans", 100, 1000000,
    )
    _integer(value.get("event_cache_size"), "policy.event_cache_size", 1, 100000)
    _integer(value.get("sensor_cache_size"), "policy.sensor_cache_size", 1, 100000)
    for key in ("state_db", "audit_log"):
        if not os.path.isabs(str(value.get(key) or "")):
            raise ValueError("policy." + key + " must be an absolute path")


def _validate_principal(principal, label):
    if not isinstance(principal, dict):
        raise ValueError(label + " must be an object")
    _unknown_fields(
        principal,
        ("subject", "permissions", "site_scopes", "resource_scopes", "session_id"),
        label,
    )
    if not isinstance(principal.get("subject"), str) or not principal["subject"]:
        raise ValueError(label + ".subject is required")
    _string_list(principal.get("permissions"), label + ".permissions", 128)
    _string_list(principal.get("site_scopes"), label + ".site_scopes", 128)
    if not isinstance(principal.get("resource_scopes"), list):
        raise ValueError(label + ".resource_scopes must be a list")
    return principal


def validate_config(config, check_files=False):
    try:
        if not isinstance(config, dict):
            raise ValueError("configuration root must be an object")
        _unknown_fields(
            config, ("schema_version", "server", "http", "mqtt", "policy", "stdio"),
            "configuration",
        )
        if config.get("schema_version") != 1:
            raise ValueError("schema_version must be 1")
        _validate_server(config)
        _validate_http(config, check_files)
        _validate_mqtt(config, check_files)
        _validate_policy(config)
        stdio = config.get("stdio")
        if not isinstance(stdio, dict):
            raise ValueError("stdio configuration is required")
        _unknown_fields(stdio, ("principal",), "stdio")
        _validate_principal(stdio.get("principal"), "stdio.principal")
        return make_success(config=copy.deepcopy(config))
    except (ValueError, TypeError, OverflowError) as exc:
        return make_error("invalid_config", str(exc))


def load_config(path=None, environment=None, check_files=True):
    selected = path or os.environ.get("MCP_CONTROLLER_CONFIG") \
        or "config/controller_config.json"
    raw = _read_json(selected)
    effective = _apply_environment(raw, environment or os.environ)
    checked = validate_config(effective, check_files=check_files)
    if not checked.get("ok"):
        raise ValueError(checked["message"])
    checked["config"]["_config_path"] = os.path.abspath(selected)
    return checked["config"]

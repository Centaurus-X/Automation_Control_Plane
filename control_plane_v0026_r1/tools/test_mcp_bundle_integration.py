#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Validate the additive MCP integration across the complete v0026 bundle."""

import json
import os
import re

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
EXPECTED_ORDER = (
    "database",
    "mqtt",
    "master",
    "proxy",
    "mcp_controller_server",
    "worker",
)
MCP_INSTANCE_ID = "mcp-controller-01"
MCP_IDENTITY = "urn:automation:site-main:proxy:" + MCP_INSTANCE_ID


def _load_json(relative):
    path = ROOT / relative
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError(relative + " must contain one JSON object")
    return value


def _read(relative):
    return (ROOT / relative).read_text(encoding="utf-8")


def _require(condition, message):
    if not condition:
        raise RuntimeError(message)


def _component_order(registry):
    components = registry.get("components")
    if not isinstance(components, dict):
        raise RuntimeError("administration registry components are missing")
    order = []
    pending = set(components)
    while pending:
        ready = sorted(
            name for name in pending
            if set(components[name].get("depends_on") or ()) <= set(order)
        )
        if not ready:
            raise RuntimeError("administration registry dependency graph is cyclic")
        for name in ready:
            order.append(name)
            pending.remove(name)
    return tuple(order)


def _validate_versions():
    _require(_read("VERSION").strip() == "0.26.0", "bundle version is not 0.26.0")
    _require(
        _read("RELEASE_REVISION").strip() == "v0026-r1",
        "bundle revision is not v0026-r1",
    )
    _require(
        _read("master_node/VERSION").strip() == "0.26.0",
        "Master version is not 0.26.0",
    )
    _require(
        _read("proxy_gateway/VERSION").strip() == "0.26.0",
        "Proxy version is not 0.26.0",
    )
    _require(
        _read("mcp_controller_server/VERSION").strip() == "0.1.0",
        "MCP component version is not 0.1.0",
    )
    _require(
        _read("mcp_controller_server/RELEASE_REVISION").strip() == "v0001-r1",
        "MCP component revision is not v0001-r1",
    )


def _validate_metadata():
    metadata = _load_json("mcp_controller_server/RELEASE_METADATA.json")
    integration = metadata.get("integration") or {}
    _require(metadata.get("component") == "mcp_controller_server", "MCP component name differs")
    _require(integration.get("bundle_version") == "0.26.0", "MCP bundle version binding differs")
    _require(integration.get("bundle_revision") == "v0026-r1", "MCP bundle revision binding differs")
    _require(
        integration.get("orchestration_component") == "mcp_controller_server",
        "MCP orchestration component binding differs",
    )
    _require(integration.get("mqtt_identity") == MCP_IDENTITY, "MCP MQTT identity binding differs")


def _validate_master_and_mqtt_boundary():
    master = _load_json("master_node/master_node/_config/application/master_config.json")
    allowed = master.get("control", {}).get("allowed_proxy_instance_ids") or []
    _require("proxy-01" in allowed, "existing Proxy identity was removed")
    _require(MCP_INSTANCE_ID in allowed, "MCP identity is not accepted by Master")

    acl = _read("mqtt_broker/config/acl.conf")
    required = (
        "user " + MCP_IDENTITY,
        "topic write automation/v1/site-main/master/master-01/commands/#",
        "topic read automation/v1/site-main/proxy/" + MCP_INSTANCE_ID + "/acks/#",
        "topic read automation/v1/site-main/events/+/master-01",
        "topic write automation/v1/site-main/proxy/" + MCP_INSTANCE_ID + "/acks/#",
    )
    for fragment in required:
        _require(fragment in acl, "MQTT MCP boundary is incomplete: " + fragment)
    mcp_section = acl.split("user " + MCP_IDENTITY, 1)[1].split("\nuser ", 1)[0]
    _require("nodes/+/commands" not in mcp_section, "MCP identity can write Worker command topics")
    _require("broadcast/commands" not in mcp_section, "MCP identity can write broadcast topics")
    _require("topic write automation/v1/site-main/proxy/" not in mcp_section, "MCP identity can forge ACK topics")
    _require("topic write automation/v1/site-main/events/" not in mcp_section, "MCP identity can forge Master events")


def _validate_mcp_configuration():
    config = _load_json("mcp_controller_server/config/controller_config.json")
    mqtt = config.get("mqtt") or {}
    _require(mqtt.get("source_service") == "proxy", "MCP contract source service changed")
    _require(mqtt.get("source_instance_id") == MCP_INSTANCE_ID, "MCP source instance differs")
    _require(mqtt.get("allow_insecure_transport") is False, "MCP MQTT transport is not fail-closed")
    _require((mqtt.get("tls") or {}).get("enabled") is True, "MCP MQTT TLS is disabled")
    _require((config.get("http") or {}).get("auth_file") == "/run/secrets/mcp_auth", "MCP auth file differs")

    compose = _read("mcp_controller_server/compose.yaml")
    required_compose = (
        "${MCP_BIND_ADDRESS:-127.0.0.1}:${MCP_HTTP_PORT:-8080}:8080",
        "MCP_MQTT_SOURCE_INSTANCE_ID: ${MCP_MQTT_SOURCE_INSTANCE_ID:-mcp-controller-01}",
        "MCP_AUTH_FILE: /run/secrets/mcp_auth",
        "read_only: true",
        "no-new-privileges:true",
        "cap_drop:",
        "automation_control",
    )
    for fragment in required_compose:
        _require(fragment in compose, "MCP Compose hardening/integration is missing: " + fragment)


def _validate_orchestration():
    registry = _load_json("administration/component_registry.test-lab.json")
    manifest = _load_json("administration/release_manifest.json")
    inventory = _load_json("administration/inventory.test-lab.json")
    _require(_component_order(registry) == EXPECTED_ORDER, "administration dependency order differs")
    _require(
        tuple((manifest.get("legacy_backend") or {}).get("component_order") or ()) == EXPECTED_ORDER,
        "legacy rollout component order differs",
    )
    targets = inventory.get("targets") or []
    control_targets = [target for target in targets if target.get("target_id") == "control-plane-01"]
    _require(len(control_targets) == 1, "control-plane target is missing or duplicated")
    components = set(control_targets[0].get("components") or ())
    _require(set(EXPECTED_ORDER[:-1]) <= components, "MCP is absent from control-plane target")

    orchestrator = _read("administration/orchestrate.py")
    adapter = _read("administration/component_adapter.py")
    literal = "(\"database\", \"mqtt\", \"master\", \"proxy\", \"mcp_controller_server\", \"worker\")"
    _require(literal in orchestrator, "orchestrator component order literal differs")
    _require(literal in adapter, "component adapter order literal differs")
    for operation in ("preflight", "backup", "apply", "ready", "status", "test", "restart", "rollback"):
        _require(operation in adapter, "component adapter operation is absent: " + operation)


def _validate_rollout_and_lifecycle():
    rollout = _read("rollout_automation.sh")
    lifecycle = _read("scripts/control_plane_lifecycle.sh")
    running = _read("test_running_all.sh")
    static = _read("test_static_all.sh")
    required_rollout = (
        "prepare_mcp_auth_document",
        "MCP_AUTH_CREDENTIAL_KEY",
        "MCP_MQTT_SOURCE_INSTANCE_ID",
        "MCP_BIND_ADDRESS",
        "MCP_HTTP_PORT",
    )
    for fragment in required_rollout:
        _require(fragment in rollout, "rollout MCP integration is missing: " + fragment)
    for fragment in (
        "mcp_controller_server",
        "automation-mcp-controller_mcp_controller_state",
        "MCP_CONTROLLER_IMAGE",
        "MCP_HTTP_PORT",
    ):
        _require(fragment in lifecycle, "lifecycle MCP integration is missing: " + fragment)
    _require("mcp-controller-server" in running, "running-service MCP verification is absent")
    _require("mcp_controller_server/test_all.sh" in static, "static MCP suite is absent")
    _require("tools/test_prepare_mcp_auth.py" in static, "MCP auth regression test is absent")


def _validate_secret_hygiene():
    forbidden_names = {"mcp_auth.json", "mcp_mqtt.key", "mqtt_ca.crt", "mcp_mqtt.crt"}
    runtime = ROOT / "mcp_controller_server" / "runtime-secrets"
    documentation = ROOT / "mcp_controller_server" / "secrets" / "README.md"
    _require(not runtime.exists(), "generated MCP runtime-secrets must not be bundled")
    _require(documentation.is_file(), "safe MCP secrets documentation was not preserved")
    secret_entries = tuple(
        path.relative_to(documentation.parent).as_posix()
        for path in documentation.parent.rglob("*") if path.is_file()
    )
    _require(secret_entries == ("README.md",), "MCP secrets directory contains runtime material")
    for path in ROOT.rglob("*"):
        if path.is_file() and path.name in forbidden_names:
            relative = path.relative_to(ROOT).as_posix()
            _require("test-pki" in relative, "private MCP runtime material is bundled: " + relative)
    credentials = _load_json("credentials.json.example")
    token = credentials.get("mcp_controller", {}).get("local_agent", {}).get("token")
    _require(
        isinstance(token, str) and token.startswith("CHANGE_ME_"),
        "credential example contains a non-placeholder MCP token",
    )
    auth_helper = _read("tools/prepare_mcp_auth.py")
    _require(
        'value.upper().startswith("CHANGE_ME_")' in auth_helper,
        "MCP auth helper does not reject the release placeholder",
    )


def main():
    _validate_versions()
    _validate_metadata()
    _validate_master_and_mqtt_boundary()
    _validate_mcp_configuration()
    _validate_orchestration()
    _validate_rollout_and_lifecycle()
    _validate_secret_hygiene()
    print(
        "PASS v0026 MCP bundle integration: isolated MQTT identity, Master/ACL boundary, "
        "six-component orchestration, lifecycle, auth and secret hygiene"
    )
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, json.JSONDecodeError, re.error) as exc:
        print("MCP bundle integration failed: " + str(exc), file=__import__("sys").stderr)
        raise SystemExit(1)

# -*- coding: utf-8 -*-

"""Build and verify the deterministic canonical-V9 authority fixture."""

import argparse
import copy
import json
import os
import sys


RELEASE_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
MASTER_ROOT = os.path.join(RELEASE_ROOT, "master_node", "master_node")
if MASTER_ROOT not in sys.path:
    sys.path.insert(0, MASTER_ROOT)

from src.library._generic_system_contract_library import (
    config_compatibility,
    config_graph,
    entity_registry,
)
from src.library._generic_type_library import fingerprint, json_ready
from src.core.shard_materializer import create_shard_materializer


CONFIG_DIRECTORY = os.path.join(
    RELEASE_ROOT, "master_node", "master_node", "_config", "application")
SOURCE_PATH = os.path.join(CONFIG_DIRECTORY, "_legacy", "initial_config.json")
TARGET_PATH = os.path.join(CONFIG_DIRECTORY, "initial_config_v9.json")
REPORT_PATH = os.path.join(CONFIG_DIRECTORY, "RULE_MIGRATION_REPORT.json")
REMOTE_CROSS_CONTROLLER = "d11032fa-33db-56de-a4ed-7e16cca3ded7"
FN02_NATIVE_CONTROLLER = "06ab4528-f8a3-5ec7-a91f-eea79d60761e"
V9_DESCRIPTION = (
    "Kanonischer V9-Global-Contract: flache Sensor-/Aktor-Entitäten mit "
    "stabiler controller_uuid plus lokaler I/O-ID; gruppierte Zeilen sind "
    "ausschließlich eine interne Runtime-Projektion."
)


def _arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true")
    return parser.parse_args()


def _json_bytes(value):
    text = json.dumps(
        json_ready(value), ensure_ascii=False, indent=2, sort_keys=True)
    return (text + "\n").encode("utf-8")


def _read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write(path, content):
    temporary = path + ".tmp"
    with open(temporary, "wb") as handle:
        handle.write(content)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def _scope_report(materialized):
    rows = []
    for scope, snapshot in sorted(materialized["scopes"].items()):
        rows.append({
            "scope": scope,
            "scope_kind": snapshot["scope_kind"],
            "owner_node_id": snapshot["owner_node_id"],
            "scope_revision": snapshot["scope_revision"],
            "execution_epoch": snapshot["execution_epoch"],
            "fingerprint": snapshot["fingerprint"],
            "rule_ids": sorted(
                int(rule["rule_id"])
                for rule in snapshot["config"].get("rules") or ()),
        })
    return rows


def _lab_topology(source):
    """Make the approved Cross rules exercise a real second hardware owner."""
    config = copy.deepcopy(source)
    for route in config.get("site_controller_map") or []:
        if route.get("controller_uuid") == REMOTE_CROSS_CONTROLLER:
            route["node_id"] = "fn-02"
            route["local_controller_id"] = 2
            route["route_key"] = "site_main.fn-02.ctrl_2"
    for section in ("controllers", "virtual_controllers"):
        for controller in config.get(section) or []:
            if controller.get("controller_uuid") == REMOTE_CROSS_CONTROLLER:
                controller["host"] = "modbus-fn02"
                controller["port"] = 5020
    for section in ("sensors", "actuators"):
        for group in config.get(section) or []:
            if group.get("controller_uuid") == REMOTE_CROSS_CONTROLLER:
                group["node_id"] = "fn-02"
                group["local_controller_id"] = 2
    for rule in config.get("rules") or []:
        if int(rule.get("rule_id") or -1) in (3, 6):
            rule["owner_node_id"] = "fn-01"
    config["local_node"] = [{
        "site_id": "site_main",
        "node_id": "fn-01",
        "active_controller_uuids": [
            value for value in config["local_node"][0]["active_controller_uuids"]
            if value != REMOTE_CROSS_CONTROLLER
        ],
        "active_controller_routes": [
            copy.deepcopy(value)
            for value in config["local_node"][0]["active_controller_routes"]
            if value.get("controller_uuid") != REMOTE_CROSS_CONTROLLER
        ],
    }, {
        "site_id": "site_main",
        "node_id": "fn-02",
        "active_controller_uuids": [FN02_NATIVE_CONTROLLER, REMOTE_CROSS_CONTROLLER],
        "active_controller_routes": [{
            "local_controller_id": 1,
            "controller_uuid": FN02_NATIVE_CONTROLLER,
            "controller_key": "ctrl_wago_5",
        }, {
            "local_controller_id": 2,
            "controller_uuid": REMOTE_CROSS_CONTROLLER,
            "controller_key": "ctrl_wago_3",
        }],
    }]
    return config


def _process_owner_candidates(config):
    owners = {}
    for route in config.get("site_controller_map") or []:
        if (
            isinstance(route, dict)
            and route.get("enabled") is not False
            and str(route.get("routing_role") or "owner") == "owner"
        ):
            owners[str(route.get("controller_uuid") or "")] = str(
                route.get("node_id") or "")
    state_process = {
        str(state.get("state_id")): str(state.get("p_id"))
        for state in config.get("process_states") or [] if isinstance(state, dict)
    }
    rule_candidates = {}
    io_candidates = {}
    for rule in config.get("rules") or []:
        if not isinstance(rule, dict):
            continue
        owner = str(rule.get("execution_owner") or "")
        if not owner:
            continue
        process_ids = set()
        for name in ("when", "until"):
            for condition in rule.get(name) or []:
                if isinstance(condition, dict) and condition.get("src") == "state":
                    process_id = state_process.get(str(condition.get("state_id")))
                    if process_id:
                        process_ids.add(process_id)
        for name in ("do", "else_do"):
            for action in rule.get(name) or []:
                if not isinstance(action, dict):
                    continue
                process_ids.update(str(value) for value in action.get("p_ids") or [])
                for state_id in action.get("state_ids") or []:
                    process_id = state_process.get(str(state_id))
                    if process_id:
                        process_ids.add(process_id)
        for process_id in process_ids:
            rule_candidates.setdefault(process_id, set()).add(owner)
    for state in config.get("process_states") or []:
        if not isinstance(state, dict):
            continue
        process_id = str(state.get("p_id"))
        for section in ("sensors", "actuators"):
            for reference in (state.get("io_refs") or {}).get(section) or []:
                if not isinstance(reference, dict):
                    continue
                owner = owners.get(str(reference.get("controller_uuid") or ""))
                if owner:
                    io_candidates.setdefault(process_id, set()).add(owner)
    return rule_candidates, io_candidates


def _assign_unambiguous_process_owners(config):
    """Add only ownership that can be proven without a tie-break guess."""
    candidate = copy.deepcopy(config)
    rule_owners, io_owners = _process_owner_candidates(candidate)
    ambiguous = []
    for process in candidate.get("processes") or []:
        process_id = str(process.get("p_id"))
        declared = str(process.get("owner_node_id") or "")
        declared_choices = set(rule_owners.get(process_id) or ())
        choices = declared_choices or set(io_owners.get(process_id) or ())
        if declared:
            if choices and declared not in choices:
                raise ValueError("process owner conflicts with its graph: " + process_id)
            continue
        if len(choices) == 1:
            process["owner_node_id"] = next(iter(choices))
        elif len(choices) > 1:
            ambiguous.append(process_id)
    return candidate, tuple(sorted(ambiguous))


def _build():
    source = _lab_topology(_read_json(SOURCE_PATH))
    migrated = config_graph.migrate_legacy_config(source, profile="lab_v2")
    if not migrated.get("ok"):
        raise ValueError("lab-v2 migration was rejected")
    config, ambiguous_processes = _assign_unambiguous_process_owners(
        migrated["config"])
    config["meta"][0]["description"] = V9_DESCRIPTION
    if config_compatibility.get_config_version(config) != \
            config_compatibility.CANONICAL_VERSION:
        raise ValueError("fixture migration did not produce V9")
    validation = config_graph.validate_authority_config(config, profile="lab_v2")
    if not validation.get("ok"):
        raise ValueError("lab-v2 authority config failed validation")
    materialized = create_shard_materializer(profile="lab_v2")["materialize"](
        config, 1)
    active = tuple(migrated["report"]["active_rule_ids"])
    migration_required = tuple(migrated["report"]["migration_required_rule_ids"])
    if active != config_graph.LAB_ACTIVE_RULE_IDS:
        raise ValueError("lab-v2 active rule set differs from the approved set")
    if migration_required != config_graph.MIGRATION_REQUIRED_RULE_IDS:
        raise ValueError("lab-v2 migration-required rule set differs")
    report = {
        "release": "automation_control_plane_release_v0026_r1",
        "profile": "lab_v2",
        "source_fixture": "_legacy/initial_config.json",
        "target_fixture": "initial_config_v9.json",
        "config_format_version": config_compatibility.CANONICAL_VERSION,
        "global_fingerprint": fingerprint(config),
        "entity_count": len(entity_registry.decompose(config)),
        "roundtrip_ok": bool(validation["registry_report"].get("ok")),
        "type_status": validation["type_report"].get("status"),
        "active_rule_ids": list(active),
        "migration_required_rule_ids": list(migration_required),
        "production_safety_blockers": migrated["report"]["production_safety_blockers"],
        "ambiguous_process_owner_ids": list(ambiguous_processes),
        "rules": migrated["report"]["rules"],
        "scopes": _scope_report(materialized),
    }
    return _json_bytes(config), _json_bytes(report)


def _matches(path, expected):
    if not os.path.isfile(path):
        return False
    with open(path, "rb") as handle:
        return handle.read() == expected


def main():
    arguments = _arguments()
    config_bytes, report_bytes = _build()
    if arguments.check:
        if not _matches(TARGET_PATH, config_bytes) or not _matches(REPORT_PATH, report_bytes):
            raise SystemExit("FAIL generated Master-V9 fixture/report is stale")
        print("PASS deterministic Master-V9 fixture and migration report")
        return
    _write(TARGET_PATH, config_bytes)
    _write(REPORT_PATH, report_bytes)
    print("WROTE deterministic Master-V9 fixture and migration report")


if __name__ == "__main__":
    main()

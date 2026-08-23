# -*- coding: utf-8 -*-

"""Canonical V9 contract, projection, diff and protocol regressions."""

import ast
import copy
import hashlib
import importlib
import json
import os

from src.library._generic_system_contract_library import (
    config_compatibility,
    entity_registry,
    protocol_contracts,
    scope_diff,
    type_library,
)
from src.library._generic_type_library import fingerprint
from src.core.config_authority import (
    DEFAULT_ALLOWED_PREFIXES,
    DENIED_PREFIXES,
    apply_changes,
)
from src.core.shard_materializer import create_shard_materializer
from src.core.proposal_ingest import WORKER_FILE_SECTIONS


CONFIG_DIRECTORY = os.path.abspath(os.path.join(
    os.path.dirname(__file__), "..", "_config", "application"))


def _read(name):
    with open(os.path.join(CONFIG_DIRECTORY, name), "r", encoding="utf-8") as handle:
        return json.load(handle)


def _contract_authority_roots():
    component_payload_root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), ".."))
    component_root = os.path.dirname(component_payload_root)
    release_root = os.path.dirname(component_root)
    candidate_contract_roots = (
        os.path.join(
            component_payload_root, "src", "library",
            "_generic_system_contract_library"),
        os.path.join(
            release_root, "proxy_gateway", "proxy_gateway", "src", "library",
            "_generic_system_contract_library"),
    )
    candidate_type_paths = (
        os.path.join(
            component_payload_root, "src", "library",
            "_generic_type_library.py"),
        os.path.join(
            release_root, "proxy_gateway", "proxy_gateway", "src", "library",
            "_generic_type_library.py"),
    )
    full_release_tree = os.path.isfile(os.path.join(release_root, "VERSION"))
    if full_release_tree:
        contract_roots = candidate_contract_roots
        type_roots = candidate_type_paths
        assert all(os.path.isdir(root) for root in contract_roots), (
            "canonical contract authority is absent from a Control Plane component")
        assert all(os.path.isfile(path) for path in type_roots), (
            "canonical Type Authority is absent from a Control Plane component")
    else:
        contract_roots = tuple(
            root for root in candidate_contract_roots if os.path.isdir(root))
        type_roots = tuple(
            path for path in candidate_type_paths if os.path.isfile(path))
        assert len(contract_roots) == 1, (
            "isolated component image must contain exactly one contract authority")
        assert len(type_roots) == 1, (
            "isolated component image must contain exactly one Type Authority")
    return contract_roots, type_roots


def _contract_authorities_are_identical():
    roots, type_paths = _contract_authority_roots()
    names = (
        "__init__.py",
        "module_contracts.py",
        "protocol_contracts.py",
        "persistence_contracts.py",
        "authority_flow_contracts.py",
        "config_sharding_contracts.py",
        "errors.py",
    )
    for name in names:
        digests = []
        for root in roots:
            with open(os.path.join(root, name), "rb") as handle:
                digests.append(hashlib.sha256(handle.read()).hexdigest())
        assert len(set(digests)) == 1, name
        module_name = "src.library._generic_system_contract_library"
        if name != "__init__.py":
            module_name += "." + name.removesuffix(".py")
        module = importlib.import_module(module_name)
        imported_path = os.path.realpath(module.__file__)
        expected_path = os.path.realpath(os.path.join(roots[0], name))
        assert imported_path == expected_path, (
            name, imported_path, expected_path)
    type_digests = []
    for path in type_paths:
        with open(path, "rb") as handle:
            type_digests.append(hashlib.sha256(handle.read()).hexdigest())
    assert len(set(type_digests)) == 1, "_generic_type_library.py"
    imported_type_path = os.path.realpath(type_library.__file__)
    assert imported_type_path == os.path.realpath(type_paths[0])

def _config_path_policies_are_identical():
    master_config = _read("master_config.json")
    policy = master_config["config_policy"]
    allowed = tuple(policy["allowed_prefixes"])
    denied = tuple(policy["denied_prefixes"])
    assert allowed == DEFAULT_ALLOWED_PREFIXES
    assert denied == DENIED_PREFIXES
    worker_file_sections = tuple(
        master_config["worker_file_proposals"]["allowed_sections"])
    assert worker_file_sections == WORKER_FILE_SECTIONS
    assert master_config["worker_compatibility"]["allowed_worker_node_ids"] \
        == ["fn-01"]

    component_root = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", ".."))
    proxy_policy = os.path.join(
        os.path.dirname(component_root), "proxy_gateway", "proxy_gateway",
        "src", "core", "access_policy.py")
    if not os.path.isfile(proxy_policy):
        return
    with open(proxy_policy, "r", encoding="utf-8") as handle:
        tree = ast.parse(handle.read(), filename=proxy_policy)
    assignments = {
        target.id: ast.literal_eval(node.value)
        for node in tree.body
        if isinstance(node, ast.Assign)
        for target in node.targets
        if isinstance(target, ast.Name)
        and target.id in ("CONFIG_ALLOWED_SECTIONS", "CONFIG_DENIED_PREFIXES")
    }
    assert assignments["CONFIG_ALLOWED_SECTIONS"] == allowed
    assert assignments["CONFIG_DENIED_PREFIXES"] == denied


def main():
    canonical = _read("initial_config_v9.json")
    mutable_sections = tuple(
        section for section in canonical if section != "meta")
    assert set(DEFAULT_ALLOWED_PREFIXES) == set(mutable_sections)
    for section in mutable_sections:
        unchanged = apply_changes(canonical, [{
            "operation": "set",
            "path": section,
            "value": copy.deepcopy(canonical[section]),
        }])
        assert unchanged["ok"], (section, unchanged)
        assert unchanged["config"] == canonical

    legacy = _read("_legacy/initial_config_v2.json")
    assert config_compatibility.get_config_version(canonical) == 9
    assert config_compatibility.get_config_io_layout(canonical) == "flat"
    assert type_library.verify_config_data(canonical)["status"] == "OK"
    assert type_library.verify_config_data(legacy)["status"] == "FAILED"
    try:
        entity_registry.decompose(legacy)
    except ValueError:
        pass
    else:
        raise AssertionError("implicit V5 registry import was accepted")

    imported = config_compatibility.import_legacy_v5_config(legacy)
    assert imported["meta"][0]["version"] == 9
    assert imported["meta"][0]["scope"] == "master_global"
    assert config_compatibility.get_config_io_layout(imported) == "flat"
    assert type_library.verify_config_data(imported)["status"] == "OK"

    runtime = config_compatibility.normalize_config_for_runtime(
        canonical, representation="storage")
    assert config_compatibility.get_config_io_layout(runtime) == "grouped"
    projected = config_compatibility.project_config_for_storage(runtime)
    assert projected == canonical
    assert type_library.config_fingerprint(runtime) \
        == type_library.config_fingerprint(canonical) \
        == fingerprint(canonical)

    roundtrip = entity_registry.verify_roundtrip(canonical)
    assert roundtrip["ok"] and roundtrip["source_fingerprint"] \
        == roundtrip["materialized_fingerprint"]
    changed = copy.deepcopy(canonical)
    process = next(row for row in changed["processes"] if row.get("owner_node_id") == "fn-01")
    process["process_location"] = str(process.get("process_location") or "") + "|unit-v9"
    operations = scope_diff.diff_configs(canonical, changed)
    assert len(operations) == 1
    assert operations[0]["section"] == "processes"
    assert operations[0]["entity_key"] == "process:" + str(process["p_id"])
    rebuilt = scope_diff.apply_operations(canonical, operations)
    assert rebuilt == changed

    for section, id_field, group_field in (
            ("sensors", "sensor_id", "sensor_group"),
            ("actuators", "actuator_id", "actuator_group")):
        appended_row = copy.deepcopy(canonical[section][0])
        same_controller = [
            row for row in canonical[section]
            if row["controller_uuid"] == appended_row["controller_uuid"]
        ]
        appended_row[id_field] = max(
            row[id_field] for row in same_controller) + 100
        appended_row["address"] = max(
            row["address"] for row in same_controller) + 100
        appended_row[group_field] = "UNIT_V9_APPEND"
        appended = apply_changes(canonical, [{
            "operation": "append", "path": section, "value": appended_row,
        }])
        assert appended["ok"]
        entity_rows = appended["config"][section]
        inserted = entity_rows.index(appended_row)
        original_positions = [
            index for index, row in enumerate(canonical[section])
            if row["controller_uuid"] == appended_row["controller_uuid"]
        ]
        assert inserted == original_positions[-1] + 1
        assert type_library.verify_config_data(appended["config"])["status"] == "OK"
        assert entity_registry.verify_roundtrip(appended["config"])["ok"]

    materializer = create_shard_materializer(profile="lab_v2")
    scopes = materializer["materialize"](canonical, 1)["scopes"]
    node = scopes["node:fn-01"]
    assert node["config"]["meta"][0]["version"] == 9
    assert config_compatibility.get_config_io_layout(node["config"]) == "flat"
    assert node["fingerprint"] == type_library.config_fingerprint(node["config"])
    assert all(row.get("owner_node_id") for row in node["config"]["processes"])

    cross = scopes["cross:fn-01.default"]
    cross_config = cross["config"]
    assert cross_config["meta"][0]["version"] == 8
    assert cross_config["meta"][0]["domain_id"] == "cross-fn-01-default"
    assert config_compatibility.get_config_io_layout(cross_config) == "flat"
    assert cross["fingerprint"] == fingerprint(cross_config)
    assert config_compatibility.ensure_v8_cross_storage_config(cross_config) \
        == cross_config
    forbidden_io = {"address", "bus_type", "function_type", "unit"}
    for section in ("sensors", "actuators"):
        assert all(not forbidden_io.intersection(row) for row in cross_config[section])
    forbidden_controller = {"bus_type", "host", "port", "unit"}
    assert all(
        not forbidden_controller.intersection(row)
        for row in cross_config["controllers"])
    assert entity_registry.verify_roundtrip(
        cross_config, allow_cross_v8=True)["ok"]
    try:
        entity_registry.decompose(cross_config)
    except ValueError:
        pass
    else:
        raise AssertionError("implicit Cross-V8 registry access was accepted")

    changed_authority = copy.deepcopy(canonical)
    cross_rule_id = cross_config["rules"][0]["rule_id"]
    changed_rule = next(
        row for row in changed_authority["rules"]
        if row["rule_id"] == cross_rule_id)
    changed_rule["priority"] += 1
    changed_scopes = materializer["materialize"](
        changed_authority, 2,
        previous_heads=scopes, previous_snapshots=scopes)["scopes"]
    changed_cross = changed_scopes["cross:fn-01.default"]
    assert changed_cross["scope_revision"] == cross["scope_revision"] + 1
    assert changed_cross["operations"]
    rebuilt_cross = scope_diff.apply_operations(
        cross_config, changed_cross["operations"], allow_cross_v8=True)
    assert rebuilt_cross == changed_cross["config"]
    assert fingerprint(rebuilt_cross) == changed_cross["fingerprint"]

    selector_row = node["config"]["sensors"][0]
    selector = {
        "section": "sensors",
        "controller_uuid": selector_row["controller_uuid"],
        "sensor_id": selector_row["sensor_id"],
        "field": "address",
    }
    assert config_compatibility.resolve_io_entity_path(
        node["config"], selector).endswith(".address")

    modern = protocol_contracts.normalize_registration({
        "node_id": "fn-01",
        "protocol_versions": [protocol_contracts.PROTOCOL_V2],
        "contracts_version": protocol_contracts.CONTRACTS_VERSION,
        "type_schema_version": protocol_contracts.TYPE_SCHEMA_VERSION,
        "config_norm_version": protocol_contracts.CONFIG_NORM_VERSION,
        "capabilities": [
            protocol_contracts.OPERATION_FORMAT,
            "mirror_v1",
            "node_full",
            "node_patch",
        ],
    })
    assert protocol_contracts.selected_protocol(modern) == protocol_contracts.PROTOCOL_V2
    assert modern["capabilities"][protocol_contracts.OPERATION_FORMAT] is True
    assert modern["capabilities"]["mirror_v1"] is True
    try:
        protocol_contracts.normalize_registration({
            "node_id": "fn-01",
            "protocol_versions": [protocol_contracts.PROTOCOL_V2],
            "contracts_version": "1.2",
            "type_schema_version": protocol_contracts.TYPE_SCHEMA_VERSION,
            "config_norm_version": protocol_contracts.CONFIG_NORM_VERSION,
            "capabilities": [protocol_contracts.OPERATION_FORMAT],
        })
    except ValueError as exc:
        assert str(exc) == "contracts_major_mismatch"
    else:
        raise AssertionError("Contract Major 1 peer was accepted")

    full = {
        "config": node["config"], "shard_scope": "node_local",
        "target_scope_fingerprint": node["fingerprint"],
        "shard_fingerprint": node["fingerprint"],
    }
    assert protocol_contracts.validate_full_payload(
        full, config_authority="master_global")["ok"]
    assert not protocol_contracts.validate_full_payload(
        full, config_authority="node_local")["ok"]
    rejected = dict(full, config=legacy)
    assert not protocol_contracts.validate_full_payload(
        rejected, config_authority="master_global")["ok"]
    cross_full = {
        "config": cross_config, "shard_scope": "cross",
        "target_scope_fingerprint": cross["fingerprint"],
        "cross_fingerprint": cross["fingerprint"],
    }
    assert protocol_contracts.validate_full_payload(
        cross_full, config_authority="master_global")["ok"]
    assert not protocol_contracts.validate_full_payload(
        dict(cross_full, shard_scope="node_local"),
        config_authority="master_global",
    )["ok"]
    _contract_authorities_are_identical()
    _config_path_policies_are_identical()
    print("PASS canonical V9 contracts, projection, diff, shard and protocol invariants")


if __name__ == "__main__":
    main()

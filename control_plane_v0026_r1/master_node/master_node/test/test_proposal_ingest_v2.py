# -*- coding: utf-8 -*-

"""Scope-owner, field-policy and durable proposal result tests."""

import copy
import json
import os

from src.library._generic_system_contract_library import entity_registry
from src.library._generic_type_library import fingerprint
from src.library._generic_system_contract_library.config_sharding_contracts import diff_configs
from src.core.proposal_ingest import (
    WORKER_FILE_SECTIONS, create_proposal_ingest,
)
from src.core.shard_materializer import create_shard_materializer


def main():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application", "initial_config_v9.json"))
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    materialized = create_shard_materializer()["materialize"](config, 1)
    heads = {
        scope: snapshot for scope, snapshot in materialized["scopes"].items()
    }
    proposals = {}

    def config_reader():
        return {"revision": 1, "config_data": copy.deepcopy(config)}

    def scope_heads():
        return copy.deepcopy(heads)

    def record(request):
        proposals[request["operation_id"]] = copy.deepcopy(request)
        return {"ok": True, "duplicate": False}

    def complete(operation_id, state, result):
        proposals[operation_id]["state"] = state
        proposals[operation_id]["result"] = copy.deepcopy(result)
        return True

    ingest = create_proposal_ingest(
        config_reader=config_reader, scope_head_reader=scope_heads,
        record_proposal=record, complete_proposal=complete,
        file_policy={
            "enabled": True, "allow_deletes": True, "max_operations": 128,
            "allowed_sections": WORKER_FILE_SECTIONS,
        })
    records = {
        record_row["entity_key"]: record_row
        for record_row in entity_registry.decompose(config)
        if record_row["section"] == "process_states"
    }
    state = records["state:1"]
    payload = {
        "proposal_id": "proposal:fn-01:1", "operation_id": "local:fn-01:1",
        "scope": "node:fn-01",
        "base_scope_revision": heads["node:fn-01"]["scope_revision"],
        "base_scope_fingerprint": heads["node:fn-01"]["fingerprint"],
        "operations": [{
            "op": "replace_field", "section": "process_states",
            "entity_key": "state:1", "json_pointer": "/value_id",
            "expected_value_hash": fingerprint(state["payload"]["value_id"]),
            "value": 2,
        }],
    }
    prepared = ingest["prepare_v2"]("fn-01", payload)
    assert prepared["ok"] and prepared["config"]["process_states"][0]["value_id"] == 2
    ingest["complete"](prepared["operation_id"], {
        "ok": True,
        "changed": True,
        "scope_targets": {
            "node:fn-01": {
                "scope_revision": 2,
                "fingerprint": "a" * 64,
            },
        },
    })
    assert proposals[prepared["operation_id"]]["state"] == "accepted"

    no_change = copy.deepcopy(payload)
    no_change["proposal_id"] = "proposal:fn-01:no-change"
    no_change["operation_id"] = "local:fn-01:no-change"
    no_change["operations"][0]["value"] = state["payload"]["value_id"]
    prepared_no_change = ingest["prepare_v2"]("fn-01", no_change)
    assert prepared_no_change["ok"] and prepared_no_change["config"] == config
    ingest["complete"](prepared_no_change["operation_id"], {
        "ok": True,
        "changed": False,
        "revision": 1,
    })
    assert proposals[prepared_no_change["operation_id"]]["state"] == "already_committed"

    empty = copy.deepcopy(payload)
    empty["proposal_id"] = "proposal:fn-01:empty"
    empty["operation_id"] = "local:fn-01:empty"
    empty["operations"] = []
    prepared_empty = ingest["prepare_v2"]("fn-01", empty)
    assert prepared_empty["ok"] and prepared_empty["config"] == config
    ingest["complete"](prepared_empty["operation_id"], {
        "ok": True,
        "changed": False,
        "revision": 1,
    })
    assert proposals[prepared_empty["operation_id"]]["state"] == "already_committed"

    foreign = ingest["prepare_v2"]("fn-02", dict(payload, operation_id="foreign"))
    assert not foreign["ok"] and foreign["code"] == "not_authorized"
    topology = copy.deepcopy(payload)
    topology["operation_id"] = "topology"
    topology["operations"][0]["section"] = "controllers"
    topology["operations"][0]["entity_key"] = "controller:unknown"
    rejected = ingest["prepare_v2"]("fn-01", topology)
    assert not rejected["ok"] and rejected["code"] == "rejected"

    node_scope = copy.deepcopy(heads["node:fn-01"]["config"])
    file_candidate = copy.deepcopy(node_scope)
    file_candidate["controllers"][0]["tags"] = ["file-sync", "verified"]
    file_payload = {
        "proposal_id": "proposal:fn-01:file", "operation_id": "file:fn-01:1",
        "proposal_origin": "worker_config_file", "scope": "node:fn-01",
        "base_scope_revision": heads["node:fn-01"]["scope_revision"],
        "base_scope_fingerprint": heads["node:fn-01"]["fingerprint"],
        "operations": list(diff_configs(node_scope, file_candidate)),
    }
    prepared_file = ingest["prepare_v2"]("fn-01", file_payload)
    assert prepared_file["ok"], prepared_file
    changed_controller = next(
        row for row in prepared_file["config"]["controllers"]
        if row["controller_uuid"] == file_candidate["controllers"][0]["controller_uuid"])
    assert changed_controller["tags"] == ["file-sync", "verified"]

    owned_process = next(
        row for row in config["processes"]
        if row.get("owner_node_id") == "fn-01")
    process_candidate = copy.deepcopy(node_scope)
    process_row = next(
        row for row in process_candidate["processes"]
        if row["p_id"] == owned_process["p_id"])
    process_row["process_location"] = str(
        process_row.get("process_location") or "") + "|worker-file"
    process_payload = copy.deepcopy(file_payload)
    process_payload["proposal_id"] = "proposal:fn-01:process"
    process_payload["operation_id"] = "file:fn-01:process"
    process_payload["operations"] = list(diff_configs(
        node_scope, process_candidate))
    prepared_process = ingest["prepare_v2"]("fn-01", process_payload)
    assert prepared_process["ok"], prepared_process
    changed_process = next(
        row for row in prepared_process["config"]["processes"]
        if row["p_id"] == owned_process["p_id"])
    assert changed_process["process_location"].endswith("|worker-file")

    stolen_process_payload = copy.deepcopy(process_payload)
    stolen_process_payload["proposal_id"] = "proposal:fn-01:process-theft"
    stolen_process_payload["operation_id"] = "file:fn-01:process-theft"
    stolen_process = stolen_process_payload["operations"][0]["value"]
    stolen_process["payload"]["owner_node_id"] = "fn-02"
    stolen_process_payload["operations"][0]["value_hash"] = fingerprint(
        stolen_process)
    stolen_process_result = ingest["prepare_v2"](
        "fn-01", stolen_process_payload)
    assert not stolen_process_result["ok"] \
        and stolen_process_result["code"] == "not_authorized"

    # Shared runtime catalogs are first-class file proposals. They still pass
    # through the authoritative Master validation and revision pipeline.
    shared_candidate = copy.deepcopy(node_scope)
    shared_candidate["pid_configs"][0]["pid_parameters"]["kp"] += 0.001
    shared_payload = copy.deepcopy(file_payload)
    shared_payload["proposal_id"] = "proposal:fn-01:shared"
    shared_payload["operation_id"] = "file:fn-01:shared"
    shared_payload["operations"] = list(diff_configs(
        node_scope, shared_candidate))
    shared_result = ingest["prepare_v2"]("fn-01", shared_payload)
    assert shared_result["ok"], shared_result
    changed_pid = next(
        row for row in shared_result["config"]["pid_configs"]
        if row["pid_config_id"] == shared_candidate[
            "pid_configs"][0]["pid_config_id"])
    assert changed_pid["pid_parameters"]["kp"] == shared_candidate[
        "pid_configs"][0]["pid_parameters"]["kp"]
    assert tuple(sorted(WORKER_FILE_SECTIONS)) == tuple(sorted(
        section for section in node_scope if section != "meta"))

    # A Worker may propose a new route that it will own, but must never take
    # over an existing route or controller owned by another Worker merely by
    # rewriting the candidate owner field.
    global_records = {
        (row["section"], row["entity_key"]): row
        for row in entity_registry.decompose(config)
    }
    foreign_route = next(
        row for row in global_records.values()
        if row["section"] == "site_controller_map"
        and row["payload"].get("node_id") == "fn-02")
    stolen_route = copy.deepcopy(foreign_route)
    stolen_route["payload"]["node_id"] = "fn-01"
    theft_payload = copy.deepcopy(file_payload)
    theft_payload["proposal_id"] = "proposal:fn-01:route-theft"
    theft_payload["operation_id"] = "file:fn-01:route-theft"
    theft_payload["operations"] = [{
        "op": "upsert_entity",
        "section": foreign_route["section"],
        "entity_key": foreign_route["entity_key"],
        "expected_value_hash": fingerprint(foreign_route),
        "value": stolen_route,
        "value_hash": fingerprint(stolen_route),
    }]
    theft_result = ingest["prepare_v2"]("fn-01", theft_payload)
    assert not theft_result["ok"] and theft_result["code"] == "not_authorized"

    allowed = ingest["record_legacy"]("fn-01", {
        "operation_id": "legacy-active", "actions": [{
            "rule_id": 1, "action": {"state_ids": [1], "value_id": 1},
        }],
    }, config)
    assert allowed["ok"]
    denied = ingest["record_legacy"]("fn-01", {
        "operation_id": "legacy-disabled", "actions": [{
            "rule_id": 5, "action": {"state_ids": [10], "value_id": 1},
        }],
    }, config)
    assert not denied["ok"] and denied["code"] == "not_authorized"
    foreign_state = ingest["record_legacy"]("fn-02", {
        "operation_id": "legacy-foreign-state", "actions": [{
            "action": {"state_ids": [1], "value_id": 1},
        }],
    }, config)
    assert not foreign_state["ok"] and foreign_state["code"] == "not_authorized"
    untargeted = ingest["record_legacy"]("fn-01", {
        "operation_id": "legacy-untargeted", "actions": [{
            "action": {"value_id": 1},
        }],
    }, config)
    assert not untargeted["ok"] and untargeted["code"] == "invalid_payload"
    print("PASS durable owner-checked Worker proposal ingestion")


if __name__ == "__main__":
    main()

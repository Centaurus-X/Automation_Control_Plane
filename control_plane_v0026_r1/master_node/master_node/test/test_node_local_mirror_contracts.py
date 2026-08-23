# -*- coding: utf-8 -*-

"""Mirror tuple, flat-storage validation and unchanged Relay tests."""

import copy
import json
import os
import sys

from functools import partial


PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.node_local_coordination import create_node_local_coordination
from src.library import _generic_type_library as type_authority
from src.library.master_mirror_store_postgresql import _mirror_transition_error
from src.library._generic_system_contract_library import persistence_contracts
from src.library._generic_system_contract_library import protocol_contracts


FIXTURE_PATH = os.path.join(
    os.path.dirname(__file__),
    "fixtures",
    "node_scope_v9_fn-01.json",
)


def _load_config():
    with open(FIXTURE_PATH, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _authority_reader():
    return {
        "lease_active": True,
        "leader_instance_id": "master-a",
        "lease_token": "lease-a",
        "authority_term": 13,
    }


def _node_reader(node_id):
    if str(node_id) not in ("fn-01", "fn-02"):
        return None
    return {
        "node_id": str(node_id),
        "admission_state": "APPROVED",
    }


def _get_mirror(state, scope):
    return copy.deepcopy(state["mirrors"].get(str(scope)))


def _persist_mirror(state, record, **fence):
    state["persisted"].append({
        "record": copy.deepcopy(record),
        "fence": copy.deepcopy(fence),
    })
    mirror = copy.deepcopy(record)
    state["mirrors"][record["scope"]] = mirror
    return {
        "ok": True,
        "duplicate": False,
        "audit_id": "audit-mirror-1",
        "mirror": mirror,
    }


def _stage_command(state, command):
    state["commands"].append(copy.deepcopy(command))
    return {"ok": True, "message_id": command["message_id"]}


def _coordination(state):
    return create_node_local_coordination(
        site_id="site-main",
        instance_id="master-a",
        authority_reader=_authority_reader,
        node_reader=_node_reader,
        mirror_store={
            "get_worker_mirror": partial(_get_mirror, state),
            "persist_worker_mirror": partial(_persist_mirror, state),
            "get_registry_head": partial(_get_mirror, state),
            "persist_registry_transition": partial(
                _stage_command, state),
        },
        stage_worker_command=partial(_stage_command, state),
    )


def test_flat_node_snapshot_is_validated_and_acked_exactly():
    storage_config = _load_config()
    config = type_authority.normalize_config_for_runtime(
        storage_config, representation="storage")
    fingerprint = type_authority.config_fingerprint(config)
    snapshot = persistence_contracts.make_scope_snapshot(
        config,
        scope="node:fn-01",
        authority_epoch=3,
        scope_revision=9,
        execution_epoch=0,
        fingerprint=fingerprint,
    )
    assert snapshot["config"] == storage_config
    assert all("rows" not in row for row in snapshot["config"]["sensors"])
    assert all("rows" not in row for row in snapshot["config"]["actuators"])
    state = {"mirrors": {}, "persisted": [], "commands": []}
    api = _coordination(state)
    payload = {
        "mirror_id": "mirror-node-3-9",
        "scope": "node:fn-01",
        "authority_epoch": 3,
        "scope_revision": 9,
        "execution_epoch": 0,
        "fingerprint": fingerprint,
        "snapshot": snapshot,
    }
    result = api["handle_mirror_snapshot"]({
        "source_node": "fn-01",
        "payload": payload,
    })
    assert result["ok"] is True, result
    assert result == {
        "ok": True,
        "persisted": True,
        "duplicate": False,
        "audit_id": "audit-mirror-1",
        "mirror_id": "mirror-node-3-9",
        "scope": "node:fn-01",
        "authority_epoch": 3,
        "scope_revision": 9,
        "execution_epoch": 0,
        "fingerprint": fingerprint,
    }
    persisted = state["persisted"][0]
    assert persisted["record"]["snapshot"] == snapshot
    assert persisted["fence"]["authority_term"] == 13
    announced = api["handle_mirror_announce"]({
        "source_node": "fn-01",
        "payload": {
            "scope": "node:fn-01",
            "authority_epoch": 3,
            "scope_revision": 9,
            "execution_epoch": 0,
            "fingerprint": fingerprint,
        },
    })
    assert announced["ok"] is True
    assert announced["already_current"] is True
    assert announced["request_content"] is False


def test_cross_owner_change_requires_the_next_execution_epoch():
    current = {
        "scope": "cross:domain-a",
        "scope_kind": "cross",
        "owner_node_id": "fn-01",
        "source_node_id": "fn-01",
        "authority_epoch": 2,
        "scope_revision": 6,
        "execution_epoch": 4,
        "fingerprint": "a" * 64,
    }
    moved = dict(
        current,
        owner_node_id="fn-02",
        source_node_id="fn-02",
        execution_epoch=5,
    )
    assert _mirror_transition_error(current, moved) is None
    unfenced = dict(moved, execution_epoch=4)
    rejected = _mirror_transition_error(current, unfenced)
    assert rejected["ok"] is False
    assert rejected["code"] == "execution_epoch_stale"
    skipped = dict(moved, execution_epoch=6)
    rejected_skip = _mirror_transition_error(current, skipped)
    assert rejected_skip["ok"] is False
    assert rejected_skip["code"] == "execution_epoch_stale"


def test_master_relay_preserves_canonical_operations_without_revision_assignment():
    config = _load_config()
    rule = config["rules"][0]
    operations = [{
        "op": "replace_field",
        "section": "rules",
        "entity_key": "rule:" + str(rule["rule_id"]),
        "json_pointer": "/enabled",
        "expected_value_hash": "b" * 64,
        "value": not bool(rule.get("enabled")),
    }]
    state = {"mirrors": {}, "persisted": [], "commands": []}
    api = _coordination(state)
    envelope = {
        "message_id": "external-proposal-1",
        "ttl_s": 30.0,
        "source": {"service": "proxy", "instance_id": "proxy-a"},
        "principal": {"subject": "operator:test"},
        "payload": {
            "operation": "scope_operations",
            "operation_format": protocol_contracts.OPERATION_FORMAT,
            "target_node_id": "fn-01",
            "scope": "node:fn-01",

            "base_authority_epoch": 3,
            "base_scope_revision": 9,
            "base_scope_fingerprint": "a" * 64,
            "reason": "test unchanged relay",
            "operations": copy.deepcopy(operations),
        },
    }
    result = api["relay_external_proposal"](envelope)
    assert result["ok"] is True, result
    assert result["worker_revision_assigned"] is False
    command = state["commands"][0]
    assert command["command"] == protocol_contracts.CONFIG_PROPOSE
    assert command["payload"]["operations"] == operations
    assert command["payload"]["base_authority_epoch"] == 3
    assert command["payload"]["base_scope_revision"] == 9
    assert command["payload"]["proposal_id"] == envelope["message_id"]
    assert command["payload"]["operation_id"] == envelope["message_id"]
    assert "authority_revision" not in command["payload"]
    assert command["payload"]["relay_identity"]["external_principal"] \
        == envelope["principal"]
    legacy = copy.deepcopy(envelope)
    legacy["message_id"] = "external-proposal-legacy"
    legacy["payload"].pop("operations")
    legacy["payload"].pop("operation_format")
    legacy["payload"]["changes"] = [{"path": "rules.0.enabled", "value": True}]
    rejected = api["relay_external_proposal"](legacy)
    assert rejected["ok"] is False
    assert rejected["code"] == "unsupported_operation_format"
    invalid_hash = copy.deepcopy(envelope)
    invalid_hash["message_id"] = "external-proposal-invalid-hash"
    invalid_hash["payload"]["base_scope_fingerprint"] = "A" * 64
    rejected_hash = api["relay_external_proposal"](invalid_hash)
    assert rejected_hash["ok"] is False
    assert rejected_hash["code"] == "invalid_payload"
    assert len(state["commands"]) == 1


def main():
    tests = (
        test_flat_node_snapshot_is_validated_and_acked_exactly,
        test_cross_owner_change_requires_the_next_execution_epoch,
        test_master_relay_preserves_canonical_operations_without_revision_assignment,
    )
    for test_function in tests:
        test_function()
        print("PASS " + test_function.__name__)
    print("PASS node_local Mirror/Relay suite ({} checks)".format(len(tests)))


if __name__ == "__main__":
    main()

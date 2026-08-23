# -*- coding: utf-8 -*-

"""Registry-only tests for controlled Authority transfer and restore."""

import copy
import json
import os
import sys

from functools import partial


PROJECT_ROOT = os.path.normpath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from src.core.authority_transition_orchestrator import (
    create_authority_transition_orchestrator,
)
from src.library import _generic_type_library as type_authority
from src.library._generic_system_contract_library import authority_flow_contracts
from src.library._generic_system_contract_library import persistence_contracts


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
        "authority_term": 11,
    }


def _save_transfer(state, record, **fence):
    state["transfers"][record["transfer_id"]] = copy.deepcopy(record)
    state["fences"].append(copy.deepcopy(fence))
    return {"ok": True, "record": copy.deepcopy(record)}


def _save_restore(state, record, **fence):
    state["restores"][record["restore_id"]] = copy.deepcopy(record)
    state["fences"].append(copy.deepcopy(fence))
    return {"ok": True, "record": copy.deepcopy(record)}


def _stage_command(state, command):
    state["commands"].append(copy.deepcopy(command))
    return {"ok": True, "message_id": command["message_id"]}


def _mirror_reader(state, scope):
    return copy.deepcopy(state["mirrors"].get(str(scope)))


def _orchestrator(state):
    return create_authority_transition_orchestrator(
        site_id="site-main",
        instance_id="master-a",
        authority_reader=_authority_reader,
        store={
            "save_authority_transfer": partial(_save_transfer, state),
            "save_worker_restore": partial(_save_restore, state),
        },
        mirror_reader=partial(_mirror_reader, state),
        stage_worker_command=partial(_stage_command, state),
    )


def _state(snapshot):
    return {
        "transfers": {},
        "restores": {},
        "fences": [],
        "commands": [],
        "mirrors": {
            snapshot["scope"]: {
                "scope": snapshot["scope"],
                "authority_epoch": snapshot["authority_epoch"],
                "scope_revision": snapshot["scope_revision"],
                "execution_epoch": snapshot["execution_epoch"],
                "fingerprint": snapshot["fingerprint"],
                "snapshot": copy.deepcopy(snapshot),
            },
        },
    }


def test_registry_transfer_waits_for_exact_worker_ack():
    master_config = _load_config()
    node_config = copy.deepcopy(master_config)
    node_config["meta"][0]["description"] = "Node-selected Registry truth"
    master_snapshot = persistence_contracts.make_scope_snapshot(
        master_config,
        scope="node:fn-01",
        authority_epoch=2,
        scope_revision=7,
        execution_epoch=0,
    )
    node_snapshot = persistence_contracts.make_scope_snapshot(
        node_config,
        scope="node:fn-01",
        authority_epoch=2,
        scope_revision=7,
        execution_epoch=0,
    )
    state = _state(node_snapshot)
    api = _orchestrator(state)
    scope_head = authority_flow_contracts.make_scope_head(
        "node:fn-01",
        authority_mode="master_global",
        authority_epoch=2,
        scope_revision=7,
        execution_epoch=0,
        fingerprint=master_snapshot["fingerprint"],
    )
    prepared = api["begin_transfer"]({
        "transfer_id": "registry-transfer-a5",
        "operator_confirmed": True,
        "operator_subject": "operator:test",
        "scope_head": scope_head,
        "master_snapshot": master_snapshot,
        "node_snapshot": node_snapshot,
        "source_truth": "node_local",
        "target_mode": "node_local",
    })
    assert prepared["ok"] is True, prepared
    transfer = prepared["transfer"]
    assert transfer["state"] == "awaiting_target_ack"
    assert len(state["commands"]) == 1
    command = state["commands"][0]
    assert command["command"] == "distributed.authority.transfer"
    assert command["payload"]["fingerprint"] == node_snapshot["fingerprint"]
    wrong = api["complete_transfer"](
        transfer,
        {
            "accepted": True,
            "authority_epoch": transfer["target_authority_epoch"],
            "fingerprint": "0" * 64,
        },
    )
    assert wrong["ok"] is False
    assert wrong["code"] == "invalid_ack"
    completed = api["complete_transfer"](
        transfer,
        {
            "accepted": True,
            "authority_epoch": transfer["target_authority_epoch"],
            "fingerprint": node_snapshot["fingerprint"],
        },
    )
    assert completed["ok"] is True, completed
    assert completed["transfer"]["state"] == "completed"
    assert completed["transfer"]["scope_head"]["locked"] is False
    assert completed["transfer"]["scope_head"]["authority_mode"] == "node_local"
    assert all(fence["authority_term"] == 11 for fence in state["fences"])


def test_registry_restore_stages_preflight_only_after_sem_ack():
    config = _load_config()
    snapshot = persistence_contracts.make_scope_snapshot(
        config,
        scope="node:fn-01",
        authority_epoch=5,
        scope_revision=12,
        execution_epoch=0,
    )
    state = _state(snapshot)
    api = _orchestrator(state)
    started = api["begin_restore"]({
        "restore_id": "registry-restore-a6",
        "scope": "node:fn-01",
        "target_node_id": "fn-01",
        "operator_confirmed": True,
        "operator_subject": "operator:test",
    })
    assert started["ok"] is True, started
    assert started["restore"]["state"] == "quarantined"
    prepared = api["prepare_restore"](
        started["restore"],
        {
            "selected_source": "master_mirror",
            "snapshot": snapshot,
            "local_scope_revision": 12,
        },
    )
    assert prepared["ok"] is True, prepared
    restore = prepared["restore"]
    assert state["commands"][-1]["command"] == "distributed.restore.request"
    invalid_ack = api["complete_restore_activation"](
        restore,
        {
            "ok": True,
            "authority_epoch": restore["target_authority_epoch"],
            "fingerprint": snapshot["fingerprint"],
            "hardware_writes_enabled": True,
        },
    )
    assert invalid_ack["ok"] is False
    activated = api["complete_restore_activation"](
        restore,
        {
            "ok": True,
            "authority_epoch": restore["target_authority_epoch"],
            "fingerprint": snapshot["fingerprint"],
            "hardware_writes_enabled": False,
            "sem_commit_id": "sem-restore-a6",
        },
    )
    assert activated["ok"] is True, activated
    assert state["commands"][-1]["command"] == "distributed.restore.activate"
    completed = api["complete_restore_preflight"](
        activated["restore"],
        {
            "ok": True,
            "authority_epoch": restore["target_authority_epoch"],
            "preflight_ok": True,
            "hardware_writes_enabled": True,
        },
    )
    assert completed["ok"] is True, completed
    assert completed["restore"]["state"] == "hardware_enabled"


def main():
    tests = (
        test_registry_transfer_waits_for_exact_worker_ack,
        test_registry_restore_stages_preflight_only_after_sem_ack,
    )
    for test_function in tests:
        test_function()
        print("PASS " + test_function.__name__)
    print("PASS node_local Authority transition suite ({} checks)".format(
        len(tests)))


if __name__ == "__main__":
    main()

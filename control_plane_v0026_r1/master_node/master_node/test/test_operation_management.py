# -*- coding: utf-8 -*-

"""Functional tests for fenced actuator routing and deterministic commands."""

import copy
import datetime
import json
import os
import uuid

from src.library._generic_system_contract_library.protocol_contracts import make_envelope
from src.core.operation_management import _resolve_actuator, create_operation_management


def _fixture(synchronised=True, contract_version=9, replica_revision=4):
    parts = [os.path.dirname(__file__), "..", "_config", "application"]
    if contract_version < 8:
        parts.extend(("_legacy", "initial_config.json"))
    else:
        parts.append("initial_config_v9.json")
    path = os.path.abspath(os.path.join(*parts))
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    controller_uuid = config["actuators"][0]["controller_uuid"]
    first_actuator = config["actuators"][0]
    actuator_id = first_actuator["rows"][0]["actuator_id"] \
        if isinstance(first_actuator.get("rows"), list) \
        else first_actuator["actuator_id"]
    authority = {
        "lease_active": True,
        "leader_instance_id": "master-01",
        "lease_token": str(uuid.uuid4()),
        "authority_term": 7,
        "config_revision": 11,
    }
    operations = {}

    def create(request):
        operation = copy.deepcopy(request)
        operation["operation_id"] = str(uuid.uuid4())
        operation["state"] = "RECEIVED"
        operations[operation["operation_id"]] = operation
        return {"ok": True, "duplicate": False, "operation": copy.deepcopy(operation)}

    def transition(operation_id, state, **_kwargs):
        operations[operation_id]["state"] = state
        return {"ok": True, "operation": copy.deepcopy(operations[operation_id])}

    store = {
        "create": create,
        "transition": transition,
        "get": operations.get,
        "list": unexpected_call,
        "apply_worker_result": unexpected_call,
    }

    def authority_reader():
        return copy.deepcopy(authority)

    def config_reader():
        return {"revision": 11, "config_data": copy.deepcopy(config)}

    def resolve(_controller_uuid):
        return {
            "owner_node_id": "fn-01",
            "lease_token": str(uuid.uuid4()),
        }

    def scope_reader(scope):
        assert scope == "node:fn-01"
        return {
            "scope_revision": 4,
            "fingerprint": "b" * 64,
            "execution_epoch": 6,
            "global_revision": 11,
        }

    def node_reader(node_id):
        assert node_id == "fn-01"
        return {
            "node_id": node_id,
            "admission_state": "APPROVED",
            "desired_state": "ACTIVE",
            "observed_state": "ACTIVE" if synchronised else "DEGRADED",
            "observed_global_revision": 11,
            "node_replica_scope": "node:fn-01",
            "node_replica_revision": replica_revision,
            "node_replica_fingerprint": "b" * 64,
        }

    management = create_operation_management(
        site_id="site_main", instance_id="master-01",
        legacy_coordinator_node_id="master-01",
        authority_reader=authority_reader,
        config_reader=config_reader,
        ownership={"resolve": resolve},
        store=store,
        scope_reader=scope_reader,
        node_reader=node_reader,
    )
    return management, controller_uuid, actuator_id


def unexpected_call(*_args, **_kwargs):
    raise AssertionError("unexpected fake capability call")


def _test_fenced_worker_command(contract_version=9):
    management, controller_uuid, actuator_id = _fixture(
        contract_version=contract_version)
    envelope = make_envelope(
        "actuator.request",
        {"service": "proxy", "instance_id": "proxy-01"},
        {"service": "master", "instance_id": "master-01"},
        site_id="site_main",
        idempotency_key="actuator-test-01",
        principal={"subject": str(uuid.uuid4())},
        payload={
            "controller_uuid": controller_uuid,
            "actuator_id": actuator_id,
            "value": True,
            "idempotent": False,
            "device_command_id_supported": False,
        },
        now=datetime.datetime.now(datetime.timezone.utc),
    )
    result = management["request"](envelope)
    assert result["ok"]
    assert result["operation"]["state"] == "ROUTED"
    command = result["worker_command"]
    assert command["target_node"] == "fn-01"
    assert command["command"] == "distributed.actuator.execute"
    assert command["payload"]["coordinator_term"] == 7
    assert command["payload"]["authority_revision"] == 11
    assert command["payload"]["require_hardware_readback"] is True
    assert command["payload"]["target_node_scope_revision"] == 4
    assert command["payload"]["target_node_execution_epoch"] == 6


def _test_v2_protocol_and_target_scope_fence():
    management, controller_uuid, actuator_id = _fixture()
    envelope = make_envelope(
        "actuator.request",
        {"service": "worker", "instance_id": "fn-01"},
        {"service": "master", "instance_id": "master-01"},
        site_id="site_main", idempotency_key="actuator-test-v2",
        principal={"subject": "worker:fn-01"},
        payload={
            "controller_uuid": controller_uuid,
            "actuator_id": actuator_id,
            "value": True,
            "execution_scope": "cross:fn-01.default",
            "execution_epoch": 3,
            "distributed_protocol": "automation.distributed.v2",
        },
        now=datetime.datetime.now(datetime.timezone.utc),
    )
    result = management["request"](envelope)
    assert result["ok"]
    payload = result["worker_command"]["payload"]
    assert payload["protocol"] == "automation.distributed.v2"
    assert payload["target_node_scope_revision"] == 4
    assert payload["target_node_scope_fingerprint"] == "b" * 64
    assert payload["target_node_execution_epoch"] == 6


def _test_unsynchronised_owner_is_rejected_before_outbox_creation():
    management, controller_uuid, actuator_id = _fixture(synchronised=False)
    envelope = make_envelope(
        "actuator.request",
        {"service": "proxy", "instance_id": "proxy-01"},
        {"service": "master", "instance_id": "master-01"},
        site_id="site_main", idempotency_key="actuator-not-ready",
        principal={"subject": str(uuid.uuid4())},
        payload={
            "controller_uuid": controller_uuid,
            "actuator_id": actuator_id,
            "value": True,
        },
        now=datetime.datetime.now(datetime.timezone.utc),
    )
    result = management["request"](envelope)
    assert not result["ok"]
    assert result["code"] == "worker_not_synchronised"


def _test_matching_fingerprint_with_old_revision_is_rejected():
    management, controller_uuid, actuator_id = _fixture(replica_revision=3)
    envelope = make_envelope(
        "actuator.request",
        {"service": "proxy", "instance_id": "proxy-01"},
        {"service": "master", "instance_id": "master-01"},
        site_id="site_main", idempotency_key="actuator-old-replica-revision",
        principal={"subject": str(uuid.uuid4())},
        payload={
            "controller_uuid": controller_uuid,
            "actuator_id": actuator_id,
            "value": True,
        },
        now=datetime.datetime.now(datetime.timezone.utc),
    )
    result = management["request"](envelope)
    assert not result["ok"]
    assert result["code"] == "worker_not_synchronised"


def _test_v9_composite_identity_and_fail_closed_lookup():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application",
        "initial_config_v9.json"))
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    duplicate_local_ids = [
        row for row in config["actuators"]
        if str(row.get("actuator_id")) == "1"
    ]
    assert len(duplicate_local_ids) >= 2
    first, second = duplicate_local_ids[:2]
    first_match = _resolve_actuator(
        config, first["controller_uuid"], first["actuator_id"])
    second_match = _resolve_actuator(
        config, second["controller_uuid"], second["actuator_id"])
    assert first_match["address"] == first["address"]
    assert second_match["address"] == second["address"]
    assert first_match["address"] != second_match["address"]
    disabled = copy.deepcopy(config)
    disabled["actuators"][0]["enabled"] = False
    assert _resolve_actuator(
        disabled, first["controller_uuid"], first["actuator_id"]) is None
    assert _resolve_actuator(config, first["controller_uuid"], "missing") is None


def main():
    _test_fenced_worker_command()
    _test_fenced_worker_command(contract_version=5)
    _test_v2_protocol_and_target_scope_fence()
    _test_unsynchronised_owner_is_rejected_before_outbox_creation()
    _test_matching_fingerprint_with_old_revision_is_rejected()
    _test_v9_composite_identity_and_fail_closed_lookup()
    print("PASS canonical V9 and grouped legacy actuator routing (6 checks)")


if __name__ == "__main__":
    main()

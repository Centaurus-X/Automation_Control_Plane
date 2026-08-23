# -*- coding: utf-8 -*-

"""Major-2 master_global Node Full synchronization must not depend on Cross runtime."""

import copy
import json
import os

from src.core.rollout_orchestrator import create_rollout_orchestrator
from src.core.shard_materializer import create_shard_materializer


def _config():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application",
        "initial_config_v9.json"))
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _cross_disabled_config():
    config = _config()
    config["rules"] = [
        rule for rule in config["rules"]
        if rule.get("deployment_status") != "active_cross"
    ]
    return config


def _empty_authority():
    return {}


def _missing_node(_node_id):
    return None


def _major2_full_worker():
    return {
        "node_id": "fn-01",
        "protocol_version": "automation.distributed.v2",
        "protocol_versions": ["automation.distributed.v2"],
        "contracts_version": "2.0",
        "node_replica_revision": 0,
        "node_replica_fingerprint": "",
        "max_packet_size": 1048576,
        "capabilities": {"features": {
            "config_scope_operations_v1": True,
            "node_full": True,
            "node_patch": False,
            "cross_runtime": False,
            "cross_full": False,
            "master_automation_execution": False,
        }},
    }

def _test_cross_disabled_materialization_and_full_plan():
    materialized = create_shard_materializer(profile="lab_v2")["materialize"](
        _cross_disabled_config(), 1)
    assert set(materialized["scopes"]) == {"node:fn-01", "node:fn-02"}
    snapshot = materialized["scopes"]["node:fn-01"]
    rollout = {
        "rollout_id": "rollout:1:node:fn-01:1",
        "node_id": "fn-01",
        "scope": "node:fn-01",
        "detail": {},
    }
    api = create_rollout_orchestrator(
        site_id="site_main", instance_id="master-01",
        coordinator_node_id="master-01", command_ttl_s=60,
        authority_reader=_empty_authority, node_reader=_missing_node,
        authority_store={}, entity_store={},
    )
    planned = api["plan"](
        rollout, _major2_full_worker(), snapshot,
        {"authority_term": 1, "config_fingerprint": "a" * 64},
        operations=(), now_s=10.0,
    )
    assert planned["ok"] and planned["status"] == "full"
    assert len(planned["commands"]) == 1
    command = planned["commands"][0]
    assert command["command"] == "distributed.config.apply"
    assert command["payload"]["shard_scope"] == "node_local"
    assert command["payload"]["scope_id"] == "node:fn-01"
    assert command["payload"]["protocol"] == "automation.distributed.v2"
    simulator = next(
        row for row in command["payload"]["config"]["virtual_controllers"]
        if row["controller_uuid"] == "62671112-766c-59ed-b0ff-30a49d48f747")
    assert (simulator["host"], simulator["port"]) == ("192.168.0.3", 5020)


def _test_cross_retry_does_not_block_node_staging():
    materialized = create_shard_materializer(profile="lab_v2")["materialize"](
        _config(), 1)
    scopes = materialized["scopes"]
    due = ({
        "rollout_id": "rollout:1:node:fn-01:1",
        "node_id": "fn-01", "scope": "node:fn-01",
        "target_scope_revision": 1, "detail": {},
    }, {
        "rollout_id": "rollout:1:cross:fn-01.default:1",
        "node_id": "fn-01", "scope": "cross:fn-01.default",
        "target_scope_revision": 1, "detail": {},
    })
    staged_commands = []
    transitions = {}

    def authority_reader():
        return {
            "authority_term": 1,
            "lease_token": "lease-01",
            "config_fingerprint": materialized["global_fingerprint"],
        }

    def node_reader(_node_id):
        return _major2_full_worker()

    def due_rollouts(**_kwargs):
        return copy.deepcopy(due)

    def get_scope(scope, **_kwargs):
        return copy.deepcopy(scopes.get(scope))

    def get_scope_ops(_scope, _base_revision, _target_revision):
        return ()

    def mark_rollout(rollout_id, **kwargs):
        transitions[rollout_id] = copy.deepcopy(kwargs)
        return True

    def enqueue_outbox(record, **_kwargs):
        staged_commands.append(copy.deepcopy(record))
        return {"ok": True}

    entity_store = {
        "due_rollouts": due_rollouts,
        "get_scope": get_scope,
        "get_scope_ops": get_scope_ops,
        "mark_rollout": mark_rollout,
    }
    authority_store = {"enqueue_outbox": enqueue_outbox}
    api = create_rollout_orchestrator(
        site_id="site_main", instance_id="master-01",
        coordinator_node_id="master-01", command_ttl_s=60,
        authority_reader=authority_reader, node_reader=node_reader,
        authority_store=authority_store, entity_store=entity_store,
    )
    assert api["stage_due"](limit=64) == 1
    assert [row["event_type"] for row in staged_commands] == [
        "distributed.config.apply"]
    assert transitions["rollout:1:node:fn-01:1"]["state"] == "sending"
    cross = transitions["rollout:1:cross:fn-01.default:1"]
    assert cross["state"] == "retry_wait"
    assert cross["detail"]["code"] == "cross_capability"


def main():
    _test_cross_disabled_materialization_and_full_plan()
    _test_cross_retry_does_not_block_node_staging()
    print("PASS Major-2 master_global Node Full sync is independent of Cross configuration")


if __name__ == "__main__":
    main()

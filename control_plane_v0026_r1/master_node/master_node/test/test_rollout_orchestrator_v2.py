# -*- coding: utf-8 -*-

"""Capability-gated Major-2 Full/Patch/Chunk planning tests."""

import copy
import json
import os

from src.library._generic_type_library import canonical_bytes, fingerprint
from src.library._generic_system_contract_library.protocol_contracts import make_worker_command
from src.core.rollout_orchestrator import create_rollout_orchestrator, handle_rollout_ack
from src.core.shard_materializer import create_shard_materializer


def _snapshot():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application", "initial_config_v9.json"))
    with open(path, "r", encoding="utf-8") as handle:
        source = json.load(handle)
    return create_shard_materializer()["materialize"](
        source, 7)["scopes"]["node:fn-01"]


def _api(max_packet_size=4 * 1024 * 1024):
    return create_rollout_orchestrator(
        site_id="site_main", instance_id="master-01",
        coordinator_node_id="master-01", command_ttl_s=60,
        max_packet_size=max_packet_size,
        authority_reader=lambda_forbidden, node_reader=lambda_forbidden,
        authority_store={}, entity_store={},
    )


def lambda_forbidden(*_args, **_kwargs):
    return {}


def _rollout(snapshot):
    return {
        "rollout_id": "rollout:4:node:fn-01:1",
        "node_id": "fn-01", "scope": snapshot["scope"],
        "detail": {},
    }


def _test_patch_fallback_uses_a_stable_unique_outbox_identity():
    snapshot = _snapshot()
    snapshot["scope_revision"] = 2
    rollout = dict(
        _rollout(snapshot), target_scope_revision=2, state="staged")
    worker = {
        "node_id": "fn-01",
        "protocol_versions": [
            "automation.distributed.v1", "automation.distributed.v2"],
        "contracts_version": "2.0",
        "max_packet_size": 1048576,
        "node_replica_scope": "node:fn-01",
        "node_replica_revision": 1,
        "node_replica_fingerprint": "b" * 64,
        "capabilities": {"features": {
            "config_scope_operations_v1": True,
            "canonical_config_v9": True,
            "node_full": True,
            "node_patch": True,
            "chunk_reassembly": True,
            "master_automation_execution": False,
        }},
    }
    authority = {
        "authority_term": 4,
        "lease_token": "lease-04",
        "config_fingerprint": "f" * 64,
    }
    outbox = {}
    conflicts = []

    def authority_reader():
        return copy.deepcopy(authority)

    def node_reader(_node_id):
        return copy.deepcopy(worker)

    def due_rollouts(**_kwargs):
        return (copy.deepcopy(rollout),) if rollout["state"] in (
            "staged", "retry_wait") else ()

    def get_scope(_scope, **_kwargs):
        return copy.deepcopy(snapshot)

    def get_scope_ops(_scope, _base_revision, _target_revision):
        return ({
            "operation": "replace_field",
            "section": "rules",
            "entity_key": "rule:1",
            "json_pointer": "/payload/priority",
            "expected_value_hash": "c" * 64,
            "value": 101,
            "value_hash": "d" * 64,
        },)

    def mark_rollout(_rollout_id, **kwargs):
        rollout["state"] = kwargs["state"]
        rollout.setdefault("detail", {}).update(copy.deepcopy(
            kwargs.get("detail") or {}))
        return {"ok": True, "rollout": copy.deepcopy(rollout)}

    def enqueue_outbox(record, **_kwargs):
        message_id = record["message_id"]
        previous = outbox.get(message_id)
        if previous is not None and previous != record:
            conflicts.append(message_id)
            return {"ok": False, "code": "conflict"}
        outbox[message_id] = copy.deepcopy(record)
        return {
            "ok": True,
            "duplicate": previous is not None,
            "state": "PENDING",
        }

    entity_store = {
        "due_rollouts": due_rollouts,
        "get_scope": get_scope,
        "get_scope_ops": get_scope_ops,
        "mark_rollout": mark_rollout,
    }
    api = create_rollout_orchestrator(
        site_id="site_main", instance_id="master-01",
        coordinator_node_id="master-01", command_ttl_s=60,
        authority_reader=authority_reader, node_reader=node_reader,
        authority_store={"enqueue_outbox": enqueue_outbox},
        entity_store=entity_store,
    )

    rollout_id = rollout["rollout_id"]
    assert api["stage_due"]() == 1
    patch = outbox[rollout_id]
    assert patch["event_type"] == "distributed.config.patch"
    assert api["handle_ack"](
        patch["envelope"], {"ok": False, "code": "base_scope_revision"})
    assert rollout["state"] == "retry_wait"
    assert rollout["detail"]["force_full"] is True

    assert api["stage_due"]() == 1
    fallback_id = rollout_id + ":fallback-full"
    assert set(outbox) == {rollout_id, fallback_id}
    assert outbox[fallback_id]["event_type"] == "distributed.config.apply"
    assert outbox[fallback_id]["envelope"]["message_id"] == fallback_id
    assert not conflicts
    assert api["stage_due"]() == 0
    assert set(outbox) == {rollout_id, fallback_id}
    assert not conflicts


def _test_real_worker_patch_rejections_force_full():
    transitions = []

    def mark_rollout(rollout_id, **kwargs):
        transitions.append((rollout_id, copy.deepcopy(kwargs)))
        return {"ok": True}

    context = {"entity_store": {"mark_rollout": mark_rollout}}
    command = {"rollout_id": "rollout:4:node:fn-01:2"}
    for code in (
            "base_scope_revision", "base_scope_fingerprint", "patch_operations"):
        assert handle_rollout_ack(
            context, command, {"ok": False, "code": code})
        _, detail = transitions[-1]
        assert detail["state"] == "retry_wait"
        assert detail["detail"]["force_full"] is True
        assert detail["detail"]["code"] == code

    assert handle_rollout_ack(
        context, command, {"ok": False, "code": "scope"})
    _, detail = transitions[-1]
    assert detail["state"] == "failed"
    assert detail["detail"]["force_full"] is False

def _test_terminal_outbox_is_not_reopened():
    snapshot = _snapshot()
    rollout = dict(
        _rollout(snapshot),
        target_scope_revision=snapshot["scope_revision"],
        state="staged",
    )
    worker = {
        "node_id": "fn-01",
        "protocol_version": "automation.distributed.v2",
        "contracts_version": "2.0",
        "max_packet_size": 1048576,
        "node_replica_revision": 2,
        "node_replica_fingerprint": "b" * 64,
        "capabilities": {"features": {
            "config_scope_operations_v1": True,
            "node_full": True,
            "node_patch": False,
            "master_automation_execution": False,
        }},
    }
    transitions = []

    def mark_rollout(_rollout_id, **kwargs):
        transitions.append(copy.deepcopy(kwargs))
        rollout["state"] = kwargs["state"]
        return {"ok": True, "rollout": copy.deepcopy(rollout)}

    def due_rollouts(**_kwargs):
        return (copy.deepcopy(rollout),)

    def get_scope(_scope, **_kwargs):
        return copy.deepcopy(snapshot)

    def get_scope_ops(*_args, **_kwargs):
        return ()

    def authority_reader():
        return {
            "authority_term": 4,
            "lease_token": "lease-04",
            "config_fingerprint": "f" * 64,
        }

    def node_reader(_node_id):
        return copy.deepcopy(worker)

    def enqueue_outbox(*_args, **_kwargs):
        return {
            "ok": True,
            "duplicate": True,
            "state": "FAILED",
            "last_error_code": "stale_replica",
        }

    entity_store = {
        "due_rollouts": due_rollouts,
        "get_scope": get_scope,
        "get_scope_ops": get_scope_ops,
        "mark_rollout": mark_rollout,
    }
    api = create_rollout_orchestrator(
        site_id="site_main",
        instance_id="master-01",
        coordinator_node_id="master-01",
        command_ttl_s=60,
        authority_reader=authority_reader,
        node_reader=node_reader,
        authority_store={"enqueue_outbox": enqueue_outbox},
        entity_store=entity_store,
    )
    assert api["stage_due"]() == 0
    assert transitions[-1]["state"] == "failed"
    assert transitions[-1]["detail"] == {
        "code": "terminal_outbox_reuse",
        "outbox_state": "FAILED",
        "last_error_code": "stale_replica",
    }


def _test_rollout_fingerprint_is_bound_to_its_persisted_revision():
    """A pre-commit authority read must not contaminate a post-commit rollout."""
    snapshot = _snapshot()
    snapshot["global_revision"] = 8
    rollout = dict(
        _rollout(snapshot),
        target_scope_revision=snapshot["scope_revision"],
        state="staged",
    )
    worker = {
        "node_id": "fn-01",
        "protocol_version": "automation.distributed.v2",
        "contracts_version": "2.0",
        "max_packet_size": 1048576,
        "node_replica_revision": 0,
        "node_replica_fingerprint": "",
        "capabilities": {"features": {
            "config_scope_operations_v1": True,
            "node_full": True,
            "node_patch": False,
            "master_automation_execution": False,
        }},
    }
    staged = []
    transitions = []

    def authority_reader():
        # Simulates the publisher reading revision 7 immediately before the
        # revision-8 transaction becomes visible while it waits for the row.
        return {
            "authority_term": 4,
            "lease_token": "lease-04",
            "config_revision": 7,
            "config_fingerprint": "7" * 64,
        }

    def config_reader(**kwargs):
        assert kwargs == {"revision": 8}
        return {
            "global_revision": 8,
            "revision": 8,
            "fingerprint": "8" * 64,
            "config_data": {},
        }

    def mark_rollout(_rollout_id, **kwargs):
        transitions.append(copy.deepcopy(kwargs))
        rollout["state"] = kwargs["state"]
        return {"ok": True, "rollout": copy.deepcopy(rollout)}

    def due_rollouts(**_kwargs):
        return (copy.deepcopy(rollout),)

    def get_scope(*_args, **_kwargs):
        return copy.deepcopy(snapshot)

    def get_scope_ops(*_args, **_kwargs):
        return ()

    def node_reader(_node_id):
        return copy.deepcopy(worker)

    def enqueue_outbox(record, **_kwargs):
        staged.append(copy.deepcopy(record))
        return {"ok": True}

    entity_store = {
        "due_rollouts": due_rollouts,
        "get_scope": get_scope,
        "get_scope_ops": get_scope_ops,
        "mark_rollout": mark_rollout,
    }
    api = create_rollout_orchestrator(
        site_id="site_main", instance_id="master-01",
        coordinator_node_id="master-01", command_ttl_s=60,
        authority_reader=authority_reader, config_reader=config_reader,
        node_reader=node_reader,
        authority_store={"enqueue_outbox": enqueue_outbox},
        entity_store=entity_store,
    )
    assert api["stage_due"]() == 1
    payload = staged[0]["envelope"]["payload"]
    assert payload["authority_revision"] == 8
    assert payload["global_revision"] == 8
    assert payload["authority_fingerprint"] == "8" * 64
    assert payload["authority_fingerprint"] != "7" * 64
    assert transitions[-1]["state"] == "sending"


def main():
    api = _api()
    snapshot = _snapshot()
    authority = {"authority_term": 4, "config_fingerprint": "f" * 64}
    legacy = {
        "node_id": "fn-01", "protocol_version": "automation.distributed.v2",
        "contracts_version": "2.0", "max_packet_size": 1048576,
        "node_replica_revision": 0, "node_replica_fingerprint": "",
        "capabilities": {"features": {
            "config_scope_operations_v1": True,
            "node_full": True, "node_patch": False,
            "master_automation_execution": False,
        }},
    }
    full = api["plan"](
        _rollout(snapshot), legacy, snapshot, authority, operations=(), now_s=10.0)
    assert full["ok"] and full["status"] == "full"
    command = full["commands"][0]
    assert command["command"] == "distributed.config.apply"
    assert command["payload"]["protocol"] == "automation.distributed.v2"
    assert command["payload"]["shard_fingerprint"] == fingerprint(snapshot["config"])
    envelope = make_worker_command(
        "fn-01", command["command"], site_id="site_main",
        source_node="master-01", message_id=command["message_id"],
        ttl_s=60, now_s=10.0, payload=command["payload"],
    )
    serialized_bytes = len(canonical_bytes(envelope))
    assert serialized_bytes > 8192
    assert serialized_bytes <= legacy["max_packet_size"]

    constrained = _api(max_packet_size=8192)["plan"](
        _rollout(snapshot), legacy, snapshot, authority,
        operations=(), now_s=10.0)
    assert constrained["ok"] is False
    assert constrained["code"] == "packet_limit"
    assert constrained["serialized_bytes"] == serialized_bytes
    assert constrained["master_limit_bytes"] == 8192
    assert constrained["worker_limit_bytes"] == 1048576
    assert constrained["effective_limit_bytes"] == 8192
    assert constrained["chunk_reassembly"] is False

    modern = copy.deepcopy(legacy)
    modern.update({
        "protocol_versions": ["automation.distributed.v1", "automation.distributed.v2"],
        "contracts_version": "2.0", "node_replica_scope": "node:fn-01",
        "node_replica_revision": 1, "node_replica_fingerprint": "b" * 64,
    })
    modern["capabilities"]["features"].update({
        "canonical_config_v9": True,
        "node_patch": True, "chunk_reassembly": True,
    })
    same_content_old_cursor = copy.deepcopy(modern)
    same_content_old_cursor.update({
        "node_replica_revision": 0,
        "node_replica_fingerprint": snapshot["fingerprint"],
    })
    revision_sync = api["plan"](
        _rollout(snapshot), same_content_old_cursor, snapshot, authority,
        operations=(), now_s=10.0)
    assert revision_sync["ok"] and revision_sync["status"] == "full"
    assert revision_sync["commands"][0]["payload"][
        "target_scope_revision"] == snapshot["scope_revision"]

    exact_replica = copy.deepcopy(same_content_old_cursor)
    exact_replica["node_replica_revision"] = snapshot["scope_revision"]
    already_current = api["plan"](
        _rollout(snapshot), exact_replica, snapshot, authority,
        operations=(), now_s=10.0)
    assert already_current["ok"] and already_current["status"] == "already_current"
    assert already_current["commands"] == ()

    target = copy.deepcopy(snapshot)
    target["scope_revision"] = 2
    patch = api["plan"](
        _rollout(target), modern, target, authority,
        operations=({
            "operation": "replace_field", "section": "rules",
            "entity_key": "rule:1", "json_pointer": "/payload/priority",
            "value": 101, "expected_value_hash": "c" * 64,
            "value_hash": "d" * 64,
        },), now_s=10.0)
    assert patch["ok"] and patch["status"] == "patch"
    assert patch["commands"][0]["command"] == "distributed.config.patch"
    assert patch["commands"][0]["payload"]["scope_id"] == "node:fn-01"
    assert patch["commands"][0]["payload"]["scope"] == "node:fn-01"
    operation = patch["commands"][0]["payload"]["operations"][0]
    assert operation["expected_value_hash"] == "c" * 64
    assert operation["value_hash"] == "d" * 64

    huge = copy.deepcopy(target)
    huge["config"]["meta"][0]["description"] = "0123456789abcdef" * 12000
    huge["fingerprint"] = fingerprint(huge["config"])
    modern["max_packet_size"] = 65536
    chunked = api["plan"](
        _rollout(huge), modern, huge, authority,
        operations=None, now_s=10.0)
    assert chunked["ok"] and len(chunked["commands"]) > 1
    assert {item["command"] for item in chunked["commands"]} == {
        "distributed.config.chunk"}
    for item in chunked["commands"]:
        assert item["payload"]["coordinator_term"] == 4
        assert item["payload"]["protocol"] == "automation.distributed.v2"
        envelope = make_worker_command(
            "fn-01", item["command"], site_id="site_main",
            source_node="master-01", message_id=item["message_id"],
            ttl_s=60, now_s=10.0, payload=item["payload"],
        )
        assert len(canonical_bytes(envelope)) <= modern["max_packet_size"]
    _test_patch_fallback_uses_a_stable_unique_outbox_identity()
    _test_real_worker_patch_rejections_force_full()
    _test_terminal_outbox_is_not_reopened()
    _test_rollout_fingerprint_is_bound_to_its_persisted_revision()
    print("PASS v0082 realistic Full, packet-limit diagnostics and v2 Patch/Chunk planning")


if __name__ == "__main__":
    main()

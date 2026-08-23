# -*- coding: utf-8 -*-

"""Worker runtime loss replays one safe Node rollout with a new identity."""

import copy

from types import ModuleType

from src.library import master_entity_store_postgresql
from src.library.master_entity_store_postgresql import (
    _rollout_identity,
    _rollout_rearm_plan,
    _runtime_delivery_generation,
    converge_node_rollouts,
    ensure_node_rollouts,
)


TARGET_FINGERPRINT = "a" * 64


def _head():
    return {
        "scope": "node:fn-01",
        "scope_kind": "node",
        "owner_node_id": "fn-01",
        "scope_revision": 1,
        "fingerprint": TARGET_FINGERPRINT,
    }


def _node(runtime_nonce, revision=0, fingerprint=""):
    return {
        "site_id": "site_main",
        "node_id": "fn-01",
        "admission_state": "APPROVED",
        "desired_state": "ACTIVE",
        "runtime_nonce": runtime_nonce,
        "node_replica_revision": revision,
        "node_replica_fingerprint": fingerprint,
        "capabilities": {"features": {"node_full": True}},
    }


def _existing(runtime_nonce, **kwargs):
    generation = _runtime_delivery_generation(_node(runtime_nonce))
    return {
        "rollout_id": _rollout_identity(7, _head(), generation),
        "node_id": "fn-01",
        "scope": "node:fn-01",
        "target_scope_revision": 1,
        "target_fingerprint": TARGET_FINGERPRINT,
        "coordinator_term": kwargs.get("coordinator_term", 7),
        "state": kwargs.get("state", "converged"),
        "detail": {
            "delivery_generation": generation,
            "code": kwargs.get("code"),
        },
    }


def _test_runtime_rearm_is_safe_and_generation_unique():
    head = _head()
    existing = _existing("runtime-old")

    unchanged = _rollout_rearm_plan(
        existing, 7, _node("runtime-old"), head)
    assert unchanged is None

    replay = _rollout_rearm_plan(
        existing, 7, _node("runtime-new", revision=0, fingerprint=""), head)
    assert replay is not None
    assert replay["reason"] == "worker_runtime_replica_recovery"
    assert replay["rollout_id"] != existing["rollout_id"]
    assert replay["rollout_id"] == _rollout_identity(
        7, head, _runtime_delivery_generation(_node("runtime-new")))
    assert replay["detail"]["observed_replica_revision"] == 0
    assert replay["detail"]["previous_rollout_id"] == existing["rollout_id"]

    repeated = _rollout_rearm_plan(
        dict(existing, rollout_id=replay["rollout_id"], detail=replay["detail"]),
        7,
        _node("runtime-new", revision=0, fingerprint=""),
        head,
    )
    assert repeated is None

    divergent_equal_revision = _rollout_rearm_plan(
        existing,
        7,
        _node("runtime-new", revision=1, fingerprint="b" * 64),
        head,
    )
    assert divergent_equal_revision is None

    current = _rollout_rearm_plan(
        existing,
        7,
        _node("runtime-new", revision=1, fingerprint=TARGET_FINGERPRINT),
        head,
    )
    assert current is None

    legacy_terminal = copy.deepcopy(existing)
    legacy_terminal["rollout_id"] = _rollout_identity(7, head)
    legacy_terminal["detail"] = {}
    legacy_replay = _rollout_rearm_plan(
        legacy_terminal,
        7,
        _node("runtime-new", revision=0, fingerprint=""),
        head,
    )
    assert legacy_replay is not None
    assert legacy_replay["reason"] == "worker_runtime_replica_recovery"
    assert legacy_replay["rollout_id"] != legacy_terminal["rollout_id"]


def _result(row):
    result = ModuleType("worker_runtime_recovery_result")

    def fetchone():
        return copy.deepcopy(row)

    def fetchall():
        return copy.deepcopy(row)

    result.fetchone = fetchone
    result.fetchall = fetchall
    return result


def _test_ensure_rearms_terminal_rollout_and_resets_delivery_state():
    node = _node("runtime-new")
    head = _head()
    existing = _existing("runtime-old", state="failed", code="stale_replica")
    captured = {}
    connection = ModuleType("worker_runtime_recovery_connection")

    def execute(statement, parameters=None):
        if statement.startswith("SELECT * FROM master_control.nodes"):
            return _result(node)
        if statement.startswith("SELECT * FROM master_control.scope_heads"):
            return _result((head,))
        if statement.startswith("SELECT * FROM master_control.shard_rollouts"):
            return _result(existing)
        if statement.startswith("UPDATE master_control.shard_rollouts SET rollout_id"):
            captured["statement"] = statement
            captured["parameters"] = copy.deepcopy(parameters)
            return _result({"rollout_id": parameters[0]})
        raise AssertionError("unexpected SQL: " + statement)

    def transaction(operation, **_kwargs):
        return operation(connection)

    connection.execute = execute
    database = {"transaction": transaction}
    master_entity_store_postgresql.jsonb = copy.deepcopy

    assert ensure_node_rollouts(database, "fn-01", 7) == 1
    parameters = captured["parameters"]
    detail = parameters[3]
    assert parameters[0] != existing["rollout_id"]
    assert detail["rearm_reason"] == "worker_runtime_replica_recovery"
    assert detail["previous_code"] == "stale_replica"
    assert "attempts=0" in captured["statement"]
    assert "transfer_id=NULL" in captured["statement"]


def _test_matching_fingerprint_with_old_revision_still_requires_rollout():
    node = _node("runtime-current", revision=0, fingerprint=TARGET_FINGERPRINT)
    head = _head()
    captured = {}
    connection = ModuleType("worker_old_revision_connection")

    def execute(statement, parameters=None):
        if statement.startswith("SELECT * FROM master_control.nodes"):
            return _result(node)
        if statement.startswith("SELECT * FROM master_control.scope_heads"):
            return _result((head,))
        if statement.startswith("UPDATE master_control.shard_rollouts SET state='converged'"):
            raise AssertionError("old replica revision was treated as converged")
        if statement.startswith("SELECT * FROM master_control.shard_rollouts"):
            return _result(None)
        if statement.startswith("INSERT INTO master_control.shard_rollouts"):
            captured["parameters"] = copy.deepcopy(parameters)
            return _result({"rollout_id": parameters[0]})
        raise AssertionError("unexpected SQL: " + statement)

    connection.execute = execute

    def transaction(operation, **_kwargs):
        return operation(connection)

    database = {"transaction": transaction}
    master_entity_store_postgresql.jsonb = copy.deepcopy
    assert ensure_node_rollouts(database, "fn-01", 7) == 1
    assert captured["parameters"][3] == 1
    assert captured["parameters"][4] == TARGET_FINGERPRINT


def _test_convergence_query_binds_revision_and_fingerprint_pair():
    node = _node("runtime-current", revision=0, fingerprint=TARGET_FINGERPRINT)
    captured = {}
    connection = ModuleType("worker_convergence_pair_connection")

    def execute(statement, parameters=None):
        if statement.startswith("SELECT * FROM master_control.nodes"):
            return _result(node)
        if statement.startswith("UPDATE master_control.shard_rollouts r"):
            captured["statement"] = statement
            captured["parameters"] = copy.deepcopy(parameters)
            return _result(())
        raise AssertionError("unexpected SQL: " + statement)

    connection.execute = execute

    def transaction(operation, **_kwargs):
        return operation(connection)

    database = {"transaction": transaction}
    assert converge_node_rollouts(database, "fn-01") == 0
    assert "target_scope_revision=CASE" in captured["statement"]
    assert captured["parameters"][:3] == ("fn-01", 0, 0)
    assert captured["parameters"][-1] == TARGET_FINGERPRINT


def main():
    _test_runtime_rearm_is_safe_and_generation_unique()
    _test_ensure_rearms_terminal_rollout_and_resets_delivery_state()
    _test_matching_fingerprint_with_old_revision_still_requires_rollout()
    _test_convergence_query_binds_revision_and_fingerprint_pair()
    print("PASS Worker replica recovery and revision/fingerprint convergence gates")


if __name__ == "__main__":
    main()

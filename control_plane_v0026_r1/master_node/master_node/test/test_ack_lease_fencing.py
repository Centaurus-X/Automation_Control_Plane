# -*- coding: utf-8 -*-

"""Stale Master ACK paths must not mutate fenced or completed state."""

import copy
import inspect

from types import ModuleType

from src.library.authority_store_postgresql import mark_outbox
from src.library.master_entity_store_postgresql import (
    _rollout_rearm_allowed,
    _rollout_transition_allowed,
    due_rollouts,
    mark_rollout,
)
from src.library.operation_store_postgresql import transition_operation


def _empty_result():
    result = ModuleType("empty_database_result")

    def fetchone():
        return None

    result.fetchone = fetchone
    return result


def _row_result(row):
    result = ModuleType("database_row_result")

    def fetchone():
        return copy.deepcopy(row)

    result.fetchone = fetchone
    return result


def _rollout_database(state):
    statements = []
    record = {"rollout_id": "rollout-01", "state": state}
    connection = ModuleType("rollout_connection")

    def execute(statement, parameters=None):
        statements.append((statement, parameters))
        if statement.startswith("SELECT * FROM master_control.shard_rollouts"):
            return _row_result(record)
        if statement.startswith("UPDATE master_control.shard_rollouts"):
            record["state"] = parameters[0]
            return _row_result(record)
        raise AssertionError("unexpected rollout statement: " + statement)

    def transaction(operation, **_kwargs):
        return operation(connection)

    connection.execute = execute
    return {"transaction": transaction}, statements


def _expired_database():
    statements = []
    connection = ModuleType("expired_lease_connection")

    def execute(statement, parameters=None):
        statements.append((statement, parameters))
        if "authority_state" not in statement:
            raise AssertionError("mutation occurred after authority loss: " + statement)
        return _empty_result()

    def transaction(operation, **_kwargs):
        return operation(connection)

    connection.execute = execute
    return {"transaction": transaction}, statements


def _authority_fence():
    return {
        "site_id": "site_main",
        "instance_id": "master-01",
        "lease_token": "expired-lease",
        "authority_term": 7,
    }


def main():
    database, statements = _expired_database()
    assert mark_outbox(
        database, "message-01", state="ACKED", **_authority_fence()) is None
    assert len(statements) == 1

    database, statements = _expired_database()
    operation_fence = _authority_fence()
    operation_fence["leader_lease_token"] = operation_fence.pop("lease_token")
    operation_fence["config_revision"] = 11
    moved = transition_operation(
        database, "operation-01", "DISPATCHED", **operation_fence)
    assert not moved["ok"] and moved["code"] == "not_config_authority"
    assert len(statements) == 1

    database, statements = _expired_database()
    rollout = mark_rollout(
        database, "rollout-01", state="applied", **_authority_fence())
    assert not rollout["ok"] and rollout["code"] == "not_config_authority"
    assert len(statements) == 1

    for current in ("applied", "converged", "superseded"):
        for late in ("received", "failed"):
            database, statements = _rollout_database(current)
            rollout = mark_rollout(
                database, "rollout-01", state=late)
            assert rollout["ok"] and rollout["ignored"]
            assert rollout["rollout"]["state"] == current
            assert len(statements) == 1

    for late in (
            "staged", "sending", "received", "applied", "converged", "retry_wait"):
        database, statements = _rollout_database("failed")
        rollout = mark_rollout(database, "rollout-01", state=late)
        assert rollout["ok"] and rollout["ignored"]
        assert rollout["rollout"]["state"] == "failed"
        assert len(statements) == 1

    assert not _rollout_rearm_allowed({
        "state": "failed", "coordinator_term": 7,
        "detail": {"code": "stale_replica"},
    }, 7)
    assert _rollout_rearm_allowed({
        "state": "failed", "coordinator_term": 7,
        "detail": {"code": "target_missing"},
    }, 7)
    assert not _rollout_rearm_allowed({
        "state": "sending", "coordinator_term": 7, "detail": {},
    }, 7)
    assert _rollout_rearm_allowed({
        "state": "sending", "coordinator_term": 7, "detail": {},
    }, 8)
    due_source = inspect.getsource(due_rollouts)
    assert "state IN ('staged','retry_wait')" in due_source
    assert "state IN ('staged','sending','retry_wait')" not in due_source

    assert _rollout_transition_allowed("applied", "converged")
    assert _rollout_transition_allowed("converged", "superseded")

    print("PASS lease-fenced and monotone ACK/operation/rollout writes")


if __name__ == "__main__":
    main()

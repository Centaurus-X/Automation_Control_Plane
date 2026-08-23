# -*- coding: utf-8 -*-

"""Regression test for first-snapshot locking across the Mirror outer join."""

import copy

from functools import partial
from unittest import mock

from src.library.master_mirror_store_postgresql import persist_worker_mirror


def _fetch_cursor(value=None):
    cursor = mock.MagicMock()
    cursor.fetchone.return_value = copy.deepcopy(value)
    return cursor


def _execute(state, statement, _parameters=None):
    normalized = " ".join(str(statement).split())
    state["queries"].append(normalized)
    if "FROM master_control.authority_state" in normalized:
        return _fetch_cursor({
            "lease_active": True,
            "leader_instance_id": "master-a",
            "lease_token": "lease-a",
            "authority_term": 13,
        })
    if "LEFT JOIN master_control.worker_mirror_snapshots" in normalized:
        assert normalized.endswith("FOR UPDATE OF h")
        return _fetch_cursor()
    if normalized.startswith(
            "SELECT * FROM master_control.worker_mirror_snapshots"):
        return _fetch_cursor()
    if normalized.startswith(
            "INSERT INTO master_control.worker_mirror_heads"):
        return _fetch_cursor({
            "scope": "node:fn-01",
            "scope_kind": "node",
            "owner_node_id": "fn-01",
            "source_node_id": "fn-01",
            "authority_epoch": 0,
            "scope_revision": 0,
            "execution_epoch": 0,
            "fingerprint": "a" * 64,
            "contracts_version": "2.0",
            "type_schema_version": "9.0",
            "config_norm_version": {"major": 2},
        })
    return _fetch_cursor()


def _transaction(connection, operation, **_options):
    return operation(connection)


def _identity(value):
    return value


def test_initial_mirror_locks_only_the_non_nullable_head_row():
    state = {"queries": []}
    connection = mock.MagicMock()
    connection.execute.side_effect = partial(_execute, state)
    database = {
        "transaction": partial(_transaction, connection),
    }
    snapshot = {
        "scope": "node:fn-01",
        "authority_epoch": 0,
        "scope_revision": 0,
        "execution_epoch": 0,
        "fingerprint": "a" * 64,
        "config": {},
    }
    patcher = mock.patch(
        "src.library.master_mirror_store_postgresql.jsonb", _identity)
    patcher.start()
    result = persist_worker_mirror(
        database,
        {
            "mirror_id": "mirror-initial-node",
            "scope": "node:fn-01",
            "scope_kind": "node",
            "owner_node_id": "fn-01",
            "source_node_id": "fn-01",
            "authority_epoch": 0,
            "scope_revision": 0,
            "execution_epoch": 0,
            "fingerprint": "a" * 64,
            "contracts_version": "2.0",
            "type_schema_version": "9.0",
            "config_norm_version": {"major": 2},
            "snapshot": snapshot,
        },
        site_id="site-main",
        instance_id="master-a",
        lease_token="lease-a",
        authority_term=13,
    )
    patcher.stop()
    assert result["ok"] is True, result
    lock_queries = [
        query for query in state["queries"]
        if "LEFT JOIN master_control.worker_mirror_snapshots" in query
    ]
    assert len(lock_queries) == 1
    assert lock_queries[0].endswith("FOR UPDATE OF h")


def main():
    test_initial_mirror_locks_only_the_non_nullable_head_row()
    print("PASS initial Worker Mirror locks only its non-nullable head row")


if __name__ == "__main__":
    main()

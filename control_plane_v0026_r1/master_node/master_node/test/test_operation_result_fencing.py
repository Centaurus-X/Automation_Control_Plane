# -*- coding: utf-8 -*-

"""Terminal actuator results remain fenced and content-idempotent."""

import copy

from types import ModuleType

import src.library.operation_store_postgresql as operation_store


PERSISTED_RESULT = {
    "success": True,
    "readback_checked": True,
    "readback_verified": True,
    "readback_value": True,
}


def _result(one=None):
    value = ModuleType("database_result")

    def fetchone():
        return copy.deepcopy(one)

    value.fetchone = fetchone
    return value


def _database(operation, authority_term):
    statements = []

    def execute(statement, parameters=None):
        statements.append((statement, copy.deepcopy(parameters)))
        if statement.startswith("SELECT *,lease_until>clock_timestamp()"):
            return _result({
                "lease_active": True,
                "leader_instance_id": "master-01",
                "lease_token": "lease-01",
                "authority_term": authority_term,
                "config_revision": 11,
            })
        if statement.startswith(
                "SELECT * FROM master_control.actuator_operations WHERE operation_id"):
            return _result(operation)
        raise AssertionError("terminal duplicate must not mutate SQL state: " + statement)

    connection = ModuleType("database_connection")
    connection.execute = execute

    def transaction(callback, **_kwargs):
        return callback(connection)

    return {"transaction": transaction}, statements


def _operation():
    return {
        "operation_id": "operation-01",
        "site_id": "site_main",
        "owner_node_id": "fn-01",
        "authority_term": 7,
        "config_revision": 11,
        "state": "CONFIRMED",
        "result": copy.deepcopy(PERSISTED_RESULT),
    }


def _request():
    return {
        "site_id": "site_main",
        "instance_id": "master-01",
        "leader_lease_token": "lease-01",
        "operation_id": "operation-01",
        "source_node_id": "fn-01",
        "authority_term": 7,
        "config_revision": 11,
        "terminal_state": "CONFIRMED",
        "result": copy.deepcopy(PERSISTED_RESULT),
    }


def _apply(request, authority_term):
    database, statements = _database(_operation(), authority_term)
    result = operation_store.apply_worker_result(database, request)
    assert len(statements) == 2
    return result


def _test_terminal_duplicate_rechecks_source_term_and_revision():
    cases = (
        ("source_node_id", "fn-02", 7),
        ("authority_term", 8, 8),
        ("config_revision", 12, 7),
    )
    for field, value, active_term in cases:
        request = _request()
        request[field] = value
        result = _apply(request, active_term)
        assert not result["ok"], field
        assert result["code"] == "identity_mismatch", field


def _test_terminal_duplicate_rejects_conflicting_content():
    request = _request()
    request["result"]["readback_value"] = False
    result = _apply(request, 7)
    assert not result["ok"]
    assert result["code"] == "conflict"
    assert result["state"] == "CONFIRMED"


def _test_exact_terminal_duplicate_is_idempotent():
    result = _apply(_request(), 7)
    assert result["ok"] and result["duplicate"]
    assert result["operation"]["state"] == "CONFIRMED"
    assert result["operation"]["result"] == PERSISTED_RESULT


def main():
    _test_terminal_duplicate_rechecks_source_term_and_revision()
    _test_terminal_duplicate_rejects_conflicting_content()
    _test_exact_terminal_duplicate_is_idempotent()
    print("PASS terminal operation result fencing and content idempotency (5 checks)")


if __name__ == "__main__":
    main()

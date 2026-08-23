# -*- coding: utf-8 -*-

"""Expired actuator intents are terminal and can never be republished."""

from types import ModuleType

import src.library.operation_store_postgresql as operation_store


def _jsonb(value):
    return value


def _result(one=None, many=None):
    rows = many or []

    def fetchone():
        return one

    def fetchall():
        return rows

    result = ModuleType("database_result")
    result.fetchone = fetchone
    result.fetchall = fetchall
    return result


def _connection():
    captured = {"updated_states": [], "cancelled": []}

    def execute(statement, parameters=None):
        if statement.startswith("SELECT operation_id,authority_term,state"):
            return _result(many=[
                {"operation_id": "not-sent", "authority_term": 4, "state": "ROUTED"},
                {"operation_id": "maybe-written", "authority_term": 4, "state": "DISPATCHED"},
            ])
        if statement.startswith("UPDATE master_control.actuator_operations SET state"):
            state, _encoded_result, operation_id = parameters
            captured["updated_states"].append((operation_id, state))
            return _result(one={
                "operation_id": operation_id,
                "state": state,
                "result": {"code": "operation_expired"},
            })
        if statement.startswith("INSERT INTO master_control.operation_events"):
            return _result()
        if statement.startswith("UPDATE master_control.outbox SET state='CANCELLED'"):
            captured["cancelled"].append(parameters[0])
            return _result()
        raise AssertionError("unexpected SQL: " + statement)

    connection = ModuleType("database_connection")
    connection.execute = execute
    return captured, connection


def main():
    captured, connection = _connection()

    def transaction(callback, **_kwargs):
        return callback(connection)

    database = {"transaction": transaction}
    operation_store.jsonb = _jsonb
    result = operation_store.expire_due_operations(database, "site_main", limit=64)
    assert result["count"] == 2
    assert captured["updated_states"] == [
        ("not-sent", "FAILED"),
        ("maybe-written", "UNCERTAIN"),
    ]
    assert captured["cancelled"] == [
        "actuator-execute:4:not-sent",
        "actuator-execute:4:maybe-written",
    ]
    print("PASS expired actuator intents terminalize and cancel Outbox delivery")


if __name__ == "__main__":
    main()

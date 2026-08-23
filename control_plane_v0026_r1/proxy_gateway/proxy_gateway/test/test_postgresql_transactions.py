# -*- coding: utf-8 -*-

# proxy_gateway/test/test_postgresql_transactions.py

"""Regression tests for the closed Psycopg transaction isolation boundary."""

import contextlib
import sys
import types

from unittest import mock

from src.library import postgresql


def _install_fake_psycopg():
    module = types.ModuleType("psycopg")
    errors = types.ModuleType("psycopg.errors")
    errors.SerializationFailure = ConnectionResetError
    errors.DeadlockDetected = BrokenPipeError
    module.errors = errors
    previous = sys.modules.get("psycopg")
    sys.modules["psycopg"] = module
    return previous


def _restore_psycopg(previous):
    if previous is None:
        sys.modules.pop("psycopg", None)
    else:
        sys.modules["psycopg"] = previous


def _operation(connection):
    connection.execute("SELECT 42 AS value")
    return {"ok": True}


def _failing_operation(_connection):
    raise RuntimeError("expected rollback probe")


def _fake_pool():
    connection = mock.MagicMock()
    connection.transaction.return_value = contextlib.nullcontext()
    pool = mock.MagicMock()
    pool.connection.return_value = contextlib.nullcontext(connection)
    return pool, connection


def main():
    checks = 0
    expected = {
        "read_committed": "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
        "REPEATABLE READ": "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
        "serializable": "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
    }
    for supplied, statement in expected.items():
        assert postgresql._transaction_isolation_statement(supplied) == statement
        checks += 1

    try:
        postgresql._transaction_isolation_statement(
            "SERIALIZABLE; DROP SCHEMA proxy_gateway")
    except ValueError:
        checks += 1
    else:
        raise AssertionError("unsafe isolation value was accepted")

    previous = _install_fake_psycopg()
    try:
        pool, connection = _fake_pool()
        result = postgresql.run_transaction(
            pool, {}, _operation, isolation_level="READ COMMITTED")
        assert result == {"ok": True}
        connection.transaction.assert_called_once_with()
        assert connection.execute.call_args_list[0] == mock.call(
            "SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
        checks += 1

        pool, _connection = _fake_pool()
        try:
            postgresql.run_transaction(
                pool, {}, _failing_operation,
                isolation_level="SERIALIZABLE", max_attempts=1)
        except RuntimeError as exc:
            assert str(exc) == "expected rollback probe"
            checks += 1
        else:
            raise AssertionError("transaction failure did not propagate")
    finally:
        _restore_psycopg(previous)

    print("PASS test_postgresql_transactions ({} checks)".format(checks))


if __name__ == "__main__":
    main()

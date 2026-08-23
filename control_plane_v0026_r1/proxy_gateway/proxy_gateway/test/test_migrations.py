# -*- coding: utf-8 -*-

"""Functional PostgreSQL migration discovery and SQL-splitting checks."""

import os

from src.library.migrations import discover_migrations, split_sql_statements


def main():
    directory = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "deployments", "migrations"))
    migrations = discover_migrations(directory)
    assert [item["version"] for item in migrations] == [1, 2]
    source = migrations[0]["content"].decode("utf-8")
    statements = split_sql_statements(source)
    assert len(statements) > 20
    functions = [item for item in statements if item.startswith(
        "CREATE OR REPLACE FUNCTION reject_append_only_change")]
    assert len(functions) == 1 and "RAISE EXCEPTION" in functions[0]
    additive = migrations[1]["content"].decode("utf-8")
    assert "local_action_idempotency" in additive
    assert "REVOKE DELETE ON local_action_idempotency FROM proxy_app" in additive
    assert "DROP TABLE" not in additive.upper()
    print("PASS test_migrations ({} SQL statements)".format(len(statements)))


if __name__ == "__main__":
    main()

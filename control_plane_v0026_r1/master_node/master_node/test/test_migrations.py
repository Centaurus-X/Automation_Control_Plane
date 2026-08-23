# -*- coding: utf-8 -*-

"""Functional PostgreSQL migration discovery and SQL-splitting checks."""

import os
import re

from src.library.migrations import discover_migrations, split_sql_statements


def _assert_trigger_functions_are_defined_in_order(migrations):
    function_pattern = re.compile(
        r"^CREATE(?: OR REPLACE)? FUNCTION\s+([A-Za-z_][A-Za-z0-9_.]*)\s*\(",
        re.IGNORECASE,
    )
    trigger_pattern = re.compile(
        r"\bEXECUTE\s+FUNCTION\s+([A-Za-z_][A-Za-z0-9_.]*)\s*\(",
        re.IGNORECASE,
    )
    defined_functions = set()
    for migration in migrations:
        source = migration["content"].decode("utf-8")
        for statement in split_sql_statements(source):
            function_match = function_pattern.match(statement)
            if function_match is not None:
                defined_functions.add(
                    function_match.group(1).rsplit(".", 1)[-1].lower())
            for referenced_function in trigger_pattern.findall(statement):
                normalized_name = referenced_function.rsplit(".", 1)[-1].lower()
                assert normalized_name in defined_functions, (
                    migration["path"], referenced_function)

def main():
    directory = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "deployments", "migrations"))
    migrations = discover_migrations(directory)
    assert [item["version"] for item in migrations] == [1, 2, 3, 4]
    source = migrations[0]["content"].decode("utf-8")
    statements = split_sql_statements(source)
    v2_source = migrations[1]["content"].decode("utf-8")
    v2_statements = split_sql_statements(v2_source)
    v3_source = migrations[2]["content"].decode("utf-8")
    v3_statements = split_sql_statements(v3_source)
    v4_source = migrations[3]["content"].decode("utf-8")
    v4_statements = split_sql_statements(v4_source)
    assert len(statements) > 20 and len(v2_statements) > 20
    assert len(v3_statements) > 15
    functions = [item for item in statements if item.startswith(
        "CREATE OR REPLACE FUNCTION reject_immutable_change")]
    assert len(functions) == 1 and "RAISE EXCEPTION" in functions[0]
    _assert_trigger_functions_are_defined_in_order(migrations)
    for table in (
        "config_commits", "config_entities", "config_entity_versions",
        "scope_heads", "scope_snapshots", "scope_ops", "shard_rollouts",
        "worker_proposals", "scope_activations",
    ):
        assert "CREATE TABLE IF NOT EXISTS " + table in v2_source
    for table in (
        "worker_mirror_heads", "worker_mirror_snapshots",
        "cross_registry_heads", "cross_registry_transitions",
        "authority_transfers", "worker_restores",
    ):
        assert "CREATE TABLE IF NOT EXISTS " + table in v3_source
    assert (
        "PRIMARY KEY (scope, authority_epoch, scope_revision, execution_epoch)"
        in v3_source
    )
    assert "cross_registry_transitions_immutable" in v3_source
    assert "GRANT SELECT, INSERT ON worker_mirror_snapshots," in v3_source
    assert "DROP CONSTRAINT IF EXISTS controller_ownership_config_revision_check" in v4_source
    assert "CHECK (config_revision >= 0)" in v4_source
    assert "CHECK (config_revision > 0)" not in v4_source
    print("PASS test_migrations ({} SQL statements)".format(
        len(statements) + len(v2_statements) + len(v3_statements)
        + len(v4_statements)))

if __name__ == "__main__":
    main()

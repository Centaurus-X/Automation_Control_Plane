# -*- coding: utf-8 -*-

"""Independent Node/Cross revision and hardware-free materialization tests."""

import copy
import json
import os

from src.library._generic_system_contract_library import config_graph
from src.core.shard_materializer import create_shard_materializer


HARDWARE_FIELDS = {
    "address", "bus_type", "function_type", "host", "port",
}


def _config():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application", "_legacy",
        "initial_config.json"))
    with open(path, "r", encoding="utf-8") as handle:
        source = json.load(handle)
    return config_graph.migrate_legacy_config(source, profile="lab_v2")["config"]


def _assert_hardware_free(value):
    if isinstance(value, dict):
        assert not HARDWARE_FIELDS.intersection(value)
        for child in value.values():
            _assert_hardware_free(child)
    elif isinstance(value, list):
        for child in value:
            _assert_hardware_free(child)


def main():
    materializer = create_shard_materializer(profile="lab_v2")
    first = materializer["materialize"](_config(), 1)
    assert set(first["scopes"]) == {
        "node:fn-01", "node:fn-02", "cross:fn-01.default",
    }
    node = first["scopes"]["node:fn-01"]
    cross = first["scopes"]["cross:fn-01.default"]
    assert [rule["rule_id"] for rule in node["config"]["rules"]] == [1, 2, 4, 10, 11]
    assert [rule["rule_id"] for rule in cross["config"]["rules"]] == [3, 6]
    _assert_hardware_free(cross["config"])

    changed = _config()
    changed["controllers"][0]["host"] = "simulator-fn01-new"
    heads = {
        scope: {key: value for key, value in snapshot.items() if key != "config"}
        for scope, snapshot in first["scopes"].items()
    }
    second = materializer["materialize"](
        changed, 2, previous_heads=heads, previous_snapshots=first["scopes"])
    assert second["scopes"]["node:fn-01"]["scope_revision"] == 2
    assert second["scopes"]["node:fn-01"]["changed"]
    assert second["scopes"]["node:fn-02"]["scope_revision"] == 1
    assert not second["scopes"]["node:fn-02"]["changed"]
    assert second["scopes"]["cross:fn-01.default"]["scope_revision"] == 1
    assert not second["scopes"]["cross:fn-01.default"]["changed"]
    print("PASS independent deterministic Node/Cross shard materialization")


if __name__ == "__main__":
    main()

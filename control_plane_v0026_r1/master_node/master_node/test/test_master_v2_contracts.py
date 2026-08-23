# -*- coding: utf-8 -*-

"""Lossless entity registry, migration matrix and deterministic diff tests."""

import copy
import json
import os

from src.library._generic_system_contract_library import config_graph, entity_registry, scope_diff
from src.library._generic_type_library import fingerprint


def _config():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application", "_legacy",
        "initial_config.json"))
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    source = _config()
    try:
        entity_registry.decompose(source)
    except ValueError:
        pass
    else:
        raise AssertionError("V5 entered the canonical registry without explicit import")

    migrated = config_graph.migrate_legacy_config(source, profile="lab_v2")
    assert migrated["ok"]
    config = migrated["config"]
    roundtrip = entity_registry.verify_roundtrip(config)
    assert roundtrip["ok"] and roundtrip["entity_count"] == 197
    records = entity_registry.decompose(config)
    assert entity_registry.materialize(records) == config
    report = migrated["report"]
    assert report["active_rule_ids"] == [1, 2, 3, 4, 6, 10, 11]
    assert report["migration_required_rule_ids"] == [5, 7, 8, 9, 13, 15, 17]
    assert report["production_safety_blockers"] == [13, 15, 17]
    assert config_graph.validate_authority_config(
        migrated["config"], profile="lab_v2")["ok"]
    assert not config_graph.validate_authority_config(
        migrated["config"], profile="production_v2")["ok"]

    changed = copy.deepcopy(config)
    changed["process_states"][0]["value_id"] = 2
    operations = scope_diff.diff_configs(config, changed)
    assert len(operations) == 1
    rebuilt = scope_diff.apply_operations(config, operations)
    assert fingerprint(rebuilt) == fingerprint(changed)
    corrupt = copy.deepcopy(list(operations))
    corrupt[0]["expected_value_hash"] = "0" * 64
    try:
        scope_diff.apply_operations(config, corrupt)
    except ValueError:
        pass
    else:
        raise AssertionError("mismatched entity hash was accepted")
    print("PASS Master v2 shared entity/graph/diff contracts")


if __name__ == "__main__":
    main()

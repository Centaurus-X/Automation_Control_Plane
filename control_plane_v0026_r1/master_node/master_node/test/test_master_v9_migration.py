# -*- coding: utf-8 -*-

"""One-time fenced V5 entity-store migration and rollback regression."""

import copy
import json
import os

from src.library._generic_system_contract_library import config_compatibility, entity_registry, scope_diff
from src.library._generic_type_library import fingerprint
from src.core.config_authority import create_config_authority
from src.core.shard_materializer import create_shard_materializer


def _read(name):
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application", name))
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    legacy = _read("_legacy/initial_config_v2.json")
    canonical = _read("initial_config_v9.json")
    assert config_compatibility.get_config_version(legacy) == 5
    assert entity_registry.materialize(
        entity_registry.decompose(legacy, allow_legacy_v5=True)) == legacy
    migration_ops = scope_diff.diff_configs(
        legacy,
        config_compatibility.import_legacy_v5_config(legacy),
        allow_legacy_v5=True,
    )
    assert any(row["section"] == "meta" for row in migration_ops)

    state = {
        "authority": {
            "lease_active": True,
            "leader_instance_id": "master-01",
            "lease_token": "00000000-0000-0000-0000-000000000099",
            "authority_term": 12,
            "config_revision": 5,
            "config_fingerprint": fingerprint(legacy),
        },
        "config": copy.deepcopy(legacy),
        "history": {1: copy.deepcopy(legacy)},
        "commits": [],
    }

    def authority_reader():
        return copy.deepcopy(state["authority"])

    def config_reader():
        return {
            "revision": state["authority"]["config_revision"],
            "fingerprint": state["authority"]["config_fingerprint"],
            "config_data": copy.deepcopy(state["config"]),
        }

    def config_by_revision(revision):
        value = state["history"].get(int(revision))
        return {
            "revision": int(revision),
            "fingerprint": fingerprint(value),
            "config_data": copy.deepcopy(value),
        } if value else None

    def commit(request):
        assert request["base_revision"] == state["authority"]["config_revision"]
        assert request["lease_token"] == state["authority"]["lease_token"]
        assert request["authority_term"] == state["authority"]["authority_term"]
        assert request["config_format_version"] == 9
        assert config_compatibility.get_config_version(request["config"]) == 9
        assert config_compatibility.get_config_io_layout(request["config"]) == "flat"
        revision = int(request["base_revision"]) + 1
        state["commits"].append(copy.deepcopy(request))
        state["config"] = copy.deepcopy(request["config"])
        state["history"][revision] = copy.deepcopy(request["config"])
        state["authority"]["config_revision"] = revision
        state["authority"]["config_fingerprint"] = fingerprint(request["config"])
        return {
            "ok": True,
            "changed": True,
            "revision": revision,
            "global_revision": revision,
            "fingerprint": state["authority"]["config_fingerprint"],
            "targets": (),
        }

    def empty():
        return {}
    authority = create_config_authority(
        site_id="site_main",
        instance_id="master-01",
        authority_reader=authority_reader,
        config_reader=config_reader,
        config_by_revision=config_by_revision,
        commit=commit,
        policy={},
        profile="lab_v2",
        materializer=create_shard_materializer(profile="lab_v2")["materialize"],
        scope_heads_reader=empty,
        scope_snapshots_reader=empty,
        activations_reader=empty,
    )

    migrated = authority["migrate_legacy"]()
    assert migrated["ok"] and migrated["revision"] == 6
    assert len(state["commits"]) == 1
    assert state["commits"][0]["source_kind"] == "legacy_import"
    assert state["commits"][0]["changes"][0]["before"] == 5
    assert state["commits"][0]["changes"][0]["after"] == 9
    for snapshot in state["commits"][0]["materialization"]["scopes"].values():
        expected_version = 8 if snapshot["scope_kind"] == "cross" else 9
        assert snapshot["config"]["meta"][0]["version"] == expected_version
        assert config_compatibility.get_config_io_layout(
            snapshot["config"]) == "flat"
    repeated = authority["migrate_legacy"]()
    assert repeated["ok"] and repeated["changed"] is False
    assert repeated["status"] == "already_v9"
    assert len(state["commits"]) == 1

    rolled_back = authority["rollback"]({
        "base_revision": 6,
        "target_revision": 1,
        "principal_subject": "engineer-01",
        "request_id": "rollback-v5-history-as-v9",
        "reason": "regression",
    })
    assert rolled_back["ok"] and rolled_back["revision"] == 7
    assert state["commits"][-1]["source_kind"] == "rollback"
    assert state["commits"][-1]["config"]["meta"][0]["version"] == 9

    state["authority"]["lease_active"] = False
    rejected = authority["migrate_legacy"]()
    assert not rejected["ok"] and rejected["code"] == "not_config_authority"
    assert canonical["meta"][0]["version"] == 9
    print("PASS fenced one-time V5-to-V9 authority migration and V9 rollback")


if __name__ == "__main__":
    main()

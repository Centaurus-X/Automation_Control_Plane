# -*- coding: utf-8 -*-

"""Functional tests for immutable revision, policy and rollback behavior."""

import copy
import json
import os

from src.library._generic_type_library import fingerprint
from src.core.config_authority import (
    _apply_worker_actions,
    create_config_authority,
    validate_worker_config,
)


def _fixture():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application", "initial_config_v9.json"))
    with open(path, "r", encoding="utf-8") as handle:
        initial = json.load(handle)
    state = {
        "authority": {
            "lease_active": True,
            "leader_instance_id": "master-01",
            "lease_token": "00000000-0000-0000-0000-000000000010",
            "authority_term": 4,
            "config_revision": 1,
            "config_fingerprint": fingerprint(initial),
        },
        "snapshots": {1: copy.deepcopy(initial)},
        "commits": [],
    }

    def authority_reader():
        return copy.deepcopy(state["authority"])

    def config_reader():
        revision = state["authority"]["config_revision"]
        data = state["snapshots"].get(revision)
        return {
            "revision": revision,
            "fingerprint": fingerprint(data),
            "config_data": copy.deepcopy(data),
        } if data else None

    def config_by_revision(revision):
        data = state["snapshots"].get(revision)
        return {
            "revision": revision,
            "fingerprint": fingerprint(data),
            "config_data": copy.deepcopy(data),
        } if data else None

    def commit(request):
        assert request["instance_id"] == "master-01"
        assert request["lease_token"] == state["authority"]["lease_token"]
        assert request["base_revision"] == state["authority"]["config_revision"]
        state["commits"].append(copy.deepcopy(request))
        revision = request["base_revision"] + 1
        state["snapshots"][revision] = copy.deepcopy(request["config"])
        state["authority"]["config_revision"] = revision
        state["authority"]["config_fingerprint"] = fingerprint(request["config"])
        return {
            "ok": True,
            "revision": revision,
            "fingerprint": state["authority"]["config_fingerprint"],
            "rollout_id": "00000000-0000-0000-0000-000000000020",
            "targets": (),
        }

    authority = create_config_authority(
        site_id="site_main",
        instance_id="master-01",
        authority_reader=authority_reader,
        config_reader=config_reader,
        config_by_revision=config_by_revision,
        commit=commit,
        policy={},
    )
    return initial, state, authority


def _test_worker_schema_and_revision_commit():
    initial, state, authority = _fixture()
    assert validate_worker_config(initial)["ok"]
    result = authority["propose"]({
        "base_revision": 1,
        "changes": [{
            "operation": "set",
            "path": "virtual_controllers.2.virt_active",
            "value": True,
        }],
        "principal_subject": "engineer-01",
        "request_id": "request-01",
    })
    assert result["ok"] and result["revision"] == 2
    assert state["snapshots"][1]["virtual_controllers"][2]["virt_active"] is False
    assert state["snapshots"][2]["virtual_controllers"][2]["virt_active"] is True


def _test_stale_and_denied_mutations():
    _initial, _state, authority = _fixture()
    stale = authority["propose"]({
        "base_revision": 0,
        "changes": [{"path": "virtual_controllers.0.virt_active", "value": False}],
        "principal_subject": "engineer-01",
        "request_id": "request-stale",
    })
    assert stale["code"] == "stale_revision"
    denied = authority["propose"]({
        "base_revision": 1,
        "changes": [{"path": "secrets.mqtt", "value": "forbidden"}],
        "principal_subject": "engineer-01",
        "request_id": "request-denied",
    })
    assert denied["code"] == "authorization_denied"


def _test_rollback_creates_a_new_revision():
    _initial, state, authority = _fixture()
    changed = authority["propose"]({
        "base_revision": 1,
        "changes": [{"path": "virtual_controllers.2.virt_active", "value": True}],
        "principal_subject": "engineer-01",
        "request_id": "request-change",
    })
    assert changed["revision"] == 2
    rolled = authority["rollback"]({
        "base_revision": 2,
        "target_revision": 1,
        "principal_subject": "engineer-01",
        "request_id": "request-rollback",
    })
    assert rolled["ok"] and rolled["revision"] == 3
    assert state["snapshots"][3] == state["snapshots"][1]


def _test_identical_changes_are_noop():
    initial, state, authority = _fixture()
    unchanged = initial["virtual_controllers"][2]["virt_active"]
    result = authority["propose"]({
        "base_revision": 1,
        "changes": [{
            "operation": "set",
            "path": "virtual_controllers.2.virt_active",
            "value": unchanged,
        }],
        "principal_subject": "engineer-01",
        "request_id": "request-noop",
    })
    assert result["ok"] and result["changed"] is False
    assert result["revision"] == 1
    assert state["authority"]["config_revision"] == 1
    assert set(state["snapshots"]) == {1}


def _test_prepared_worker_noops_do_not_commit():
    initial, state, authority = _fixture()
    requests = (
        {
            "base_revision": 1,
            "config": copy.deepcopy(initial),
            "changes": [],
            "principal_subject": "worker:fn-01",
            "request_id": "worker-empty-proposal",
            "source_node_id": "fn-01",
        },
        {
            "base_revision": 1,
            "config": copy.deepcopy(initial),
            "changes": [{
                "op": "replace_field",
                "section": "process_states",
                "entity_key": "state:1",
                "json_pointer": "/value_id",
                "value": initial["process_states"][0]["value_id"],
            }],
            "principal_subject": "worker:fn-01",
            "request_id": "worker-semantic-noop",
            "source_node_id": "fn-01",
        },
    )
    for request in requests:
        result = authority["commit_prepared"](request)
        assert result["ok"] and result["changed"] is False
        assert result["revision"] == 1
        assert result["fingerprint"] == state["authority"]["config_fingerprint"]
    assert state["authority"]["config_revision"] == 1
    assert set(state["snapshots"]) == {1}
    assert state["commits"] == []


def _test_rule_actions_only_mutate_the_required_mode():
    initial, _state, _authority = _fixture()
    before = {
        row["state_id"]: copy.deepcopy(row)
        for row in initial["process_states"]
    }
    result = _apply_worker_actions(initial, [{
        "rule_id": 17,
        "action": {
            "state_ids": [2, 4, 9, 10, 11, 15],
            "value_id": 2,
        },
    }])
    assert result["ok"]
    changed_ids = {
        initial["process_states"][int(item["path"].split(".")[1])]["state_id"]
        for item in result["changes"]
    }
    assert changed_ids == {2, 10, 15}
    for state_id in (4, 9, 11):
        current = next(
            row for row in initial["process_states"]
            if row["state_id"] == state_id)
        assert current == before[state_id]


def _test_action_engine_identity_is_fenced():
    initial, _state, _authority = _fixture()
    before = copy.deepcopy(initial)
    unknown = _apply_worker_actions(initial, [{
        "engine_id": "unknown-engine",
        "action": {"state_ids": [2, 4], "value_id": 2},
    }])
    assert not unknown["ok"] and unknown["code"] == "invalid_payload"
    assert initial == before

    conflicting = copy.deepcopy(initial)
    second_engine = copy.deepcopy(conflicting["rule_engine"][0])
    second_engine["engine_id"] = 2
    second_engine["required_mode_id"] = 1
    conflicting["rule_engine"].append(second_engine)
    conflicting_before = copy.deepcopy(conflicting)
    mismatch = _apply_worker_actions(conflicting, [{
        "rule_id": 17,
        "engine_id": 2,
        "action": {"state_ids": [2, 4], "value_id": 2},
    }])
    assert not mismatch["ok"] and mismatch["code"] == "invalid_payload"
    assert conflicting == conflicting_before

    compatible = _apply_worker_actions(copy.deepcopy(initial), [{
        "rule_id": 17,
        "engine_id": 1,
        "action": {
            "state_ids": [2, 4, 9, 10, 11, 15],
            "value_id": 2,
        },
    }])
    assert compatible["ok"]
    changed_ids = {
        initial["process_states"][int(item["path"].split(".")[1])]["state_id"]
        for item in compatible["changes"]
    }
    assert changed_ids == {2, 10, 15}


def _test_legacy_snapshot_can_seed_nonempty_authority_revision():
    legacy_path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application",
        "_legacy", "initial_config_v2.json"))
    with open(legacy_path, "r", encoding="utf-8") as handle:
        initial = json.load(handle)
    authority_state = {
        "lease_active": True,
        "leader_instance_id": "master-01",
        "lease_token": "00000000-0000-0000-0000-000000000010",
        "authority_term": 9,
        "config_revision": 5,
        "config_fingerprint": "a" * 64,
    }
    captured = []

    def authority_reader():
        return copy.deepcopy(authority_state)

    def empty_reader(*_args, **_kwargs):
        return None

    def commit(request):
        captured.append(copy.deepcopy(request))
        return {"ok": True, "revision": 6, "fingerprint": fingerprint(request["config"])}

    authority = create_config_authority(
        site_id="site_main", instance_id="master-01",
        authority_reader=authority_reader, config_reader=empty_reader,
        config_by_revision=empty_reader, commit=commit, policy={},
    )
    rejected = authority["seed"](initial)
    assert not rejected["ok"] and rejected["code"] == "inconsistent_storage"
    migrated = authority["seed"](
        initial, source_kind="migration", request_id="migration-v0013")
    assert migrated["ok"] and captured[0]["base_revision"] == 5
    assert captured[0]["source_kind"] == "migration"


def main():
    tests = (
        _test_worker_schema_and_revision_commit,
        _test_stale_and_denied_mutations,
        _test_rollback_creates_a_new_revision,
        _test_identical_changes_are_noop,
        _test_prepared_worker_noops_do_not_commit,
        _test_rule_actions_only_mutate_the_required_mode,
        _test_action_engine_identity_is_fenced,
        _test_legacy_snapshot_can_seed_nonempty_authority_revision,
    )
    for test in tests:
        test()
    print("PASS test_config_authority ({} checks)".format(len(tests)))


if __name__ == "__main__":
    main()

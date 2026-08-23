# -*- coding: utf-8 -*-

"""Exercise the exact full-Cross authority seed path before PostgreSQL I/O."""

import copy
import json
import os

from src.library._generic_type_library import fingerprint
from src.core.config_authority import create_config_authority
from src.core.shard_materializer import create_shard_materializer


def _fixture():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application",
        "initial_config_v9.json"))
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def main():
    config = _fixture()
    captured = []
    authority_state = {
        "lease_active": True,
        "leader_instance_id": "master-01",
        "lease_token": "00000000-0000-0000-0000-000000000013",
        "authority_term": 1,
        "config_revision": 0,
        "config_fingerprint": "0" * 64,
    }

    def authority_reader():
        return copy.deepcopy(authority_state)

    def no_config(*_args, **_kwargs):
        return None

    def empty_mapping():
        return {}

    def commit(request):
        captured.append(copy.deepcopy(request))
        return {
            "ok": True,
            "status": "seeded",
            "revision": 1,
            "global_revision": 1,
            "fingerprint": fingerprint(request["config"]),
        }

    authority = create_config_authority(
        site_id="site_main", instance_id="master-01",
        authority_reader=authority_reader, config_reader=no_config,
        config_by_revision=no_config, commit=commit, policy={},
        profile="lab_v2",
        materializer=create_shard_materializer(
            profile="lab_v2")["materialize"],
        scope_heads_reader=empty_mapping,
        scope_snapshots_reader=empty_mapping,
        activations_reader=empty_mapping,
    )
    result = authority["seed"](config)
    assert result["ok"] and result["revision"] == 1
    assert len(captured) == 1
    request = captured[0]
    assert set(request["materialization"]["scopes"]) == {
        "node:fn-01", "node:fn-02", "cross:fn-01.default"}
    activations = request["activations"]
    assert len(activations) == 20
    assert "process:1" not in activations
    assert activations["rule:3"]["active_scope"] \
        == "cross:fn-01.default"
    assert activations["state:18"]["active_scope"] == "node:fn-02"
    print("PASS full-Cross initial authority seed and activation plan")


if __name__ == "__main__":
    main()

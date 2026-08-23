# -*- coding: utf-8 -*-

"""Regression coverage for the node-local Registry site boundary."""

import copy

from src.core.master_runtime import _node_local_topology_snapshot


CONTROLLER_UUID = "62671112-766c-59ed-b0ff-30a49d48f747"


def _worker_mirrors():
    return [{
        "scope": "node:fn-01",
        "scope_kind": "node",
        "owner_node_id": "fn-01",
        "authority_epoch": 0,
        "scope_revision": 7,
        "fingerprint": "a" * 64,
        "snapshot": {
            "config": {
                "site_controller_map": [{
                    "site_id": "site_main",
                    "controller_uuid": CONTROLLER_UUID,
                    "node_id": "fn-01",
                    "routing_role": "owner",
                    "enabled": True,
                }],
            },
        },
    }]


def _test_registry_projection_uses_the_effective_operational_site():
    mirrors = _worker_mirrors()
    original = copy.deepcopy(mirrors)
    context = {
        "config": {"site_id": "site-main"},
        "store": {
            "authority": {
                "list_worker_mirrors": mirrors.copy,
            },
        },
    }

    snapshot = _node_local_topology_snapshot(context)
    routes = snapshot["config_data"]["site_controller_map"]

    assert len(routes) == 1
    assert routes[0]["controller_uuid"] == CONTROLLER_UUID
    assert routes[0]["node_id"] == "fn-01"
    assert routes[0]["site_id"] == "site-main"
    assert mirrors == original


def main():
    _test_registry_projection_uses_the_effective_operational_site()
    print("PASS node-local Registry routes use the effective operational site identity")


if __name__ == "__main__":
    main()

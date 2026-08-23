# -*- coding: utf-8 -*-

"""JSON-boundary regression for complete Master runtime diagnostics."""

import json
import threading

from src.core.master_runtime import get_master_snapshot


def _database_ready():
    return True


def _worker_mirrors():
    return ()


def _legacy_snapshot():
    return {
        "state": "connected",
        "transport": {"connected": True},
    }


def _control_snapshot():
    return {"state": "connected", "connected": True}


def _orchestrator_snapshot():
    return {
        "state": "running",
        "role": "leader",
        "authority_term": 7,
        "authority_services_ready": True,
    }


def _heap_snapshot():
    return {
        "generation": 0,
        "global_revision": 0,
        "global_fingerprint": None,
        "scope_count": 0,
        "scopes": (),
    }


def _router_snapshot():
    return {
        "dependency_count": 0,
        "latest_fact_count": 0,
        "owner_count": 0,
    }


def _metrics_snapshot():
    return {
        "state": "running",
        "thread_alive": True,
        "errors": 0,
    }


def _registry_snapshot():
    return {
        "fingerprint": "f" * 64,
        "global_revision": 0,
    }


def _runtime_context():
    registry_config = {
        "registry_schema": "automation_registry_v1",
        "authority_mode": "node_local",
        "site_id": "site-main",
        "instance_id": "master-01",
        "coordinator_node_id": "master-01",
    }
    return {
        "config": {
            "site_id": "site-main",
            "instance_id": "master-01",
            "authority_mode": "node_local",
            "mqtt": {"required": True},
        },
        "lock": threading.RLock(),
        "state": "running",
        "last_error": None,
        "bootstrap": {
            "state": "ready",
            "attempts": 1,
            "last_status": "registry_ready",
            "last_error": None,
            "global_revision": 0,
            "required_node_scopes": (),
        },
        "registry_config": registry_config,
        "registry_reader": _registry_snapshot,
        "store": {
            "ping": _database_ready,
            "authority": {"list_worker_mirrors": _worker_mirrors},
        },
        "legacy_mqtt": {"snapshot": _legacy_snapshot},
        "control_mqtt": {"snapshot": _control_snapshot},
        "orchestrator": {"snapshot": _orchestrator_snapshot},
        "heap": {"snapshot": _heap_snapshot},
        "cross_router": {"snapshot": _router_snapshot},
        "metrics": {"snapshot": _metrics_snapshot},
    }


def _test_runtime_snapshot_is_fully_materialized_json():
    snapshot = get_master_snapshot(_runtime_context())
    assert snapshot["ready"]
    assert snapshot["orchestrator"] == _orchestrator_snapshot()
    assert not callable(snapshot["orchestrator"])
    encoded = json.dumps(
        snapshot,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    assert b'"authority_services_ready":true' in encoded
    assert b'"role":"leader"' in encoded


def main():
    _test_runtime_snapshot_is_fully_materialized_json()
    print("PASS fully materialized JSON-safe Master runtime snapshot")


if __name__ == "__main__":
    main()

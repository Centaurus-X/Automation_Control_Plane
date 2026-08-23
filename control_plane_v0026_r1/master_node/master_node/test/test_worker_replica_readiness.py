# -*- coding: utf-8 -*-

"""Node readiness follows the exact Node shard and never the Cross shard."""

import copy

from src.core.node_registry import create_node_registry
from src.library._generic_system_contract_library import protocol_contracts


TARGET_FINGERPRINT = "a" * 64


def _authority():
    return {"authority_term": 1, "config_revision": 9, "config_fingerprint": "global"}


def _scope_reader(scope):
    assert scope == "node:fn-01"
    return {
        "scope": scope,
        "scope_revision": 4,
        "global_revision": 9,
        "fingerprint": TARGET_FINGERPRINT,
    }


def _message(fingerprint, **kwargs):
    return {
        "source_node": "fn-01",
        "message_id": kwargs.get("message_id", "heartbeat-01"),
        "payload": {
            "site_id": "site_main",
            "node_id": "fn-01",
            "protocol": protocol_contracts.PROTOCOL_V2,
            "protocol_versions": [protocol_contracts.PROTOCOL_V2],
            "contracts_version": protocol_contracts.CONTRACTS_VERSION,
            "type_schema_version": protocol_contracts.TYPE_SCHEMA_VERSION,
            "config_norm_version": protocol_contracts.CONFIG_NORM_VERSION,
            "capabilities": [
                protocol_contracts.OPERATION_FORMAT,
                "mirror_v1",
                "node_full",
            ],
            "state": "ACTIVE",
            "observed_global_revision": 9,
            "live_config_revision": 9,
            "node_replica": {
                "scope": "node:fn-01",
                "authority_epoch": 2,
                "revision": kwargs.get("revision", 4),
                "fingerprint": fingerprint,
            },
            "cross_replica": {
                "scope": "cross:fn-01.default",
                "authority_epoch": 2,
                "revision": 99,
                "execution_epoch": 4,
                "fingerprint": kwargs.get("cross_fingerprint", "mismatch"),
            },
        },
    }

def _registry(captured):
    def heartbeat(request):
        captured.append(copy.deepcopy(request))
        return {
            "ok": True,
            "node": {
                "node_id": "fn-01",
                "admission_state": "APPROVED",
                "desired_state": "ACTIVE",
                "observed_state": request["observed_state"],
            },
        }

    def unused(*_args, **_kwargs):
        raise AssertionError("unexpected fake capability")

    return create_node_registry(
        site_id="site_main",
        instance_id="master-01",
        coordinator_node_id="master-01",
        store={
            "register": unused,
            "heartbeat": heartbeat,
            "decide_admission": unused,
            "set_desired_state": unused,
            "list": unused,
            "get": unused,
        },
        authority_reader=_authority,
        scope_reader=_scope_reader,
    )


def main():
    captured = []
    registry = _registry(captured)
    assert registry["heartbeat"](_message("old", message_id="old"))["ok"]
    assert captured[-1]["observed_state"] == "DEGRADED"
    sync = captured[-1]["health"]["master_node_scope_sync"]
    assert sync["converged"] is False

    assert registry["heartbeat"](_message(
        TARGET_FINGERPRINT, message_id="same-fingerprint-old-revision",
        revision=3))["ok"]
    assert captured[-1]["observed_state"] == "DEGRADED"
    sync = captured[-1]["health"]["master_node_scope_sync"]
    assert sync["revision_matches"] is False
    assert sync["fingerprint_matches"] is True

    assert registry["heartbeat"](_message(
        TARGET_FINGERPRINT, message_id="current", cross_fingerprint="still-old"))["ok"]
    assert captured[-1]["observed_state"] == "ACTIVE"
    assert captured[-1]["health"]["master_node_scope_sync"]["converged"] is True
    print("PASS exact Node replica readiness remains independent from Cross")


if __name__ == "__main__":
    main()

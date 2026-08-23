# -*- coding: utf-8 -*-

"""Worker registration and heartbeat capability persistence tests."""

import copy

from src.library._generic_system_contract_library.protocol_contracts import (
    CONFIG_NORM_VERSION,
    CONTRACTS_VERSION,
    OPERATION_FORMAT,
    PROTOCOL_V2,
    TYPE_SCHEMA_VERSION,
    normalize_registration,
)
from src.core.node_registry import create_node_registry
from src.lab_v2_probe import COMMON_REQUIRED_CAPABILITIES, _capability_errors


def _authority_reader():
    return {
        "authority_term": 3,
        "config_revision": 9,
        "config_fingerprint": "fingerprint-09",
    }


def _unused(*_args, **_kwargs):
    raise AssertionError("unexpected fake store capability")


def _message(kind):
    return {
        "protocol": "automation.mqtt.v1",
        "message_id": kind + "-01",
        "source_node": "fn-01",
        "certificate_identity": "urn:automation:site_main:worker:fn-01",
        "payload": {
            "site_id": "site_main",
            "node_id": "fn-01",
            "protocol": PROTOCOL_V2,
            "protocol_versions": [PROTOCOL_V2],
            "contracts_version": CONTRACTS_VERSION,
            "type_schema_version": TYPE_SCHEMA_VERSION,
            "config_norm_version": CONFIG_NORM_VERSION,
            "authority_revision": 9,
            "observed_global_revision": 0,
            "live_config_revision": 9,
            "fingerprint": "fingerprint-09",
            "state": "ACTIVE",
            "controller_uuids": ["controller-01"],
            "capabilities": {
                OPERATION_FORMAT: True,
                "canonical_config_v9": True,
                "local_automation_authority": True,
                "mirror_v1": True,
            },
        },
    }

def _test_registration_and_heartbeat_forward_same_capabilities():
    captured = {"register": None, "heartbeat": None}

    def register(request):
        captured["register"] = copy.deepcopy(request)
        node = copy.deepcopy(request)
        node["admission_state"] = "APPROVED"
        node["desired_state"] = "ACTIVE"
        return {"ok": True, "node": node}

    def heartbeat(request):
        captured["heartbeat"] = copy.deepcopy(request)
        return {
            "ok": True,
            "node": {
                "node_id": "fn-01", "admission_state": "APPROVED",
                "desired_state": "ACTIVE",
            },
        }

    registry = create_node_registry(
        site_id="site_main", instance_id="master-01",
        coordinator_node_id="master-01", trust_broker_acl_identity=True,
        store={
            "register": register, "heartbeat": heartbeat,
            "decide_admission": _unused, "set_desired_state": _unused,
            "list": _unused, "get": _unused,
        },
        authority_reader=_authority_reader,
    )
    assert registry["register"](_message("registration"))["ok"]
    assert registry["heartbeat"](_message("heartbeat"))["ok"]
    raw_payload = _message("expected")["payload"]
    expected = {
        "features": normalize_registration(raw_payload)["capabilities"],
        "reported_features": copy.deepcopy(raw_payload["capabilities"]),
        "controller_uuids": ["controller-01"],
    }
    assert captured["register"]["capabilities"] == expected
    assert captured["heartbeat"]["capabilities"] == expected


def _test_incomplete_heartbeat_is_rejected_before_store_write():
    captured = {}

    def heartbeat(request):
        captured.update(copy.deepcopy(request))
        return {"ok": True, "node": request}

    registry = create_node_registry(
        site_id="site_main", instance_id="master-01",
        coordinator_node_id="master-01", trust_broker_acl_identity=True,
        store={
            "register": _unused, "heartbeat": heartbeat,
            "decide_admission": _unused, "set_desired_state": _unused,
            "list": _unused, "get": _unused,
        },
        authority_reader=_authority_reader,
    )
    message = _message("incomplete-heartbeat")
    del message["payload"]["capabilities"]
    result = registry["heartbeat"](message)
    assert result["ok"] is False
    assert result["code"] == "unsupported_operation_format"
    assert captured == {}


def _probe_node(node_id, supplied):
    capabilities = copy.deepcopy(supplied)
    capabilities[OPERATION_FORMAT] = True
    normalized = normalize_registration({
        "node_id": node_id,
        "protocol_version": PROTOCOL_V2,
        "protocol_versions": [PROTOCOL_V2],
        "contracts_version": CONTRACTS_VERSION,
        "type_schema_version": TYPE_SCHEMA_VERSION,
        "config_norm_version": CONFIG_NORM_VERSION,
        "capabilities": capabilities,
    })
    return {
        "protocol_version": PROTOCOL_V2,
        "protocol_versions": list(normalized["protocol_versions"]),
        "contracts_version": normalized["contracts_version"],
        "capabilities": {"features": normalized["capabilities"]},
    }


def _test_lab_probe_uses_node_specific_normalized_capabilities():
    supplied = {
        name: True
        for name in COMMON_REQUIRED_CAPABILITIES
    }
    supplied["master_automation_execution"] = False
    fn01 = _probe_node("fn-01", supplied)
    fn02 = _probe_node("fn-02", supplied)
    assert _capability_errors("fn-01", fn01) == []
    assert _capability_errors("fn-02", fn02) == []
    unsafe = copy.deepcopy(fn02)
    unsafe["capabilities"]["features"]["master_automation_execution"] = True
    assert any(
        "Master automation" in error
        for error in _capability_errors("fn-02", unsafe))


def main():
    tests = (
        _test_registration_and_heartbeat_forward_same_capabilities,
        _test_incomplete_heartbeat_is_rejected_before_store_write,
        _test_lab_probe_uses_node_specific_normalized_capabilities,
    )
    for test in tests:
        test()
    print("PASS test_node_capabilities ({} scenarios)".format(len(tests)))


if __name__ == "__main__":
    main()

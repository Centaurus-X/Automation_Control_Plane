# -*- coding: utf-8 -*-

"""Broker-bound worker identity proof and automatic admission tests."""

import copy
import json
import time

from src.core.coordinator_service import (
    handle_broker_identity_heartbeat,
    handle_legacy_registration,
)
from src.interfaces.legacy_worker_mqtt_interface import (
    _handle_broker_identity_heartbeat,
    create_mqtt_interface,
)


def _mqtt_fixture():
    captured = {}

    def snapshot():
        return {"connected": True}

    def transport_factory(**kwargs):
        captured.update(kwargs)
        return {"snapshot": snapshot}

    api = create_mqtt_interface(
        options={
            "enabled": True,
            "site_id": "site_main",
            "node_id": "master-01",
            "allow_retained_commands": False,
            "transport": {
                "host": "127.0.0.1",
                "port": 1883,
                "allow_insecure_transport": True,
                "tls": {"enabled": False},
            },
        },
        transport_factory=transport_factory,
    )
    return api, captured


def _heartbeat(node_id="fn-01", **kwargs):
    envelope = {
        "protocol": "automation.mqtt.v1",
        "kind": "heartbeat",
        "message_id": kwargs.get("message_id") or "heartbeat-01",
        "site_id": kwargs.get("site_id") or "site_main",
        "source_node": kwargs.get("source_node") or node_id,
        "created_at_s": kwargs.get("created_at_s") or time.time(),
    }
    return {
        "topic": "automation/v1/site_main/nodes/{}/heartbeat".format(node_id),
        "payload": json.dumps(envelope).encode("utf-8"),
        "retain": bool(kwargs.get("retain", False)),
    }


def _test_mqtt_identity_proof_validation():
    api, captured = _mqtt_fixture()
    received = []

    def receive(envelope):
        received.append(envelope)
        return True

    api["register_identity_heartbeat_handler"](receive)
    subscriptions = {item["topic"] for item in captured["subscriptions"]}
    assert "automation/v1/site_main/nodes/+/heartbeat" in subscriptions
    context = api["context"]
    assert _handle_broker_identity_heartbeat(context, _heartbeat())
    assert received[0]["source_node"] == "fn-01"
    assert received[0]["broker_bound_topic_node"] == "fn-01"
    assert not _handle_broker_identity_heartbeat(
        context, _heartbeat(retain=True, message_id="retained"))
    assert not _handle_broker_identity_heartbeat(
        context, _heartbeat(source_node="fn-02", message_id="mismatch"))
    assert not _handle_broker_identity_heartbeat(
        context, _heartbeat(node_id="master-01", message_id="self"))
    statistics = context["statistics"]
    assert statistics["identity_heartbeats_received"] == 1
    assert statistics["identity_heartbeats_rejected"] == 2
    assert statistics["identity_heartbeats_ignored"] == 1


def _coordinator_fixture(**kwargs):
    operational_site_id = str(kwargs.get("site_id") or "site_main")
    business_site_id = str(kwargs.get("config_site_id") or "site_main")
    state = {"node": kwargs.get("node"), "decisions": [], "reconciles": 0}
    configured_nodes = [
        {
            "site_id": business_site_id,
            "node_id": "fn-01",
            "node_role": "worker",
            "enabled": True,
        },
    ]
    config_data = {
        "nodes": [] if kwargs.get("empty_config_nodes") else configured_nodes,
    }

    def config_reader():
        return {
            "revision": 1,
            "fingerprint": "config-01",
            "config_data": copy.deepcopy(config_data),
        }

    def authority_reader():
        return {
            "lease_active": True,
            "leader_instance_id": "master-01",
            "lease_token": "lease-01",
            "authority_term": 1,
            "config_revision": 1,
            "config_fingerprint": "config-01",
        }

    def get_node(_node_id):
        return copy.deepcopy(state["node"])

    def register(message):
        if state["node"] is None:
            state["node"] = {
                "node_id": message["source_node"],
                "certificate_identity": "urn:automation:{}:worker:fn-01".format(
                    operational_site_id),
                "admission_state": "PENDING",
                "desired_state": "ACTIVE",
            }
        if state["node"]["admission_state"] != "APPROVED":
            return {
                "ok": False,
                "code": "authorization_denied",
                "message": "worker is pending explicit admission",
            }
        return {
            "ok": True,
            "node_id": "fn-01",
            "authority_revision": 1,
            "fingerprint": "config-01",
        }

    def decide(request):
        state["decisions"].append(copy.deepcopy(request))
        state["node"]["admission_state"] = request["decision"]
        return {"ok": True, "node": copy.deepcopy(state["node"])}

    def reconcile(_config):
        state["reconciles"] += 1
        return True

    def get_worker_mirror(_scope):
        return None

    def enqueue_outbox(_record, **_fence):
        return {"ok": True}

    context = {
        "site_id": operational_site_id,
        "instance_id": "master-01",
        "authority_mode": str(kwargs.get("authority_mode") or "master_global"),
        "allowed_worker_node_ids": tuple(
            kwargs.get("allowed_worker_node_ids", ())),
        "legacy_coordinator_node_id": "master-01",
        "authority_store": {
            "get_worker_mirror": get_worker_mirror,
            "enqueue_outbox": enqueue_outbox,
        },
        "config_reader": config_reader,
        "authority_reader": authority_reader,
        "nodes": {
            "get": get_node,
            "register": register,
            "decide_admission": decide,
        },
        "ownership": {"reconcile": reconcile},
        "auto_admit_broker_bound_workers": True,
        "identity_proof_max_age_s": 45.0,
        "broker_identity_proofs": {},
        "event_publisher": None,
    }
    return context, state


def _proof(node_id="fn-01"):
    return {
        "source_node": node_id,
        "broker_bound_topic_node": node_id,
        "message_id": "proof-01",
    }


def _registration():
    return {
        "source_node": "fn-01",
        "message_id": "registration-01",
        "payload": {
            "node_id": "fn-01",
            "authority_revision": 1,
            "fingerprint": "config-01",
        },
    }


def _test_proof_before_registration_auto_approves():
    context, state = _coordinator_fixture()
    assert handle_broker_identity_heartbeat(context, _proof())
    result = handle_legacy_registration(context, _registration())
    assert result["ok"]
    assert state["node"]["admission_state"] == "APPROVED"
    assert state["reconciles"] == 1
    decision = state["decisions"][0]
    assert decision["principal_subject"] == "system:broker-mtls-auto-admission"
    assert decision["reason"] == "broker-bound live mTLS heartbeat proof"


def _test_operational_site_identity_is_independent_of_business_site_label():
    context, state = _coordinator_fixture(
        site_id="site-main", config_site_id="site_main")
    assert handle_broker_identity_heartbeat(context, _proof())
    result = handle_legacy_registration(context, _registration())
    assert result["ok"]
    assert state["node"]["certificate_identity"] \
        == "urn:automation:site-main:worker:fn-01"
    assert state["node"]["admission_state"] == "APPROVED"


def _test_node_local_admission_uses_explicit_registry_allowlist():
    context, state = _coordinator_fixture(
        site_id="site-main",
        authority_mode="node_local",
        allowed_worker_node_ids=("fn-01",),
        empty_config_nodes=True,
    )
    assert handle_broker_identity_heartbeat(context, _proof())
    result = handle_legacy_registration(context, _registration())
    assert result["ok"]
    assert state["node"]["admission_state"] == "APPROVED"

    context, state = _coordinator_fixture(
        authority_mode="node_local",
        allowed_worker_node_ids=(),
        empty_config_nodes=True,
    )
    assert not handle_broker_identity_heartbeat(context, _proof())
    assert not state["decisions"]


def _test_unconfigured_and_quarantined_nodes_are_never_approved():
    context, state = _coordinator_fixture()
    assert not handle_broker_identity_heartbeat(context, _proof("fn-99"))
    assert not state["decisions"]
    context, state = _coordinator_fixture(node={
        "node_id": "fn-01",
        "certificate_identity": "urn:automation:site_main:worker:fn-01",
        "admission_state": "QUARANTINED",
        "desired_state": "QUARANTINED",
    })
    assert not handle_broker_identity_heartbeat(context, _proof())
    assert state["node"]["admission_state"] == "QUARANTINED"
    assert not state["decisions"]


def _test_registration_without_live_proof_remains_pending():
    context, state = _coordinator_fixture()
    result = handle_legacy_registration(context, _registration())
    assert not result["ok"] and result["code"] == "authorization_denied"
    assert state["node"]["admission_state"] == "PENDING"
    assert not state["decisions"]


def main():
    tests = (
        _test_mqtt_identity_proof_validation,
        _test_proof_before_registration_auto_approves,
        _test_operational_site_identity_is_independent_of_business_site_label,
        _test_node_local_admission_uses_explicit_registry_allowlist,
        _test_unconfigured_and_quarantined_nodes_are_never_approved,
        _test_registration_without_live_proof_remains_pending,
    )
    for test in tests:
        test()
    print("PASS test_worker_auto_admission ({} scenarios)".format(len(tests)))


if __name__ == "__main__":
    main()

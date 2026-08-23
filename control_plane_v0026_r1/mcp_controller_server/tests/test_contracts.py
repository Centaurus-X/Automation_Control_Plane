# -*- coding: utf-8 -*-

"""Automation envelope, topic and node-local proposal tests."""

import datetime
import uuid

from mcp_controller_server.automation_contract import (
    control_event_filter,
    make_envelope,
    master_command_topic,
    proxy_ack_topic,
    validate_envelope,
    validate_payload,
)
from mcp_controller_server.canonical import fingerprint


def test_topics_are_exact_and_uninjectable():
    message_id = str(uuid.uuid4())
    assert master_command_topic("site-main", "master-01", "runtime.status") == (
        "automation/v1/site-main/master/master-01/commands/runtime/status")
    assert proxy_ack_topic("site-main", "proxy-01", "master-01", message_id) == (
        "automation/v1/site-main/proxy/proxy-01/acks/master-01/" + message_id)
    assert control_event_filter("site-main", "master-01") == (
        "automation/v1/site-main/events/+/master-01")
    try:
        master_command_topic("site-main/#", "master-01", "runtime.status")
    except ValueError:
        pass
    else:
        raise AssertionError("MQTT topic wildcard injection was accepted")


def test_envelope_lifetime_identity_and_payload():
    now = datetime.datetime(2026, 8, 12, 0, 0, tzinfo=datetime.timezone.utc)
    envelope = make_envelope(
        "node.drain",
        {"service": "proxy", "instance_id": "proxy-01"},
        {"service": "master", "instance_id": "master-01"},
        site_id="site-main",
        now=now,
        payload={"node_id": "fn-01", "deadline_s": 60},
    )
    assert validate_envelope(
        envelope, expected_site_id="site-main", now=now)["ok"]
    envelope["source"]["instance_id"] = "proxy/#"
    assert validate_envelope(envelope, expected_site_id="site-main", now=now)["ok"] is False


def test_node_local_config_proposal_is_revision_bound():
    entity = {"id": "sensor-1", "enabled": True}
    payload = {
        "target_node_id": "fn-01",
        "operation": "scope_operations",
        "operation_format": "config_scope_operations_v1",
        "scope": "node:fn-01",
        "base_authority_epoch": 4,
        "base_scope_revision": 12,
        "base_scope_fingerprint": "a" * 64,
        "reason": "validated test",
        "operations": [{
            "op": "upsert_entity",
            "section": "sensors",
            "entity_key": "sensor-1",
            "value": entity,
            "value_hash": fingerprint(entity),
        }],
    }
    assert validate_payload("config.propose", payload)["ok"]
    payload["base_scope_fingerprint"] = "stale"
    assert not validate_payload("config.propose", payload)["ok"]
    assert validate_payload("terminal.execute", {"command": "status"})["ok"] is True

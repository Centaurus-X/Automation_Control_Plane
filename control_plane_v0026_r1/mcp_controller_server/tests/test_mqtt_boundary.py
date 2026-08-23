# -*- coding: utf-8 -*-

"""Inbound MQTT identity, retained-message and event-cache boundary tests."""

import tempfile
import uuid

from mcp_controller_server.automation_contract import control_event_topic, make_envelope
from mcp_controller_server.canonical import canonical_bytes
from mcp_controller_server.runtime import create_runtime
from tests.support import make_test_config, make_transport_factory


def _event_message(config, retain=False, source_instance=None):
    controller_uuid = str(uuid.uuid4())
    envelope = make_envelope(
        "sensor.value",
        {
            "service": "master",
            "instance_id": source_instance or config["mqtt"]["master_instance_id"],
        },
        {
            "service": "proxy",
            "instance_id": config["mqtt"]["source_instance_id"],
        },
        site_id=config["mqtt"]["site_id"],
        payload={
            "controller_uuid": controller_uuid,
            "sensor_id": "temperature",
            "value": 21.5,
            "quality": "good",
            "source_node_id": "fn-01",
        },
    )
    topic = control_event_topic(
        config["mqtt"]["site_id"], "sensor.value",
        config["mqtt"]["master_instance_id"],
    )
    return {
        "topic": topic,
        "payload": canonical_bytes(envelope),
        "qos": 1,
        "retain": bool(retain),
    }


def test_inbound_mqtt_requires_exact_master_identity_topic_and_nonretained_frame():
    with tempfile.TemporaryDirectory() as directory:
        config = make_test_config(directory)
        runtime = create_runtime(
            config,
            transport_factory=make_transport_factory({}, config),
            auth_state={"test": True},
        )
        retained = runtime["mqtt"]["process_message"](
            _event_message(config, retain=True))
        assert not retained["ok"] and retained["code"] == "invalid_envelope"
        wrong_source = runtime["mqtt"]["process_message"](
            _event_message(config, source_instance="master-02"))
        assert not wrong_source["ok"] and wrong_source["code"] == "identity_mismatch"
        wrong_topic = _event_message(config)
        wrong_topic["topic"] = wrong_topic["topic"] + "/unexpected"
        rejected = runtime["mqtt"]["process_message"](wrong_topic)
        assert not rejected["ok"] and rejected["code"] == "identity_mismatch"
        accepted = runtime["mqtt"]["process_message"](_event_message(config))
        assert accepted["ok"] and accepted["handled"]
        snapshot = runtime["mqtt"]["snapshot"]()
        assert snapshot["cache"]["retained_sensors"] == 1
        assert snapshot["statistics"]["invalid"] == 3

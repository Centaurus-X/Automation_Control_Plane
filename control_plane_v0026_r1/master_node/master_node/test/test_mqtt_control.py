# -*- coding: utf-8 -*-

"""Functional control-MQTT topic, envelope and ACK correlation tests."""

import datetime

from src.library._generic_system_contract_library.protocol_contracts import make_envelope
from src.library._generic_type_library import decode_json_bytes
from src.library._generic_system_contract_library.protocol_contracts import master_command_topic
from src.interfaces.mqtt_control_interface import _process_message, create_control_mqtt_interface


def _transport_factory(**kwargs):
    published = kwargs.pop("published") if "published" in kwargs else []

    def publish(topic, payload, **options):
        published.append({"topic": topic, "payload": payload, "options": options})
        return True

    def start():
        return True

    def stop(**_kwargs):
        return True

    def wait_connected(_timeout):
        return True

    def snapshot():
        return {"connected": True}

    return {
        "publish": publish,
        "start": start,
        "stop": stop,
        "wait_connected": wait_connected,
        "snapshot": snapshot,
    }


def _test_valid_and_mismatched_topics():
    published = []

    def factory(**kwargs):
        transport = _transport_factory(**kwargs)
        original = transport["publish"]

        def publish(topic, payload, **options):
            published.append({"topic": topic, "payload": payload})
            return original(topic, payload, **options)

        transport["publish"] = publish
        return transport

    def handler(_envelope):
        return {
            "ok": True,
            "generated_at": datetime.datetime.now(datetime.timezone.utc),
        }

    api = create_control_mqtt_interface(
        site_id="site_main",
        instance_id="master-01",
        options={
            "host": "127.0.0.1",
            "port": 1883,
            "allow_insecure_transport": True,
            "tls": {"enabled": False},
        },
        handler=handler,
        authority_reader=authority_fixture,
        allowed_proxy_instance_ids=("proxy-01",),
        transport_factory=factory,
    )
    envelope = make_envelope(
        "nodes.list",
        {"service": "proxy", "instance_id": "proxy-01"},
        {"service": "master", "instance_id": "master-01"},
        site_id="site_main",
        principal={
            "subject": "00000000-0000-0000-0000-000000000001",
            "permissions": ["node.read"],
            "site_scopes": ["site_main"],
        },
        payload={},
    )
    from src.library._generic_type_library import canonical_bytes

    message = {
        "topic": master_command_topic("site_main", "master-01", "nodes.list"),
        "payload": canonical_bytes(envelope),
        "retain": False,
    }
    result = _process_message(api["context"], message)
    assert result["ok"] and len(published) == 1
    ack = decode_json_bytes(published[-1]["payload"])
    assert ack["causation_id"] == envelope["message_id"]
    assert ack["payload"]["result"]["generated_at"].endswith("+00:00")
    message["topic"] = master_command_topic("site_main", "master-01", "runtime.status")
    rejected = _process_message(api["context"], message)
    assert rejected["code"] == "identity_mismatch"


def authority_fixture():
    return {"authority_term": 3, "config_revision": 9}


def main():
    _test_valid_and_mismatched_topics()
    print("PASS test_mqtt_control (1 scenario)")


if __name__ == "__main__":
    main()

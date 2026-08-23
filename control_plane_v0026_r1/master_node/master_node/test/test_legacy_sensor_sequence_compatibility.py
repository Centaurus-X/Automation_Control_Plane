# -*- coding: utf-8 -*-

"""v0082 sensor events must preserve their monotone source sequence."""

import copy

from src.core.coordinator_service import handle_legacy_sensor_event


CONTROLLER_UUID = "62671112-766c-59ed-b0ff-30a49d48f747"


def _context():
    captured = {
        "last_sequence": None,
        "writes": [],
        "events": [],
        "routed": [],
    }

    def resolve(controller_uuid):
        if str(controller_uuid) != CONTROLLER_UUID:
            return None
        return {"owner_node_id": "fn-01"}

    def upsert_sensor(request):
        value = copy.deepcopy(request)
        sequence = value.get("source_sequence")
        last_sequence = captured["last_sequence"]
        updated = sequence is not None and (
            last_sequence is None or sequence > last_sequence
        )
        if updated:
            captured["last_sequence"] = sequence
            captured["writes"].append(value)
        return {"ok": True, "updated": updated, "stale": not updated}

    def route(payload, **kwargs):
        captured["routed"].append({
            "payload": copy.deepcopy(payload),
            "source_node": kwargs.get("source_node"),
        })
        return {"ok": True}

    def publish(event_type, payload, **kwargs):
        captured["events"].append({
            "event_type": event_type,
            "payload": copy.deepcopy(payload),
            "principal": copy.deepcopy(kwargs.get("principal")),
        })
        return True

    def authority_reader():
        return {
            "authority_term": 7,
            "config_revision": 11,
        }

    context = {
        "site_id": "site_main",
        "instance_id": "master-01",
        "authority_reader": authority_reader,
        "ownership": {"resolve": resolve},
        "node_store": {"upsert_sensor": upsert_sensor},
        "cross_router": {"route": route},
        "event_publisher": publish,
    }
    return context, captured


def _envelope(sequence, **kwargs):
    payload = {
        "controller_uuid": CONTROLLER_UUID,
        "sensor_id": 1,
        "value": kwargs.get("value", sequence),
        "quality": "good",
        "sensor_store_sequence": sequence,
        "timestamp": "2026-07-19T06:55:00+00:00",
    }
    if "legacy_sequence" in kwargs:
        payload["sequence"] = kwargs["legacy_sequence"]
    return {
        "event_type": "SENSOR_VALUE_UPDATE",
        "message_id": "sensor-event-{}".format(sequence),
        "source_node": "fn-01",
        "payload": {
            "event_type": "SENSOR_VALUE_UPDATE",
            "payload": payload,
        },
    }


def _test_v0082_sensor_store_sequence_advances_latest_state_and_wss_event_source():
    context, captured = _context()
    assert handle_legacy_sensor_event(context, _envelope(101)) is True
    assert handle_legacy_sensor_event(context, _envelope(102)) is True
    assert [item["source_sequence"] for item in captured["writes"]] == [101, 102]
    assert captured["last_sequence"] == 102
    assert [item["event_type"] for item in captured["events"]] == [
        "sensor.value", "sensor.value",
    ]
    assert len(captured["routed"]) == 2


def _test_legacy_sequence_keeps_precedence_when_both_are_present():
    context, captured = _context()
    assert handle_legacy_sensor_event(
        context,
        _envelope(901, legacy_sequence=41, value=1.5),
    ) is True
    assert captured["writes"][0]["source_sequence"] == 41


def _test_invalid_or_missing_sequences_are_rejected_before_persistence():
    invalid_values = (None, 0, -1, True, 1.5, "103")
    for index, invalid in enumerate(invalid_values, start=1):
        context, captured = _context()
        envelope = _envelope(1000 + index)
        payload = envelope["payload"]["payload"]
        payload.pop("sensor_store_sequence", None)
        if invalid is not None:
            payload["sensor_store_sequence"] = invalid
        assert handle_legacy_sensor_event(context, envelope) is False
        assert captured["writes"] == []
        assert captured["events"] == []
        assert captured["routed"] == []


def main():
    _test_v0082_sensor_store_sequence_advances_latest_state_and_wss_event_source()
    _test_legacy_sequence_keeps_precedence_when_both_are_present()
    _test_invalid_or_missing_sequences_are_rejected_before_persistence()
    print("PASS v0082 sensor_store_sequence compatibility and live event continuity")


if __name__ == "__main__":
    main()

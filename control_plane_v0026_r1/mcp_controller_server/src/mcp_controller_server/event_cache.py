# -*- coding: utf-8 -*-

"""Bounded in-memory cache for normalized Master events and latest sensors."""

import copy
import threading
import time

from collections import deque


def create_event_cache(event_limit, sensor_limit):
    return {
        "lock": threading.RLock(),
        "events": deque(maxlen=max(1, int(event_limit))),
        "sensors": {},
        "sensor_order": deque(),
        "sensor_limit": max(1, int(sensor_limit)),
        "event_count": 0,
        "sensor_updates": 0,
    }


def _sensor_identity(payload):
    controller_uuid = str(payload.get("controller_uuid") or "")
    sensor_id = payload.get("sensor_id")
    if not controller_uuid or sensor_id is None:
        return None
    return controller_uuid + ":" + str(sensor_id)


def _remember_sensor(context, record):
    if record["event_type"] != "sensor.value":
        return False
    payload = record.get("payload") if isinstance(record.get("payload"), dict) else {}
    identity = _sensor_identity(payload)
    if identity is None:
        return False
    if identity in context["sensors"]:
        try:
            context["sensor_order"].remove(identity)
        except ValueError:
            pass
    context["sensor_order"].append(identity)
    context["sensors"][identity] = copy.deepcopy(record)
    while len(context["sensors"]) > context["sensor_limit"]:
        expired = context["sensor_order"].popleft()
        context["sensors"].pop(expired, None)
    context["sensor_updates"] += 1
    return True


def record_event(context, envelope, received_at_s=None):
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    record = {
        "message_id": str(envelope.get("message_id") or ""),
        "correlation_id": str(envelope.get("correlation_id") or ""),
        "event_type": str(envelope.get("event_type") or ""),
        "issued_at": envelope.get("issued_at"),
        "received_at_s": time.time() if received_at_s is None else float(received_at_s),
        "principal": copy.deepcopy(envelope.get("principal")),
        "payload": copy.deepcopy(payload),
    }
    with context["lock"]:
        context["events"].append(record)
        context["event_count"] += 1
        _remember_sensor(context, record)
    return copy.deepcopy(record)


def recent_events(context, limit=100, event_types=None):
    bounded = max(1, min(1000, int(limit)))
    selected_types = {str(value) for value in event_types or ()}
    with context["lock"]:
        records = [
            copy.deepcopy(record) for record in reversed(context["events"])
            if not selected_types or record["event_type"] in selected_types
        ]
    return records[:bounded]


def latest_sensors(context, controller_uuid=None, sensor_id=None, limit=500):
    bounded = max(1, min(5000, int(limit)))
    controller = str(controller_uuid) if controller_uuid is not None else None
    sensor = str(sensor_id) if sensor_id is not None else None
    with context["lock"]:
        records = []
        for identity in reversed(context["sensor_order"]):
            record = context["sensors"].get(identity)
            if not record:
                continue
            payload = record["payload"]
            if controller is not None and str(payload.get("controller_uuid")) != controller:
                continue
            if sensor is not None and str(payload.get("sensor_id")) != sensor:
                continue
            records.append(copy.deepcopy(record))
            if len(records) >= bounded:
                break
    return records


def snapshot_cache(context):
    with context["lock"]:
        return {
            "retained_events": len(context["events"]),
            "retained_sensors": len(context["sensors"]),
            "event_count": context["event_count"],
            "sensor_updates": context["sensor_updates"],
        }

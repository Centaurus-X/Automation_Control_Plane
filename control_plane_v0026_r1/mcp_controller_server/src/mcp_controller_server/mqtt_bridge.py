# -*- coding: utf-8 -*-

"""Strict MQTT command, ACK and normalized event bridge for MCP tools."""

import copy
import threading
import time

from functools import partial
from queue import Empty, Full, Queue

from mcp_controller_server._lightweight_mqtt_client import create_mqtt_client
from mcp_controller_server.automation_contract import (
    control_event_filter,
    control_event_topic,
    make_envelope,
    master_command_topic,
    proxy_ack_filter,
    proxy_ack_topic,
    validate_envelope,
    validate_payload,
)
from mcp_controller_server.canonical import canonical_bytes, decode_json_bytes
from mcp_controller_server.errors import make_error, make_success
from mcp_controller_server.event_cache import record_event, snapshot_cache


def _transport_options(context):
    options = context["options"]
    return {
        "client_id": "automation-mcp-controller-" + context["instance_id"],
        "host": options["host"],
        "port": int(options["port"]),
        "keepalive_s": int(options["keepalive_s"]),
        "connect_timeout_s": float(options["connect_timeout_s"]),
        "socket_timeout_s": float(options["socket_timeout_s"]),
        "reconnect_min_s": float(options["reconnect_min_s"]),
        "reconnect_max_s": float(options["reconnect_max_s"]),
        "outbound_queue_maxsize": int(options["outbound_queue_maxsize"]),
        "max_packet_size": int(options["max_packet_size"]),
        "allow_insecure_transport": bool(options["allow_insecure_transport"]),
        "tls": copy.deepcopy(options["tls"]),
        "subscriptions": [
            {
                "topic": proxy_ack_filter(
                    context["site_id"], context["source_instance_id"]),
                "qos": 1,
            },
            {
                "topic": control_event_filter(
                    context["site_id"], context["master_instance_id"]),
                "qos": 1,
            },
        ],
    }


def _on_message(context, message):
    if context["stop_event"].is_set():
        return False
    try:
        context["queue"].put_nowait(copy.deepcopy(message))
        return True
    except Full:
        with context["lock"]:
            context["statistics"]["queue_rejected"] += 1
        return False


def _on_connect(context, _snapshot):
    with context["lock"]:
        context["state"] = "connected"
        context["last_error"] = None
    context["connected_event"].set()


def _on_disconnect(context, reason):
    with context["lock"]:
        context["state"] = "disconnected"
        context["last_error"] = str(reason)
    context["connected_event"].clear()


def _publish(context, topic, envelope):
    try:
        return bool(context["transport"]["publish"](
            topic, canonical_bytes(envelope), qos=1, retain=False))
    except Exception as exc:
        with context["lock"]:
            context["last_error"] = str(exc)
        return False


def _decode_envelope(context, message):
    if bool(message.get("retain")):
        return None, make_error("invalid_envelope", "retained control messages are forbidden")
    try:
        envelope = decode_json_bytes(
            message.get("payload") or b"",
            maximum_bytes=context["options"]["max_packet_size"],
            require_object=True,
        )
    except Exception as exc:
        return None, make_error("invalid_envelope", str(exc))
    checked = validate_envelope(
        envelope,
        expected_site_id=context["site_id"],
        maximum_bytes=context["options"]["max_packet_size"],
        allow_unknown_event=True,
    )
    if not checked.get("ok"):
        return None, checked
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    target = envelope.get("target") if isinstance(envelope.get("target"), dict) else {}
    if source.get("service") != "master" \
            or source.get("instance_id") != context["master_instance_id"]:
        return None, make_error("identity_mismatch", "message source is not the configured Master")
    if target.get("service") != "proxy" or target.get("instance_id") not in (
            context["source_instance_id"], "broadcast", "*"):
        return None, make_error("identity_mismatch", "message target is not this MQTT identity")
    try:
        if envelope.get("event_type") == "command.ack":
            expected_topic = proxy_ack_topic(
                context["site_id"], context["source_instance_id"],
                context["master_instance_id"], envelope.get("causation_id"),
            )
        else:
            expected_topic = control_event_topic(
                context["site_id"], envelope.get("event_type"),
                context["master_instance_id"],
            )
    except ValueError as exc:
        return None, make_error("invalid_envelope", str(exc))
    if str(message.get("topic") or "") != expected_topic:
        return None, make_error("identity_mismatch", "MQTT topic and envelope differ")
    return envelope, None


def _handle_ack(context, envelope):
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    if set(payload) != {"command", "result"} or not isinstance(payload.get("result"), dict):
        return False
    request_id = str(envelope.get("causation_id") or "")
    with context["lock"]:
        pending = context["pending"].get(request_id)
        if pending is None:
            context["statistics"]["uncorrelated_acks"] += 1
            return False
        correlated = (
            str(pending["envelope"].get("correlation_id") or "")
            == str(envelope.get("correlation_id") or "")
            and str(pending["envelope"].get("event_type") or "")
            == str(payload.get("command") or "")
        )
        if not correlated:
            context["statistics"]["invalid"] += 1
            return False
        pending["result"] = copy.deepcopy(payload["result"])
        pending["event"].set()
        context["statistics"]["acks_received"] += 1
    return True


def _handle_event(context, envelope):
    record_event(context["event_cache"], envelope)
    with context["lock"]:
        context["statistics"]["events_received"] += 1
    return True


def process_mqtt_message(context, message):
    envelope, error = _decode_envelope(context, message)
    if error:
        with context["lock"]:
            context["statistics"]["invalid"] += 1
            context["last_error"] = error.get("message")
        return error
    handled = _handle_ack(context, envelope) \
        if envelope.get("event_type") == "command.ack" \
        else _handle_event(context, envelope)
    return make_success(handled=bool(handled))


def _message_loop(context):
    with context["lock"]:
        context["state"] = "running"
    while not context["stop_event"].is_set():
        try:
            message = context["queue"].get(timeout=0.2)
        except Empty:
            continue
        try:
            process_mqtt_message(context, message)
        except Exception as exc:
            with context["lock"]:
                context["statistics"]["invalid"] += 1
                context["last_error"] = str(exc)
        finally:
            context["queue"].task_done()
    with context["lock"]:
        context["state"] = "stopped"


def send_command(context, event_type, payload, principal, **kwargs):
    checked = validate_payload(event_type, payload)
    if not checked.get("ok"):
        return checked
    timeout_s = float(kwargs.get("timeout_s", context["options"]["command_timeout_s"]))
    timeout_s = max(0.1, min(300.0, timeout_s))
    if context["options"]["required"] and not context["connected_event"].is_set():
        if not context["transport"]["wait_connected"](
                min(timeout_s, float(context["options"]["connect_timeout_s"]))):
            return make_error(
                "not_ready", "required MQTT connection is unavailable", retryable=True)
    envelope = make_envelope(
        event_type,
        {"service": "proxy", "instance_id": context["source_instance_id"]},
        {"service": "master", "instance_id": context["master_instance_id"]},
        site_id=context["site_id"],
        message_id=kwargs.get("message_id"),
        correlation_id=kwargs.get("correlation_id"),
        idempotency_key=kwargs.get("idempotency_key"),
        principal=copy.deepcopy(principal),
        payload=copy.deepcopy(payload),
        ttl_s=min(300.0, max(30.0, timeout_s + 10.0)),
    )
    envelope_checked = validate_envelope(
        envelope,
        expected_site_id=context["site_id"],
        maximum_bytes=context["options"]["max_packet_size"],
    )
    if not envelope_checked.get("ok"):
        return envelope_checked
    request_id = envelope["message_id"]
    waiter = {"event": threading.Event(), "result": None, "envelope": envelope}
    with context["lock"]:
        if len(context["pending"]) >= context["options"]["max_pending_requests"]:
            context["statistics"]["pending_rejected"] += 1
            return make_error("overloaded", "pending MQTT request limit reached", retryable=True)
        if request_id in context["pending"]:
            return make_error("duplicate_request", "request identifier is already pending")
        context["pending"][request_id] = waiter
    topic = master_command_topic(
        context["site_id"], context["master_instance_id"], event_type)
    deadline = time.monotonic() + timeout_s
    attempts = 0
    retry_interval = float(context["options"]["retry_interval_s"])
    try:
        while time.monotonic() < deadline and not context["stop_event"].is_set():
            attempts += 1
            published = _publish(context, topic, envelope)
            with context["lock"]:
                context["statistics"]["commands_published"] += int(published)
                context["statistics"]["publish_failed"] += int(not published)
            remaining = max(0.0, deadline - time.monotonic())
            if waiter["event"].wait(min(retry_interval, remaining)):
                result = copy.deepcopy(waiter["result"])
                result.setdefault("request_id", request_id)
                result.setdefault("mqtt_attempts", attempts)
                return result
        return make_error(
            "command_timeout", "Master application ACK was not received in time",
            retryable=True, request_id=request_id, mqtt_attempts=attempts,
        )
    finally:
        with context["lock"]:
            context["pending"].pop(request_id, None)


def start_mqtt_bridge(context):
    thread = context.get("thread")
    if thread and thread.is_alive():
        return thread
    context["stop_event"].clear()
    thread = threading.Thread(
        target=partial(_message_loop, context),
        name="MCPControllerMQTT-" + context["instance_id"],
        daemon=False,
    )
    context["thread"] = thread
    thread.start()
    context["transport"]["start"]()
    return thread


def stop_mqtt_bridge(context, join_timeout_s=5.0):
    context["stop_event"].set()
    context["transport"]["stop"](join_timeout_s=float(join_timeout_s))
    with context["lock"]:
        pending = tuple(context["pending"].values())
    for waiter in pending:
        waiter["result"] = make_error(
            "not_ready", "MQTT bridge stopped", retryable=True)
        waiter["event"].set()
    thread = context.get("thread")
    if thread and thread is not threading.current_thread():
        thread.join(timeout=float(join_timeout_s))
    return snapshot_mqtt_bridge(context)


def snapshot_mqtt_bridge(context):
    with context["lock"]:
        return {
            "state": context["state"],
            "connected": context["connected_event"].is_set(),
            "queue_depth": context["queue"].qsize(),
            "pending_requests": len(context["pending"]),
            "thread_alive": bool(context["thread"] and context["thread"].is_alive()),
            "site_id": context["site_id"],
            "source_service": "proxy",
            "source_instance_id": context["source_instance_id"],
            "master_instance_id": context["master_instance_id"],
            "statistics": copy.deepcopy(context["statistics"]),
            "cache": snapshot_cache(context["event_cache"]),
            "transport": context["transport"]["snapshot"](),
            "last_error": context["last_error"],
        }


def create_mqtt_bridge(options, instance_id, event_cache, transport_factory=None):
    context = {
        "options": copy.deepcopy(options),
        "instance_id": str(instance_id),
        "site_id": options["site_id"],
        "source_instance_id": options["source_instance_id"],
        "master_instance_id": options["master_instance_id"],
        "event_cache": event_cache,
        "lock": threading.RLock(),
        "stop_event": threading.Event(),
        "connected_event": threading.Event(),
        "queue": Queue(maxsize=int(options["message_queue_maxsize"])),
        "pending": {},
        "thread": None,
        "transport": None,
        "state": "created",
        "last_error": None,
        "statistics": {
            "commands_published": 0,
            "publish_failed": 0,
            "acks_received": 0,
            "uncorrelated_acks": 0,
            "events_received": 0,
            "invalid": 0,
            "queue_rejected": 0,
            "pending_rejected": 0,
        },
    }
    factory = transport_factory or create_mqtt_client
    context["transport"] = factory(
        on_message=partial(_on_message, context),
        on_connect=partial(_on_connect, context),
        on_disconnect=partial(_on_disconnect, context),
        **_transport_options(context)
    )
    return {
        "context": context,
        "start": partial(start_mqtt_bridge, context),
        "stop": partial(stop_mqtt_bridge, context),
        "send_command": partial(send_command, context),
        "snapshot": partial(snapshot_mqtt_bridge, context),
        "process_message": partial(process_mqtt_message, context),
    }

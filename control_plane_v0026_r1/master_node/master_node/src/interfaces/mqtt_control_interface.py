# -*- coding: utf-8 -*-

# master_node/src/interfaces/mqtt_control_interface.py

"""MQTTS boundary for strict automation.control.v1 proxy commands."""

import copy
import os
import threading

from functools import partial
from queue import Empty, Full, Queue

from src.library._generic_system_contract_library.protocol_contracts import make_envelope, validate_envelope
from src.library._generic_system_contract_library.errors import make_error
from src.library._generic_type_library import canonical_bytes, decode_json_bytes, json_ready
from src.library._generic_system_contract_library.protocol_contracts import (
    control_event_topic,
    master_command_filter,
    master_command_topic,
    proxy_ack_topic,
)
from src.library._lightweight_mqtt_client import create_mqtt_client


def _validate_tls(options):
    tls = options.get("tls") if isinstance(options.get("tls"), dict) else {}
    if not tls.get("enabled", True) and not options.get("allow_insecure_transport", False):
        raise ValueError("unencrypted MQTT requires an explicit unsafe test flag")
    for key in ("ca_file", "cert_file", "key_file"):
        path = tls.get(key)
        if path and not os.path.isfile(path):
            raise ValueError("MQTT TLS file does not exist: " + str(path))
    if bool(tls.get("cert_file")) != bool(tls.get("key_file")):
        raise ValueError("MQTT mTLS certificate and key must be configured together")


def _transport_options(context):
    options = context["options"]
    tls = copy.deepcopy(options.get("tls") or {})
    return {
        "client_id": "automation-master-control-" + context["instance_id"],
        "host": options["host"],
        "port": int(options.get("port", 8883)),
        "keepalive_s": int(options.get("keepalive_s", 30)),
        "connect_timeout_s": float(options.get("connect_timeout_s", 5.0)),
        "socket_timeout_s": float(options.get("socket_timeout_s", 2.0)),
        "reconnect_min_s": float(options.get("reconnect_min_s", 1.0)),
        "reconnect_max_s": float(options.get("reconnect_max_s", 30.0)),
        "outbound_queue_maxsize": int(options.get("outbound_queue_maxsize", 4096)),
        "max_packet_size": int(options.get("max_packet_size", 4 * 1024 * 1024)),
        "allow_insecure_transport": bool(options.get("allow_insecure_transport", False)),
        "tls": tls,
        "subscriptions": [{
            "topic": master_command_filter(context["site_id"], context["instance_id"]),
            "qos": 1,
        }],
    }


def _on_transport_message(context, message):
    if context["stop_event"].is_set():
        return False
    try:
        context["queue"].put_nowait(copy.deepcopy(message))
        return True
    except Full:
        with context["lock"]:
            context["statistics"]["queue_rejected"] += 1
        return False


def _on_transport_connect(context, _snapshot):
    with context["lock"]:
        context["state"] = "connected"
    context["connected_event"].set()


def _on_transport_disconnect(context, reason):
    with context["lock"]:
        context["state"] = "disconnected"
        context["last_error"] = str(reason)
    context["connected_event"].clear()


def _publish(context, topic, envelope, **kwargs):
    try:
        return bool(context["transport"]["publish"](
            topic, canonical_bytes(envelope),
            qos=int(kwargs.get("qos", 1)), retain=bool(kwargs.get("retain", False)),
        ))
    except Exception as exc:
        with context["lock"]:
            context["last_error"] = str(exc)
        return False


def _ack_envelope(context, command, result):
    authority = context["authority_reader"]() or {}
    return make_envelope(
        "command.ack",
        {"service": "master", "instance_id": context["instance_id"]},
        copy.deepcopy(command["source"]),
        site_id=context["site_id"],
        correlation_id=command["correlation_id"],
        causation_id=command["message_id"],
        idempotency_key=command.get("idempotency_key"),
        principal=copy.deepcopy(command.get("principal")),
        authority_term=int(authority.get("authority_term") or 0),
        config_revision=int(authority.get("config_revision") or 0),
        payload={"command": command["event_type"], "result": json_ready(result)},
        ttl_s=30.0,
    )


def _process_message(context, message):
    try:
        envelope = decode_json_bytes(
            message.get("payload") or b"",
            maximum_bytes=context["options"].get("max_packet_size", 4 * 1024 * 1024),
        )
    except Exception as exc:
        with context["lock"]:
            context["statistics"]["invalid"] += 1
        return make_error("invalid_envelope", str(exc))
    try:
        checked = validate_envelope(
            envelope,
            expected_site_id=context["site_id"],
            maximum_bytes=context["options"].get("max_packet_size", 4 * 1024 * 1024),
        )
    except Exception as exc:
        with context["lock"]:
            context["statistics"]["invalid"] += 1
        return make_error("invalid_envelope", str(exc))
    target = envelope.get("target") if isinstance(envelope.get("target"), dict) else {}
    valid_target = target.get("service") == "master" and target.get("instance_id") in (
        context["instance_id"], "*", "broadcast",
    )
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    can_ack = (
        source.get("service") == "proxy"
        and bool(source.get("instance_id"))
        and bool(envelope.get("message_id"))
        and bool(envelope.get("correlation_id"))
    )
    if not can_ack:
        with context["lock"]:
            context["statistics"]["invalid"] += 1
        return make_error("invalid_envelope", "message cannot be safely acknowledged")
    try:
        expected_topic = master_command_topic(
            context["site_id"], context["instance_id"], envelope.get("event_type"))
    except ValueError:
        expected_topic = ""
    valid_topic = str(message.get("topic") or "") == expected_topic
    retained = bool(message.get("retain"))
    source_allowed = (
        not context["allowed_proxy_instance_ids"]
        or source.get("instance_id") in context["allowed_proxy_instance_ids"]
    )
    if (
            not checked.get("ok") or not valid_target
            or source.get("service") != "proxy" or not source_allowed
            or not valid_topic or retained):
        result = checked if not checked.get("ok") else make_error(
            "identity_mismatch", "control message source, target or topic is invalid")
    else:
        try:
            result = context["handler"](copy.deepcopy(envelope))
        except Exception as exc:
            with context["lock"]:
                context["last_error"] = str(exc)
            result = make_error(
                "internal_error", "control command failed before durable completion",
                retryable=True)
    ack = _ack_envelope(context, envelope, result)
    topic = proxy_ack_topic(
        context["site_id"], source.get("instance_id"),
        context["instance_id"], envelope.get("message_id"),
    )
    published = _publish(context, topic, ack, qos=1)
    with context["lock"]:
        context["statistics"]["processed"] += 1
        context["statistics"]["acks_published"] += int(published)
    return result


def _message_loop(context):
    with context["lock"]:
        context["state"] = "running"
    while not context["stop_event"].is_set():
        try:
            message = context["queue"].get(timeout=0.2)
        except Empty:
            continue
        try:
            _process_message(context, message)
        except Exception as exc:
            with context["lock"]:
                context["last_error"] = str(exc)
                context["statistics"]["invalid"] += 1
        finally:
            context["queue"].task_done()
    with context["lock"]:
        context["state"] = "stopped"


def publish_control_event(context, event_type, payload, **kwargs):
    """Publish one non-retained logical master event."""
    authority = context["authority_reader"]() or {}
    envelope = make_envelope(
        event_type,
        {"service": "master", "instance_id": context["instance_id"]},
        {"service": str(kwargs.get("target_service") or "proxy"),
         "instance_id": str(kwargs.get("target_instance_id") or "broadcast")},
        site_id=context["site_id"],
        correlation_id=kwargs.get("correlation_id"),
        causation_id=kwargs.get("causation_id"),
        principal=copy.deepcopy(kwargs.get("principal")),
        authority_term=int(authority.get("authority_term") or 0),
        config_revision=int(authority.get("config_revision") or 0),
        payload=json_ready(payload),
        ttl_s=float(kwargs.get("ttl_s", 60.0)),
    )
    topic = control_event_topic(context["site_id"], event_type, context["instance_id"])
    return _publish(context, topic, envelope, qos=1)


def start_control_mqtt(context):
    """Start bounded command and transport owners."""
    if context["thread"] and context["thread"].is_alive():
        return context["thread"]
    context["stop_event"].clear()
    thread = threading.Thread(
        target=partial(_message_loop, context),
        name="MasterControlMQTT-" + context["instance_id"],
        daemon=False,
    )
    context["thread"] = thread
    thread.start()
    context["transport"]["start"]()
    return thread


def stop_control_mqtt(context, **kwargs):
    """Stop ingress, drain queued messages and close the transport."""
    context["stop_event"].set()
    context["transport"]["stop"](
        join_timeout_s=float(kwargs.get("join_timeout_s", 5.0)))
    thread = context.get("thread")
    if thread and thread is not threading.current_thread():
        thread.join(timeout=float(kwargs.get("join_timeout_s", 5.0)))
    return snapshot_control_mqtt(context)


def wait_control_mqtt(context, timeout_s=5.0):
    """Wait for the underlying broker connection."""
    return bool(context["transport"]["wait_connected"](float(timeout_s)))


def snapshot_control_mqtt(context):
    """Return secret-free health data."""
    with context["lock"]:
        return {
            "state": context["state"],
            "connected": context["connected_event"].is_set(),
            "queue_depth": context["queue"].qsize(),
            "thread_alive": bool(context["thread"] and context["thread"].is_alive()),
            "statistics": dict(context["statistics"]),
            "transport": context["transport"]["snapshot"](),
            "last_error": context["last_error"],
        }


def create_control_mqtt_interface(**kwargs):
    """Create the strict proxy-control MQTT capability dictionary."""
    options = copy.deepcopy(kwargs.get("options") or {})
    _validate_tls(options)
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "options": options,
        "handler": kwargs["handler"],
        "authority_reader": kwargs["authority_reader"],
        "allowed_proxy_instance_ids": {
            str(value) for value in kwargs.get("allowed_proxy_instance_ids") or ()
        },
        "lock": threading.RLock(),
        "stop_event": threading.Event(),
        "connected_event": threading.Event(),
        "queue": Queue(maxsize=int(options.get("command_queue_maxsize", 512))),
        "thread": None,
        "transport": None,
        "state": "created",
        "last_error": None,
        "statistics": {"processed": 0, "invalid": 0, "queue_rejected": 0, "acks_published": 0},
    }
    factory = kwargs.get("transport_factory") or create_mqtt_client
    context["transport"] = factory(
        on_message=partial(_on_transport_message, context),
        on_connect=partial(_on_transport_connect, context),
        on_disconnect=partial(_on_transport_disconnect, context),
        **_transport_options(context)
    )
    return {
        "context": context,
        "start": partial(start_control_mqtt, context),
        "stop": partial(stop_control_mqtt, context),
        "publish_event": partial(publish_control_event, context),
        "wait_connected": partial(wait_control_mqtt, context),
        "snapshot": partial(snapshot_control_mqtt, context),
    }

# -*- coding: utf-8 -*-

# proxy_gateway/src/interfaces/mqtt_gateway_interface.py

"""Strict Proxy-to-Master MQTTS command, ACK and event bridge."""

import copy
import os
import threading
import time
import uuid

from functools import partial
from queue import Empty, Full, Queue

from src.library._generic_system_contract_library.protocol_contracts import make_envelope, parse_timestamp, utc_now, validate_envelope
from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library._generic_type_library import canonical_bytes, decode_json_bytes
from src.library._generic_system_contract_library.protocol_contracts import (
    control_event_topic,
    control_event_filter,
    master_command_topic,
    proxy_ack_topic,
    proxy_ack_filter,
)
from src.core.event_stream import bind_operation_owner, operation_id_from_envelope
from src.library._lightweight_mqtt_client import create_mqtt_client


def _validate_options(options):
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
    return {
        "client_id": "automation-proxy-" + context["instance_id"],
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
        "tls": copy.deepcopy(options.get("tls") or {}),
        "subscriptions": [
            {"topic": proxy_ack_filter(context["site_id"], context["instance_id"]), "qos": 1},
            {"topic": control_event_filter(
                context["site_id"], context["master_instance_id"]), "qos": 1},
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


def _record_attempt(context, request_id, published):
    try:
        context["gateway_store"]["mark_command_attempt"](
            request_id, published=published,
            retry_s=context["options"].get("retry_interval_s", 2.0))
        return True
    except Exception as exc:
        with context["lock"]:
            context["last_error"] = str(exc)
        return False


def send_master_command(context, event_type, payload, **kwargs):
    """Persist and publish one site-scoped command, optionally waiting for its ACK."""
    principal = copy.deepcopy(kwargs["principal"])
    envelope = make_envelope(
        event_type,
        {"service": "proxy", "instance_id": context["instance_id"]},
        {"service": "master", "instance_id": context["master_instance_id"]},
        site_id=context["site_id"],
        idempotency_key=kwargs.get("idempotency_key"),
        principal=principal,
        authority_term=int(kwargs.get("authority_term", 0)),
        config_revision=int(kwargs.get("config_revision", 0)),
        payload=copy.deepcopy(payload or {}),
        ttl_s=float(kwargs.get("ttl_s", 30.0)),
    )
    stored = context["gateway_store"]["create_command"]({
        "request_id": envelope["message_id"],
        "correlation_id": envelope["correlation_id"],
        "idempotency_key": envelope.get("idempotency_key"),
        "proxy_instance_id": context["instance_id"],
        "user_id": principal["subject"],
        "site_id": context["site_id"],
        "event_type": event_type,
        "request_payload": copy.deepcopy(payload or {}),
        "envelope": copy.deepcopy(envelope),
    })
    if not stored.get("ok"):
        return stored
    if stored.get("duplicate"):
        previous = stored.get("command") or {}
        if isinstance(previous.get("result"), dict):
            result = copy.deepcopy(previous["result"])
            result["duplicate"] = True
            return result
        return make_success(
            accepted=True,
            duplicate=True,
            request_id=previous.get("request_id"),
            state=previous.get("state") or "PENDING",
        )
    timeout = max(0.0, float(kwargs.get("wait_timeout_s", 0.0)))
    waiter = {"event": threading.Event(), "result": None} if timeout > 0.0 else None
    if waiter:
        with context["lock"]:
            context["waiters"][envelope["message_id"]] = waiter
    topic = master_command_topic(context["site_id"], context["master_instance_id"], event_type)
    if not _publish(context, topic, envelope):
        _record_attempt(context, envelope["message_id"], False)
        if waiter:
            with context["lock"]:
                context["waiters"].pop(envelope["message_id"], None)
        return make_success(
            accepted=True,
            delivery_pending=True,
            request_id=envelope["message_id"],
            state="PENDING",
        )
    _record_attempt(context, envelope["message_id"], True)
    with context["lock"]:
        context["statistics"]["commands_published"] += 1
    if timeout <= 0.0:
        return make_success(accepted=True, request_id=envelope["message_id"])
    completed = waiter["event"].wait(timeout)
    with context["lock"]:
        context["waiters"].pop(envelope["message_id"], None)
    if not completed:
        return make_success(accepted=True, request_id=envelope["message_id"], state="PENDING")
    return copy.deepcopy(waiter["result"])


def _decode_and_validate(context, message):
    try:
        envelope = decode_json_bytes(
            message.get("payload") or b"",
            maximum_bytes=context["options"].get("max_packet_size", 4 * 1024 * 1024),
        )
        checked = validate_envelope(
            envelope,
            expected_site_id=context["site_id"],
            allow_unknown_event=True,
            maximum_bytes=context["options"].get("max_packet_size", 4 * 1024 * 1024),
        )
    except Exception as exc:
        return None, make_error("invalid_envelope", str(exc))
    if not checked.get("ok"):
        return None, checked
    source = envelope.get("source") or {}
    target = envelope.get("target") or {}
    if source.get("service") != "master" or source.get("instance_id") != context["master_instance_id"]:
        return None, make_error("identity_mismatch", "event source is not the configured Master")
    if target.get("service") not in ("proxy", "broadcast"):
        return None, make_error("identity_mismatch", "event target service is invalid")
    if target.get("instance_id") not in (context["instance_id"], "broadcast", "*"):
        return None, make_error("identity_mismatch", "event targets another Proxy")
    if message.get("retain"):
        return None, make_error("invalid_envelope", "retained control messages are forbidden")
    try:
        if envelope.get("event_type") == "command.ack":
            expected_topic = proxy_ack_topic(
                context["site_id"], context["instance_id"], context["master_instance_id"],
                envelope.get("causation_id"),
            )
        else:
            expected_topic = control_event_topic(
                context["site_id"], envelope.get("event_type"), context["master_instance_id"])
    except ValueError as exc:
        return None, make_error("invalid_envelope", str(exc))
    if str(message.get("topic") or "") != expected_topic:
        return None, make_error("identity_mismatch", "event topic and envelope differ")
    return envelope, None


def _handle_ack(context, envelope):
    if envelope.get("event_type") != "command.ack":
        return False
    request_id = str(envelope.get("causation_id") or "")
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    if set(payload) != {"command", "result"} or not isinstance(payload.get("result"), dict):
        return False
    command = context["gateway_store"]["get_command"](request_id)
    principal = envelope.get("principal") if isinstance(envelope.get("principal"), dict) else {}
    correlated = (
        command
        and str(command.get("correlation_id") or "") == str(envelope.get("correlation_id") or "")
        and str(command.get("event_type") or "") == str(payload.get("command") or "")
        and str(command.get("user_id") or "") == str(principal.get("subject") or "")
    )
    if not correlated:
        return False
    result = payload["result"]
    stored = context["gateway_store"]["update_command"](request_id, result)
    if not stored:
        with context["lock"]:
            context["last_error"] = "command ACK could not be persisted"
        return False
    with context["lock"]:
        waiter = context["waiters"].get(request_id)
        context["statistics"]["acks_received"] += 1
    _cache_and_stream(context, envelope, resource_type="command", resource_id=request_id)
    if waiter:
        with context["lock"]:
            waiter["result"] = copy.deepcopy(result)
            waiter["event"].set()
    return bool(stored)


def _event_resource(envelope):
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    if payload.get("node_id"):
        return "node", str(payload["node_id"])
    if payload.get("operation_id"):
        return "operation", str(payload["operation_id"])
    if payload.get("controller_uuid") and payload.get("sensor_id") is not None:
        return "sensor", "{}:{}".format(
            payload["controller_uuid"], payload["sensor_id"])
    if payload.get("controller_uuid"):
        return "controller", str(payload["controller_uuid"])
    if payload.get("revision") is not None:
        return "config", str(payload["revision"])
    return "event", str(envelope.get("event_type") or "unknown")


def _bind_persisted_operation_owner(context, envelope):
    """Attach only a Proxy-persisted request owner to operation events."""
    operation_id = operation_id_from_envelope(envelope)
    resolver = context["gateway_store"].get("operation_owners")
    if not operation_id or not callable(resolver):
        return copy.deepcopy(envelope)
    try:
        resolved = resolver(context["site_id"], [operation_id])
    except Exception as exc:
        with context["lock"]:
            context["last_error"] = "operation owner lookup failed: " + type(exc).__name__
        return copy.deepcopy(envelope)
    if not isinstance(resolved, dict) or resolved.get("ok") is not True:
        return copy.deepcopy(envelope)
    owners = resolved.get("owners") if isinstance(resolved.get("owners"), dict) else {}
    return bind_operation_owner(envelope, owners.get(operation_id))


def _cache_and_stream(context, envelope, **kwargs):
    resource_type = kwargs.get("resource_type")
    resource_id = kwargs.get("resource_id")
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    cached = context["gateway_store"]["cache_event"]({
        "event_id": envelope["message_id"],
        "site_id": context["site_id"],
        "event_type": envelope["event_type"],
        "resource_type": resource_type,
        "resource_id": resource_id,
        "envelope": copy.deepcopy(envelope),
        "payload": copy.deepcopy(payload),
        "ttl_s": context["options"].get("replay_ttl_s", 3600),
    })
    outgoing = copy.deepcopy(envelope)
    if cached.get("event_sequence") is not None:
        outgoing.setdefault("extensions", {})["replay_sequence"] = cached["event_sequence"]
    return bool(context["stream_publisher"](
        outgoing,
        resource_type=resource_type,
        resource_id=resource_id,
    ))


def _handle_event(context, envelope):
    outgoing = _bind_persisted_operation_owner(context, envelope)
    resource_type, resource_id = _event_resource(outgoing)
    with context["lock"]:
        context["statistics"]["events_received"] += 1
    return _cache_and_stream(
        context, outgoing, resource_type=resource_type, resource_id=resource_id)


def _message_loop(context):
    with context["lock"]:
        context["state"] = "running"
    while not context["stop_event"].is_set():
        try:
            message = context["queue"].get(timeout=0.2)
        except Empty:
            try:
                _retry_commands_if_due(context)
            except Exception as exc:
                with context["lock"]:
                    context["last_error"] = str(exc)
            continue
        try:
            envelope, error = _decode_and_validate(context, message)
            if error:
                with context["lock"]:
                    context["statistics"]["invalid"] += 1
            elif envelope.get("event_type") == "command.ack":
                _handle_ack(context, envelope)
            else:
                _handle_event(context, envelope)
        except Exception as exc:
            with context["lock"]:
                context["statistics"]["invalid"] += 1
                context["last_error"] = str(exc)
        finally:
            context["queue"].task_done()
        try:
            _retry_commands_if_due(context)
        except Exception as exc:
            with context["lock"]:
                context["last_error"] = str(exc)
    with context["lock"]:
        context["state"] = "stopped"


def _retry_commands_if_due(context):
    now = time.monotonic()
    if now < context["next_retry_monotonic"]:
        return 0
    context["next_retry_monotonic"] = now + float(
        context["options"].get("retry_interval_s", 2.0))
    if not context["connected_event"].is_set():
        return 0
    records = context["gateway_store"]["due_commands"](
        context["instance_id"], limit=context["options"].get("retry_batch_size", 32))
    published = 0
    for record in records:
        envelope = record.get("envelope") if isinstance(record.get("envelope"), dict) else {}
        try:
            expired = parse_timestamp(envelope.get("expires_at")) < utc_now()
        except Exception:
            expired = True
        if expired:
            context["gateway_store"]["expire_command"](record["request_id"])
            continue
        topic = master_command_topic(
            context["site_id"], context["master_instance_id"], record["event_type"])
        written = _publish(context, topic, envelope)
        _record_attempt(context, record["request_id"], written)
        published += int(written)
    return published


def start_mqtt_gateway(context):
    """Start the bounded message owner and MQTT transport."""
    if context["thread"] and context["thread"].is_alive():
        return context["thread"]
    context["stop_event"].clear()
    thread = threading.Thread(
        target=partial(_message_loop, context),
        name="ProxyMQTT-" + context["instance_id"],
        daemon=False,
    )
    context["thread"] = thread
    thread.start()
    context["transport"]["start"]()
    return thread


def stop_mqtt_gateway(context, **kwargs):
    """Stop MQTT ingress and resolve local waiters as unavailable."""
    context["stop_event"].set()
    context["transport"]["stop"](join_timeout_s=float(kwargs.get("join_timeout_s", 5.0)))
    thread = context.get("thread")
    if thread and thread is not threading.current_thread():
        thread.join(timeout=float(kwargs.get("join_timeout_s", 5.0)))
    with context["lock"]:
        waiters = tuple(context["waiters"].values())
        context["waiters"].clear()
    for waiter in waiters:
        with context["lock"]:
            waiter["result"] = make_error("not_ready", "Proxy MQTT bridge stopped")
            waiter["event"].set()
    return snapshot_mqtt_gateway(context)


def wait_mqtt_gateway(context, timeout_s=5.0):
    return bool(context["transport"]["wait_connected"](float(timeout_s)))


def snapshot_mqtt_gateway(context):
    """Return secret-free bridge diagnostics."""
    with context["lock"]:
        return {
            "state": context["state"],
            "connected": context["connected_event"].is_set(),
            "queue_depth": context["queue"].qsize(),
            "waiters": len(context["waiters"]),
            "statistics": dict(context["statistics"]),
            "transport": context["transport"]["snapshot"](),
            "last_error": context["last_error"],
        }


def create_mqtt_gateway_interface(**kwargs):
    """Create the strict Proxy MQTT bridge."""
    options = copy.deepcopy(kwargs.get("options") or {})
    _validate_options(options)
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "master_instance_id": kwargs["master_instance_id"],
        "options": options,
        "gateway_store": kwargs["gateway_store"],
        "stream_publisher": kwargs["stream_publisher"],
        "lock": threading.RLock(),
        "stop_event": threading.Event(),
        "connected_event": threading.Event(),
        "queue": Queue(maxsize=int(options.get("command_queue_maxsize", 512))),
        "waiters": {},
        "transport": None,
        "thread": None,
        "next_retry_monotonic": 0.0,
        "state": "created",
        "last_error": None,
        "statistics": {
            "commands_published": 0, "acks_received": 0, "events_received": 0,
            "invalid": 0, "queue_rejected": 0,
        },
    }
    factory = kwargs.get("transport_factory") or create_mqtt_client
    context["transport"] = factory(
        on_message=partial(_on_message, context),
        on_connect=partial(_on_connect, context),
        on_disconnect=partial(_on_disconnect, context),
        **_transport_options(context)
    )
    return {
        "context": context,
        "start": partial(start_mqtt_gateway, context),
        "stop": partial(stop_mqtt_gateway, context),
        "wait_connected": partial(wait_mqtt_gateway, context),
        "send_command": partial(send_master_command, context),
        "snapshot": partial(snapshot_mqtt_gateway, context),
    }

# -*- coding: utf-8 -*-

# master_node/src/interfaces/metrics_interface.py

"""Minimal bounded HTTP health and Prometheus diagnostics interface."""

import json
import logging
import socket
import threading

from functools import partial


LOGGER = logging.getLogger("master_node.metrics")


def _response(status, content_type, body):
    payload = body.encode("utf-8")
    reason = "OK" if status == 200 else "Service Unavailable"
    headers = (
        "HTTP/1.1 {} {}\r\n"
        "Content-Type: {}\r\n"
        "Content-Length: {}\r\n"
        "Cache-Control: no-store\r\n"
        "Connection: close\r\n\r\n"
    ).format(status, reason, content_type, len(payload)).encode("ascii")
    return headers + payload


def _json_response(status, value):
    body = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return _response(status, "application/json; charset=utf-8", body)


def _metrics_text(snapshot):
    role = snapshot.get("orchestrator", {}).get("role", "standby")
    ready = 1 if snapshot.get("ready") else 0
    mqtt = snapshot.get("mqtt", {})
    lines = [
        "# TYPE automation_master_ready gauge",
        "automation_master_ready {}".format(ready),
        "# TYPE automation_master_leader gauge",
        "automation_master_leader {}".format(1 if role == "leader" else 0),
        "# TYPE automation_master_mqtt_connected gauge",
        "automation_master_mqtt_connected {}".format(1 if mqtt.get("connected") else 0),
    ]
    return "\n".join(lines) + "\n"


def _handle_client(context, connection):
    connection.settimeout(context["client_timeout_s"])
    request = connection.recv(context["max_request_bytes"])
    first = request.split(b"\r\n", 1)[0].decode("ascii", errors="replace")
    parts = first.split(" ")
    method = parts[0] if parts else ""
    path = parts[1].split("?", 1)[0] if len(parts) > 1 else ""
    snapshot = context["snapshot_reader"]()
    if method != "GET":
        response = _json_response(503, {"ok": False, "code": "method_not_allowed"})
    elif path == "/health/live":
        response = _json_response(200, {"ok": True, "state": "live"})
    elif path == "/health/ready":
        status = 200 if snapshot.get("ready") else 503
        response = _json_response(status, {"ok": bool(snapshot.get("ready")), "runtime": snapshot})
    elif path == "/metrics":
        response = _response(200, "text/plain; version=0.0.4", _metrics_text(snapshot))
    else:
        response = _json_response(503, {"ok": False, "code": "not_found"})
    connection.sendall(response)


def _server_loop(context):
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind((context["host"], context["port"]))
    listener.listen(context["backlog"])
    listener.settimeout(0.5)
    context["listener"] = listener
    context["state"] = "running"
    try:
        while not context["stop_event"].is_set():
            try:
                connection, _address = listener.accept()
            except TimeoutError:
                continue
            except OSError:
                if context["stop_event"].is_set():
                    break
                raise
            try:
                _handle_client(context, connection)
            except Exception:
                context["errors"] += 1
                LOGGER.exception("Master diagnostics request failed")
            finally:
                connection.close()
    finally:
        listener.close()
        context["state"] = "stopped"


def start_metrics_interface(context):
    """Start one bounded sequential diagnostics server."""
    thread = context.get("thread")
    if thread and thread.is_alive():
        return thread
    context["stop_event"].clear()
    thread = threading.Thread(
        target=partial(_server_loop, context),
        name="MasterMetrics",
        daemon=False,
    )
    context["thread"] = thread
    thread.start()
    return thread


def stop_metrics_interface(context, **kwargs):
    """Stop the diagnostics listener."""
    context["stop_event"].set()
    listener = context.get("listener")
    if listener:
        try:
            listener.close()
        except OSError:
            pass
    thread = context.get("thread")
    if thread and thread is not threading.current_thread():
        thread.join(timeout=float(kwargs.get("join_timeout_s", 3.0)))
    return snapshot_metrics_interface(context)


def snapshot_metrics_interface(context):
    """Return interface health."""
    return {
        "state": context["state"],
        "host": context["host"],
        "port": context["port"],
        "thread_alive": bool(context["thread"] and context["thread"].is_alive()),
        "errors": context["errors"],
    }


def create_metrics_interface(**kwargs):
    """Create health and metrics interface capabilities."""
    context = {
        "host": str(kwargs.get("host") or "0.0.0.0"),
        "port": int(kwargs.get("port", 9090)),
        "backlog": min(64, max(1, int(kwargs.get("backlog", 16)))),
        "client_timeout_s": max(0.1, float(kwargs.get("client_timeout_s", 2.0))),
        "max_request_bytes": min(65536, max(1024, int(kwargs.get("max_request_bytes", 8192)))),
        "snapshot_reader": kwargs["snapshot_reader"],
        "stop_event": threading.Event(),
        "thread": None,
        "listener": None,
        "state": "created",
        "errors": 0,
    }
    return {
        "context": context,
        "start": partial(start_metrics_interface, context),
        "stop": partial(stop_metrics_interface, context),
        "snapshot": partial(snapshot_metrics_interface, context),
    }

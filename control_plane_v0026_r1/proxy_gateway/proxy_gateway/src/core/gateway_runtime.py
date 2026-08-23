# -*- coding: utf-8 -*-

# proxy_gateway/src/core/gateway_runtime.py

"""Functional Proxy composition root and shared WSS/HTTPS runtime state."""

import asyncio
import copy
import logging
import threading

from functools import partial

from src.core.auth_management import create_auth_management
from src.core.client_command_dispatcher import create_client_command_dispatcher
from src.core.command_gateway import create_command_gateway
from src.core.event_stream import create_event_stream
from src.interfaces.async_websockets_server import create_async_websocket_server
from src.interfaces.https_interface import create_https_application
from src.interfaces.mqtt_gateway_interface import create_mqtt_gateway_interface
from src.library.gateway_data_store import create_gateway_data_store
from src.library.postgresql import env_database_options, read_secret


def _maintenance_loop(context):
    while not context["stop_event"].wait(
            context["config"]["maintenance"]["interval_s"]):
        try:
            context["store"]["gateway"]["cleanup"]()
        except Exception as exc:
            context["last_error"] = str(exc)


def _bearer_token(authorization):
    value = str(authorization or "")
    return value[7:].strip() if value.lower().startswith("bearer ") else None


def _authenticate_handshake_sync(context, handshake):
    bearer = _bearer_token(handshake.get("authorization"))
    if bearer:
        return context["auth"]["authenticate_api_token"](bearer)
    return context["auth"]["authenticate_session"]({
        "session_token": (handshake.get("cookies") or {}).get(
            context["config"]["https"]["session_cookie"]),
        "source_ip": handshake.get("source_ip"),
        "user_agent": handshake.get("user_agent"),
        "csrf_required": False,
    })


async def authenticate_wss(context, handshake):
    """Authenticate one WSS handshake through the existing auth core."""
    return await asyncio.to_thread(partial(
        _authenticate_handshake_sync, context, copy.deepcopy(handshake)))


async def revalidate_wss_principal(context, request):
    """Re-run token/session lookup and return a stable current principal."""
    checked = await asyncio.to_thread(partial(
        _authenticate_handshake_sync,
        context,
        copy.deepcopy(request.get("handshake") or {}),
    ))
    if not checked.get("ok"):
        return checked
    principal = checked.get("principal") or {}
    if str(principal.get("subject") or "") != str(
            (request.get("principal") or {}).get("subject") or ""):
        return {"ok": False, "code": "authentication_required"}
    return {"ok": True, "principal": principal}


async def dispatch_wss_command(context, request):
    """Delegate WSS requests to the transport-neutral dispatcher."""
    return await context["dispatcher"]["dispatch"](request)


def _wss_config(config):
    result = copy.deepcopy(config["wss"])
    result["site_id"] = config["site_id"]
    result["instance_id"] = config["instance_id"]
    return result


def _wire_context(context, **kwargs):
    config = context["config"]
    store_factory = kwargs.get("data_store_factory") or create_gateway_data_store
    context["store"] = store_factory(
        kwargs.get("database_options") or env_database_options())
    auth_options = config["auth"]
    password_pepper = read_secret(auth_options["password_pepper_file"])
    token_pepper = read_secret(auth_options["token_pepper_file"])
    binding_pepper = read_secret(auth_options["binding_pepper_file"])
    context["auth"] = create_auth_management(
        identity=context["store"]["identity"],
        rate_limit=context["store"]["rate_limit"],
        password_pepper=password_pepper,
        token_pepper=token_pepper,
        binding_pepper=binding_pepper,
        options=auth_options,
    )
    stream_options = config["stream"]
    context["stream"] = create_event_stream(
        queue_maxsize=stream_options["queue_maxsize"],
        coalesced_maxsize=stream_options.get(
            "coalesced_maxsize", stream_options["queue_maxsize"]),
        gateway_store=context["store"]["gateway"],
    )
    context["mqtt"] = create_mqtt_gateway_interface(
        site_id=config["site_id"],
        instance_id=config["instance_id"],
        master_instance_id=config["master_instance_id"],
        options=config["mqtt"],
        gateway_store=context["store"]["gateway"],
        stream_publisher=context["stream"]["publish"],
        transport_factory=kwargs.get("transport_factory"),
    )
    context["commands"] = create_command_gateway(
        site_id=config["site_id"],
        authority_mode=config["authority_mode"],
        mqtt=context["mqtt"],
        wait_timeout_s=5.0,
    )
    context["dispatcher"] = create_client_command_dispatcher(
        site_id=config["site_id"],
        instance_id=config["instance_id"],
        authority_mode=config["authority_mode"],
        auth=context["auth"],
        identity=context["store"]["identity"],
        rate_limit=context["store"]["rate_limit"],
        audit=context["store"]["audit"],
        commands=context["commands"],
        gateway_store=context["store"]["gateway"],
        runtime_snapshot=partial(snapshot_gateway_runtime, context),
        idempotency_key=binding_pepper,
        rate_limit_count=config["https"]["api_rate_limit"],
        rate_limit_window_s=config["https"]["api_rate_window_s"],
        logger=context["logger"],
    )
    context["wss"] = create_async_websocket_server(
        config=_wss_config(config),
        allowed_actions=context["dispatcher"]["actions"],
        authenticate=partial(authenticate_wss, context),
        revalidate_principal=partial(revalidate_wss_principal, context),
        dispatch_command=partial(dispatch_wss_command, context),
        open_event_subscription=context["stream"]["open_subscription"],
        read_replay_page=context["stream"]["read_replay_page"],
        update_event_subscription=context["stream"]["update_subscription"],
        close_event_subscription=context["stream"]["close_subscription"],
        event_consumed=context["stream"]["event_consumed"],
        logger=context["logger"],
    )
    context["https_context"] = {
        "site_id": config["site_id"],
        "instance_id": config["instance_id"],
        "auth": context["auth"],
        "identity": context["store"]["identity"],
        "rate_limit": context["store"]["rate_limit"],
        "audit": context["store"]["audit"],
        "commands": context["commands"],
        "dispatcher": context["dispatcher"],
        "runtime_snapshot": partial(snapshot_gateway_runtime, context),
        "origin_allowlist": set(config["https"]["origin_allowlist"]),
        "max_json_bytes": config["https"]["max_json_bytes"],
        "api_rate_limit": config["https"]["api_rate_limit"],
        "api_rate_window_s": config["https"]["api_rate_window_s"],
        "session_cookie": config["https"]["session_cookie"],
        "csrf_cookie": config["https"]["csrf_cookie"],
        "session_max_age_s": config["https"]["session_max_age_s"],
    }
    context["application"] = create_https_application(context["https_context"])


def create_gateway_runtime(config, **kwargs):
    """Create all Gateway components without starting network owners."""
    context = {
        "config": copy.deepcopy(config),
        "logger": kwargs.get("logger") or logging.getLogger("proxy_gateway"),
        "lock": threading.RLock(),
        "stop_event": threading.Event(),
        "maintenance_thread": None,
        "state": "created",
        "last_error": None,
        "store": None,
        "auth": None,
        "stream": None,
        "mqtt": None,
        "commands": None,
        "dispatcher": None,
        "wss": None,
        "https_ready": False,
        "https_context": None,
        "application": None,
    }
    _wire_context(context, **kwargs)
    return {
        "context": context,
        "application": context["application"],
        "wss": context["wss"],
        "set_https_ready": partial(set_https_ready, context),
        "start": partial(start_gateway_runtime, context),
        "stop": partial(stop_gateway_runtime, context),
        "snapshot": partial(snapshot_gateway_runtime, context),
    }


def start_gateway_runtime(context):
    """Start MQTT and maintenance before accepting listener readiness."""
    with context["lock"]:
        if context["state"] == "running":
            return snapshot_gateway_runtime(context)
        context["state"] = "starting"
    try:
        context["mqtt"]["start"]()
        required = context["config"]["mqtt"].get("required", True)
        timeout = context["config"]["mqtt"].get("connect_timeout_s", 5.0)
        if required and not context["mqtt"]["wait_connected"](timeout):
            raise RuntimeError("required MQTT broker connection is unavailable")
        context["stop_event"].clear()
        thread = threading.Thread(
            target=partial(_maintenance_loop, context),
            name="ProxyMaintenance",
            daemon=False,
        )
        context["maintenance_thread"] = thread
        thread.start()
    except Exception as exc:
        context["last_error"] = str(exc)
        context["mqtt"]["stop"]()
        context["store"]["close"]()
        with context["lock"]:
            context["state"] = "failed"
        raise
    with context["lock"]:
        context["state"] = "running"
    return snapshot_gateway_runtime(context)


def stop_gateway_runtime(context, **kwargs):
    """Stop ingress, MQTT, maintenance and the database pool in order."""
    with context["lock"]:
        if context["state"] == "stopped":
            return snapshot_gateway_runtime(context, include_database=False)
        context["state"] = "stopping"
        context["https_ready"] = False
    if context.get("wss"):
        context["wss"]["stop"]()
    timeout = float(kwargs.get("join_timeout_s", 5.0))
    context["mqtt"]["stop"](join_timeout_s=timeout)
    context["stop_event"].set()
    thread = context.get("maintenance_thread")
    if thread and thread is not threading.current_thread():
        thread.join(timeout=timeout)
    context["store"]["close"]()
    with context["lock"]:
        context["state"] = "stopped"
    return snapshot_gateway_runtime(context, include_database=False)


def set_https_ready(context, ready):
    """Publish Uvicorn listener state into the combined readiness gate."""
    with context["lock"]:
        context["https_ready"] = bool(ready)
    return context["https_ready"]


def snapshot_gateway_runtime(context, **kwargs):
    """Return secret-free combined readiness and backpressure state."""
    include_database = kwargs.get("include_database", True)
    database_ready = context["store"]["ping"]() if include_database else False
    mqtt = context["mqtt"]["snapshot"]() if context.get("mqtt") else {}
    wss = context["wss"]["snapshot"]() if context.get("wss") else {}
    required = context["config"]["mqtt"].get("required", True)
    wss_required = context["config"].get("wss", {}).get("enabled", True)
    with context["lock"]:
        state = context["state"]
        error = context["last_error"]
        https_ready = context["https_ready"]
    ready = bool(
        state == "running"
        and database_ready
        and (mqtt.get("connected") or not required)
        and (wss.get("ready") or not wss_required)
        and https_ready
    )
    return {
        "service": "proxy-gateway",
        "site_id": context["config"]["site_id"],
        "instance_id": context["config"]["instance_id"],
        "state": state,
        "ready": ready,
        "database_ready": database_ready,
        "mqtt": mqtt,
        "wss": wss,
        "https": {
            "enabled": context["config"]["https"].get("enabled", True),
            "port": context["config"]["https"]["port"],
            "ready": https_ready,
        },
        "stream": context["stream"]["snapshot"]() if context.get("stream") else {},
        "maintenance_thread_alive": bool(
            context.get("maintenance_thread")
            and context["maintenance_thread"].is_alive()),
        "last_error": error,
    }

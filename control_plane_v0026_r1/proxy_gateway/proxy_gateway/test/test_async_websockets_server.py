# -*- coding: utf-8 -*-

"""Direct TLS ``websockets.serve`` transport integration tests with fakes."""

import asyncio
import copy
import datetime
import decimal
import http
import ipaddress
import json
import logging
import os
import shutil
import socket
import ssl
import stat
import tempfile
import threading
import time
import uuid

from functools import partial

import websockets

import src.interfaces.async_websockets_server as websocket_server_module

from src.core.event_stream import create_event_stream
from src.interfaces.async_websockets_server import (
    PROTOCOL,
    create_async_websocket_server,
)
from src.library.generate_ssl_certs import generate_lab_websocket_pki


SITE_ID = "site-main"
ORIGIN = "https://control.example:8444"
AUTHORIZATION = "Bearer direct-wss-test-token"
USER_AGENT = "automation-wss-test/0015"


def _unsupported_reply_repr(_value):
    return "TOP-SECRET-REPR-MUST-NOT-BE-LOGGED"


_UnsupportedReplyValue = type(
    "_UnsupportedReplyValue", (), {"__repr__": _unsupported_reply_repr})


def _principal():
    return {
        "subject": "wss-user",
        "permissions": ["node.read", "operation.read"],
        "site_scopes": [SITE_ID],
        "resource_scopes": [],
        "_authentication_method": "api_token",
    }


def _event(sequence, event_type, **kwargs):
    now = datetime.datetime.now(datetime.timezone.utc)
    payload = kwargs.get("payload")
    if payload is None:
        payload = {"sequence": sequence}
    return {
        "protocol": "automation.control.v1",
        "schema_version": 1,
        "message_id": str(uuid.uuid4()),
        "correlation_id": str(uuid.uuid4()),
        "causation_id": None,
        "event_type": event_type,
        "site_id": SITE_ID,
        "issued_at": now.isoformat(),
        "expires_at": (now + datetime.timedelta(seconds=30)).isoformat(),
        "authority_term": 1,
        "config_revision": 1,
        "source": {"service": "master", "instance_id": "master-01"},
        "target": {"service": "proxy", "instance_id": "broadcast"},
        "principal": {
            "subject": str(kwargs.get("subject") or "system:master"),
        },
        "payload": copy.deepcopy(payload),
        "extensions": {},
    }


async def _authenticate(state, request):
    state["handshakes"].append(copy.deepcopy(request))
    if state.get("authentication_outcome") is not None:
        return copy.deepcopy(state["authentication_outcome"])
    if request.get("authorization") != AUTHORIZATION:
        return {"ok": False, "code": "authentication_required"}
    return {"ok": True, "principal": _principal()}


async def _revalidate_principal(state, request):
    state["revalidations"].append(copy.deepcopy(request))
    if state.get("revalidation_outcome") is not None:
        return copy.deepcopy(state["revalidation_outcome"])
    if state.get("revalidation_code"):
        return {"ok": False, "code": state["revalidation_code"]}
    return {"ok": True, "principal": copy.deepcopy(request["principal"])}


async def _dispatch_command(state, request):
    state["commands"].append(copy.deepcopy(request))
    if state.get("blocking_dispatch"):
        await asyncio.to_thread(_blocking_mutation, state)
    if state.get("dispatch_outcome") is not None:
        return copy.deepcopy(state["dispatch_outcome"])
    if request["action"] != "runtime.status":
        return {"ok": False, "code": "invalid_payload"}
    return {
        "ok": True,
        "state": "COMPLETED",
        "operation_id": "operation-wss-01",
        "reply": {
            "ok": True,
            "status": "READY",
            "transport": request["transport"],
        },
    }


async def _open_event_subscription(state, request):
    state["opens"].append(copy.deepcopy(request))
    if state.get("open_outcome") is not None:
        return copy.deepcopy(state["open_outcome"])
    subscription_id = "subscription-" + str(len(state["opens"]))
    queue = asyncio.Queue(maxsize=8)
    disconnect_event = asyncio.Event()
    subscription = {
        "subscription_id": subscription_id,
        "live_queue": queue,
        "disconnect_event": disconnect_event,
        "generation": 1,
        "filters": copy.deepcopy(request["filters"]),
    }
    state["subscriptions"][subscription_id] = subscription
    state["last_subscription"] = subscription
    return {
        "ok": True,
        "subscription_id": subscription_id,
        "live_queue": queue,
        "disconnect_event": disconnect_event,
        "generation": 1,
        "high_watermark": 2,
        "retention_floor": 1,
    }


async def _read_replay_page(state, request):
    state["replay_requests"].append(copy.deepcopy(request))
    if state.get("page_outcome") is not None:
        return copy.deepcopy(state["page_outcome"])
    if state.get("replay_gap"):
        return {
            "ok": True,
            "records": [],
            "gap": True,
            "next_sequence": request["after_sequence"],
            "complete": False,
        }
    after = int(request["after_sequence"])
    through = int(request["through_sequence"])
    records = [
        {
            "event_sequence": sequence,
            "envelope": _event(sequence, "operation.result"),
        }
        for sequence in range(after + 1, through + 1)
    ]
    records = records[:int(request["limit"])]
    next_sequence = records[-1]["event_sequence"] if records else through
    return {
        "ok": True,
        "records": records,
        "gap": False,
        "next_sequence": next_sequence,
        "complete": next_sequence >= through,
    }


async def _update_event_subscription(state, request):
    state["updates"].append(copy.deepcopy(request))
    if state.get("update_outcome") is not None and \
            request.get("reason") == "client_update":
        return copy.deepcopy(state["update_outcome"])
    subscription = state["subscriptions"].get(request["subscription_id"])
    if not subscription or request["generation"] != subscription["generation"]:
        return {"ok": False, "code": "replay_resync_required"}
    changed = request.get("reason") == "client_update" or \
        request["filters"] != subscription["filters"]
    if changed:
        subscription["generation"] += 1
        subscription["filters"] = copy.deepcopy(request["filters"])
    return {"ok": True, "generation": subscription["generation"]}


async def _close_event_subscription(state, request):
    state["closes"].append(copy.deepcopy(request))
    state["subscriptions"].pop(request["subscription_id"], None)
    return {"ok": True, "closed": True}


async def _event_consumed(state, request):
    state["consumed"].append(copy.deepcopy(request))
    return {"ok": True, "refilled": False}


def _core_replay_window(state, site_id):
    assert site_id == SITE_ID
    records = state["durable_records"]
    high_watermark = int(state.get("durable_high_watermark") or 0)
    retention_floor = min(
        (int(record["event_sequence"]) for record in records),
        default=0,
    )
    return {
        "high_watermark": high_watermark,
        "retention_floor": retention_floor,
        "snapshot_at": "2026-07-18T12:00:00+00:00",
    }


def _core_replay_page(state, request):
    after_sequence = int(request.get("after_sequence") or 0)
    through_sequence = int(request.get("through_sequence") or 0)
    limit = int(request.get("limit") or 1)
    eligible = [
        copy.deepcopy(record)
        for record in state["durable_records"]
        if after_sequence < int(record["event_sequence"]) <= through_sequence
    ]
    eligible.sort(key=_event_record_sequence)
    records = eligible[:limit]
    scanned_through = int(records[-1]["event_sequence"]) \
        if records else through_sequence
    return {
        "gap": False,
        "records": records,
        "scanned_through": scanned_through,
        "complete": scanned_through >= through_sequence,
    }


def _event_record_sequence(record):
    return int(record["event_sequence"])


async def _controlled_core_update(state, request):
    if state.get("generation_race_enabled") and \
            request.get("reason") == "client_update":
        state["generation_update_entered"].set()
        await state["generation_update_release"].wait()
    return await state["core_stream"]["update_subscription"](request)


def _install_core_event_stream(state):
    state["durable_records"] = []
    state["durable_high_watermark"] = 0
    store = {
        "replay_window": partial(_core_replay_window, state),
        "replay_page": partial(_core_replay_page, state),
    }
    stream = create_event_stream(
        gateway_store=store, queue_maxsize=8, coalesced_maxsize=8)
    state["core_stream"] = stream
    state["event_callbacks"] = {
        "open_event_subscription": stream["open_subscription"],
        "read_replay_page": stream["read_replay_page"],
        "update_event_subscription": partial(_controlled_core_update, state),
        "close_event_subscription": stream["close_subscription"],
        "event_consumed": stream["event_consumed"],
    }
    return stream


def _persist_and_publish(state, sequence, event_type, resource_type, resource_id,
                         **kwargs):
    envelope = _event(
        sequence, event_type,
        subject=kwargs.get("subject"), payload=kwargs.get("payload"))
    record = {
        "event_sequence": int(sequence),
        "event_type": event_type,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "envelope": copy.deepcopy(envelope),
    }
    state["durable_records"].append(record)
    state["durable_high_watermark"] = max(
        int(state["durable_high_watermark"]), int(sequence))
    return state["core_stream"]["publish"](
        envelope,
        resource_type=resource_type,
        resource_id=resource_id,
        event_sequence=int(sequence),
    )


def _free_port():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def _state():
    return {
        "handshakes": [],
        "revalidations": [],
        "commands": [],
        "opens": [],
        "replay_requests": [],
        "updates": [],
        "closes": [],
        "consumed": [],
        "subscriptions": {},
        "last_subscription": None,
        "replay_gap": False,
        "revalidation_code": None,
        "authentication_outcome": None,
        "revalidation_outcome": None,
        "dispatch_outcome": None,
        "open_outcome": None,
        "page_outcome": None,
        "update_outcome": None,
        "blocking_dispatch": False,
        "blocking_delay_s": 0.3,
        "blocking_started": threading.Event(),
        "blocking_completed": False,
    }


def _blocking_mutation(state):
    state["blocking_started"].set()
    time.sleep(float(state["blocking_delay_s"]))
    state["blocking_completed"] = True
    return True


def _server(state, paths, port):
    config = {
        "site_id": SITE_ID,
        "instance_id": "proxy-01",
        "host": "127.0.0.1",
        "port": port,
        "origin_allowlist": [ORIGIN],
        "session_cookie": "automation_session",
        "tls": {
            "certfile": paths["server_cert"],
            "keyfile": paths["server_key"],
            "minimum_tls_version": "TLSv1_2",
        },
        "max_connections": 16,
        "max_connections_per_source": 8,
        "max_connections_per_principal": 8,
        "event_live_queue_maxsize": 8,
        "response_queue_maxsize": 8,
        "replay_page_size": 1,
        "replay_page_limit": 8,
        "replay_record_limit": 16,
        "principal_revalidate_interval_s": 1,
        "max_connection_age_s": 60,
        "replay_timeout_s": 5,
        "open_timeout_s": 5,
        "close_timeout_s": 2,
        "ping_interval_s": 5,
        "ping_timeout_s": 5,
        "send_timeout_s": 2,
        "initial_message_timeout_s": 2,
    }
    config.update(copy.deepcopy(state.get("server_config") or {}))
    event_callbacks = state.get("event_callbacks") or {
        "open_event_subscription": partial(_open_event_subscription, state),
        "read_replay_page": partial(_read_replay_page, state),
        "update_event_subscription": partial(_update_event_subscription, state),
        "close_event_subscription": partial(_close_event_subscription, state),
        "event_consumed": partial(_event_consumed, state),
    }
    return create_async_websocket_server(
        config=config,
        allowed_actions=tuple(
            state.get("allowed_actions") or ("runtime.status",)),
        authenticate=partial(_authenticate, state),
        revalidate_principal=partial(_revalidate_principal, state),
        dispatch_command=partial(_dispatch_command, state),
        open_event_subscription=event_callbacks["open_event_subscription"],
        read_replay_page=event_callbacks["read_replay_page"],
        update_event_subscription=event_callbacks["update_event_subscription"],
        close_event_subscription=event_callbacks["close_event_subscription"],
        event_consumed=event_callbacks["event_consumed"],
        logger=logging.getLogger("test.async-websockets-server"),
    )


def _client_ssl_context(cafile):
    context = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH, cafile=cafile)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


async def _writer_send_timeout_scenario():
    blocker = asyncio.Event()
    closes = []

    async def close(code, reason):
        closes.append((code, reason))

    connection = close
    connection.close = close

    async def reader():
        await blocker.wait()

    async def writer():
        raise TimeoutError("synthetic slow-client send timeout")

    watchdog = asyncio.create_task(blocker.wait())
    client = {
        "client_id": "slow-client",
        "path": "/ws/v1/events",
        "connection": connection,
        "auth_watchdog_task": watchdog,
        "disconnect_event": asyncio.Event(),
        "lock": threading.RLock(),
        "active_task_count": 0,
    }
    context = {
        "logger": logging.getLogger("test.async-websockets-server.timeout"),
        "config": {"close_timeout_s": 1.0},
    }
    await websocket_server_module._run_client_tasks(
        context, client, reader=reader(), writer=writer()
    )
    assert closes == [(1013, "send timeout")]
    assert client["active_task_count"] == 0


async def _wait_ready(server, task):
    for _index in range(500):
        if task.done():
            await task
        if server["snapshot"]()["ready"]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("direct WSS listener did not become ready")


async def _receive_json(connection):
    message = await asyncio.wait_for(connection.recv(), timeout=3)
    value = json.loads(message)
    assert isinstance(value, dict)
    return value


async def _upgrade_status(uri, client_ssl, **kwargs):
    headers = kwargs.get("headers")
    try:
        async with websockets.connect(
                uri,
                ssl=client_ssl,
                origin=kwargs.get("origin", ORIGIN),
                additional_headers=headers,
                user_agent_header=USER_AGENT,
                proxy=None,
                open_timeout=3):
            return http.HTTPStatus.SWITCHING_PROTOCOLS
    except websockets.InvalidStatus as exc:
        return exc.response.status_code


async def _control_scenario(base_uri, client_ssl, state):
    async with websockets.connect(
            base_uri + "/ws/v1/control",
            ssl=client_ssl,
            origin=ORIGIN,
            additional_headers={"Authorization": AUTHORIZATION},
            user_agent_header=USER_AGENT,
            proxy=None,
            open_timeout=3) as connection:
        ready = await _receive_json(connection)
        assert ready["protocol"] == PROTOCOL
        assert ready["type"] == "connection.ready"
        assert ready["endpoint"] == "control"

        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "command.request",
            "request_id": "command-01",
            "action": "runtime.status",
            "payload": {},
        }))
        accepted = await _receive_json(connection)
        result = await _receive_json(connection)
        assert accepted["type"] == "command.accepted"
        assert accepted["request_id"] == "command-01"
        assert accepted["state"] == "COMPLETED"
        assert result["type"] == "command.result"
        assert result["result"]["transport"] == "wss"
        assert state["commands"][-1]["principal"]["subject"] == "wss-user"
        assert state["commands"][-1]["user_agent"] == USER_AGENT

        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "command.request",
            "request_id": "command-invalid",
            "action": "runtime.status",
            "payload": {},
            "unknown": True,
        }))
        rejected = await _receive_json(connection)
        assert rejected["type"] == "command.rejected"
        assert rejected["code"] == "invalid_payload"


async def _event_scenario(base_uri, client_ssl, state):
    async with websockets.connect(
            base_uri + "/ws/v1/events",
            ssl=client_ssl,
            origin=ORIGIN,
            additional_headers={"Authorization": AUTHORIZATION},
            user_agent_header=USER_AGENT,
            proxy=None,
            open_timeout=3) as connection:
        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "subscribe",
            "request_id": "subscribe-01",
            "last_event_id": 0,
            "filters": [{
                "resource_type": "operation",
                "resource_pattern": "*",
            }],
        }))
        accepted = await _receive_json(connection)
        replay_one = await _receive_json(connection)
        replay_two = await _receive_json(connection)
        assert accepted["type"] == "subscription.accepted"
        assert accepted["replay_high_watermark"] == 2
        assert accepted["generation"] == 1
        assert replay_one["extensions"]["replay_sequence"] == 1
        assert replay_two["extensions"]["replay_sequence"] == 2
        assert [item["after_sequence"] for item in state["replay_requests"][-2:]] == [0, 1]

        subscription = state["last_subscription"]
        await subscription["live_queue"].put({
            "event_sequence": 3,
            "generation": 1,
            "envelope": _event(3, "operation.result"),
            "queued_monotonic": time.monotonic(),
        })
        live = await _receive_json(connection)
        assert live["extensions"]["live_sequence"] == 3
        assert state["consumed"][-1]["event_sequence"] == 3

        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "subscribe",
            "request_id": "subscribe-update",
            "filters": [{
                "resource_type": "operation",
                "resource_pattern": "operation-*",
            }],
        }))
        updated = await _receive_json(connection)
        assert updated["type"] == "subscription.accepted", updated
        assert updated["request_id"] == "subscribe-update"
        assert updated["generation"] == 2

        consumed_before = len(state["consumed"])
        await subscription["live_queue"].put({
            "event_sequence": 4,
            "generation": 1,
            "envelope": _event(4, "operation.result"),
            "queued_monotonic": time.monotonic(),
        })
        await subscription["live_queue"].put({
            "event_sequence": 4,
            "generation": 2,
            "envelope": _event(4, "operation.result"),
            "queued_monotonic": time.monotonic(),
        })
        current = await _receive_json(connection)
        assert current["extensions"]["live_sequence"] == 4
        assert [item["generation"] for item in state["consumed"][consumed_before:]] \
            == [1, 2]


async def _gap_scenario(base_uri, client_ssl, state):
    state["replay_gap"] = True
    async with websockets.connect(
            base_uri + "/ws/v1/events",
            ssl=client_ssl,
            origin=ORIGIN,
            additional_headers={"Authorization": AUTHORIZATION},
            user_agent_header=USER_AGENT,
            proxy=None,
            open_timeout=3) as connection:
        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "subscribe",
            "request_id": "subscribe-gap",
            "last_event_id": 0,
            "filters": [],
        }))
        accepted = await _receive_json(connection)
        resync = await _receive_json(connection)
        assert accepted["type"] == "subscription.accepted"
        assert resync["type"] == "replay.resync_required"
        assert resync["code"] == "replay_resync_required"
        await connection.wait_closed()
        assert connection.close_code == 1013
    state["replay_gap"] = False


async def _revocation_scenario(base_uri, client_ssl, state):
    async with websockets.connect(
            base_uri + "/ws/v1/control",
            ssl=client_ssl,
            origin=ORIGIN,
            additional_headers={"Authorization": AUTHORIZATION},
            user_agent_header=USER_AGENT,
            proxy=None,
            open_timeout=3) as connection:
        ready = await _receive_json(connection)
        assert ready["type"] == "connection.ready"
        state["revalidation_code"] = "authentication_expired"
        await asyncio.wait_for(connection.wait_closed(), timeout=3)
        assert connection.close_code == 1008
    state["revalidation_code"] = None


async def _json_boundary_control_scenario(paths):
    """Exercise production-like PostgreSQL scalars and fail-closed recovery."""
    state = _state()
    state["allowed_actions"] = ("users.list", "runtime.status")
    port = _free_port()
    server = _server(state, paths, port)
    task = asyncio.create_task(server["serve"]())
    uri = "wss://127.0.0.1:{}/ws/v1/control".format(port)
    options = {
        "ssl": _client_ssl_context(paths["ca_cert"]),
        "origin": ORIGIN,
        "additional_headers": {"Authorization": AUTHORIZATION},
        "user_agent_header": USER_AGENT,
        "proxy": None,
        "open_timeout": 3,
    }
    user_id = uuid.UUID("9ffaf6d0-40ff-4e61-9121-a371e0b255ab")
    created_at = datetime.datetime(
        2026, 7, 18, 12, 34, 56, tzinfo=datetime.timezone.utc)
    try:
        await _wait_ready(server, task)
        async with websockets.connect(uri, **options) as connection:
            await _receive_json(connection)
            state["dispatch_outcome"] = {
                "ok": True,
                "state": "COMPLETED",
                "reply": {
                    "ok": True,
                    "user": {
                        "user_id": user_id,
                        "created_at": created_at,
                        "maintenance_date": datetime.date(2026, 7, 19),
                        "maintenance_time": datetime.time(1, 2, 3),
                        "retry_after": datetime.timedelta(seconds=30.25),
                        "quota": decimal.Decimal("12.3400"),
                        "source_address": ipaddress.ip_address("192.0.2.10"),
                        "source_network": ipaddress.ip_network("192.0.2.0/24"),
                        "certificate": b"certificate-data",
                        "token_prefix": bytearray(b"prefix-data"),
                    },
                },
            }
            await connection.send(json.dumps({
                "protocol": PROTOCOL,
                "type": "command.request",
                "request_id": "json-database-scalars",
                "action": "users.list",
                "payload": {},
            }))
            accepted = await _receive_json(connection)
            result = await _receive_json(connection)
            assert accepted["type"] == "command.accepted"
            assert result["type"] == "command.result"
            user = result["result"]["user"]
            assert user["user_id"] == str(user_id)
            assert user["created_at"] == str(created_at)
            assert user["maintenance_date"] == "2026-07-19"
            assert user["maintenance_time"] == "01:02:03"
            assert user["retry_after"] == "0:00:30.250000"
            assert user["quota"] == "12.3400"
            assert user["source_address"] == "192.0.2.10"
            assert user["source_network"] == "192.0.2.0/24"
            assert user["certificate"] == "certificate-data"
            assert user["token_prefix"] == "prefix-data"

            state["dispatch_outcome"] = {
                "ok": True,
                "state": "COMPLETED",
                "reply": {"unsafe": _UnsupportedReplyValue()},
            }
            await connection.send(json.dumps({
                "protocol": PROTOCOL,
                "type": "command.request",
                "request_id": "json-unsupported-reply",
                "action": "runtime.status",
                "payload": {},
            }))
            rejected = await _receive_json(connection)
            assert rejected["type"] == "command.rejected"
            assert rejected["request_id"] == "json-unsupported-reply"
            assert rejected["code"] == "internal_error"

            # The unsupported reply must not terminate the socket.  A second
            # valid command on the same connection has to complete normally.
            state["dispatch_outcome"] = None
            await connection.send(json.dumps({
                "protocol": PROTOCOL,
                "type": "command.request",
                "request_id": "json-recovery",
                "action": "runtime.status",
                "payload": {},
            }))
            recovered = await _receive_json(connection)
            recovered_result = await _receive_json(connection)
            assert recovered["type"] == "command.accepted"
            assert recovered_result["type"] == "command.result"
            assert recovered_result["result"]["transport"] == "wss"
            assert connection.close_code is None
        snapshot = server["snapshot"]
        assert snapshot()["statistics"]["command_results"][
            "runtime.status|internal_error"] == 1
    finally:
        server["stop"]()
        await asyncio.wait_for(task, timeout=5)


async def _direct_wss_scenario(paths):
    state = _state()
    port = _free_port()
    server = _server(state, paths, port)
    task = asyncio.create_task(server["serve"]())
    base_uri = "wss://127.0.0.1:{}".format(port)
    client_ssl = _client_ssl_context(paths["ca_cert"])
    try:
        await _wait_ready(server, task)
        assert await _upgrade_status(
            base_uri + "/ws/v1/control", client_ssl,
            origin="https://cross-site.invalid",
            headers={"Authorization": AUTHORIZATION},
        ) == http.HTTPStatus.FORBIDDEN
        assert await _upgrade_status(
            base_uri + "/ws/v1/control?site=other", client_ssl,
            headers={"Authorization": AUTHORIZATION},
        ) == http.HTTPStatus.BAD_REQUEST
        assert await _upgrade_status(
            base_uri + "/ws/v1/control", client_ssl,
        ) == http.HTTPStatus.UNAUTHORIZED

        await _control_scenario(base_uri, client_ssl, state)
        await _event_scenario(base_uri, client_ssl, state)
        await _gap_scenario(base_uri, client_ssl, state)
        await _revocation_scenario(base_uri, client_ssl, state)
        await asyncio.sleep(0)
        snapshot = server["snapshot"]()
        assert snapshot["ready"] is True
        assert snapshot["reserved_connections"] == 0
        assert snapshot["pending_principal_connections"] == 0
        assert snapshot["statistics"]["commands_accepted"] == 1
        assert snapshot["statistics"]["commands_rejected"] == 1
        rejections = snapshot["statistics"]["handshake_rejections"]
        assert rejections["origin"] >= 1
        assert rejections["path"] >= 1
        assert rejections["authentication"] >= 1
        assert snapshot["statistics"]["replay_sessions"] >= 2
        assert snapshot["statistics"]["replay_records"] == 2
        assert snapshot["statistics"]["event_fanout_deliveries"] == 2
        assert snapshot["statistics"]["command_results"][
            "runtime.status|completed"] == 1
        assert snapshot["statistics"]["command_results"][
            "other|invalid_payload"] == 1
        assert state["closes"]
        successful = [
            request for request in state["handshakes"]
            if request.get("authorization") == AUTHORIZATION
            and request.get("origin") == ORIGIN
        ]
        assert successful
        assert all(request["user_agent"] == USER_AGENT for request in successful)
    finally:
        server["stop"]()
        await asyncio.wait_for(task, timeout=5)
    stopped = server["snapshot"]()
    assert stopped["ready"] is False
    assert stopped["statistics"]["connections_closed"] >= 3
    assert stopped["statistics"]["connections_closed_cleanly"] >= 2
    assert stopped["task_count"] == 0


async def _pre_start_stop_scenario(paths):
    state = _state()
    server = _server(state, paths, _free_port())
    assert server["stop"]() is False
    assert server["snapshot"]()["shutting_down"] is True
    await asyncio.wait_for(server["serve"](), timeout=5)
    snapshot = server["snapshot"]()
    assert snapshot["ready"] is False
    assert snapshot["shutting_down"] is True


async def _connection_limit_scenario(paths):
    state = _state()
    state["server_config"] = {
        "max_connections": 2,
        "max_connections_per_source": 2,
        "max_connections_per_principal": 2,
    }
    port = _free_port()
    server = _server(state, paths, port)
    task = asyncio.create_task(server["serve"]())
    base_uri = "wss://127.0.0.1:{}".format(port)
    client_ssl = _client_ssl_context(paths["ca_cert"])
    first = None
    second = None
    try:
        await _wait_ready(server, task)
        options = {
            "ssl": client_ssl,
            "origin": ORIGIN,
            "additional_headers": {"Authorization": AUTHORIZATION},
            "user_agent_header": USER_AGENT,
            "proxy": None,
            "open_timeout": 3,
        }
        first = await websockets.connect(base_uri + "/ws/v1/control", **options)
        second = await websockets.connect(base_uri + "/ws/v1/control", **options)
        await _receive_json(first)
        await _receive_json(second)
        status = await _upgrade_status(
            base_uri + "/ws/v1/control", client_ssl,
            origin=ORIGIN, headers={"Authorization": AUTHORIZATION},
        )
        assert status == http.HTTPStatus.SERVICE_UNAVAILABLE
        assert server["snapshot"]()["statistics"]["handshake_rejections"][
            "connection_limit"] >= 1
    finally:
        if first:
            await first.close(code=1000)
        if second:
            await second.close(code=1000)
        server["stop"]()
        await asyncio.wait_for(task, timeout=5)
    assert server["snapshot"]()["clients"] == 0


async def _open_retention_gap_scenario(paths):
    state = _state()
    state["open_outcome"] = {"ok": False, "code": "replay_resync_required"}
    port = _free_port()
    server = _server(state, paths, port)
    task = asyncio.create_task(server["serve"]())
    connection = None
    try:
        await _wait_ready(server, task)
        connection = await websockets.connect(
            "wss://127.0.0.1:{}/ws/v1/events".format(port),
            ssl=_client_ssl_context(paths["ca_cert"]),
            origin=ORIGIN,
            additional_headers={"Authorization": AUTHORIZATION},
            user_agent_header=USER_AGENT,
            proxy=None,
            open_timeout=3,
        )
        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "subscribe",
            "request_id": "retention-open-gap",
            "last_event_id": 1,
            "filters": [],
        }))
        response = await _receive_json(connection)
        assert response == {
            "protocol": PROTOCOL,
            "type": "replay.resync_required",
            "code": "replay_resync_required",
        }
        await connection.wait_closed()
        assert connection.close_code == 1013
    finally:
        if connection and connection.close_code is None:
            await connection.close(code=1000)
        server["stop"]()
        await asyncio.wait_for(task, timeout=5)


async def _truthy_callback_scenario(paths):
    state = _state()
    port = _free_port()
    server = _server(state, paths, port)
    task = asyncio.create_task(server["serve"]())
    base_uri = "wss://127.0.0.1:{}".format(port)
    client_ssl = _client_ssl_context(paths["ca_cert"])
    try:
        await _wait_ready(server, task)
        state["authentication_outcome"] = {
            "ok": "true", "principal": _principal(),
        }
        status = await _upgrade_status(
            base_uri + "/ws/v1/control", client_ssl,
            headers={"Authorization": AUTHORIZATION},
        )
        assert status == http.HTTPStatus.UNAUTHORIZED
        state["authentication_outcome"] = None

        async with websockets.connect(
                base_uri + "/ws/v1/control", ssl=client_ssl, origin=ORIGIN,
                additional_headers={"Authorization": AUTHORIZATION},
                user_agent_header=USER_AGENT, proxy=None, open_timeout=3) as connection:
            await _receive_json(connection)
            state["revalidation_outcome"] = {
                "ok": "true", "principal": _principal(),
            }
            await connection.send(json.dumps({
                "protocol": PROTOCOL,
                "type": "command.request",
                "request_id": "truthy-revalidate",
                "action": "runtime.status",
                "payload": {},
            }))
            rejected = await _receive_json(connection)
            assert rejected["type"] == "command.rejected"
            assert rejected["code"] == "authentication_expired"

        state["revalidation_outcome"] = None
        async with websockets.connect(
                base_uri + "/ws/v1/control", ssl=client_ssl, origin=ORIGIN,
                additional_headers={"Authorization": AUTHORIZATION},
                user_agent_header=USER_AGENT, proxy=None, open_timeout=3) as connection:
            await _receive_json(connection)
            state["dispatch_outcome"] = {"ok": "true", "state": "COMPLETED"}
            await connection.send(json.dumps({
                "protocol": PROTOCOL,
                "type": "command.request",
                "request_id": "truthy-dispatch",
                "action": "runtime.status",
                "payload": {},
            }))
            rejected = await _receive_json(connection)
            assert rejected["type"] == "command.rejected"
            assert rejected["code"] == "internal_error"
    finally:
        server["stop"]()
        await asyncio.wait_for(task, timeout=5)


async def _truthy_event_callback_scenario(paths):
    state = _state()
    port = _free_port()
    server = _server(state, paths, port)
    task = asyncio.create_task(server["serve"]())
    base_uri = "wss://127.0.0.1:{}/ws/v1/events".format(port)
    options = {
        "ssl": _client_ssl_context(paths["ca_cert"]),
        "origin": ORIGIN,
        "additional_headers": {"Authorization": AUTHORIZATION},
        "user_agent_header": USER_AGENT,
        "proxy": None,
        "open_timeout": 3,
    }
    try:
        await _wait_ready(server, task)
        state["open_outcome"] = {"ok": "true"}
        async with websockets.connect(base_uri, **options) as connection:
            await connection.send(json.dumps({
                "protocol": PROTOCOL,
                "type": "subscribe",
                "request_id": "truthy-open",
                "last_event_id": 2,
                "filters": [],
            }))
            rejected = await _receive_json(connection)
            assert rejected["type"] == "subscription.rejected"
            assert rejected["code"] == "invalid_event_stream_contract"
            await connection.wait_closed()
            assert connection.close_code == 1011

        state["open_outcome"] = None
        state["page_outcome"] = {"ok": "true"}
        async with websockets.connect(base_uri, **options) as connection:
            await connection.send(json.dumps({
                "protocol": PROTOCOL,
                "type": "subscribe",
                "request_id": "truthy-page",
                "last_event_id": 0,
                "filters": [],
            }))
            accepted = await _receive_json(connection)
            resync = await _receive_json(connection)
            assert accepted["type"] == "subscription.accepted"
            assert resync["type"] == "replay.resync_required"
            await connection.wait_closed()
            assert connection.close_code == 1013

        state["page_outcome"] = None
        async with websockets.connect(base_uri, **options) as connection:
            await connection.send(json.dumps({
                "protocol": PROTOCOL,
                "type": "subscribe",
                "request_id": "truthy-update-open",
                "last_event_id": 2,
                "filters": [],
            }))
            accepted = await _receive_json(connection)
            assert accepted["type"] == "subscription.accepted"
            state["update_outcome"] = {"ok": "true", "generation": 2}
            await connection.send(json.dumps({
                "protocol": PROTOCOL,
                "type": "subscribe",
                "request_id": "truthy-update",
                "filters": [{
                    "resource_type": "operation",
                    "resource_pattern": "*",
                }],
            }))
            rejected = await _receive_json(connection)
            assert rejected["type"] == "subscription.rejected"
            assert rejected["code"] == "internal_error"
            await connection.wait_closed()
            assert connection.close_code == 1008
    finally:
        server["stop"]()
        await asyncio.wait_for(task, timeout=5)


def _single_core_client(stream):
    clients = tuple(stream["context"]["clients"].values())
    assert len(clients) == 1, len(clients)
    return clients[0]


async def _wait_core_live_waiter(stream):
    for _index in range(200):
        if len(stream["context"]["clients"]) == 1:
            client = _single_core_client(stream)
            if len(getattr(client["queue"], "_getters", ())) == 1:
                return client
        await asyncio.sleep(0.01)
    raise AssertionError("WSS event writer did not arm its live queue waiter")


async def _wait_core_client_count(stream, expected):
    for _index in range(200):
        if len(stream["context"]["clients"]) == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("event core client count did not converge")


async def _evt005_coalescing_scenario(paths):
    """EVT-005: bound telemetry and retain only the latest coalesced value."""
    state = _state()
    stream = _install_core_event_stream(state)
    port = _free_port()
    server = _server(state, paths, port)
    task = asyncio.create_task(server["serve"]())
    connection = None
    try:
        await _wait_ready(server, task)
        connection = await websockets.connect(
            "wss://127.0.0.1:{}/ws/v1/events".format(port),
            ssl=_client_ssl_context(paths["ca_cert"]),
            origin=ORIGIN,
            additional_headers={"Authorization": AUTHORIZATION},
            user_agent_header=USER_AGENT,
            proxy=None,
            open_timeout=3,
        )
        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "subscribe",
            "request_id": "evt-005-subscribe",
            "last_event_id": 0,
            "filters": [{
                "resource_type": "sensor",
                "resource_pattern": "*",
            }],
        }))
        accepted = await _receive_json(connection)
        assert accepted["type"] == "subscription.accepted"
        client = await _wait_core_live_waiter(stream)
        wss_client = tuple(server["context"]["clients"].values())[0]
        await wss_client["event_transition_lock"].acquire()
        try:
            for index in range(8):
                sequence = index + 1
                accepted_count = _persist_and_publish(
                    state, sequence, "sensor.value", "sensor",
                    "filler:{:02d}".format(index),
                    payload={
                        "controller_uuid": "filler",
                        "sensor_id": index,
                        "value": index,
                    },
                )
                assert accepted_count == 1
            for index in range(20):
                sequence = 100 + index
                accepted_count = _persist_and_publish(
                    state, sequence, "sensor.value", "sensor", "coalesced:1",
                    payload={
                        "controller_uuid": "coalesced",
                        "sensor_id": 1,
                        "value": index,
                    },
                )
                assert accepted_count == 1
            await asyncio.sleep(0)

            snapshot = stream["snapshot"]()
            assert snapshot["queue_depth"] == 8, snapshot
            assert snapshot["coalesced"] == 1, snapshot
            assert client["disconnect"] is False
            assert client["statistics"]["coalesced"] == 20
            assert client["statistics"]["telemetry_dropped"] == 0
        finally:
            wss_client["event_transition_lock"].release()

        received = []
        for _index in range(9):
            received.append(await _receive_json(connection))
        sequences = [
            event["extensions"]["live_sequence"] for event in received
        ]
        assert sequences == list(range(1, 9)) + [119], sequences
        coalesced = [
            event for event in received
            if event.get("payload", {}).get("controller_uuid") == "coalesced"
        ]
        assert len(coalesced) == 1
        assert coalesced[0]["payload"]["value"] == 19
        await asyncio.sleep(0)
        assert stream["snapshot"]()["coalesced"] == 0
        assert connection.close_code is None
    finally:
        if connection and connection.close_code is None:
            await connection.close(code=1000)
        server["stop"]()
        await asyncio.wait_for(task, timeout=5)


async def _evt006_critical_overflow_replay_scenario(paths):
    """EVT-006: critical overflow closes 1013 and reconnect replay recovers."""
    state = _state()
    state["server_config"] = {
        "replay_page_size": 8,
        "replay_page_limit": 8,
        "replay_record_limit": 64,
    }
    stream = _install_core_event_stream(state)
    port = _free_port()
    server = _server(state, paths, port)
    task = asyncio.create_task(server["serve"]())
    first = None
    replay = None
    options = {
        "ssl": _client_ssl_context(paths["ca_cert"]),
        "origin": ORIGIN,
        "additional_headers": {"Authorization": AUTHORIZATION},
        "user_agent_header": USER_AGENT,
        "proxy": None,
        "open_timeout": 3,
    }
    uri = "wss://127.0.0.1:{}/ws/v1/events".format(port)
    try:
        await _wait_ready(server, task)
        first = await websockets.connect(uri, **options)
        await first.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "subscribe",
            "request_id": "evt-006-overflow",
            "last_event_id": 0,
            "filters": [],
        }))
        accepted = await _receive_json(first)
        assert accepted["type"] == "subscription.accepted"
        client = await _wait_core_live_waiter(stream)

        for index in range(9):
            _persist_and_publish(
                state, index + 1, "sensor.value", "sensor",
                "overflow:{:02d}".format(index),
                payload={
                    "controller_uuid": "overflow",
                    "sensor_id": index,
                    "value": index,
                },
            )
        _persist_and_publish(
            state, 10, "operation.result", "operation",
            "operation-critical",
            subject="wss-user",
            payload={
                "operation_id": "operation-critical",
                "terminal_state": "CONFIRMED",
                "result": {"ok": True},
            },
        )
        await asyncio.sleep(0)
        assert client["disconnect"] is True
        assert client["statistics"]["critical_overflow"] == 1
        await asyncio.wait_for(first.wait_closed(), timeout=3)
        assert first.close_code == 1013
        await _wait_core_client_count(stream, 0)

        replay = await websockets.connect(uri, **options)
        await replay.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "subscribe",
            "request_id": "evt-006-replay",
            "last_event_id": 0,
            "filters": [{
                "resource_type": "operation",
                "resource_pattern": "operation-*",
            }],
        }))
        replay_accepted = await _receive_json(replay)
        recovered = await _receive_json(replay)
        assert replay_accepted["type"] == "subscription.accepted"
        assert replay_accepted["replay_high_watermark"] == 10
        assert recovered["event_type"] == "operation.result"
        assert recovered["payload"]["operation_id"] == "operation-critical"
        assert recovered["extensions"]["replay_sequence"] == 10
        assert replay.close_code is None
    finally:
        if first and first.close_code is None:
            await first.close(code=1000)
        if replay and replay.close_code is None:
            await replay.close(code=1000)
        server["stop"]()
        await asyncio.wait_for(task, timeout=5)


async def _evt007_generation_race_scenario(paths):
    """EVT-007: an orchestrated update drops stale generation exactly once."""
    state = _state()
    state["generation_race_enabled"] = True
    state["generation_update_entered"] = asyncio.Event()
    state["generation_update_release"] = asyncio.Event()
    stream = _install_core_event_stream(state)
    port = _free_port()
    server = _server(state, paths, port)
    task = asyncio.create_task(server["serve"]())
    connection = None
    try:
        await _wait_ready(server, task)
        connection = await websockets.connect(
            "wss://127.0.0.1:{}/ws/v1/events".format(port),
            ssl=_client_ssl_context(paths["ca_cert"]),
            origin=ORIGIN,
            additional_headers={"Authorization": AUTHORIZATION},
            user_agent_header=USER_AGENT,
            proxy=None,
            open_timeout=3,
        )
        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "subscribe",
            "request_id": "evt-007-initial",
            "last_event_id": 0,
            "filters": [{
                "resource_type": "operation",
                "resource_pattern": "old-*",
            }],
        }))
        initial = await _receive_json(connection)
        assert initial["type"] == "subscription.accepted"
        assert initial["generation"] == 1
        await _wait_core_live_waiter(stream)

        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "subscribe",
            "request_id": "evt-007-update",
            "filters": [{
                "resource_type": "operation",
                "resource_pattern": "new-*",
            }],
        }))
        await asyncio.wait_for(
            state["generation_update_entered"].wait(), timeout=2)
        _persist_and_publish(
            state, 1, "operation.result", "operation", "old-operation",
            subject="wss-user",
            payload={
                "operation_id": "old-operation",
                "terminal_state": "CONFIRMED",
                "marker": "stale-old-generation",
            },
        )
        await asyncio.sleep(0)
        state["generation_update_release"].set()

        updated = await _receive_json(connection)
        assert updated["type"] == "subscription.accepted", updated
        assert updated["request_id"] == "evt-007-update"
        assert updated["generation"] == 2
        _persist_and_publish(
            state, 2, "operation.result", "operation", "new-operation",
            subject="wss-user",
            payload={
                "operation_id": "new-operation",
                "terminal_state": "CONFIRMED",
                "marker": "current-generation",
            },
        )
        current = await _receive_json(connection)
        assert current["payload"]["marker"] == "current-generation"
        assert current["extensions"]["live_sequence"] == 2
        try:
            unexpected = await asyncio.wait_for(connection.recv(), timeout=0.25)
        except TimeoutError:
            unexpected = None
        assert unexpected is None, unexpected
        client = _single_core_client(stream)
        wss_clients = tuple(server["context"]["clients"].values())
        assert len(wss_clients) == 1
        assert wss_clients[0]["statistics"]["stale_events_dropped"] == 1
        assert client["statistics"]["sent"] == 1
        assert connection.close_code is None
    finally:
        if connection and connection.close_code is None:
            await connection.close(code=1000)
        server["stop"]()
        await asyncio.wait_for(task, timeout=5)


async def _shutdown_drain_scenario(paths):
    state = _state()
    state["blocking_dispatch"] = True
    state["blocking_delay_s"] = 1.2
    state["server_config"] = {"command_drain_timeout_s": 1.0}
    port = _free_port()
    server = _server(state, paths, port)
    task = asyncio.create_task(server["serve"]())
    connection = None
    try:
        await _wait_ready(server, task)
        connection = await websockets.connect(
            "wss://127.0.0.1:{}/ws/v1/control".format(port),
            ssl=_client_ssl_context(paths["ca_cert"]),
            origin=ORIGIN,
            additional_headers={"Authorization": AUTHORIZATION},
            user_agent_header=USER_AGENT,
            proxy=None,
            open_timeout=3,
        )
        await _receive_json(connection)
        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "command.request",
            "request_id": "shutdown-drain",
            "action": "runtime.status",
            "payload": {},
        }))
        started = await asyncio.to_thread(state["blocking_started"].wait, 2.0)
        assert started is True
        before_stop = time.monotonic()
        server["stop"]()
        await asyncio.wait_for(task, timeout=5)
        assert time.monotonic() - before_stop >= 1.05
        assert state["blocking_completed"] is True
        assert server["snapshot"]()["inflight_commands"] == 0
    finally:
        if connection and connection.close_code is None:
            await connection.close(code=1000)
        if not task.done():
            server["stop"]()
            await asyncio.wait_for(task, timeout=5)


async def _shutdown_admission_barrier_scenario(paths):
    state = _state()
    server = _server(state, paths, _free_port())
    context = server["context"]
    context["command_semaphore"] = asyncio.Semaphore(1)
    client = {
        "client_id": "admission-race-client",
        "source_ip": "127.0.0.1",
        "request": {
            "user_agent": USER_AGENT,
            "cookies": {},
        },
        "principal": _principal(),
    }
    request = {
        "request_id": "shutdown-admission-race",
        "action": "runtime.status",
        "idempotency_key": "shutdown-admission-race",
        "ttl_s": 30,
        "csrf_token": None,
        "payload": {},
    }
    create_entered = threading.Event()
    stop_attempted = threading.Event()
    stop_finished = threading.Event()
    stop_completed_during_create = []
    original_create_task = websocket_server_module.asyncio.create_task

    def stop_from_thread():
        create_entered.wait(2.0)
        stop_attempted.set()
        server["stop"]()
        stop_finished.set()

    def observed_create_task(coroutine, **kwargs):
        create_entered.set()
        stop_attempted.wait(1.0)
        time.sleep(0.02)
        stop_completed_during_create.append(stop_finished.is_set())
        return original_create_task(coroutine, **kwargs)

    thread = threading.Thread(target=stop_from_thread, daemon=True)
    thread.start()
    websocket_server_module.asyncio.create_task = observed_create_task
    try:
        result = await websocket_server_module._dispatch_command(
            context, client, request)
    finally:
        websocket_server_module.asyncio.create_task = original_create_task
    await asyncio.to_thread(thread.join, 2.0)
    assert not thread.is_alive()
    assert result["ok"] is True
    assert stop_completed_during_create == [False]
    assert stop_finished.is_set()
    assert context["shutting_down"] is True
    assert context["inflight_commands"] == set()


def _test_constructor_rejects_insecure_transport_contracts():
    state = _state()
    callbacks = {
        "authenticate": partial(_authenticate, state),
        "revalidate_principal": partial(_revalidate_principal, state),
        "dispatch_command": partial(_dispatch_command, state),
        "open_event_subscription": partial(_open_event_subscription, state),
        "read_replay_page": partial(_read_replay_page, state),
        "update_event_subscription": partial(_update_event_subscription, state),
        "close_event_subscription": partial(_close_event_subscription, state),
        "event_consumed": partial(_event_consumed, state),
    }
    insecure_configs = (
        {
            "site_id": SITE_ID, "instance_id": "proxy-01",
            "port": 8443, "origin_allowlist": ["*"],
            "tls": {"certfile": "cert", "keyfile": "key"},
        },
        {
            "site_id": SITE_ID, "instance_id": "proxy-01",
            "port": 8443, "origin_allowlist": [ORIGIN], "tls": {},
        },
        {
            "site_id": SITE_ID, "instance_id": "proxy-01",
            "port": 8443, "origin_allowlist": [ORIGIN],
            "tls": {"certfile": "cert", "keyfile": "key"},
            "principal_revalidate_interval_s": 30.001,
        },
        {
            "site_id": SITE_ID, "instance_id": "proxy-01",
            "port": 8443, "origin_allowlist": [ORIGIN],
            "tls": {"certfile": "cert", "keyfile": "key"},
            "max_connection_age_s": 1800.001,
        },
    )
    for config in insecure_configs:
        try:
            create_async_websocket_server(config=config, **callbacks)
        except ValueError:
            continue
        raise AssertionError("insecure direct WSS configuration was accepted")
    boundary = create_async_websocket_server(config={
        "site_id": SITE_ID,
        "instance_id": "proxy-01",
        "port": 8443,
        "origin_allowlist": [ORIGIN],
        "tls": {"certfile": "cert", "keyfile": "key"},
        "principal_revalidate_interval_s": 30.0,
        "max_connection_age_s": 1800.0,
    }, **callbacks)
    assert boundary["snapshot"]()["ready"] is False


def _test_json_boundary_types_and_diagnostics():
    prepared = json.loads(websocket_server_module._encode_json({
        "buffer": memoryview(b"buffer-data"),
        "interface": ipaddress.ip_interface("2001:db8::1/64"),
        "value": decimal.Decimal("1.250"),
    }))
    assert prepared == {
        "buffer": "buffer-data",
        "interface": "2001:db8::1/64",
        "value": "1.250",
    }
    try:
        websocket_server_module._encode_json({
            "reply": {"unsafe": _UnsupportedReplyValue()},
        })
    except ValueError as exc:
        diagnostic = str(exc)
        assert "$.reply.unsafe" in diagnostic
        assert "_UnsupportedReplyValue" in diagnostic
        assert "TOP-SECRET-REPR-MUST-NOT-BE-LOGGED" not in diagnostic
    else:
        raise AssertionError("unsupported WSS reply value was accepted")


def _test_direct_tls_websocket_server():
    output_directory = tempfile.mkdtemp(prefix="automation-wss-v0025-")
    try:
        material = generate_lab_websocket_pki(
            output_directory=output_directory,
            ip_addresses=["127.0.0.1"],
            valid_days=1,
            test_only=True,
        )
        paths = material["paths"]
        for name in ("server_key", "ca_key"):
            info = os.stat(paths[name])
            assert stat.S_ISREG(info.st_mode)
            if os.name != "nt":
                assert stat.S_IMODE(info.st_mode) == 0o600
        asyncio.run(_writer_send_timeout_scenario())
        asyncio.run(_direct_wss_scenario(paths))
        asyncio.run(_pre_start_stop_scenario(paths))
        asyncio.run(_connection_limit_scenario(paths))
        asyncio.run(_open_retention_gap_scenario(paths))
        asyncio.run(_json_boundary_control_scenario(paths))
        asyncio.run(_truthy_callback_scenario(paths))
        asyncio.run(_truthy_event_callback_scenario(paths))
        asyncio.run(_evt005_coalescing_scenario(paths))
        asyncio.run(_evt006_critical_overflow_replay_scenario(paths))
        asyncio.run(_evt007_generation_race_scenario(paths))
        asyncio.run(_shutdown_drain_scenario(paths))
        asyncio.run(_shutdown_admission_barrier_scenario(paths))
    finally:
        shutil.rmtree(output_directory)


def main():
    tests = (
        _test_constructor_rejects_insecure_transport_contracts,
        _test_json_boundary_types_and_diagnostics,
        _test_direct_tls_websocket_server,
    )
    for test in tests:
        test()
    print("PASS test_async_websockets_server (direct TLS, control, events, origin)")


if __name__ == "__main__":
    main()

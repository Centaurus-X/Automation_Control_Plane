# -*- coding: utf-8 -*-

"""Mandatory isolated component gates for the v0025 WSS E2E package.

These checks intentionally do not connect to the running lab.  The XWK gates
compose the production Master operation core with the fn-02 protocol actor.
PAR-002 starts the production direct-``websockets`` Proxy listener with an
ephemeral, CA-signed TLS identity and real WebSocket clients.
"""

import argparse
import asyncio
import copy
import http
import json
import logging
import os
import pathlib
import shutil
import socket
import ssl
import sys
import tempfile
import time

from functools import partial

import websockets


VERSION = "0.25.0-e2e.3"
SITE_ID = "site-main"
MASTER_ID = "master-01"
WORKER_ID = "fn-02"
ORIGIN = "https://isolated-e2e.invalid:8444"
AUTHORIZATION = "Bearer isolated-component-gate"

ROOT = pathlib.Path(__file__).resolve().parents[2]
MASTER_SOURCE = ROOT / "master_node" / "master_node" / "src"
PROXY_ROOT = ROOT / "proxy_gateway" / "proxy_gateway"
for source_path in (PROXY_ROOT, MASTER_SOURCE):
    sys.path.insert(0, str(source_path))

from src.library._generic_system_contract_library import make_envelope
from src.library._generic_system_contract_library.protocol_contracts import PROTOCOL_V2
from core.operation_management import create_operation_management
from core.scope_activation import validate_scope_intent
from src.interfaces.async_websockets_server import create_async_websocket_server
from src.library.generate_ssl_certs import generate_lab_websocket_pki

import fn02_protocol_simulator as fn02_protocol


def _fixture():
    path = pathlib.Path(__file__).with_name("fixtures") / "cross_worker_site-main.json"
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


def _master_management(captured):
    fixture = _fixture()
    target = fixture["target"]
    cross = fixture["cross"]
    authority = {
        "lease_active": True,
        "leader_instance_id": MASTER_ID,
        "lease_token": "isolated-master-lease",
        "authority_term": 9,
        "config_revision": 12,
    }
    config = {
        "actuators": [{
            "site_id": SITE_ID,
            "node_id": WORKER_ID,
            "controller_uuid": target["controller_uuid"],
            "enabled": True,
            "rows": [{
                "actuator_id": target["actuator_id"],
                "function_type": target["function_type"],
                "address": target["address"],
                "enabled": True,
            }],
        }],
    }
    operations = {}
    cross_snapshot = {
        "scope": cross["scope"],
        "scope_kind": "cross",
        "owner_node_id": cross["owner_node_id"],
        "scope_revision": 6,
        "fingerprint": "b" * 64,
        "execution_epoch": 7,
    }

    def create(request):
        captured["creates"].append(copy.deepcopy(request))
        operation = copy.deepcopy(request)
        operation["operation_id"] = "00000000-0000-0000-0000-000000000015"
        operation["state"] = "RECEIVED"
        operations[operation["operation_id"]] = operation
        return {"ok": True, "duplicate": False, "operation": copy.deepcopy(operation)}

    def transition(operation_id, state, **_kwargs):
        captured["transitions"].append((operation_id, state))
        operations[operation_id]["state"] = state
        return {"ok": True, "operation": copy.deepcopy(operations[operation_id])}

    def unexpected(*_args, **_kwargs):
        raise AssertionError("unexpected Master capability call")

    def read_authority():
        return copy.deepcopy(authority)

    def read_config():
        return {"revision": 12, "config_data": copy.deepcopy(config)}

    def resolve_owner(controller_uuid):
        assert controller_uuid == target["controller_uuid"]
        return {"owner_node_id": WORKER_ID, "lease_token": "isolated-owner-lease"}

    def read_scope(scope):
        assert scope == "node:" + WORKER_ID
        return {
            "scope": scope,
            "scope_revision": 5,
            "fingerprint": "c" * 64,
            "execution_epoch": 4,
            "global_revision": 12,
        }

    def read_node(node_id):
        assert node_id == WORKER_ID
        return {
            "node_id": node_id,
            "admission_state": "APPROVED",
            "desired_state": "ACTIVE",
            "observed_state": "ACTIVE",
            "observed_global_revision": 12,
            "node_replica_scope": "node:" + WORKER_ID,
            "node_replica_revision": 5,
            "node_replica_fingerprint": "c" * 64,
        }

    def validate_scope(source_node, payload):
        return validate_scope_intent(cross_snapshot, source_node, payload)

    store = {
        "create": create,
        "transition": transition,
        "get": operations.get,
        "list": unexpected,
        "apply_worker_result": unexpected,
    }
    return create_operation_management(
        site_id=SITE_ID,
        instance_id=MASTER_ID,
        legacy_coordinator_node_id=MASTER_ID,
        authority_reader=read_authority,
        config_reader=read_config,
        ownership={"resolve": resolve_owner},
        store=store,
        scope_validator=validate_scope,
        scope_reader=read_scope,
        node_reader=read_node,
    )


def _master_request(execution_epoch, ttl_s):
    captured = {"creates": [], "transitions": []}
    management = _master_management(captured)
    fixture = _fixture()
    target = fixture["target"]
    envelope = make_envelope(
        "actuator.request",
        {"service": "worker", "instance_id": fixture["cross"]["owner_node_id"]},
        {"service": "master", "instance_id": MASTER_ID},
        site_id=SITE_ID,
        idempotency_key="isolated-xwk-intent",
        principal={"subject": "worker:" + fixture["cross"]["owner_node_id"]},
        ttl_s=ttl_s,
        payload={
            "controller_uuid": target["controller_uuid"],
            "actuator_id": target["actuator_id"],
            "value": target["trigger_command_value"],
            "execution_scope": fixture["cross"]["scope"],
            "execution_epoch": execution_epoch,
            "distributed_protocol": PROTOCOL_V2,
        },
    )
    return management["request"](envelope), captured


def _outer_command(worker_command):
    return {
        "protocol": fn02_protocol.OUTER_PROTOCOL,
        "kind": "command",
        "message_id": worker_command["message_id"],
        "site_id": SITE_ID,
        "source_node": MASTER_ID,
        "target_node": WORKER_ID,
        "created_at_s": time.time(),
        "expires_at_s": worker_command["expires_at_s"],
        "command": worker_command["command"],
        "payload": copy.deepcopy(worker_command["payload"]),
    }


def _worker_context():
    node_config = fn02_protocol._load_node_authority_fixture(WORKER_ID)
    context = {
        "site_id": SITE_ID,
        "node_id": WORKER_ID,
        "master_node_id": MASTER_ID,
        "run_id": "isolated-component-gates",
        "fixture": _fixture(),
        "self_test": True,
        "state": fn02_protocol._initial_state(node_config),
        "dedupe": {},
        "statistics": {
            "commands": 0,
            "duplicates": 0,
            "scope_applies": 0,
            "full_applies": 0,
            "patch_applies": 0,
            "actuator_results": 0,
        },
        "actuator_trace": [],
    }
    context["state"]["coordinator_term"] = 9
    context["state"]["observed_global_revision"] = 12
    context["state"]["node_replica"] = {
        "scope": "node:" + WORKER_ID,
        "revision": 5,
        "fingerprint": "c" * 64,
        "execution_epoch": 4,
    }
    context["state"]["actuators"] = {"sentinel": "unchanged"}
    return context


def _valid_command():
    result, captured = _master_request(7, 30.0)
    assert result.get("ok"), result
    assert result.get("duplicate") is False
    assert result["operation"]["state"] == "ROUTED"
    assert len(captured["creates"]) == 1
    assert captured["transitions"] == [
        ("00000000-0000-0000-0000-000000000015", "ROUTED")
    ]
    command = _outer_command(result["worker_command"])
    payload = command["payload"]
    assert payload["coordinator_term"] == 9
    assert payload["authority_revision"] == 12
    assert payload["target_node_scope_revision"] == 5
    assert payload["target_node_scope_fingerprint"] == "c" * 64
    assert payload["target_node_execution_epoch"] == 4
    assert payload["task"]["execution_scope"] == _fixture()["cross"]["scope"]
    assert payload["task"]["execution_epoch"] == 7
    return command


def _assert_rejected_without_execution(context, result, expected_code):
    assert not result.get("ok"), result
    assert result.get("code") == expected_code, result
    assert context["state"]["actuators"] == {"sentinel": "unchanged"}
    assert context["actuator_trace"] == []
    assert context["statistics"]["actuator_results"] == 0


def _gate_xwk_004():
    stale_master, captured = _master_request(6, 30.0)
    assert not stale_master.get("ok"), stale_master
    assert stale_master.get("code") == "stale_execution_epoch", stale_master
    assert captured["creates"] == []
    assert captured["transitions"] == []

    valid = _valid_command()
    control = _worker_context()
    accepted = fn02_protocol._execute_command(control, copy.deepcopy(valid))
    assert accepted.get("ok"), accepted
    assert len(control["actuator_trace"]) == 1

    stale_term = copy.deepcopy(valid)
    stale_term["message_id"] += ":stale-term"
    stale_term["payload"]["coordinator_term"] = 8
    term_context = _worker_context()
    term_result = fn02_protocol._execute_command(term_context, stale_term)
    _assert_rejected_without_execution(
        term_context, term_result, "stale_authority_term")

    stale_epoch = copy.deepcopy(valid)
    stale_epoch["message_id"] += ":stale-epoch"
    stale_epoch["payload"]["target_node_execution_epoch"] = 3
    epoch_context = _worker_context()
    epoch_result = fn02_protocol._execute_command(epoch_context, stale_epoch)
    _assert_rejected_without_execution(
        epoch_context, epoch_result, "stale_execution_epoch")
    print("PASS XWK-004 isolated Master-to-worker term/epoch fencing")


def _gate_xwk_005():
    command = _valid_command()
    context = _worker_context()
    first = fn02_protocol._execute_command(context, copy.deepcopy(command))
    second = fn02_protocol._execute_command(context, copy.deepcopy(command))
    assert first.get("ok") and second == first
    assert len(context["dedupe"]) == 1
    assert context["statistics"]["commands"] == 1
    assert context["statistics"]["duplicates"] == 1
    assert context["statistics"]["actuator_results"] == 1
    assert len(context["actuator_trace"]) == 1
    assert context["actuator_trace"][0]["message_id"] == command["message_id"]
    target = _fixture()["target"]
    assert context["state"]["actuators"][str(target["actuator_id"])] \
        == target["trigger_command_value"]
    print("PASS XWK-005 isolated duplicate intent executes exactly once")


def _gate_xwk_006():
    expired_master, captured = _master_request(7, -1.0)
    assert not expired_master.get("ok"), expired_master
    assert expired_master.get("code") == "expired", expired_master
    assert captured["creates"] == []
    assert captured["transitions"] == []

    valid = _valid_command()
    control = _worker_context()
    assert fn02_protocol._execute_command(
        control, copy.deepcopy(valid)).get("ok")

    expired = copy.deepcopy(valid)
    expired["message_id"] += ":expired"
    expired["expires_at_s"] = time.time() - 1.0
    context = _worker_context()
    result = fn02_protocol._execute_command(context, expired)
    _assert_rejected_without_execution(context, result, "expired")
    assert context["statistics"]["commands"] == 0
    assert context["statistics"]["duplicates"] == 0
    assert context["dedupe"] == {}
    print("PASS XWK-006 isolated expired intent is never executed")


async def _authenticate(state, request):
    state["handshakes"].append(copy.deepcopy(request))
    if request.get("authorization") != AUTHORIZATION:
        return {"ok": False, "code": "authentication_required"}
    return {"ok": True, "principal": {
        "subject": "isolated-par-002",
        "permissions": [],
        "site_scopes": [SITE_ID],
        "resource_scopes": [],
    }}


async def _revalidate(_state, request):
    return {"ok": True, "principal": copy.deepcopy(request["principal"])}


async def _dispatch(_state, _request):
    raise AssertionError("PAR-002 must not dispatch commands")


async def _open_subscription(_state, _request):
    raise AssertionError("PAR-002 must not open event subscriptions")


async def _read_replay(_state, _request):
    raise AssertionError("PAR-002 must not read replay pages")


async def _update_subscription(_state, _request):
    raise AssertionError("PAR-002 must not update event subscriptions")


async def _close_subscription(_state, _request):
    return {"ok": True, "closed": True}


async def _event_consumed(_state, _request):
    raise AssertionError("PAR-002 must not consume events")


def _free_port():
    listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])
    finally:
        listener.close()


def _proxy_server(state, paths, port):
    config = {
        "site_id": SITE_ID,
        "instance_id": "proxy-par-002",
        "host": "127.0.0.1",
        "port": port,
        "origin_allowlist": [ORIGIN],
        "tls": {
            "certfile": paths["server_cert"],
            "keyfile": paths["server_key"],
            "minimum_tls_version": "TLSv1_2",
        },
        "max_connections": 2,
        "max_connections_per_source": 8,
        "max_connections_per_principal": 8,
        "open_timeout_s": 3,
        "close_timeout_s": 2,
        "ping_interval_s": 5,
        "ping_timeout_s": 5,
        "send_timeout_s": 2,
        "initial_message_timeout_s": 2,
        "principal_revalidate_interval_s": 5,
        "max_connection_age_s": 60,
        "replay_timeout_s": 5,
    }
    return create_async_websocket_server(
        config=config,
        allowed_actions=(),
        authenticate=partial(_authenticate, state),
        revalidate_principal=partial(_revalidate, state),
        dispatch_command=partial(_dispatch, state),
        open_event_subscription=partial(_open_subscription, state),
        read_replay_page=partial(_read_replay, state),
        update_event_subscription=partial(_update_subscription, state),
        close_event_subscription=partial(_close_subscription, state),
        event_consumed=partial(_event_consumed, state),
        logger=logging.getLogger("e2e.v0025.par-002"),
    )


def _client_ssl(ca_file):
    context = ssl.create_default_context(
        purpose=ssl.Purpose.SERVER_AUTH, cafile=ca_file)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    return context


async def _wait_ready(server, task):
    for _index in range(500):
        if task.done():
            await task
        if server["snapshot"]()["ready"]:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("PAR-002 direct TLS-WSS listener did not become ready")


async def _wait_clients(server, expected):
    for _index in range(500):
        if server["snapshot"]()["clients"] == expected:
            return
        await asyncio.sleep(0.01)
    raise AssertionError("PAR-002 client count did not converge to " + str(expected))


async def _connect(uri, client_ssl):
    connection = await websockets.connect(
        uri,
        ssl=client_ssl,
        origin=ORIGIN,
        additional_headers={"Authorization": AUTHORIZATION},
        user_agent_header="automation-e2e-par-002/0015",
        proxy=None,
        open_timeout=3,
    )
    ready = json.loads(await asyncio.wait_for(connection.recv(), timeout=3))
    assert ready.get("type") == "connection.ready", ready
    assert ready.get("endpoint") == "control", ready
    return connection


async def _par_002_scenario(paths):
    state = {"handshakes": []}
    port = _free_port()
    server = _proxy_server(state, paths, port)
    task = asyncio.create_task(server["serve"]())
    uri = "wss://127.0.0.1:{}/ws/v1/control".format(port)
    client_ssl = _client_ssl(paths["ca_cert"])
    first = None
    second = None
    replacement = None
    try:
        await _wait_ready(server, task)
        first = await _connect(uri, client_ssl)
        second = await _connect(uri, client_ssl)
        before = server["snapshot"]()
        assert before["clients"] == 2
        assert before["reserved_connections"] == 0
        assert before["task_count"] == 8
        try:
            rejected = await _connect(uri, client_ssl)
        except websockets.InvalidStatus as exc:
            assert exc.response.status_code == http.HTTPStatus.SERVICE_UNAVAILABLE
        else:
            await rejected.close(code=1000)
            raise AssertionError("PAR-002 third client bypassed max_connections=2")
        after = server["snapshot"]()
        assert after["clients"] == before["clients"] == 2
        assert after["reserved_connections"] == 0
        assert after["task_count"] == before["task_count"] == 8
        assert after["statistics"]["connections_accepted"] == 2
        assert after["statistics"]["handshake_rejections"]["connection_limit"] == 1
        assert len(state["handshakes"]) == 2

        await first.close(code=1000)
        first = None
        await _wait_clients(server, 1)
        replacement = await _connect(uri, client_ssl)
        recovered = server["snapshot"]()
        assert recovered["clients"] == 2
        assert recovered["statistics"]["connections_accepted"] == 3
    finally:
        for connection in (first, second, replacement):
            if connection is not None:
                await connection.close(code=1000)
        await _wait_clients(server, 0)
        server["stop"]()
        await asyncio.wait_for(task, timeout=5)
    stopped = server["snapshot"]()
    assert stopped["ready"] is False
    assert stopped["clients"] == 0
    assert stopped["reserved_connections"] == 0
    assert stopped["task_count"] == 0


def _gate_par_002():
    output_directory = tempfile.mkdtemp(
        prefix="automation-e2e-par-002-", dir="/tmp")
    try:
        material = generate_lab_websocket_pki(
            output_directory=output_directory,
            ip_addresses=["127.0.0.1"],
            valid_days=1,
            test_only=True,
        )
        asyncio.run(_par_002_scenario(material["paths"]))
    finally:
        shutil.rmtree(output_directory)
    print("PASS PAR-002 isolated direct TLS-WSS max_connections=2 returns 503")


def _parser():
    parser = argparse.ArgumentParser(
        description="Mandatory isolated v0025 E2E component gates")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def main():
    _parser().parse_args()
    if not __debug__:
        raise RuntimeError(
            "isolated component gates require assertions; Python optimization is forbidden")
    _gate_xwk_004()
    _gate_xwk_005()
    _gate_xwk_006()
    _gate_par_002()
    print("PASS mandatory isolated component gates XWK-004/005/006 and PAR-002")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

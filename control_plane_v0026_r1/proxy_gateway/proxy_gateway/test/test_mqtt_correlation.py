# -*- coding: utf-8 -*-

"""Functional Proxy-to-Master command and exact ACK-correlation tests."""

import asyncio
import copy
import threading
import uuid

from unittest.mock import Mock

from src.library._generic_system_contract_library.protocol_contracts import make_envelope
from src.library._generic_type_library import canonical_bytes, decode_json_bytes
from src.library._generic_system_contract_library.protocol_contracts import proxy_ack_topic
from src.core.event_stream import create_event_stream
from src.interfaces.mqtt_gateway_interface import (
    _decode_and_validate,
    _handle_ack,
    _handle_event,
    create_mqtt_gateway_interface,
)
from src.library.gateway_store_postgresql import operation_owners


def _test_command_ack_correlation():
    commands = {}
    published = []
    transport_options = {}
    delivery_order = []

    def create_command(request):
        record = copy.deepcopy(request)
        record.update({"state": "PENDING", "result": None})
        commands[record["request_id"]] = record
        return {"ok": True, "duplicate": False, "command": copy.deepcopy(record)}

    def update_command(request_id, result):
        if not transport_options.get("persist_ack", True):
            return None
        commands[request_id]["state"] = "COMPLETED" if result.get("ok") else "FAILED"
        commands[request_id]["result"] = copy.deepcopy(result)
        return copy.deepcopy(commands[request_id])

    def get_command(request_id):
        return copy.deepcopy(commands.get(request_id))

    def mark_command_attempt(request_id, **kwargs):
        commands[request_id]["state"] = "ACCEPTED" if kwargs.get("published") else "PENDING"
        return copy.deepcopy(commands[request_id])

    def cache_event(_request):
        delivery_order.append("cache")
        return {"ok": True, "event_sequence": 1}

    def stream_publisher(*_args, **_kwargs):
        delivery_order.append("stream")
        return True

    def signal_waiter():
        delivery_order.append("waiter")

    def factory(**kwargs):
        transport_options["subscriptions"] = copy.deepcopy(
            kwargs["subscriptions"])
        transport_options["publish_result"] = True
        transport_options["persist_ack"] = True

        def publish(topic, payload, **_options):
            published.append({"topic": topic, "payload": payload})
            return transport_options["publish_result"]

        return {
            "publish": publish,
            "start": return_true,
            "stop": return_true,
            "wait_connected": return_true,
            "snapshot": connected_snapshot,
        }

    api = create_mqtt_gateway_interface(
        site_id="site-main",
        instance_id="proxy-01",
        master_instance_id="master-01",
        options={
            "host": "127.0.0.1", "port": 1883,
            "allow_insecure_transport": True, "tls": {"enabled": False},
        },
        gateway_store={
            "create_command": create_command,
            "update_command": update_command,
            "get_command": get_command,
            "mark_command_attempt": mark_command_attempt,
            "cache_event": cache_event,
        },
        stream_publisher=stream_publisher,
        transport_factory=factory,
    )
    subscriptions = {
        item["topic"] for item in transport_options["subscriptions"]
    }
    assert "automation/v1/site-main/events/+/master-01" in subscriptions
    assert "automation/v1/site-main/events/#" not in subscriptions
    principal = {
        "subject": "00000000-0000-0000-0000-000000000001",
        "permissions": ["node.read"],
        "site_scopes": ["site-main"],
        "resource_scopes": [],
    }
    accepted = api["send_command"](
        "nodes.list", {}, principal=principal, wait_timeout_s=0.0)
    assert accepted["ok"] and len(published) == 1
    assert api["snapshot"]()["waiters"] == 0
    command = decode_json_bytes(published[0]["payload"])
    ack = make_envelope(
        "command.ack",
        {"service": "master", "instance_id": "master-01"},
        {"service": "proxy", "instance_id": "proxy-01"},
        site_id="site-main",
        correlation_id=command["correlation_id"],
        causation_id=command["message_id"],
        principal=principal,
        payload={"command": "nodes.list", "result": {"ok": True, "nodes": []}},
    )
    topic = proxy_ack_topic(
        "site-main", "proxy-01", "master-01", command["message_id"])
    envelope, error = _decode_and_validate(api["context"], {
        "topic": topic,
        "payload": canonical_bytes(ack),
        "retain": False,
    })
    waiter_event = Mock()
    waiter_event.set = signal_waiter
    api["context"]["waiters"][command["message_id"]] = {
        "event": waiter_event,
        "result": None,
    }
    assert error is None and _handle_ack(api["context"], envelope)
    assert commands[command["message_id"]]["state"] == "COMPLETED"
    assert delivery_order == ["cache", "stream", "waiter"]
    assert api["context"]["waiters"][command["message_id"]]["result"] == {
        "ok": True,
        "nodes": [],
    }
    api["context"]["waiters"].pop(command["message_id"])
    _envelope, wrong = _decode_and_validate(api["context"], {
        "topic": topic + "-forged",
        "payload": canonical_bytes(ack),
        "retain": False,
    })
    assert wrong["code"] == "identity_mismatch"

    transport_options["publish_result"] = False
    delivery_pending = api["send_command"](
        "nodes.list", {}, principal=principal, wait_timeout_s=0.0)
    assert delivery_pending["ok"]
    assert delivery_pending["accepted"] is True
    assert delivery_pending["delivery_pending"] is True
    assert delivery_pending["state"] == "PENDING"
    assert commands[delivery_pending["request_id"]]["state"] == "PENDING"
    assert api["snapshot"]()["waiters"] == 0

    transport_options["publish_result"] = True
    unpersisted = api["send_command"](
        "nodes.list", {}, principal=principal, wait_timeout_s=0.0)
    unpersisted_message = decode_json_bytes(published[-1]["payload"])
    unpersisted_ack = make_envelope(
        "command.ack",
        {"service": "master", "instance_id": "master-01"},
        {"service": "proxy", "instance_id": "proxy-01"},
        site_id="site-main",
        correlation_id=unpersisted_message["correlation_id"],
        causation_id=unpersisted_message["message_id"],
        principal=principal,
        payload={"command": "nodes.list", "result": {"ok": True, "nodes": []}},
    )
    unpersisted_topic = proxy_ack_topic(
        "site-main", "proxy-01", "master-01", unpersisted["request_id"])
    decoded, decode_error = _decode_and_validate(api["context"], {
        "topic": unpersisted_topic,
        "payload": canonical_bytes(unpersisted_ack),
        "retain": False,
    })
    transport_options["persist_ack"] = False
    acks_before = api["snapshot"]()["statistics"]["acks_received"]
    assert decode_error is None
    assert _handle_ack(api["context"], decoded) is False
    assert api["snapshot"]()["statistics"]["acks_received"] == acks_before
    assert api["snapshot"]()["last_error"] == \
        "command ACK could not be persisted"


def _test_operation_owner_store_contract():
    state = {
        "executions": [],
        "transactions": 0,
        "rows": [
            {
                "operation_id": "operation-a",
                "owner_subject": "00000000-0000-0000-0000-000000000001",
            },
            {
                "operation_id": "operation-a",
                "owner_subject": "00000000-0000-0000-0000-000000000001",
            },
            {
                "operation_id": "operation-b",
                "owner_subject": "00000000-0000-0000-0000-000000000001",
            },
            {
                "operation_id": "operation-b",
                "owner_subject": "00000000-0000-0000-0000-000000000002",
            },
        ],
    }

    def fetchall():
        return copy.deepcopy(state["rows"])

    def execute(statement, arguments):
        state["executions"].append({
            "statement": statement,
            "arguments": copy.deepcopy(arguments),
        })
        cursor = Mock()
        cursor.fetchall = fetchall
        return cursor

    connection = Mock()
    connection.execute = execute

    def transaction(operation, **kwargs):
        state["transactions"] += 1
        state["isolation_level"] = kwargs.get("isolation_level")
        return operation(connection)

    database = {"transaction": transaction}
    resolved = operation_owners(database, "site-main", [
        "operation-b", "operation-a", "operation-a", "",
        None, "x" * 129,
    ])
    assert resolved == {
        "ok": True,
        "owners": {
            "operation-a": "00000000-0000-0000-0000-000000000001",
        },
    }
    assert state["transactions"] == 1
    assert state["isolation_level"] == "READ COMMITTED"
    query = state["executions"][0]
    assert "WHERE site_id=%s" in query["statement"]
    assert "operation_id=ANY(%s::text[])" in query["statement"]
    assert query["arguments"] == (
        "site-main", ["operation-a", "operation-b"])

    empty = operation_owners(database, "site_other", [None, "", "x" * 129])
    assert empty == {"ok": True, "owners": {}}
    assert state["transactions"] == 1
    excessive = operation_owners(database, "site-main", [
        "operation-{:03d}".format(index) for index in range(501)
    ])
    assert excessive["ok"] is False
    assert excessive["code"] == "invalid_payload"
    assert state["transactions"] == 1


def _operation_event(operation_id):
    return make_envelope(
        "operation.result",
        {"service": "master", "instance_id": "master-01"},
        {"service": "proxy", "instance_id": "broadcast"},
        site_id="site-main",
        principal={"subject": "system:master"},
        payload={
            "operation_id": operation_id,
            "terminal_state": "CONFIRMED",
            "result": {"ok": True},
        },
    )


async def _operation_live_owner_binding_scenario():
    owner_subject = "00000000-0000-0000-0000-000000000001"
    mapped_operation = str(uuid.uuid4())
    ambiguous_operation = str(uuid.uuid4())
    missing_operation = str(uuid.uuid4())
    stream = create_event_stream(queue_maxsize=8)
    registered = stream["register"](
        site_id="site-main",
        principal={
            "subject": owner_subject,
            "permissions": ["operation.read"],
            "site_scopes": ["site-main"],
            "resource_scopes": [],
        },
        filters=[],
    )
    assert registered["ok"] is True
    client = registered["client"]
    state = {
        "cached": [],
        "next_sequence": 0,
        "owners": {mapped_operation: owner_subject},
        "owner_requests": [],
    }

    def resolve_owners(site_id, operation_ids):
        state["owner_requests"].append({
            "site_id": site_id,
            "operation_ids": list(operation_ids),
        })
        return {
            "ok": True,
            "owners": {
                operation_id: state["owners"][operation_id]
                for operation_id in operation_ids
                if operation_id in state["owners"]
            },
        }

    def cache_event(request):
        state["next_sequence"] += 1
        state["cached"].append(copy.deepcopy(request))
        return {
            "ok": True,
            "event_sequence": state["next_sequence"],
        }

    gateway_store = {
        "operation_owners": resolve_owners,
        "cache_event": cache_event,
    }
    context = {
        "site_id": "site-main",
        "options": {"replay_ttl_s": 3600},
        "gateway_store": gateway_store,
        "stream_publisher": stream["publish"],
        "lock": threading.RLock(),
        "last_error": None,
        "statistics": {"events_received": 0},
    }

    original = _operation_event(mapped_operation)
    assert _handle_event(context, original) is True
    await asyncio.sleep(0)
    delivered = client["queue"].get_nowait()
    client["queue"].task_done()
    envelope = delivered["envelope"]
    assert envelope["principal"]["subject"] == owner_subject
    assert envelope["extensions"]["producer_subject"] == "system:master"
    assert envelope["extensions"]["replay_sequence"] == 1
    assert state["cached"][-1]["envelope"]["principal"]["subject"] == \
        owner_subject
    assert state["cached"][-1]["envelope"]["extensions"][
        "producer_subject"] == "system:master"
    assert original["principal"]["subject"] == "system:master"
    assert "producer_subject" not in (original.get("extensions") or {})
    assert state["owner_requests"][-1] == {
        "site_id": "site-main",
        "operation_ids": [mapped_operation],
    }

    state["owners"] = {}
    assert _handle_event(
        context, _operation_event(ambiguous_operation)) is True
    await asyncio.sleep(0)
    assert client["queue"].empty()
    assert state["cached"][-1]["envelope"]["principal"]["subject"] == \
        "system:master"

    gateway_store.pop("operation_owners")
    assert _handle_event(context, _operation_event(missing_operation)) is True
    await asyncio.sleep(0)
    assert client["queue"].empty()
    assert state["cached"][-1]["envelope"]["principal"]["subject"] == \
        "system:master"
    assert context["statistics"]["events_received"] == 3
    assert stream["unregister"](client["client_id"]) is True


def _test_operation_live_owner_binding():
    asyncio.run(_operation_live_owner_binding_scenario())


def return_true(*_args, **_kwargs):
    return True


def connected_snapshot():
    return {"connected": True}


def main():
    tests = (
        _test_command_ack_correlation,
        _test_operation_owner_store_contract,
        _test_operation_live_owner_binding,
    )
    for test in tests:
        test()
    print(
        "PASS test_mqtt_correlation "
        "(correlation, durable ACK, operation owner/live binding)")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-

"""Event RBAC, replay, generation and bounded-overflow regression tests."""

import asyncio
import copy
import threading
import uuid

from functools import partial

from src.library._generic_system_contract_library.protocol_contracts import make_envelope
from src.core.event_stream import (
    CRITICAL_EVENT_TYPES,
    create_event_stream,
    validate_filters,
)


SITE_ID = "site-main"


def _principal(subject, permissions, **kwargs):
    return {
        "subject": subject,
        "permissions": list(permissions),
        "site_scopes": list(kwargs.get("site_scopes", (SITE_ID,))),
        "resource_scopes": copy.deepcopy(kwargs.get("resource_scopes", [])),
    }


def _envelope(event_type, **kwargs):
    return make_envelope(
        event_type,
        {"service": "master", "instance_id": "master-01"},
        {"service": "proxy", "instance_id": "broadcast"},
        site_id=kwargs.get("site_id", SITE_ID),
        principal={"subject": kwargs.get("subject", "system:master")},
        payload=copy.deepcopy(kwargs.get("payload", {"value": 1})),
    )


def _record(sequence, event_type, resource_type, resource_id, **kwargs):
    return {
        "event_sequence": sequence,
        "event_type": event_type,
        "resource_type": resource_type,
        "resource_id": resource_id,
        "envelope": _envelope(event_type, **kwargs),
    }


def _replay_window(state, site_id):
    assert site_id == SITE_ID
    return {
        "high_watermark": state["high_watermark"],
        "retention_floor": state["retention_floor"],
        "snapshot_at": state["snapshot_at"],
    }


def _replay_page(state, request):
    state["page_requests"].append(copy.deepcopy(request))
    if state.get("gap"):
        return {"gap": True, "records": []}
    after = int(request.get("after_sequence") or 0)
    through = int(request.get("through_sequence") or 0)
    limit = int(request.get("limit") or 1)
    eligible = [
        copy.deepcopy(record)
        for record in state["records"]
        if after < int(record["event_sequence"]) <= through
    ]
    page = eligible[:limit]
    scanned_through = int(page[-1]["event_sequence"]) if page else through
    return {
        "gap": False,
        "records": page,
        "scanned_through": scanned_through,
        "complete": scanned_through >= through,
    }


def _operation_owners(state, site_id, operation_ids):
    assert site_id == SITE_ID
    owners = state.get("operation_owners") or {}
    return {
        "ok": True,
        "owners": {
            operation_id: owners[operation_id]
            for operation_id in operation_ids
            if operation_id in owners
        },
    }


def _store(state):
    return {
        "replay_window": partial(_replay_window, state),
        "replay_page": partial(_replay_page, state),
        "operation_owners": partial(_operation_owners, state),
    }


def _blocking_replay_window(state, site_id):
    assert site_id == SITE_ID
    state["replay_window_started"].set()
    if not state["replay_window_release"].wait(3.0):
        raise RuntimeError("blocking replay window was not released")
    return {
        "high_watermark": 0,
        "retention_floor": 0,
        "snapshot_at": state["snapshot_at"],
    }


def _drain(queue):
    records = []
    while not queue.empty():
        records.append(queue.get_nowait())
        queue.task_done()
    return records


async def _rbac_scenario():
    stream = create_event_stream(queue_maxsize=8)
    restricted = _principal(
        "user-node",
        ("node.read",),
        resource_scopes=[{
            "site_id": SITE_ID,
            "resource_type": "node",
            "resource_pattern": "allowed-*",
        }],
    )
    registered = stream["register"](
        site_id=SITE_ID,
        principal=restricted,
        filters=[{"resource_type": "node", "resource_pattern": "*"}],
    )
    assert registered["ok"]
    client = registered["client"]

    stream["publish"](
        _envelope("node.state"), resource_type="node",
        resource_id="allowed-01", event_sequence=1)
    stream["publish"](
        _envelope("node.state"), resource_type="node",
        resource_id="denied-01", event_sequence=2)
    stream["publish"](
        _envelope("unclassified.event"), resource_type="node",
        resource_id="allowed-01", event_sequence=3)
    stream["publish"](
        _envelope("node.state", site_id="site_other"), resource_type="node",
        resource_id="allowed-01", event_sequence=4)
    await asyncio.sleep(0)
    delivered = _drain(client["queue"])
    assert [item["event_sequence"] for item in delivered] == [1], delivered

    own_ack_stream = create_event_stream(queue_maxsize=8)
    own_principal = _principal("user-command", ())
    own_client = own_ack_stream["register"](
        site_id=SITE_ID, principal=own_principal, filters=[])["client"]
    own_ack_stream["publish"](
        _envelope("command.ack", subject="user-command"),
        resource_type="operation", resource_id="operation-own",
        event_sequence=5)
    own_ack_stream["publish"](
        _envelope("command.ack", subject="user-other"),
        resource_type="operation", resource_id="operation-other",
        event_sequence=6)
    await asyncio.sleep(0)
    assert [item["event_sequence"] for item in _drain(
        own_client["queue"])] == [5]

    auditor = _principal("user-auditor", ("audit.read",))
    audit_client = own_ack_stream["register"](
        site_id=SITE_ID, principal=auditor, filters=[])["client"]
    own_ack_stream["publish"](
        _envelope("command.ack", subject="user-other"),
        resource_type="operation", resource_id="operation-other",
        event_sequence=7)
    await asyncio.sleep(0)
    assert [item["event_sequence"] for item in _drain(
        audit_client["queue"])] == [7]

    operation_stream = create_event_stream(queue_maxsize=8)
    operation_principal = _principal("user-operation", ("operation.read",))
    operation_client = operation_stream["register"](
        site_id=SITE_ID, principal=operation_principal, filters=[])["client"]
    operation_stream["publish"](
        _envelope(
            "operation.result", subject="user-operation",
            payload={"operation_id": "operation-own"}),
        resource_type="operation", resource_id="operation-own",
        event_sequence=8)
    operation_stream["publish"](
        _envelope(
            "operation.result", subject="user-other",
            payload={"operation_id": "operation-other"}),
        resource_type="operation", resource_id="operation-other",
        event_sequence=9)
    await asyncio.sleep(0)
    assert [item["event_sequence"] for item in _drain(
        operation_client["queue"])] == [8]

    operation_auditor = _principal(
        "operation-auditor", ("operation.read", "audit.read"))
    operation_audit_client = operation_stream["register"](
        site_id=SITE_ID, principal=operation_auditor, filters=[])["client"]
    operation_stream["publish"](
        _envelope(
            "operation.result", subject="user-other",
            payload={"operation_id": "operation-other"}),
        resource_type="operation", resource_id="operation-other",
        event_sequence=10)
    await asyncio.sleep(0)
    assert [item["event_sequence"] for item in _drain(
        operation_audit_client["queue"])] == [10]


async def _replay_scenario():
    state = {
        "high_watermark": 5,
        "retention_floor": 2,
        "snapshot_at": "2026-07-18T12:00:00+00:00",
        "gap": False,
        "page_requests": [],
        "operation_owners": {
            "operation-02": "user-replay",
            "operation-04": "user-replay",
        },
        "records": [
            _record(
                2, "operation.result", "operation", "operation-02",
                payload={"operation_id": "operation-02"}),
            _record(3, "node.state", "node", "node-03"),
            _record(
                4, "operation.result", "operation", "operation-04",
                payload={"operation_id": "operation-04"}),
            _record(5, "unknown.event", "event", "unknown-05"),
        ],
    }
    stream = create_event_stream(gateway_store=_store(state), queue_maxsize=8)
    principal = _principal("user-replay", ("operation.read",))
    opened = await stream["open_subscription"]({
        "site_id": SITE_ID,
        "client_id": "replay-client",
        "principal": principal,
        "filters": [],
        "after_sequence": 1,
        "live_queue_maxsize": 8,
    })
    assert opened["ok"]
    assert opened["high_watermark"] == 5
    assert opened["retention_floor"] == 2
    assert opened["generation"] == 1

    first = await stream["read_replay_page"]({
        "subscription_id": opened["subscription_id"],
        "site_id": SITE_ID,
        "principal": principal,
        "after_sequence": 1,
        "through_sequence": opened["high_watermark"],
        "limit": 2,
        "generation": opened["generation"],
    })
    assert first["ok"] and not first["gap"]
    assert first["next_sequence"] == 3
    assert [item["event_sequence"] for item in first["records"]] == [2]
    assert first["records"][0]["envelope"]["principal"]["subject"] == \
        "user-replay"
    assert first["records"][0]["envelope"]["extensions"][
        "producer_subject"] == "system:master"

    second = await stream["read_replay_page"]({
        "subscription_id": opened["subscription_id"],
        "site_id": SITE_ID,
        "principal": principal,
        "after_sequence": first["next_sequence"],
        "through_sequence": opened["high_watermark"],
        "limit": 2,
        "generation": opened["generation"],
    })
    assert second["ok"] and second["complete"]
    assert second["next_sequence"] == 5
    assert [item["event_sequence"] for item in second["records"]] == [4]
    assert [item["after_sequence"] for item in state["page_requests"]] == [1, 3]
    assert all(item["snapshot_at"] == state["snapshot_at"]
               for item in state["page_requests"])

    state["gap"] = True
    gap = await stream["read_replay_page"]({
        "subscription_id": opened["subscription_id"],
        "site_id": SITE_ID,
        "principal": principal,
        "after_sequence": 2,
        "through_sequence": 5,
        "limit": 2,
        "generation": opened["generation"],
    })
    assert gap["ok"] and gap["gap"]
    assert gap["records"] == []

    stale_state = dict(state)
    stale_state.update({
        "retention_floor": 5,
        "gap": False,
        "page_requests": [],
    })
    stale_stream = create_event_stream(gateway_store=_store(stale_state))
    stale = await stale_stream["open_subscription"]({
        "site_id": SITE_ID,
        "client_id": "stale-client",
        "principal": principal,
        "filters": [],
        "after_sequence": 1,
        "live_queue_maxsize": 8,
    })
    assert not stale["ok"]
    assert stale["code"] == "replay_resync_required"
    assert stale_stream["snapshot"]()["clients"] == 0


async def _generation_scenario():
    state = {
        "high_watermark": 0,
        "retention_floor": 0,
        "snapshot_at": "2026-07-18T12:00:00+00:00",
        "gap": False,
        "page_requests": [],
        "records": [],
    }
    stream = create_event_stream(gateway_store=_store(state), queue_maxsize=8)
    principal = _principal("user-generation", ("operation.read",))
    opened = await stream["open_subscription"]({
        "site_id": SITE_ID,
        "client_id": "generation-client",
        "principal": principal,
        "filters": [{"resource_type": "operation", "resource_pattern": "*"}],
        "after_sequence": 0,
        "live_queue_maxsize": 8,
    })
    stream["publish"](
        _envelope("operation.result", subject="user-generation"),
        resource_type="operation",
        resource_id="operation-old", event_sequence=1)
    await asyncio.sleep(0)

    updated = await stream["update_subscription"]({
        "subscription_id": opened["subscription_id"],
        "site_id": SITE_ID,
        "principal": principal,
        "filters": [{"resource_type": "operation", "resource_pattern": "new-*"}],
        "generation": opened["generation"],
        "reason": "client_update",
    })
    assert updated["ok"] and updated["generation"] == 2
    conflict = await stream["update_subscription"]({
        "subscription_id": opened["subscription_id"],
        "site_id": SITE_ID,
        "principal": principal,
        "filters": [],
        "generation": 1,
        "reason": "client_update",
    })
    assert conflict["code"] == "replay_resync_required"

    old_replay = await stream["read_replay_page"]({
        "subscription_id": opened["subscription_id"],
        "site_id": SITE_ID,
        "principal": principal,
        "after_sequence": 0,
        "through_sequence": 0,
        "limit": 1,
        "generation": 1,
    })
    assert old_replay["code"] == "replay_resync_required"

    stream["publish"](
        _envelope("operation.result", subject="user-generation"),
        resource_type="operation",
        resource_id="new-operation", event_sequence=2)
    await asyncio.sleep(0)
    records = _drain(opened["live_queue"])
    assert [item["generation"] for item in records] == [1, 2]
    stale_consumed = await stream["event_consumed"]({
        "subscription_id": opened["subscription_id"],
        "event_sequence": 1,
        "generation": 1,
    })
    assert stale_consumed["ok"] and stale_consumed["stale"]


async def _cancelled_open_cleanup_scenario():
    state = {
        "high_watermark": 0,
        "retention_floor": 0,
        "snapshot_at": "2026-07-18T12:00:00+00:00",
        "gap": False,
        "page_requests": [],
        "records": [],
        "replay_window_started": threading.Event(),
        "replay_window_release": threading.Event(),
    }
    store = _store(state)
    store["replay_window"] = partial(_blocking_replay_window, state)
    stream = create_event_stream(gateway_store=store, queue_maxsize=8)
    principal = _principal("user-cancelled-open", ("operation.read",))
    task = asyncio.create_task(stream["open_subscription"]({
        "site_id": SITE_ID,
        "client_id": "cancelled-open-client",
        "principal": principal,
        "filters": [],
        "after_sequence": 0,
        "live_queue_maxsize": 8,
    }))
    try:
        started = await asyncio.to_thread(
            state["replay_window_started"].wait, 2.0)
        assert started is True
        assert stream["snapshot"]()["clients"] == 1
        task.cancel()
        cancelled = False
        try:
            await task
        except asyncio.CancelledError:
            cancelled = True
        assert cancelled is True
        assert stream["snapshot"]()["clients"] == 0
    finally:
        state["replay_window_release"].set()
        if not task.done():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)


async def _bounded_overflow_scenario():
    principal = _principal(
        "user-overflow", ("node.read", "operation.read", "audit.read"))
    coalescing_stream = create_event_stream(queue_maxsize=8, coalesced_maxsize=8)
    coalescing = coalescing_stream["register"](
        site_id=SITE_ID, principal=principal, filters=[])["client"]

    for index in range(8):
        coalescing_stream["publish"](
            _envelope("sensor.value"), resource_type="sensor",
            resource_id="queue-{:02d}".format(index),
            event_sequence=index + 1)
    for index in range(20):
        coalescing_stream["publish"](
            _envelope("sensor.value"), resource_type="sensor",
            resource_id="coalesced-one",
            event_sequence=index + 100)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert coalescing["queue"].qsize() == 8
    assert len(coalescing["coalesced"]) == 1
    assert next(iter(coalescing["coalesced"].values()))["event_sequence"] == 119
    assert coalescing["statistics"]["telemetry_dropped"] == 0
    assert coalescing["disconnect"] is False

    overflow_stream = create_event_stream(queue_maxsize=8, coalesced_maxsize=8)
    overflow = overflow_stream["register"](
        site_id=SITE_ID, principal=principal, filters=[])["client"]
    for index in range(8):
        overflow_stream["publish"](
            _envelope("sensor.value"), resource_type="sensor",
            resource_id="overflow-queue-{:02d}".format(index),
            event_sequence=index + 200)
    for index in range(9):
        overflow_stream["publish"](
            _envelope("sensor.value"), resource_type="sensor",
            resource_id="overflow-coalesced-{:02d}".format(index),
            event_sequence=index + 300)
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert overflow["statistics"]["telemetry_dropped"] == 0
    assert overflow["statistics"]["telemetry_overflow_disconnect"] == 1
    assert overflow["disconnect"] is True
    assert overflow["disconnect_event"].is_set()

    critical_stream = create_event_stream(queue_maxsize=8, coalesced_maxsize=8)
    critical = critical_stream["register"](
        site_id=SITE_ID, principal=principal, filters=[])["client"]
    for index in range(8):
        critical_stream["publish"](
            _envelope("sensor.value"), resource_type="sensor",
            resource_id="critical-queue-{:02d}".format(index),
            event_sequence=index + 400)
    critical_stream["publish"](
        _envelope("operation.result"), resource_type="operation",
        resource_id="critical-operation", event_sequence=999)
    await asyncio.sleep(0)
    await asyncio.sleep(0)

    assert critical["statistics"]["critical_overflow"] == 1
    assert critical["disconnect"] is True
    assert critical["disconnect_event"].is_set()
    overflow_snapshot = overflow_stream["snapshot"]()
    critical_snapshot = critical_stream["snapshot"]()
    assert overflow_snapshot["telemetry_overflow_disconnects"] == 1
    assert critical_snapshot["critical_disconnects"] == 1
    assert overflow_snapshot["telemetry_dropped"] == 0
    assert overflow_stream["unregister"](overflow["client_id"])
    assert critical_stream["unregister"](critical["client_id"])
    assert overflow_stream["snapshot"]()["telemetry_overflow_disconnects"] == 1
    assert critical_stream["snapshot"]()["critical_disconnects"] == 1


def _test_filter_contract_is_logical_and_bounded():
    assert validate_filters([])["ok"]
    invalid_values = (
        [{"resource_type": "operation/#", "resource_pattern": "*"}],
        [{"resource_type": "operation", "resource_pattern": "+"}],
        [{"resource_type": "operation", "resource_pattern": "*", "extra": True}],
        [{"resource_type": "operation"}],
        "operation:*",
    )
    for value in invalid_values:
        assert validate_filters(value)["code"] == "invalid_payload"


def _test_async_event_contracts():
    asyncio.run(_rbac_scenario())
    asyncio.run(_replay_scenario())
    asyncio.run(_generation_scenario())
    asyncio.run(_cancelled_open_cleanup_scenario())
    asyncio.run(_bounded_overflow_scenario())


def _test_config_proposal_result_is_critical():
    assert "config.proposal.result" in CRITICAL_EVENT_TYPES


def main():
    tests = (
        _test_filter_contract_is_logical_and_bounded,
        _test_async_event_contracts,
        _test_config_proposal_result_is_critical,
    )
    for test in tests:
        test()
    print("PASS test_event_stream (RBAC, replay, generation, overflow)")


if __name__ == "__main__":
    main()

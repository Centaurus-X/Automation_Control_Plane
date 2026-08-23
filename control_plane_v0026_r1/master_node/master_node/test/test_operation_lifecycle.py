# -*- coding: utf-8 -*-

"""Durable actuator publish, ACK correlation and terminal-event tests."""

import copy
import time

from src.library._generic_system_contract_library.protocol_contracts import make_envelope
from src.core.coordinator_service import (
    _control_actuator,
    handle_control_command,
    handle_legacy_ack,
    handle_legacy_actuator_route,
    handle_legacy_actuator_result,
    publish_due_outbox,
)
from src.interfaces.legacy_worker_mqtt_interface import _command_ack_cacheable


MESSAGE_ID = "actuator-execute:1:operation-01"


def _active_authority():
    return {
        "lease_active": True,
        "leader_instance_id": "master-01",
        "lease_token": "lease-01",
        "authority_term": 1,
        "config_revision": 1,
    }


def _command(expires_at_s=None):
    return {
        "target_node": "fn-01",
        "command": "distributed.actuator.execute",
        "message_id": MESSAGE_ID,
        "expires_at_s": expires_at_s or time.time() + 30.0,
        "payload": {
            "operation_id": "operation-01",
            "coordinator_term": 1,
            "authority_revision": 1,
            "task": {"controller_uuid": "controller-01", "actuator_id": 1},
        },
    }


def _ack(ok, **kwargs):
    result = {"ok": ok}
    result.update(kwargs)
    return {
        "protocol": "automation.mqtt.v1",
        "kind": "ack",
        "message_id": MESSAGE_ID,
        "site_id": "site_main",
        "source_node": "fn-01",
        "target_node": "master-01",
        "created_at_s": time.time(),
        "command": "distributed.actuator.execute",
        "result": result,
    }


def _ack_context(transitions, marks, events):
    record = {
        "message_id": MESSAGE_ID,
        "site_id": "site_main",
        "target_service": "worker",
        "target_instance_id": "fn-01",
        "event_type": "distributed.actuator.execute",
        "envelope": _command(),
    }

    def transition(operation_id, state, **kwargs):
        transitions.append((operation_id, state, copy.deepcopy(kwargs)))
        return {"ok": True, "duplicate": False, "operation": {"state": state}}

    def mark(message_id, **kwargs):
        marks.append((message_id, copy.deepcopy(kwargs)))
        return {"message_id": message_id}

    def publish(event_type, payload, **_kwargs):
        events.append((event_type, copy.deepcopy(payload)))
        return True

    def authority_reader():
        return _active_authority()

    def get_outbox(_identity):
        return record

    return {
        "site_id": "site_main",
        "instance_id": "master-01",
        "legacy_coordinator_node_id": "master-01",
        "authority_reader": authority_reader,
        "authority_store": {"get_outbox": get_outbox, "mark_outbox": mark},
        "operation_store": {"transition": transition},
        "rollouts": {},
        "event_publisher": publish,
    }


def _test_application_ack_transitions():
    transitions, marks, events = [], [], []
    context = _ack_context(transitions, marks, events)
    assert handle_legacy_ack(context, _ack(True, state="DISPATCHED"))
    assert marks[-1][1]["state"] == "ACKED"
    assert transitions[-1][1] == "DISPATCHED"

    transitions, marks, events = [], [], []
    context = _ack_context(transitions, marks, events)
    assert handle_legacy_ack(context, _ack(
        False, code="config_not_synchronised", error="revision differs"))
    assert marks[-1][1]["state"] == "FAILED"
    assert transitions[-1][1] == "FAILED"
    assert transitions[-1][2]["result"]["code"] == "config_not_synchronised"
    assert events[-1][0] == "operation.result"
    assert events[-1][1]["terminal_state"] == "FAILED"


def _test_transport_publish_opens_dispatch_window_with_original_expiry():
    expiry = time.time() + 30.0
    record = {
        "message_id": MESSAGE_ID,
        "target_service": "worker",
        "target_instance_id": "fn-01",
        "event_type": "distributed.actuator.execute",
        "envelope": _command(expiry),
    }
    captured = {"marks": [], "transitions": [], "publish": None}

    def publish(target, command, **kwargs):
        captured["publish"] = (target, command, copy.deepcopy(kwargs))
        return {"ok": True}

    def authority_reader():
        return _active_authority()

    def due_outbox(**_kwargs):
        return [copy.deepcopy(record)]

    def mark_outbox(message_id, **kwargs):
        captured["marks"].append((message_id, copy.deepcopy(kwargs)))
        return {"message_id": message_id}

    def transition(operation_id, state, **kwargs):
        captured["transitions"].append(
            (operation_id, state, copy.deepcopy(kwargs)))
        return {"ok": True, "operation": {"operation_id": operation_id, "state": state}}

    def stage_due(**_kwargs):
        return 0

    context = {
        "site_id": "site_main",
        "instance_id": "master-01",
        "legacy_coordinator_node_id": "master-01",
        "command_ttl_s": 60.0,
        "retry_s": 2.0,
        "authority_reader": authority_reader,
        "authority_store": {
            "due_outbox": due_outbox,
            "mark_outbox": mark_outbox,
        },
        "operation_store": {"transition": transition},
        "rollouts": {"stage_due": stage_due},
        "legacy_mqtt": {"publish_command": publish},
    }
    assert publish_due_outbox(context, limit=64) == 1
    assert captured["publish"][2]["expires_at_s"] == expiry
    assert captured["marks"][-1][1]["state"] == "SENT"
    assert captured["transitions"][-1][1] == "DISPATCHED"


def _test_terminal_result_closes_execute_outbox():
    marks, events = [], []
    operation = {
        "operation_id": "operation-01",
        "owner_node_id": "fn-01",
        "authority_term": 1,
        "state": "CONFIRMED",
        "result": {
            "success": True,
            "readback_checked": True,
            "readback_verified": True,
        },
    }

    def publish(event_type, payload, **_kwargs):
        events.append((event_type, copy.deepcopy(payload)))
        return True

    def apply_worker_result(_message):
        return {"ok": True, "duplicate": False, "operation": copy.deepcopy(operation)}

    def mark_outbox(message_id, **kwargs):
        marks.append((message_id, copy.deepcopy(kwargs)))
        return {"message_id": message_id}

    context = {
        "site_id": "site_main",
        "instance_id": "master-01",
        "authority_reader": _active_authority,
        "operations": {"apply_worker_result": apply_worker_result},
        "authority_store": {"mark_outbox": mark_outbox},
        "event_publisher": publish,
    }
    message = {
        "source_node": "fn-01",
        "payload": {
            "operation_id": "operation-01",
            "coordinator_term": 1,
            "authority_revision": 1,
            "terminal_state": "CONFIRMED",
            "result": copy.deepcopy(operation["result"]),
        },
    }
    assert handle_legacy_actuator_result(context, message)["ok"]
    assert marks[-1][0] == MESSAGE_ID
    assert marks[-1][1]["state"] == "ACKED"
    assert marks[-1][1]["site_id"] == "site_main"
    assert marks[-1][1]["lease_token"] == "lease-01"
    assert events[-1][0] == "operation.result"
    assert events[-1][1]["terminal_state"] == "CONFIRMED"
    assert events[-1][1]["result"] == operation["result"]


def _test_duplicate_terminal_result_uses_persisted_identity_without_second_event():
    marks, events = [], []
    operation = {
        "operation_id": "operation-01",
        "owner_node_id": "fn-01",
        "authority_term": 1,
        "state": "CONFIRMED",
        "result": {"success": True, "readback_checked": True, "readback_verified": True},
    }

    def apply_worker_result(_message):
        return {"ok": True, "duplicate": True, "operation": copy.deepcopy(operation)}

    def mark_outbox(message_id, **kwargs):
        marks.append((message_id, copy.deepcopy(kwargs)))
        return {"message_id": message_id}

    def publish(event_type, payload, **_kwargs):
        events.append((event_type, copy.deepcopy(payload)))
        return True

    context = {
        "site_id": "site_main",
        "instance_id": "master-01",
        "authority_reader": _active_authority,
        "operations": {"apply_worker_result": apply_worker_result},
        "authority_store": {"mark_outbox": mark_outbox},
        "event_publisher": publish,
    }
    message = {
        "source_node": "untrusted-input",
        "payload": {
            "operation_id": "different-input-id",
            "coordinator_term": 999,
            "authority_revision": 999,
            "terminal_state": "FAILED",
            "result": {"success": False},
        },
    }
    result = handle_legacy_actuator_result(context, message)
    assert result["ok"] and result["duplicate"]
    assert len(marks) == 1 and marks[0][0] == MESSAGE_ID
    assert marks[0][1]["state"] == "ACKED"
    assert events == []


def _test_rejected_terminal_result_has_no_side_effects():
    marks, events = [], []

    def apply_worker_result(_message):
        return {"ok": False, "code": "conflict", "message": "terminal result differs"}

    def mark_outbox(message_id, **kwargs):
        marks.append((message_id, copy.deepcopy(kwargs)))

    def publish(event_type, payload, **_kwargs):
        events.append((event_type, copy.deepcopy(payload)))
        return True

    context = {
        "operations": {"apply_worker_result": apply_worker_result},
        "authority_store": {"mark_outbox": mark_outbox},
        "event_publisher": publish,
    }
    result = handle_legacy_actuator_result(context, {
        "source_node": "fn-01", "payload": {"operation_id": "operation-01"},
    })
    assert not result["ok"] and result["code"] == "conflict"
    assert marks == []
    assert events == []


def _test_ack_after_authority_loss_has_no_side_effects():
    transitions, marks, events = [], [], []
    context = _ack_context(transitions, marks, events)

    def expired_authority():
        return dict(_active_authority(), lease_active=False)

    context["authority_reader"] = expired_authority
    assert not handle_legacy_ack(context, _ack(True, state="DISPATCHED"))
    assert transitions == []
    assert marks == []
    assert events == []


def _test_terminal_result_after_authority_loss_does_not_ack_outbox():
    marks, events = [], []

    def apply_worker_result(_message):
        return {"ok": True, "operation": {
            "operation_id": "operation-01",
            "owner_node_id": "fn-01",
            "authority_term": 1,
            "state": "CONFIRMED",
            "result": {"success": True},
        }}

    def expired_authority():
        return dict(_active_authority(), lease_active=False)

    def mark_outbox(message_id, **kwargs):
        marks.append((message_id, copy.deepcopy(kwargs)))

    def publish(event_type, payload, **_kwargs):
        events.append((event_type, copy.deepcopy(payload)))

    context = {
        "site_id": "site_main",
        "instance_id": "master-01",
        "authority_reader": expired_authority,
        "operations": {"apply_worker_result": apply_worker_result},
        "authority_store": {"mark_outbox": mark_outbox},
        "event_publisher": publish,
    }
    result = handle_legacy_actuator_result(context, {
        "source_node": "fn-01", "payload": {"operation_id": "operation-01"},
    })
    assert not result["ok"] and result["code"] == "not_config_authority"
    assert marks == []
    assert events == []


def _actuator_envelope():
    return make_envelope(
        "actuator.request",
        {"service": "proxy", "instance_id": "proxy-01"},
        {"service": "master", "instance_id": "master-01"},
        site_id="site_main",
        message_id="00000000-0000-0000-0000-000000000071",
        idempotency_key="actuator-retry-01",
        principal={
            "subject": "00000000-0000-0000-0000-000000000072",
            "permissions": ["actuator.request"],
            "site_scopes": ["site_main"],
        },
        payload={
            "controller_uuid": "00000000-0000-0000-0000-000000000073",
            "actuator_id": "relay-01",
            "value": True,
        },
    )


def _test_transient_enqueue_is_reconstructed_by_identical_control_retry():
    envelope = _actuator_envelope()
    command = _command()
    inbox = {}
    outbox = {}
    completed = []
    enqueue_attempts = []
    operation_requests = []

    def register_inbox(record):
        if not inbox:
            inbox.update(copy.deepcopy(record))
            inbox["state"] = "RECEIVED"
            return {"ok": True, "duplicate": False}
        if inbox["state"] == "RECEIVED":
            return {
                "ok": True, "duplicate": False, "recovered": True,
                "record": copy.deepcopy(inbox),
            }
        return {"ok": True, "duplicate": True, "record": copy.deepcopy(inbox)}

    def complete_inbox(_record, result):
        inbox["state"] = "COMPLETED" if result.get("ok") else "REJECTED"
        inbox["result"] = copy.deepcopy(result)
        completed.append(copy.deepcopy(result))
        return result

    def request_operation(_envelope):
        duplicate = bool(operation_requests)
        operation_requests.append(True)
        return {
            "ok": True,
            "duplicate": duplicate,
            "operation": {
                "operation_id": "operation-01",
                "owner_node_id": "fn-01",
                "state": "ROUTED",
            },
            "worker_command": copy.deepcopy(command),
        }

    def enqueue_outbox(record, **kwargs):
        enqueue_attempts.append((copy.deepcopy(record), copy.deepcopy(kwargs)))
        if len(enqueue_attempts) == 1:
            return {"ok": False, "code": "database_unavailable"}
        previous = outbox.get(record["message_id"])
        if previous is not None and previous != record:
            return {"ok": False, "code": "conflict"}
        outbox[record["message_id"]] = copy.deepcopy(record)
        return {"ok": True, "duplicate": previous is not None}

    def append_audit(_event):
        return "audit-01"

    context = {
        "site_id": "site_main",
        "instance_id": "master-01",
        "authority_reader": _active_authority,
        "authority_store": {
            "register_inbox": register_inbox,
            "complete_inbox": complete_inbox,
            "enqueue_outbox": enqueue_outbox,
        },
        "operations": {"request": request_operation},
        "audit": {"append": append_audit},
        "event_publisher": None,
    }

    first = handle_control_command(context, copy.deepcopy(envelope))
    assert not first["ok"] and first["retryable"]
    assert first["operation_id"] == "operation-01"
    assert inbox["state"] == "RECEIVED" and completed == []

    second = handle_control_command(context, copy.deepcopy(envelope))
    assert second["ok"] and second["duplicate"]
    assert inbox["state"] == "COMPLETED" and len(completed) == 1
    assert len(enqueue_attempts) == 2 and set(outbox) == {MESSAGE_ID}
    for _record, fence in enqueue_attempts:
        assert fence == {
            "site_id": "site_main",
            "instance_id": "master-01",
            "lease_token": "lease-01",
            "authority_term": 1,
        }

    third = handle_control_command(context, copy.deepcopy(envelope))
    assert third == second
    assert len(operation_requests) == 2
    assert len(enqueue_attempts) == 2 and len(outbox) == 1


def _test_authority_loss_between_route_and_enqueue_fails_closed():
    authorities = [
        _active_authority(),
        dict(_active_authority(), lease_active=False),
    ]
    enqueued = []

    def authority_reader():
        assert authorities
        return copy.deepcopy(authorities.pop(0))

    def request_operation(_envelope):
        assert authority_reader()["lease_active"]
        return {
            "ok": True,
            "duplicate": False,
            "operation": {
                "operation_id": "operation-race",
                "owner_node_id": "fn-01",
                "state": "ROUTED",
            },
            "worker_command": _command(),
        }

    def enqueue_outbox(record, **kwargs):
        enqueued.append((record, kwargs))
        return {"ok": True}

    context = {
        "site_id": "site_main",
        "instance_id": "master-01",
        "authority_reader": authority_reader,
        "authority_store": {"enqueue_outbox": enqueue_outbox},
        "operations": {"request": request_operation},
    }
    result = _control_actuator(context, _actuator_envelope())
    assert not result["ok"] and result["code"] == "not_config_authority"
    assert result["retryable"] and result["operation_id"] == "operation-race"
    assert enqueued == []


def _test_legacy_retry_reconstructs_the_same_outbox_command():
    attempts = []
    outbox = {}
    command = _command()

    def request_operation(_envelope):
        duplicate = bool(attempts)
        return {
            "ok": True,
            "duplicate": duplicate,
            "operation": {
                "operation_id": "operation-legacy",
                "owner_node_id": "fn-01",
                "state": "ROUTED",
            },
            "worker_command": copy.deepcopy(command),
        }

    def enqueue_outbox(record, **_kwargs):
        attempts.append(copy.deepcopy(record))
        if len(attempts) == 1:
            return {"ok": False, "code": "database_unavailable"}
        outbox.setdefault(record["message_id"], copy.deepcopy(record))
        return {"ok": True, "duplicate": len(attempts) > 2}

    context = {
        "site_id": "site_main",
        "instance_id": "master-01",
        "authority_reader": _active_authority,
        "authority_store": {"enqueue_outbox": enqueue_outbox},
        "operations": {"request": request_operation},
    }
    message = {
        "source_node": "fn-01",
        "payload": {
            "operation_id": "operation-legacy",
            "task": {
                "controller_uuid": "00000000-0000-0000-0000-000000000073",
                "actuator_id": "relay-01",
                "control_value": True,
            },
        },
    }

    first = handle_legacy_actuator_route(context, copy.deepcopy(message))
    assert not first["ok"] and first["retryable"]
    assert not _command_ack_cacheable(
        {"command": "distributed.actuator.route"}, first)
    second = handle_legacy_actuator_route(context, copy.deepcopy(message))
    assert second["ok"] and second["duplicate"]
    assert len(attempts) == 2 and attempts[0] == attempts[1]
    assert set(outbox) == {MESSAGE_ID}
    assert _command_ack_cacheable(
        {"command": "distributed.actuator.route"},
        {"ok": False, "code": "conflict"},
    )


def _test_node_local_config_proposal_ack_is_published():
    identities = (
        {
            "proposal_id": "00000000-0000-4000-8000-000000000101",
            "operation_id": "00000000-0000-4000-8000-000000000201",
        },
        {
            "proposal_id": "00000000-0000-4000-8000-000000000102",
            "operation_id": "00000000-0000-4000-8000-000000000202",
        },
    )
    cases = (
        {
            "result": {
                "ok": True,
                "code": "accepted",
                "proposal_id": identities[0]["proposal_id"],
                "operation_id": identities[0]["operation_id"],
                "authority_epoch": 3,
                "scope_revision": 8,
                "fingerprint": "a" * 64,
            },
            "state": "ACKED",
            "accepted": True,
        },
        {
            "result": {
                "ok": False,
                "code": "stale_revision",
                "scope_revision": 9,
            },
            "state": "FAILED",
            "accepted": False,
        },
    )
    for index, case in enumerate(cases):
        proposal_id = identities[index]["proposal_id"]
        operation_id = identities[index]["operation_id"]
        message_id = "relay:" + proposal_id
        payload = {
            "scope": "node:fn-01",
            "proposal_id": proposal_id,
            "operation_id": operation_id,
        }
        record = {
            "message_id": message_id,
            "site_id": "site_main",
            "target_service": "worker",
            "target_instance_id": "fn-01",
            "event_type": "distributed.config.propose",
            "envelope": {
                "target_node": "fn-01",
                "command": "distributed.config.propose",
                "message_id": message_id,
                "payload": copy.deepcopy(payload),
            },
        }
        marks = []
        events = []

        def authority_reader():
            return _active_authority()

        def get_outbox(identity):
            assert identity == message_id
            return copy.deepcopy(record)

        def mark_outbox(identity, **kwargs):
            marks.append((identity, copy.deepcopy(kwargs)))
            return {"message_id": identity, "state": kwargs.get("state")}

        def publish(event_type, event_payload, **kwargs):
            transport_envelope = make_envelope(
                event_type,
                {"service": "master", "instance_id": "master-01"},
                {"service": "proxy", "instance_id": "broadcast"},
                site_id="site_main",
                correlation_id=kwargs.get("correlation_id"),
                causation_id=kwargs.get("causation_id"),
                principal=copy.deepcopy(kwargs.get("principal")),
                authority_term=1,
                config_revision=1,
                payload=copy.deepcopy(event_payload),
            )
            events.append((
                event_type,
                copy.deepcopy(event_payload),
                copy.deepcopy(kwargs),
                transport_envelope,
            ))
            return True

        context = {
            "site_id": "site_main",
            "instance_id": "master-01",
            "legacy_coordinator_node_id": "master-01",
            "authority_mode": "node_local",
            "authority_reader": authority_reader,
            "authority_store": {
                "get_outbox": get_outbox,
                "mark_outbox": mark_outbox,
            },
            "rollouts": {},
            "event_publisher": publish,
        }
        ack = {
            "protocol": "automation.mqtt.v1",
            "kind": "ack",
            "message_id": message_id,
            "site_id": "site_main",
            "source_node": "fn-01",
            "target_node": "master-01",
            "created_at_s": time.time(),
            "command": "distributed.config.propose",
            "result": copy.deepcopy(case["result"]),
        }
        assert handle_legacy_ack(context, ack)
        assert marks == [(message_id, {
            "state": case["state"],
            "error_code": case["result"].get("code"),
            "site_id": "site_main",
            "instance_id": "master-01",
            "lease_token": "lease-01",
            "authority_term": 1,
        })]
        assert len(events) == 1 and events[0][0] == "config.proposal.result"
        event_payload = events[0][1]
        assert event_payload["node_id"] == "fn-01"
        assert event_payload["scope"] == "node:fn-01"
        assert event_payload["relay_message_id"] == message_id
        assert event_payload["proposal_id"] == proposal_id
        assert event_payload["operation_id"] == operation_id
        assert event_payload["accepted"] is case["accepted"]
        assert event_payload["code"] == case["result"]["code"]
        assert event_payload["result"] == case["result"]
        assert events[0][2]["correlation_id"] == proposal_id
        assert events[0][2]["causation_id"] == proposal_id
        assert events[0][2]["principal"] == {"subject": "worker:fn-01"}
        assert events[0][3]["causation_id"] == proposal_id
        assert events[0][3]["payload"]["relay_message_id"] == message_id

def main():
    _test_application_ack_transitions()
    _test_transport_publish_opens_dispatch_window_with_original_expiry()
    _test_terminal_result_closes_execute_outbox()
    _test_duplicate_terminal_result_uses_persisted_identity_without_second_event()
    _test_rejected_terminal_result_has_no_side_effects()
    _test_ack_after_authority_loss_has_no_side_effects()
    _test_terminal_result_after_authority_loss_does_not_ack_outbox()
    _test_transient_enqueue_is_reconstructed_by_identical_control_retry()
    _test_authority_loss_between_route_and_enqueue_fails_closed()
    _test_legacy_retry_reconstructs_the_same_outbox_command()
    _test_node_local_config_proposal_ack_is_published()
    print("PASS Outbox, ACK correlation and terminal lifecycle (11 checks)")


if __name__ == "__main__":
    main()

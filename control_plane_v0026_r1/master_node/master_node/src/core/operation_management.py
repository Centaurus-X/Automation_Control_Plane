# -*- coding: utf-8 -*-

# master_node/src/core/operation_management.py

"""Remote actuator routing with ownership, fencing and readback semantics."""

import copy
import datetime

from functools import partial

from src.library._generic_system_contract_library.protocol_contracts import parse_timestamp
from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library._generic_system_contract_library.protocol_contracts import LEGACY_DISTRIBUTED_PROTOCOL


def _resolve_actuator(config, controller_uuid, actuator_id):
    wanted_controller = str(controller_uuid)
    wanted_actuator = str(actuator_id)
    for entry in config.get("actuators") or []:
        if not isinstance(entry, dict):
            continue
        if not entry.get("enabled", True):
            continue
        if str(entry.get("controller_uuid") or "") != wanted_controller:
            continue
        rows = entry.get("rows") if isinstance(entry.get("rows"), list) \
            else [entry]
        for row in rows:
            if (
                    isinstance(row, dict) and row.get("enabled", True)
                    and str(row.get("actuator_id")) == wanted_actuator):
                return copy.deepcopy(row)
    return None


def request_actuator(context, envelope):
    """Create one durable operation and return its fenced worker command."""
    authority = context["authority_reader"]()
    owned = (
        authority
        and authority.get("lease_active")
        and authority.get("leader_instance_id") == context["instance_id"]
        and authority.get("lease_token")
    )
    if not owned:
        return make_error("not_config_authority", "actuator routing requires active authority")
    payload = envelope["payload"]
    scope_check = context["scope_validator"](
        envelope["source"]["instance_id"], payload)
    if not scope_check.get("ok"):
        return scope_check
    owner = context["ownership"]["resolve"](payload["controller_uuid"])
    if not owner:
        return make_error("not_found", "controller has no active owner lease")
    replica_ready = _validate_owner_replica(context, owner)
    if not replica_ready.get("ok"):
        return replica_ready
    target_scope = context["scope_reader"](
        "node:" + str(owner["owner_node_id"])) \
        if callable(context.get("scope_reader")) else None
    mirrored_snapshot = target_scope.get("snapshot") \
        if isinstance(target_scope, dict) else None
    config = mirrored_snapshot.get("config") \
        if isinstance(mirrored_snapshot, dict) else None
    if not isinstance(config, dict) and isinstance(target_scope, dict):
        config = target_scope.get("config")
    if not isinstance(config, dict):
        snapshot = context["config_reader"]()
        config = snapshot.get("config_data") if snapshot else {}
    actuator = _resolve_actuator(
        config, payload["controller_uuid"], payload["actuator_id"])
    if not actuator:
        return make_error("not_found", "actuator is not present in current config")
    safety = _validate_requested_value(actuator, payload.get("value"))
    if not safety.get("ok"):
        return safety
    expires_at = parse_timestamp(envelope["expires_at"])
    if expires_at <= datetime.datetime.now(datetime.timezone.utc):
        return make_error("expired", "actuator request has expired")
    idempotency_key = str(envelope.get("idempotency_key") or "")
    if not idempotency_key:
        return make_error("invalid_payload", "idempotency key is required")
    operation_request = _operation_request(
        context, envelope, authority, owner, actuator, scope_check)
    created = context["store"]["create"](operation_request)
    if not created.get("ok"):
        return created
    operation = created["operation"]
    if operation["state"] == "RECEIVED":
        routed = context["store"]["transition"](
            operation["operation_id"], "ROUTED",
            details={"owner_node_id": owner["owner_node_id"]},
            site_id=context["site_id"], instance_id=context["instance_id"],
            leader_lease_token=authority["lease_token"],
            authority_term=authority["authority_term"],
            config_revision=authority["config_revision"],
        )
        if not routed.get("ok"):
            return routed
        operation = routed["operation"]
    command = _worker_command(context, operation, payload, actuator) \
        if operation["state"] == "ROUTED" else None
    result = make_success(operation=operation, duplicate=bool(created.get("duplicate")))
    if command:
        result["worker_command"] = command
    return result


def _validate_owner_replica(context, owner):
    """Fail closed when the owner has not reported the exact current Node shard."""
    node_reader = context.get("node_reader")
    scope_reader = context.get("scope_reader")
    if not callable(node_reader) or not callable(scope_reader):
        return make_success()
    node_id = str(owner.get("owner_node_id") or "")
    node = node_reader(node_id)
    target = scope_reader("node:" + node_id)
    active = bool(
        node
        and node.get("admission_state") == "APPROVED"
        and node.get("desired_state") == "ACTIVE"
        and node.get("observed_state") == "ACTIVE"
    )
    converged = bool(
        active
        and target
        and node.get("node_replica_scope") == "node:" + node_id
        and int(node.get("node_replica_revision") or 0)
        == int(target.get("scope_revision") or 0)
        and node.get("node_replica_fingerprint") == target.get("fingerprint")
    )
    if converged:
        return make_success()
    return make_error(
        "worker_not_synchronised",
        "actuator owner has not activated the current Node shard",
        owner_node_id=node_id,
    )


def _operation_request(context, envelope, authority, owner, actuator, scope_check):
    principal = envelope.get("principal") if isinstance(envelope.get("principal"), dict) else {}
    source = envelope["source"]
    payload = envelope["payload"]
    return {
        "site_id": context["site_id"],
        "instance_id": context["instance_id"],
        "leader_lease_token": authority["lease_token"],
        "idempotency_key": str(envelope["idempotency_key"]),
        "source_service": source["service"],
        "source_instance_id": source["instance_id"],
        "principal_subject": str(principal.get("subject") or "unknown"),
        "controller_uuid": payload["controller_uuid"],
        "actuator_id": payload["actuator_id"],
        "requested_value": payload.get("value"),
        "owner_node_id": owner["owner_node_id"],
        "authority_term": authority["authority_term"],
        "config_revision": authority["config_revision"],
        "lease_token": owner["lease_token"],
        "request_payload": copy.deepcopy(payload),
        "expires_at": parse_timestamp(envelope["expires_at"]),
        "actuator_config": copy.deepcopy(actuator),
        "execution_scope": scope_check.get("execution_scope"),
        "execution_epoch": scope_check.get("execution_epoch"),
    }


def _validate_requested_value(actuator, value):
    allowed = actuator.get("allowed_values")
    if isinstance(allowed, list) and value not in allowed:
        return make_error("invalid_payload", "actuator value is not allowlisted")
    minimum = actuator.get("minimum", actuator.get("min"))
    maximum = actuator.get("maximum", actuator.get("max"))
    if minimum is None and maximum is None:
        return make_success()
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return make_error("invalid_payload", "actuator value must be numeric")
    if minimum is not None and float(value) < float(minimum):
        return make_error("invalid_payload", "actuator value is below its minimum")
    if maximum is not None and float(value) > float(maximum):
        return make_error("invalid_payload", "actuator value exceeds its maximum")
    return make_success()


def _worker_command(context, operation, payload, actuator):
    target_scope = context["scope_reader"](
        "node:" + str(operation["owner_node_id"])) if context.get("scope_reader") else None
    protocol = payload.get("distributed_protocol") or LEGACY_DISTRIBUTED_PROTOCOL
    task = copy.deepcopy(actuator)
    task.update({
        "task_uuid": operation["operation_id"],
        "controller_uuid": str(operation["controller_uuid"]),
        "actuator_id": payload["actuator_id"],
        "control_value": copy.deepcopy(payload.get("value")),
        "config_revision": operation["config_revision"],
        "idempotent": bool(payload.get("idempotent", False)),
        "device_command_id_supported": bool(payload.get("device_command_id_supported", False)),
        "execution_scope": operation.get("execution_scope"),
        "execution_epoch": operation.get("execution_epoch"),
    })
    return {
        "target_node": operation["owner_node_id"],
        "command": "distributed.actuator.execute",
        "message_id": "actuator-execute:{}:{}".format(
            operation["authority_term"], operation["operation_id"]),
        "expires_at_s": operation["expires_at"].timestamp(),
        "payload": {
            "protocol": protocol,
            "operation_id": operation["operation_id"],
            "coordinator_node_id": context["legacy_coordinator_node_id"],
            "coordinator_term": operation["authority_term"],
            "authority_revision": operation["config_revision"],
            "require_hardware_readback": True,
            "request_source_node": operation["source_instance_id"],
            "target_node_scope_revision": target_scope.get(
                "scope_revision") if target_scope else None,
            "target_node_scope_fingerprint": target_scope.get(
                "fingerprint") if target_scope else None,
            "target_node_execution_epoch": target_scope.get(
                "execution_epoch") if target_scope else None,
            "task": task,
        },
    }


def apply_worker_result(context, message):
    """Apply a worker result only after owner/term/revision validation."""
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    request = {
        "site_id": context["site_id"],
        "instance_id": context["instance_id"],
        "leader_lease_token": (context["authority_reader"]() or {}).get("lease_token"),
        "operation_id": str(payload.get("operation_id") or ""),
        "source_node_id": str(message.get("source_node") or ""),
        "authority_term": payload.get("coordinator_term"),
        "config_revision": payload.get("authority_revision"),
        "terminal_state": payload.get("terminal_state"),
        "result": copy.deepcopy(payload.get("result") or {}),
    }
    return context["store"]["apply_worker_result"](request)


def allow_unscoped_intent(_source_node, _payload):
    """Default compatibility policy used only by isolated unit compositions."""
    return make_success(execution_scope=None, execution_epoch=None)


def create_operation_management(**kwargs):
    """Create the functional remote-operation capability dictionary."""
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "legacy_coordinator_node_id": kwargs["legacy_coordinator_node_id"],
        "authority_reader": kwargs["authority_reader"],
        "config_reader": kwargs["config_reader"],
        "ownership": kwargs["ownership"],
        "store": kwargs["store"],
        "scope_validator": kwargs.get("scope_validator") or allow_unscoped_intent,
        "scope_reader": kwargs.get("scope_reader"),
        "node_reader": kwargs.get("node_reader"),
    }
    return {
        "context": context,
        "request": partial(request_actuator, context),
        "apply_worker_result": partial(apply_worker_result, context),
        "get": kwargs["store"]["get"],
        "list": partial(kwargs["store"]["list"], context["site_id"]),
    }

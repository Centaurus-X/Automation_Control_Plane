# -*- coding: utf-8 -*-

# proxy_gateway/src/core/command_gateway.py

"""Authorized HTTP/domain operations translated into fixed Master commands."""

import copy

from functools import partial

from src.library._generic_system_contract_library import (
    validate_authority_mode,
    validate_config_proposal_payload,
)
from src.library._generic_system_contract_library.errors import make_error
from src.core.access_policy import (
    authorize,
    authorize_config_changes,
    authorize_config_operations,
)


def _send(context, event_type, payload, principal, **kwargs):
    return context["mqtt"]["send_command"](
        event_type, copy.deepcopy(payload),
        principal=copy.deepcopy(principal),
        idempotency_key=kwargs.get("idempotency_key"),
        wait_timeout_s=float(kwargs.get("wait_timeout_s", context["wait_timeout_s"])),
        ttl_s=float(kwargs.get("ttl_s", 30.0)),
    )


def _mutation_metadata(request):
    key = str(request.get("idempotency_key") or "")
    if not key:
        return None, make_error("invalid_payload", "Idempotency-Key is required")
    try:
        ttl_s = float(request.get("ttl_s", 30.0))
    except (TypeError, ValueError):
        return None, make_error("invalid_payload", "ttl_s must be numeric")
    if not 1.0 <= ttl_s <= 300.0:
        return None, make_error("invalid_payload", "ttl_s is outside its allowed range")
    return {
        "idempotency_key": key,
        "ttl_s": ttl_s,
        "request_id": request.get("request_id"),
    }, None


def list_nodes(context, principal):
    checked = authorize(principal, "node.read", site_id=context["site_id"])
    return _send(context, "nodes.list", {}, principal) if checked.get("ok") else checked


def get_config(context, principal, **kwargs):
    checked = authorize(principal, "config.read", site_id=context["site_id"])
    if not checked.get("ok"):
        return checked
    if context["authority_mode"] == "node_local":
        payload = (
            {"scope": kwargs["scope"]}
            if kwargs.get("scope") is not None
            else {}
        )
    else:
        payload = (
            {"revision": kwargs["revision"]}
            if kwargs.get("revision") is not None
            else {}
        )
    return _send(context, "config.get", payload, principal)


def config_history(context, principal, **kwargs):
    checked = authorize(principal, "config.read", site_id=context["site_id"])
    payload = {"limit": min(200, max(1, int(kwargs.get("limit", 50))))}
    return _send(context, "config.history", payload, principal) if checked.get("ok") else checked


def propose_config(context, principal, request):
    metadata, error = _mutation_metadata(request)
    if error:
        return error
    proposal = copy.deepcopy(request)
    for name in ("idempotency_key", "ttl_s", "request_id"):
        proposal.pop(name, None)
    validated = validate_config_proposal_payload(
        proposal, authority_mode=context["authority_mode"])
    if not validated.get("ok"):
        return validated
    if context["authority_mode"] == "node_local":
        checked = authorize_config_operations(
            principal, context["site_id"], proposal.get("operations"))
    else:
        checked = authorize_config_changes(
            principal, context["site_id"], proposal.get("changes"))
    if not checked.get("ok"):
        return checked
    payload = copy.deepcopy(validated["payload"])
    return _send(context, "config.propose", payload, principal, **metadata)


def rollback_config(context, principal, request):
    if context["authority_mode"] == "node_local":
        return make_error(
            "unsupported_operation_format",
            "node_local rollback requires canonical target-scope operations")
    checked = authorize(principal, "config.rollback", site_id=context["site_id"])
    if not checked.get("ok"):
        return checked
    metadata, error = _mutation_metadata(request)
    if error:
        return error
    payload = {
        "base_revision": request.get("base_revision"),
        "target_revision": request.get("target_revision"),
        "reason": request.get("reason"),
    }
    if request.get("rollout_targets") is not None:
        payload["rollout_targets"] = copy.deepcopy(request["rollout_targets"])
    return _send(context, "config.rollback", payload, principal, **metadata)


def request_actuator(context, principal, request):
    checked = authorize(
        principal, "actuator.request", site_id=context["site_id"],
        resource_type="controller", resource_id=request.get("controller_uuid"),
    )
    if not checked.get("ok"):
        return checked
    metadata, error = _mutation_metadata(request)
    if error:
        return error
    payload = {
        "controller_uuid": request.get("controller_uuid"),
        "actuator_id": request.get("actuator_id"),
        "value": copy.deepcopy(request.get("value")),
        "idempotent": bool(request.get("idempotent", False)),
        "device_command_id_supported": bool(request.get("device_command_id_supported", False)),
    }
    return _send(context, "actuator.request", payload, principal, **metadata)


def get_operation(context, principal, operation_id):
    checked = authorize(
        principal, "operation.read", site_id=context["site_id"],
        resource_type="operation", resource_id=str(operation_id),
    )
    payload = {"operation_id": str(operation_id)}
    return _send(context, "operation.get", payload, principal) if checked.get("ok") else checked


def drain_node(context, principal, request):
    checked = authorize(
        principal, "node.drain", site_id=context["site_id"],
        resource_type="node", resource_id=request.get("node_id"),
    )
    if not checked.get("ok"):
        return checked
    metadata, error = _mutation_metadata(request)
    if error:
        return error
    return _send(context, "node.drain", {
        "node_id": request.get("node_id"),
        "reason": request.get("reason"),
        "deadline_s": request.get("deadline_s"),
    }, principal, **metadata)


def set_node_state(context, principal, request):
    checked = authorize(
        principal, "node.drain", site_id=context["site_id"],
        resource_type="node", resource_id=request.get("node_id"),
    )
    if not checked.get("ok"):
        return checked
    metadata, error = _mutation_metadata(request)
    if error:
        return error
    return _send(context, "node.state.set", {
        "node_id": request.get("node_id"),
        "desired_state": request.get("desired_state"),
        "reason": request.get("reason"),
    }, principal, **metadata)


def approve_node(context, principal, request):
    checked = authorize(principal, "node.admit", site_id=context["site_id"])
    if not checked.get("ok"):
        return checked
    metadata, error = _mutation_metadata(request)
    if error:
        return error
    return _send(context, "node.admission.approve", {
        "node_id": request.get("node_id"),
        "certificate_identity": request.get("certificate_identity"),
        "reason": request.get("reason"),
    }, principal, **metadata)


def execute_terminal(context, principal, command, **kwargs):
    checked = authorize(principal, "terminal.execute", site_id=context["site_id"])
    if not checked.get("ok"):
        return checked
    metadata, error = _mutation_metadata(kwargs)
    if error:
        return error
    return _send(
        context, "terminal.execute", {"command": str(command)}, principal,
        **metadata)


def runtime_status(context, principal):
    checked = authorize(principal, "node.read", site_id=context["site_id"])
    return _send(context, "runtime.status", {}, principal) if checked.get("ok") else checked


def create_command_gateway(**kwargs):
    """Create all fixed command capabilities."""
    context = {
        "site_id": kwargs["site_id"],
        "authority_mode": validate_authority_mode(
            kwargs.get("authority_mode", "node_local")),
        "mqtt": kwargs["mqtt"],
        "wait_timeout_s": max(
            0.1, float(kwargs.get("wait_timeout_s", 5.0))),
    }
    return {
        "list_nodes": partial(list_nodes, context),
        "get_config": partial(get_config, context),
        "config_history": partial(config_history, context),
        "propose_config": partial(propose_config, context),
        "rollback_config": partial(rollback_config, context),
        "request_actuator": partial(request_actuator, context),
        "get_operation": partial(get_operation, context),
        "drain_node": partial(drain_node, context),
        "set_node_state": partial(set_node_state, context),
        "approve_node": partial(approve_node, context),
        "execute_terminal": partial(execute_terminal, context),
        "runtime_status": partial(runtime_status, context),
    }

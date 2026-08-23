# -*- coding: utf-8 -*-

"""Master relay, Worker mirror and Cross Registry coordination for node_local."""

import copy
import time
import uuid

from functools import partial

from src.library import _generic_type_library as type_authority
from src.library._generic_system_contract_library import authority_flow_contracts
from src.library._generic_system_contract_library import persistence_contracts
from src.library._generic_system_contract_library import protocol_contracts
from src.library._generic_system_contract_library.config_sharding_contracts import validate_scope_operations
from src.library._generic_system_contract_library.errors import make_error, make_success


def _admitted_node(context, node_id):
    node = context["node_reader"](str(node_id))
    if not isinstance(node, dict):
        return None
    if str(node.get("admission_state") or "").upper() != "APPROVED":
        return None
    return node


def _write_fence(context):
    authority = context["authority_reader"]() or {}
    owned = (
        authority.get("lease_active")
        and authority.get("leader_instance_id") == context["instance_id"]
        and authority.get("lease_token")
    )
    if not owned:
        return None
    return {
        "site_id": context["site_id"],
        "instance_id": context["instance_id"],
        "lease_token": authority["lease_token"],
        "authority_term": int(authority["authority_term"]),
    }


def _scope_kind(scope):
    if str(scope).startswith("node:"):
        return "node"
    if str(scope).startswith("cross:"):
        return "cross"
    return ""


def _config_meta(config):
    meta = config.get("meta") if isinstance(config, dict) else None
    if isinstance(meta, list) and meta and isinstance(meta[0], dict):
        return meta[0]
    if isinstance(meta, dict):
        return meta
    return {}


def _scope_owner(scope, config):
    kind = _scope_kind(scope)
    if kind == "node":
        return str(scope).split(":", 1)[1]
    if kind == "cross":
        meta = _config_meta(config)
        return str(meta.get("owner_node_id") or meta.get("node_id") or "")
    return ""


def _integer(value, label):
    if isinstance(value, bool):
        raise ValueError(label + " must be an integer")
    result = int(value)
    if result < 0:
        raise ValueError(label + " must not be negative")
    return result


def _mirror_identity(payload):
    scope = str(payload.get("scope") or "")
    kind = _scope_kind(scope)
    if not kind:
        raise ValueError("mirror scope is invalid")
    fingerprint = str(payload.get("fingerprint") or "")
    if not type_authority.is_fingerprint(fingerprint):
        raise ValueError("mirror fingerprint must be lowercase raw SHA-256")
    return {
        "scope": scope,
        "scope_kind": kind,
        "authority_epoch": _integer(
            payload.get("authority_epoch"), "authority_epoch"),
        "scope_revision": _integer(
            payload.get("scope_revision"), "scope_revision"),
        "execution_epoch": _integer(
            payload.get("execution_epoch"), "execution_epoch"),
        "fingerprint": fingerprint,
    }

def handle_mirror_announce(context, message):
    """Compare a Worker mirror head without treating its fingerprint as content."""
    source = str(message.get("source_node") or "")
    if not _admitted_node(context, source):
        return make_error("principal_denied", "mirror source is not an admitted Worker")
    try:
        identity = _mirror_identity(message.get("payload") or {})
    except (TypeError, ValueError) as exc:
        return make_error("invalid_payload", str(exc))
    if identity["scope_kind"] == "node" and _scope_owner(identity["scope"], {}) != source:
        return make_error("path_denied", "Worker announced another node scope")
    current = context["mirror_store"]["get_worker_mirror"](identity["scope"])
    if current:
        current_epoch = int(current.get("authority_epoch") or 0)
        current_revision = int(current.get("scope_revision") or 0)
        current_execution = int(current.get("execution_epoch") or 0)
        if identity["authority_epoch"] < current_epoch:
            return make_error(
                "authority_epoch_stale",
                "announced mirror authority epoch is stale",
                authority_epoch=current_epoch,
            )
        if (
            identity["authority_epoch"] == current_epoch
            and identity["scope_revision"] < current_revision
        ):
            return make_error(
                "stale_revision",
                "announced mirror scope revision is stale",
                scope_revision=current_revision,
            )
        if identity["execution_epoch"] < current_execution:
            return make_error(
                "execution_epoch_stale",
                "announced Mirror execution epoch is stale",
                execution_epoch=current_execution,
            )
        if identity["execution_epoch"] > current_execution + 1:
            return make_error(
                "execution_epoch_stale",
                "announced Mirror skipped a Registry execution epoch",
                execution_epoch=current_execution,
            )
        exact = (
            identity["authority_epoch"] == current_epoch
            and identity["scope_revision"] == current_revision
            and identity["execution_epoch"] == current_execution
            and identity["fingerprint"] == current.get("fingerprint")
        )
        if exact:
            return make_success(
                already_current=True,
                request_content=False,
                **identity,
            )
    return make_success(
        already_current=False,
        request_content=True,
        **identity,
    )

def _validate_mirror_snapshot(source, payload):
    identity = _mirror_identity(payload)
    snapshot = persistence_contracts.validate_scope_snapshot(payload.get("snapshot"))
    for name in (
        "scope", "authority_epoch", "scope_revision", "execution_epoch",
        "fingerprint",
    ):
        if snapshot[name] != identity[name]:
            raise ValueError("mirror envelope and snapshot " + name + " differ")
    config = snapshot["config"]
    owner = _scope_owner(identity["scope"], config)
    if owner != source:
        raise ValueError("mirror source is not the scope owner")
    if identity["scope_kind"] == "cross":
        runtime = type_authority.normalize_cross_v8_for_runtime(
            config, representation="auto")
        report = type_authority.inspect_cross_v8_contract(
            runtime, representation="runtime")
    else:
        runtime = type_authority.normalize_config_for_runtime(
            config, representation="auto")
        report = type_authority.verify_runtime_config_data(runtime)
    if report.get("status") != "OK":
        raise ValueError(type_authority.format_report(report))
    if type_authority.config_fingerprint(config) != identity["fingerprint"]:
        raise ValueError("mirror config fingerprint differs")
    return {
        "scope": identity["scope"],
        "scope_kind": identity["scope_kind"],
        "owner_node_id": owner,
        "source_node_id": source,
        "mirror_id": str(payload.get("mirror_id") or ""),
        "authority_epoch": identity["authority_epoch"],
        "scope_revision": identity["scope_revision"],
        "execution_epoch": identity["execution_epoch"],
        "fingerprint": identity["fingerprint"],
        "contracts_version": protocol_contracts.CONTRACTS_VERSION,
        "type_schema_version": type_authority.TYPE_SCHEMA_VERSION,
        "config_norm_version": copy.deepcopy(type_authority.CONFIG_NORM_VERSION),
        "snapshot": snapshot,
    }

def handle_mirror_snapshot(context, message):
    """Validate and atomically persist exact Worker mirror content before ACK."""
    source = str(message.get("source_node") or "")
    if not _admitted_node(context, source):
        return make_error("principal_denied", "mirror source is not an admitted Worker")
    try:
        record = _validate_mirror_snapshot(source, message.get("payload") or {})
    except (KeyError, TypeError, ValueError) as exc:
        message = str(exc)
        code, separator, detail = message.partition(":")
        if code not in ("invalid_payload", "unsupported_operation_format"):
            code = "invalid_payload"
            detail = message
        return make_error(code, detail if separator else message)
    fence = _write_fence(context)
    if fence is None:
        return make_error(
            "not_config_authority",
            "mirror persistence requires the active Registry leader",
            retryable=True,
        )
    stored = context["mirror_store"]["persist_worker_mirror"](record, **fence)
    if not stored.get("ok"):
        return stored
    refresh = context.get("derived_refresh")
    if callable(refresh):
        refresh()
    return make_success(
        persisted=True,
        duplicate=bool(stored.get("duplicate")),
        audit_id=stored.get("audit_id"),
        mirror_id=record["mirror_id"],
        scope=record["scope"],
        authority_epoch=record["authority_epoch"],
        scope_revision=record["scope_revision"],
        execution_epoch=record["execution_epoch"],
        fingerprint=record["fingerprint"],
    )


def _relay_target(payload):
    target = str(payload.get("target_node_id") or payload.get("node_id") or "")
    scope = str(payload.get("scope") or ("node:" + target if target else ""))
    if scope.startswith("node:"):
        owner = scope.split(":", 1)[1]
        if target and target != owner:
            raise ValueError("target node and scope owner differ")
        target = owner
    if not target or not scope:
        raise ValueError("node_local proposal requires target_node_id and scope")
    return target, scope


def _relay_payload(envelope):
    supplied = copy.deepcopy(envelope.get("payload") or {})
    checked = protocol_contracts.validate_config_proposal_payload(
        supplied, authority_mode="node_local")
    if not checked.get("ok"):
        code = str(checked.get("code") or "invalid_payload")
        message = str(
            (checked.get("error") or {}).get("message")
            or "proposal payload is invalid")
        raise ValueError(code + ":" + message)
    payload = checked["payload"]
    target, scope = _relay_target(payload)
    if str(payload.get("operation") or "") != "scope_operations":
        raise ValueError("proposal operation must be scope_operations")
    if payload.get("operation_format") != protocol_contracts.OPERATION_FORMAT:
        raise ValueError("unsupported_operation_format")
    operations = payload.get("operations")
    if not isinstance(operations, list) or not operations:
        raise ValueError("proposal requires canonical operations")
    validate_scope_operations(
        operations,
        operation_format=payload["operation_format"],
    )
    result = copy.deepcopy(payload)
    result.pop("target_node_id", None)
    result.pop("node_id", None)
    result["protocol"] = protocol_contracts.PROTOCOL_V2
    result["contracts_version"] = protocol_contracts.CONTRACTS_VERSION
    result["scope"] = scope
    result["proposal_id"] = str(
        payload.get("proposal_id") or envelope["message_id"])
    result["operation_id"] = str(
        payload.get("operation_id") or envelope["message_id"])
    result["issued_at_s"] = float(
        payload.get("issued_at_s") or time.time())
    result["expires_at_s"] = float(
        payload.get("expires_at_s")
        or time.time() + float(envelope.get("ttl_s") or 30.0))
    result["relay_identity"] = {
        "relay_request_id": envelope["message_id"],
        "external_principal": copy.deepcopy(envelope.get("principal") or {}),
        "source": copy.deepcopy(envelope.get("source") or {}),
    }
    if result["operations"] != payload["operations"]:
        raise ValueError("relay modified canonical operations")
    return target, result

def relay_external_proposal(context, envelope):
    """Journal and relay a proposal without mapping or assigning Worker revision."""
    try:
        target, payload = _relay_payload(envelope)
    except (KeyError, TypeError, ValueError) as exc:
        message = str(exc)
        code, separator, detail = message.partition(":")
        if code not in ("invalid_payload", "unsupported_operation_format"):
            code = "invalid_payload"
            detail = message
        return make_error(code, detail if separator else message)
    if not _admitted_node(context, target):
        return make_error("principal_denied", "proposal target is not an admitted Worker")
    command = {
        "message_id": "relay:" + str(envelope["message_id"]),
        "target_node": target,
        "command": protocol_contracts.CONFIG_PROPOSE,
        "payload": payload,
    }
    staged = context["stage_worker_command"](command)
    if not staged.get("ok"):
        return staged
    return make_success(
        accepted=True,
        state="relayed",
        relay_message_id=command["message_id"],
        proposal_id=payload["proposal_id"],
        operation_id=payload["operation_id"],
        target_node_id=target,
        scope=payload["scope"],
        base_authority_epoch=payload["base_authority_epoch"],
        base_scope_revision=payload["base_scope_revision"],
        worker_revision_assigned=False,
    )


def accept_worker_local_notice(context, message):
    """Acknowledge a Worker-local commit notice without committing Master config."""
    source = str(message.get("source_node") or "")
    if not _admitted_node(context, source):
        return make_error("principal_denied", "proposal source is not an admitted Worker")
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    return make_success(
        accepted=True,
        state="worker_authoritative",
        operation_id=str(payload.get("operation_id") or message.get("message_id") or ""),
        mirror_required=True,
        master_commit=False,
    )


def prepare_cross_owner_move(context, request):
    """Build the freeze phase of one partition-safe Cross owner transition."""
    scope = str(request.get("scope") or "")
    if not scope.startswith("cross:"):
        return make_error("invalid_payload", "Cross owner move requires a cross scope")
    current = context["mirror_store"]["get_registry_head"](scope)
    if not current:
        return make_error("not_found", "Cross Registry head is unavailable")
    old_owner = str(current.get("owner_node_id") or "")
    new_owner = str(request.get("new_owner_node_id") or "")
    if not new_owner or new_owner == old_owner:
        return make_error("invalid_payload", "new Cross owner must differ")
    if not _admitted_node(context, old_owner) or not _admitted_node(context, new_owner):
        return make_error(
            "execution_epoch_stale",
            "Cross owner move is blocked while either owner is unavailable",
        )
    transition_id = str(request.get("transition_id") or uuid.uuid4())
    next_epoch = int(current.get("execution_epoch") or 0) + 1
    return make_success(
        transition={
            "transition_id": transition_id,
            "scope": scope,
            "old_owner_node_id": old_owner,
            "new_owner_node_id": new_owner,
            "previous_execution_epoch": next_epoch - 1,
            "execution_epoch": next_epoch,
            "state": "freeze_requested",
        },
        freeze_command={
            "message_id": "cross-freeze:" + transition_id,
            "target_node": old_owner,
            "command": "distributed.cross.apply",
            "payload": {
                "scope": scope,
                "activation_phase": "freeze_old_owner",
                "execution_epoch": next_epoch - 1,
                "transition_id": transition_id,
            },
        },
    )


def persist_cross_owner_move(context, transition, freeze_result):
    """Persist the next Registry epoch only after exact old-owner freeze ACK."""
    if not isinstance(freeze_result, dict) or not freeze_result.get("ok"):
        return make_error(
            "execution_epoch_stale",
            "Cross owner move requires a successful old-owner freeze ACK",
        )
    proof = {
        "transition_id": transition["transition_id"],
        "old_owner_node_id": transition["old_owner_node_id"],
        "new_owner_node_id": transition["new_owner_node_id"],
        "execution_epoch": transition["execution_epoch"],
        "old_owner_frozen": True,
        "registry_persisted": True,
        "freeze_result": copy.deepcopy(freeze_result),
    }
    record = copy.deepcopy(transition)
    record["activation_proof"] = proof
    fence = _write_fence(context)
    if fence is None:
        return make_error("not_config_authority", "Registry leader is unavailable")
    stored = context["mirror_store"]["persist_registry_transition"](
        record, **fence)
    if not stored.get("ok"):
        return stored
    refresh = context.get("derived_refresh")
    if callable(refresh):
        refresh()
    return make_success(
        state="registry_persisted",
        registry_head=stored["registry_head"],
        activation_command={
            "message_id": "cross-activate:" + transition["transition_id"],
            "target_node": transition["new_owner_node_id"],
            "command": "distributed.cross.apply",
            "payload": {
                "scope": transition["scope"],
                "execution_epoch": transition["execution_epoch"],
                "activation_proof": proof,
            },
        },
    )


def create_node_local_coordination(**kwargs):
    """Create non-executing node_local relay, Mirror and Registry capabilities."""
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "authority_reader": kwargs["authority_reader"],
        "node_reader": kwargs["node_reader"],
        "mirror_store": kwargs["mirror_store"],
        "stage_worker_command": kwargs["stage_worker_command"],
        "derived_refresh": kwargs.get("derived_refresh"),
    }
    return {
        "context": context,
        "relay_external_proposal": partial(relay_external_proposal, context),
        "accept_worker_local_notice": partial(accept_worker_local_notice, context),
        "handle_mirror_announce": partial(handle_mirror_announce, context),
        "handle_mirror_snapshot": partial(handle_mirror_snapshot, context),
        "prepare_cross_owner_move": partial(prepare_cross_owner_move, context),
        "persist_cross_owner_move": partial(persist_cross_owner_move, context),
    }

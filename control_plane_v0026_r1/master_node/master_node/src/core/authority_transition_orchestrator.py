# -*- coding: utf-8 -*-

"""Registry-side coordination of controlled Authority transfer and restore."""

import copy
import uuid

from functools import partial

from src.library import _generic_type_library as type_authority
from src.library._generic_system_contract_library import authority_flow_contracts
from src.library._generic_system_contract_library import persistence_contracts
from src.library._generic_system_contract_library import protocol_contracts
from src.library._generic_system_contract_library.errors import make_error, make_success


def _write_fence(context):
    authority = context["authority_reader"]() or {}
    owned = bool(
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
        "authority_term": int(authority.get("authority_term") or 0),
    }


def _scope_owner(scope, snapshot):
    if str(scope).startswith("node:"):
        return str(scope).split(":", 1)[1]
    config = snapshot.get("config") if isinstance(snapshot, dict) else {}
    meta = config.get("meta") if isinstance(config, dict) else None
    row = meta[0] if isinstance(meta, list) and meta else {}
    return str(row.get("owner_node_id") or row.get("node_id") or "")


def _validated_snapshot(scope, value):
    snapshot = persistence_contracts.validate_scope_snapshot(value)
    if snapshot["scope"] != str(scope):
        raise ValueError("scope snapshot targets another authority scope")
    config = snapshot["config"]
    if str(scope).startswith("cross:"):
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
    if type_authority.config_fingerprint(config) != snapshot["fingerprint"]:
        raise ValueError("scope snapshot fingerprint differs")
    if not _scope_owner(scope, snapshot):
        raise ValueError("scope snapshot owner is missing")
    return snapshot

def _save(context, capability, record):
    fence = _write_fence(context)
    if fence is None:
        return make_error(
            "not_config_authority",
            "Authority transition requires the active Registry leader",
            retryable=True,
        )
    return context["store"][capability](record, **fence)


def _fail_transfer(context, transfer, code, message):
    failed = copy.deepcopy(transfer)
    try:
        failed["state"] = authority_flow_contracts.transition_state(
            failed.get("state"), "failed",
            authority_flow_contracts.TRANSFER_TRANSITIONS,
        )
    except ValueError:
        failed["state"] = "failed"
    failed["failure"] = {"code": str(code), "message": str(message)}
    _save(context, "save_authority_transfer", failed)
    return make_error(code, message, transfer=failed)


def begin_transfer(context, request):
    """Persist comparison and validation before staging one target command."""
    if request.get("operator_confirmed") is not True:
        return make_error(
            "principal_denied", "Authority transfer requires operator confirmation")
    principal = str(request.get("operator_subject") or "").strip()
    if not principal:
        return make_error("principal_denied", "Authority transfer operator is missing")
    try:
        scope_head = authority_flow_contracts.validate_scope_head(
            request.get("scope_head"))
        scope = scope_head["scope"]
        master_snapshot = _validated_snapshot(
            scope, request.get("master_snapshot"))
        node_snapshot = _validated_snapshot(
            scope, request.get("node_snapshot"))
        source_truth = str(request.get("source_truth") or "")
        selected = master_snapshot if source_truth == "master_global" else node_snapshot
        transfer = authority_flow_contracts.begin_authority_transfer(
            scope_head,
            request.get("target_mode"),
            source_truth,
            transfer_id=request.get("transfer_id"),
            source_fingerprint=(
                master_snapshot["fingerprint"]
                if scope_head["authority_mode"] == "master_global"
                else node_snapshot["fingerprint"]),
            target_fingerprint=(
                node_snapshot["fingerprint"]
                if request.get("target_mode") == "node_local"
                else master_snapshot["fingerprint"]),
        )
        transfer["operator_subject"] = principal
        transfer["comparison"] = {
            "master_fingerprint": master_snapshot["fingerprint"],
            "node_fingerprint": node_snapshot["fingerprint"],
            "equal": master_snapshot["fingerprint"] == node_snapshot["fingerprint"],
        }
        transfer["master_snapshot"] = master_snapshot
        transfer["node_snapshot"] = node_snapshot
        stored = _save(context, "save_authority_transfer", transfer)
        if not stored.get("ok"):
            return stored
        transfer = authority_flow_contracts.advance_authority_transfer(
            transfer, "compared", selected_fingerprint=selected["fingerprint"])
        transfer["selected_snapshot"] = copy.deepcopy(selected)
        stored = _save(context, "save_authority_transfer", transfer)
        if not stored.get("ok"):
            return stored
        transfer = authority_flow_contracts.advance_authority_transfer(
            transfer, "validated", validation_ok=True)
        transfer["validation_proofs"] = {
            "type": True,
            "contract": True,
            "ownership": True,
            "safety": True,
        }
        stored = _save(context, "save_authority_transfer", transfer)
        if not stored.get("ok"):
            return stored
        journal_id = "transfer-intent:" + transfer["transfer_id"]
        transfer = authority_flow_contracts.advance_authority_transfer(
            transfer, "intent_persisted", journal_record_id=journal_id)
        transfer = authority_flow_contracts.advance_authority_transfer(
            transfer, "awaiting_target_ack")
        stored = _save(context, "save_authority_transfer", transfer)
        if not stored.get("ok"):
            return stored
    except (KeyError, TypeError, ValueError) as exc:
        return make_error("invalid_transfer", str(exc))
    owner = _scope_owner(scope, selected)
    command = {
        "message_id": "authority-transfer:" + transfer["transfer_id"],
        "target_node": owner,
        "command": protocol_contracts.AUTHORITY_TRANSFER,
        "payload": {
            "transfer_id": transfer["transfer_id"],
            "target_mode": transfer["target_mode"],
            "source_truth": transfer["source_truth"],
            "operator_confirmed": True,
            "operator_subject": principal,
            "master_snapshot": master_snapshot,
            "node_snapshot": node_snapshot,
            "authority_epoch": transfer["target_authority_epoch"],
            "fingerprint": transfer["selected_fingerprint"],
        },
    }
    staged = context["stage_worker_command"](command)
    if not staged.get("ok"):
        return _fail_transfer(
            context, transfer, staged.get("code") or "transport_unavailable",
            staged.get("message") or "Authority transfer command was not staged")
    return make_success(
        state="awaiting_target_ack", transfer=transfer,
        target_command=command)


def complete_transfer(context, transfer, target_result):
    """Complete and unlock only after the exact Worker target ACK."""
    ack = {
        "accepted": target_result.get("accepted") is True,
        "authority_epoch": target_result.get("authority_epoch"),
        "fingerprint": target_result.get("fingerprint"),
    }
    try:
        completed = authority_flow_contracts.advance_authority_transfer(
            transfer, "completed", target_ack=ack)
    except (KeyError, TypeError, ValueError) as exc:
        return make_error("invalid_ack", str(exc))
    stored = _save(context, "save_authority_transfer", completed)
    if not stored.get("ok"):
        return stored
    return make_success(state="completed", transfer=completed)


def begin_restore(context, request):
    """Persist replacement-Worker quarantine before selecting any snapshot."""
    scope = str(request.get("scope") or "")
    target_node = str(request.get("target_node_id") or "")
    if scope != "node:" + target_node:
        return make_error("path_denied", "restore scope and target Worker differ")
    if request.get("operator_confirmed") is not True:
        return make_error("principal_denied", "restore requires operator confirmation")
    mirror = context["mirror_reader"](scope)
    prior_epoch = int((mirror or {}).get("authority_epoch") or 0)
    try:
        restore = authority_flow_contracts.begin_restore(
            scope, restore_id=request.get("restore_id"),
            prior_authority_epoch=prior_epoch)
    except (TypeError, ValueError) as exc:
        return make_error("invalid_restore", str(exc))
    restore["target_node_id"] = target_node
    restore["operator_subject"] = str(request.get("operator_subject") or "")
    stored = _save(context, "save_worker_restore", restore)
    if not stored.get("ok"):
        return stored
    return make_success(state="quarantined", restore=restore)


def prepare_restore(context, restore, request):
    """Select and validate one explicit restore source while hardware stays off."""
    source = str(request.get("selected_source") or "")
    snapshot = request.get("snapshot")
    if source == "master_mirror":
        mirror = context["mirror_reader"](restore["scope"])
        exact = (mirror or {}).get("snapshot")
        if exact is None:
            return make_error("not_found", "Master mirror is unavailable")
        if snapshot is not None and snapshot != exact:
            return make_error("stale_revision", "selected mirror is not the current head")
        snapshot = exact
    try:
        selected = _validated_snapshot(restore["scope"], snapshot)
        if (
            source == "master_mirror"
            and int(request.get("local_scope_revision") or 0)
            > int(selected["scope_revision"])
        ):
            return make_error(
                "stale_revision", "older Master mirror cannot replace newer local state")
        prepared = authority_flow_contracts.advance_restore(
            restore, "selected", operator_confirmed=True,
            selected_source=source, snapshot=selected)
        prepared["selected_scope_snapshot"] = selected
        stored = _save(context, "save_worker_restore", prepared)
        if not stored.get("ok"):
            return stored
        prepared = authority_flow_contracts.advance_restore(
            prepared, "validated",
            proofs={
                "type": True,
                "contract": True,
                "ownership": True,
                "safety": True,
            })
        prepared = authority_flow_contracts.advance_restore(
            prepared, "intent_persisted",
            journal_record_id="restore-intent:" + prepared["restore_id"])
        stored = _save(context, "save_worker_restore", prepared)
        if not stored.get("ok"):
            return stored
    except (KeyError, TypeError, ValueError) as exc:
        return make_error("invalid_restore", str(exc))
    command = {
        "message_id": "restore-request:" + prepared["restore_id"],
        "target_node": prepared["target_node_id"],
        "command": protocol_contracts.RESTORE_REQUEST,
        "payload": {
            "restore_id": prepared["restore_id"],
            "scope": prepared["scope"],
            "authority_epoch": prepared["target_authority_epoch"],
            "selected_source": prepared["selected_source"],
            "snapshot": prepared["selected_scope_snapshot"],
            "operator_confirmed": True,
            "identity_confirmed": True,
        },
    }
    staged = context["stage_worker_command"](command)
    if not staged.get("ok"):
        return make_error(
            staged.get("code") or "transport_unavailable",
            staged.get("message") or "restore command was not staged")
    return make_success(
        state="intent_persisted", restore=prepared,
        target_command=command)


def complete_restore_activation(context, restore, result):
    """Record SEM activation proof and stage isolated hardware preflight."""
    expected_epoch = int(restore["target_authority_epoch"])
    fingerprint = restore["selected_scope_snapshot"]["fingerprint"]
    exact = bool(
        result.get("ok")
        and int(result.get("authority_epoch") or -1) == expected_epoch
        and result.get("fingerprint") == fingerprint
        and result.get("hardware_writes_enabled") is False
    )
    if not exact:
        return make_error("invalid_ack", "restore SEM activation ACK differs")
    try:
        activated = authority_flow_contracts.advance_restore(
            restore, "sem_activated",
            sem_commit_id=result.get("sem_commit_id") or str(uuid.uuid4()))
    except (TypeError, ValueError) as exc:
        return make_error("invalid_restore", str(exc))
    stored = _save(context, "save_worker_restore", activated)
    if not stored.get("ok"):
        return stored
    command = {
        "message_id": "restore-activate:" + activated["restore_id"],
        "target_node": activated["target_node_id"],
        "command": protocol_contracts.RESTORE_ACTIVATE,
        "payload": {
            "restore_id": activated["restore_id"],
            "authority_epoch": expected_epoch,
            "fingerprint": fingerprint,
            "preflight_required": True,
        },
    }
    staged = context["stage_worker_command"](command)
    if not staged.get("ok"):
        return staged
    return make_success(
        state="sem_activated", restore=activated,
        target_command=command)


def complete_restore_preflight(context, restore, result):
    """Enable hardware only after the exact Worker preflight proof."""
    exact = bool(
        result.get("ok")
        and result.get("preflight_ok") is True
        and result.get("hardware_writes_enabled") is True
        and int(result.get("authority_epoch") or -1)
        == int(restore["target_authority_epoch"])
    )
    if not exact:
        return make_error("invalid_ack", "restore preflight ACK differs")
    try:
        completed = authority_flow_contracts.advance_restore(
            restore, "preflight_passed", preflight_ok=True)
        completed = authority_flow_contracts.advance_restore(
            completed, "hardware_enabled")
    except (TypeError, ValueError) as exc:
        return make_error("invalid_restore", str(exc))
    stored = _save(context, "save_worker_restore", completed)
    if not stored.get("ok"):
        return stored
    return make_success(state="hardware_enabled", restore=completed)


def create_authority_transition_orchestrator(**kwargs):
    """Create the non-executing Registry-side A5/A6 capability dictionary."""
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "authority_reader": kwargs["authority_reader"],
        "store": kwargs["store"],
        "mirror_reader": kwargs["mirror_reader"],
        "stage_worker_command": kwargs["stage_worker_command"],
    }
    return {
        "context": context,
        "begin_transfer": partial(begin_transfer, context),
        "complete_transfer": partial(complete_transfer, context),
        "begin_restore": partial(begin_restore, context),
        "prepare_restore": partial(prepare_restore, context),
        "complete_restore_activation": partial(
            complete_restore_activation, context),
        "complete_restore_preflight": partial(
            complete_restore_preflight, context),
    }

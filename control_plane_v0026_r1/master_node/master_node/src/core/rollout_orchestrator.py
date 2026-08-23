# -*- coding: utf-8 -*-

"""Capability-gated Full/Patch/Chunk rollout planning and convergence."""

import copy
import time

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library._generic_type_library import canonical_bytes
from src.library._generic_system_contract_library.protocol_contracts import make_worker_command
from src.library._generic_system_contract_library.protocol_contracts import (
    CONFIG_CHUNK,
    CONTRACTS_VERSION,
    CROSS_FULL,
    CROSS_PATCH,
    NODE_FULL,
    NODE_PATCH,
    PROTOCOL_V1,
    PROTOCOL_V2,
    selected_protocol,
)
from src.library.chunk_transport import encode_transfer


def _node_registration(node):
    capabilities = node.get("capabilities") if isinstance(node, dict) else {}
    capabilities = capabilities if isinstance(capabilities, dict) else {}
    supplied_features = capabilities.get("features")
    if isinstance(supplied_features, dict):
        features = {
            str(name): bool(enabled)
            for name, enabled in supplied_features.items()
        }
    elif isinstance(supplied_features, (list, tuple, set)):
        features = {
            str(name): True
            for name in supplied_features
            if str(name)
        }
    else:
        features = {}
    protocols = node.get("protocol_versions")
    protocols = tuple(protocols) if isinstance(protocols, (list, tuple)) else ()
    if not protocols:
        protocols = (str(node.get("protocol_version") or PROTOCOL_V1),)
    return {
        "protocol_versions": protocols,
        "contracts_version": str(node.get("contracts_version") or ""),
        "capabilities": features,
        "node_replica": {
            "scope": node.get("node_replica_scope") or "node:" + str(
                node.get("node_id") or ""),
            "revision": int(node.get("node_replica_revision") or node.get(
                "config_revision") or 0),
            "fingerprint": node.get("node_replica_fingerprint") or capabilities.get(
                "node_shard_fingerprint"),
        },
        "cross_replica": {
            "scope": node.get("cross_replica_scope") or "cross:" + str(
                node.get("node_id") or "") + ".default",
            "revision": int(node.get("cross_replica_revision") or 0),
            "fingerprint": node.get("cross_replica_fingerprint"),
        },
        "max_packet_size": int(node.get("max_packet_size") or 1024 * 1024),
    }

def _replica(registration, snapshot):
    return registration["cross_replica"] if snapshot["scope_kind"] == "cross" \
        else registration["node_replica"]


def _command_names(snapshot):
    return (CROSS_FULL, CROSS_PATCH) if snapshot["scope_kind"] == "cross" \
        else (NODE_FULL, NODE_PATCH)


def _full_payload(context, registration, snapshot, authority):
    protocol = selected_protocol(registration) or PROTOCOL_V1
    payload = {
        "protocol": protocol,
        "contracts_version": CONTRACTS_VERSION,
        "coordinator_node_id": context["coordinator_node_id"],
        "coordinator_term": int(authority.get("authority_term") or 0),
        "authority_revision": int(snapshot["global_revision"]),
        "authority_fingerprint": authority.get("config_fingerprint"),
        "global_revision": int(snapshot["global_revision"]),
        "scope_id": snapshot["scope"],
        "target_scope_revision": int(snapshot["scope_revision"]),
        "target_scope_fingerprint": snapshot["fingerprint"],
        "execution_epoch": int(snapshot["execution_epoch"]),
        "fingerprint": snapshot["fingerprint"],
        "config": copy.deepcopy(snapshot["config"]),
    }
    if snapshot["scope_kind"] == "cross":
        payload.update({
            "shard_scope": "cross",
            "owner_node_id": snapshot["owner_node_id"],
            "cross_revision": int(snapshot["scope_revision"]),
            "cross_fingerprint": snapshot["fingerprint"],
        })
    else:
        payload.update({
            "shard_scope": "node_local",
            "shard_node_id": snapshot["owner_node_id"],
            "shard_fingerprint": snapshot["fingerprint"],
        })
    return payload


def _patch_operations(rows):
    operations = []
    for row in rows or ():
        operations.append({
            "op": row.get("operation") or row.get("op"),
            "section": row["section"],
            "entity_key": row["entity_key"],
            "json_pointer": row.get("json_pointer"),
            "expected_value_hash": row.get("expected_value_hash"),
            "value": copy.deepcopy(row.get("value")),
            "value_hash": row.get("value_hash"),
        })
    return operations


def _patch_payload(context, snapshot, authority, replica, rows):
    return {
        "protocol": PROTOCOL_V2,
        "contracts_version": CONTRACTS_VERSION,
        "coordinator_node_id": context["coordinator_node_id"],
        "coordinator_term": int(authority.get("authority_term") or 0),
        "authority_revision": int(snapshot["global_revision"]),
        "authority_fingerprint": authority.get("config_fingerprint"),
        "global_revision": int(snapshot["global_revision"]),
        "shard_scope": "cross" if snapshot["scope_kind"] == "cross" else "node_local",
        "scope_id": snapshot["scope"],
        # Compatibility alias for rolling upgrades from pre-v0088 Workers.
        # ``scope_id`` remains the canonical distributed-v2 field.
        "scope": snapshot["scope"],
        "owner_node_id": snapshot["owner_node_id"] if snapshot["scope_kind"] == "cross" else None,
        "shard_node_id": snapshot["owner_node_id"] if snapshot["scope_kind"] == "node" else None,
        "base_scope_revision": int(replica.get("revision") or 0),
        "base_scope_fingerprint": replica.get("fingerprint"),
        "target_scope_revision": int(snapshot["scope_revision"]),
        "target_scope_fingerprint": snapshot["fingerprint"],
        "execution_epoch": int(snapshot["execution_epoch"]),
        "operations": _patch_operations(rows),
    }


def _outer_size(context, target_node, command, message_id, payload, now_s):
    envelope = make_worker_command(
        target_node, command,
        site_id=context["site_id"], source_node=context["coordinator_node_id"],
        message_id=message_id, ttl_s=context["command_ttl_s"], now_s=now_s,
        payload=payload,
    )
    return len(canonical_bytes(envelope))


def _delivery_identity(rollout):
    identity = str(rollout["rollout_id"])
    detail = rollout.get("detail") if isinstance(rollout.get("detail"), dict) else {}
    return identity + ":fallback-full" if detail.get("force_full") else identity


def _commands_for_payload(context, rollout, snapshot, registration, command, payload, now_s):
    node_id = rollout["node_id"]
    message_id = _delivery_identity(rollout)
    master_limit = int(context["max_packet_size"])
    worker_limit = int(registration["max_packet_size"])
    maximum = min(master_limit, worker_limit)
    serialized_bytes = _outer_size(
        context, node_id, command, message_id, payload, now_s)
    if serialized_bytes <= maximum:
        return make_success(commands=({
            "target_node": node_id,
            "command": command,
            "message_id": message_id,
            "rollout_id": rollout["rollout_id"],
            "payload": payload,
        },), transfer_id=None)
    if not registration["capabilities"].get("chunk_reassembly"):
        return make_error(
            "packet_limit",
            "rollout exceeds packet size and Worker lacks chunk support",
            serialized_bytes=serialized_bytes,
            master_limit_bytes=master_limit,
            worker_limit_bytes=worker_limit,
            effective_limit_bytes=maximum,
            chunk_reassembly=False,
        )
    transfer_id = message_id + ":transfer"
    chunk_bytes = None
    encoded = None
    commands = ()
    for _attempt in range(12):
        encoded = encode_transfer(
            payload,
            transfer_id=transfer_id,
            target_command=command,
            scope=snapshot["scope"],
            target_scope_revision=snapshot["scope_revision"],
            target_scope_fingerprint=snapshot["fingerprint"],
            max_packet_size=maximum,
            chunk_bytes=chunk_bytes,
        )
        commands = tuple({
            "target_node": node_id,
            "command": CONFIG_CHUNK,
            "message_id": message_id + ":chunk:" + str(chunk["sequence"]),
            "rollout_id": rollout["rollout_id"],
            "payload": dict(
                chunk,
                protocol=PROTOCOL_V2,
                contracts_version=CONTRACTS_VERSION,
                coordinator_node_id=context["coordinator_node_id"],
                coordinator_term=int(payload.get("coordinator_term") or 0),
            ),
        } for chunk in encoded["chunks"])
        oversized = tuple(
            item for item in commands
            if _outer_size(
                context, node_id, CONFIG_CHUNK, item["message_id"],
                item["payload"], now_s,
            ) > maximum
        )
        if not oversized:
            break
        largest_base64 = max(len(item["payload"]["chunk_base64"]) for item in oversized)
        approximate_raw = max(1024, largest_base64 * 3 // 4)
        chunk_bytes = max(1024, int(approximate_raw * 0.8))
    else:
        return make_error("packet_limit", "serialized Chunk command exceeds packet size")
    return make_success(commands=commands, transfer_id=transfer_id)


def plan_rollout(context, rollout, node, snapshot, authority, **kwargs):
    """Choose already-current, Patch, Full or Chunk from persisted anchors."""
    registration = _node_registration(node)
    protocol = selected_protocol(registration)
    if not protocol:
        return make_error("unsupported_protocol", "Worker has no compatible distributed protocol")
    capabilities = registration["capabilities"]
    if capabilities.get("master_automation_execution"):
        return make_error("unsafe_capability", "Worker advertises forbidden Master automation execution")
    if snapshot["scope_kind"] == "cross" and not (
        capabilities.get("cross_runtime") and capabilities.get("cross_full")
    ):
        return make_error("cross_capability", "Cross owner lacks its required runtime capability")
    if snapshot["scope_kind"] == "node" and not capabilities.get("node_full", True):
        return make_error("node_capability", "Worker lacks Node Full capability")
    replica = _replica(registration, snapshot)
    if (
        replica.get("scope") == snapshot["scope"]
        and replica.get("fingerprint") == snapshot["fingerprint"]
        and int(replica.get("revision") or 0) == int(snapshot["scope_revision"])
    ):
        return make_success(status="already_current", commands=())
    full_command, patch_command = _command_names(snapshot)
    force_full = bool((rollout.get("detail") or {}).get("force_full"))
    patch_capability = "cross_patch" if snapshot["scope_kind"] == "cross" else "node_patch"
    rows = kwargs.get("operations")
    use_patch = (
        protocol == PROTOCOL_V2
        and capabilities.get(patch_capability)
        and not force_full
        and int(replica.get("revision") or 0) > 0
        and int(replica.get("revision") or 0) < int(snapshot["scope_revision"])
        and rows is not None
        and 0 < len(rows) <= 500
    )
    command = patch_command if use_patch else full_command
    payload = _patch_payload(context, snapshot, authority, replica, rows) if use_patch \
        else _full_payload(context, registration, snapshot, authority)
    planned = _commands_for_payload(
        context, rollout, snapshot, registration, command, payload,
        float(kwargs.get("now_s", time.time())),
    )
    if planned.get("ok"):
        planned["status"] = "patch" if use_patch else "full"
        planned["protocol"] = protocol
    return planned


def _stage_command(context, command, authority):
    record = {
        "message_id": command["message_id"],
        "site_id": context["site_id"],
        "target_service": "worker",
        "target_instance_id": command["target_node"],
        "event_type": command["command"],
        "envelope": copy.deepcopy(command),
    }
    return context["authority_store"]["enqueue_outbox"](
        record, site_id=context["site_id"], instance_id=context["instance_id"],
        lease_token=authority.get("lease_token"),
        authority_term=authority.get("authority_term"))


def _revision_bound_authority(context, snapshot, authority):
    """Bind rollout authority metadata to the persisted global revision.

    The periodic outbox publisher may read the authority row immediately before
    a concurrent config commit and then block while claiming the newly-created
    rollout.  Reusing that pre-commit dictionary would combine the new global
    revision with the old global fingerprint.  Workers correctly reject that
    pair as non-converged.  Resolve the immutable config snapshot by the scope's
    own global revision so the two fields always describe the same commit.
    """
    reader = context.get("config_reader")
    if not callable(reader):
        # Kept only for the isolated planner adapters used by legacy callers;
        # the production runtime always supplies the revision reader.
        return make_success(authority=copy.deepcopy(authority))
    revision = int(snapshot.get("global_revision") or 0)
    persisted = reader(revision=revision)
    persisted_revision = int(
        (persisted or {}).get("global_revision")
        or (persisted or {}).get("revision") or 0)
    persisted_fingerprint = str((persisted or {}).get("fingerprint") or "")
    if revision <= 0 or persisted_revision != revision or not persisted_fingerprint:
        return make_error(
            "authority_snapshot_missing",
            "rollout global revision has no matching persisted fingerprint",
            retryable=True,
        )
    bound = copy.deepcopy(authority)
    bound["config_revision"] = revision
    bound["config_fingerprint"] = persisted_fingerprint
    return make_success(authority=bound)


def stage_due_rollouts(context, **kwargs):
    """Plan and durably stage each due rollout under the current authority fence."""
    authority = context["authority_reader"]() or {}
    fence = {
        "site_id": context["site_id"],
        "instance_id": context["instance_id"],
        "lease_token": authority.get("lease_token"),
        "authority_term": authority.get("authority_term"),
    }
    due = context["entity_store"]["due_rollouts"](
        site_id=context["site_id"], instance_id=context["instance_id"],
        lease_token=authority.get("lease_token"), authority_term=authority.get("authority_term"),
        limit=kwargs.get("limit", 64),
    )
    staged = 0
    for rollout in due:
        node = context["node_reader"](rollout["node_id"])
        snapshot = context["entity_store"]["get_scope"](
            rollout["scope"], revision=rollout["target_scope_revision"])
        if not node:
            context["entity_store"]["mark_rollout"](
                rollout["rollout_id"], state="retry_wait", retry_s=5.0,
                detail={"code": "target_missing"}, **fence)
            continue
        if not snapshot:
            context["entity_store"]["mark_rollout"](
                rollout["rollout_id"], state="failed",
                detail={"code": "scope_snapshot_missing"}, **fence)
            continue
        bound = _revision_bound_authority(context, snapshot, authority)
        if not bound.get("ok"):
            context["entity_store"]["mark_rollout"](
                rollout["rollout_id"], state="retry_wait", retry_s=5.0,
                detail={
                    "code": bound.get("code"),
                    "message": bound.get("message"),
                },
                **fence,
            )
            continue
        registration = _node_registration(node)
        replica = _replica(registration, snapshot)
        operations = context["entity_store"]["get_scope_ops"](
            snapshot["scope"], replica.get("revision") or 0,
            snapshot["scope_revision"],
        )
        planned = plan_rollout(
            context, rollout, node, snapshot, bound["authority"],
            operations=operations)
        if not planned.get("ok"):
            detail = {
                "code": planned.get("code"),
                "message": planned.get("message"),
            }
            for key in (
                    "serialized_bytes", "master_limit_bytes",
                    "worker_limit_bytes", "effective_limit_bytes",
                    "chunk_reassembly"):
                if key in planned:
                    detail[key] = copy.deepcopy(planned[key])
            context["entity_store"]["mark_rollout"](
                rollout["rollout_id"], state="retry_wait", retry_s=30.0,
                detail=detail, **fence)
            continue
        if planned.get("status") == "already_current":
            context["entity_store"]["mark_rollout"](
                rollout["rollout_id"], state="converged",
                detail={"reason": "matching_replica_fingerprint"}, **fence)
            continue
        accepted = True
        terminal_outbox = None
        for command in planned["commands"]:
            result = _stage_command(context, command, authority)
            state = str(result.get("state") or "").upper()
            if (result.get("ok") and result.get("duplicate")
                    and state not in ("", "PENDING", "SENT")):
                terminal_outbox = copy.deepcopy(result)
                accepted = False
                break
            accepted = bool(result.get("ok")) and accepted
        if terminal_outbox:
            marked = context["entity_store"]["mark_rollout"](
                rollout["rollout_id"], state="failed",
                detail={
                    "code": "terminal_outbox_reuse",
                    "outbox_state": terminal_outbox.get("state"),
                    "last_error_code": terminal_outbox.get("last_error_code"),
                },
                **fence,
            )
        else:
            marked = context["entity_store"]["mark_rollout"](
                rollout["rollout_id"],
                state="sending" if accepted else "retry_wait",
                attempted=accepted, retry_s=0.0 if accepted else 5.0,
                transfer_id=planned.get("transfer_id"),
                detail={
                    "mode": planned.get("status"),
                    "protocol": planned.get("protocol"),
                },
                **fence,
            )
        ignored = isinstance(marked, dict) and bool(marked.get("ignored"))
        staged += int(accepted and not ignored)
    return staged


def handle_rollout_ack(context, command, result, **kwargs):
    """Record application ACK; Patch basis failures force a Full retry."""
    rollout_id = command.get("rollout_id")
    if not rollout_id:
        return False
    if result.get("ok"):
        chunk_received = (
            command.get("command") == CONFIG_CHUNK
            and result.get("status") not in ("applied", "already_current")
        )
        state = "received" if chunk_received else "applied"
        detail = {"result": copy.deepcopy(result)}
        marked = context["entity_store"]["mark_rollout"](
            rollout_id, state=state, detail=detail, **kwargs)
        return bool(marked.get("ok"))
    code = str(result.get("code") or "worker_rejected")
    fallback = code in (
        "patch_base_mismatch", "value_hash_mismatch", "journal_gap",
        "patch_validation", "patch_base_fingerprint",
        "base_scope_revision", "base_scope_fingerprint", "patch_operations",
    )
    marked = context["entity_store"]["mark_rollout"](
        rollout_id, state="retry_wait" if fallback else "failed",
        retry_s=1.0 if fallback else 0.0,
        detail={"code": code, "force_full": fallback, "result": copy.deepcopy(result)},
        **kwargs,
    )
    return bool(marked.get("ok"))


def create_rollout_orchestrator(**kwargs):
    """Create the rollout capability dictionary."""
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "coordinator_node_id": kwargs["coordinator_node_id"],
        "command_ttl_s": float(kwargs.get("command_ttl_s", 60.0)),
        "max_packet_size": int(kwargs.get("max_packet_size", 4 * 1024 * 1024)),
        "authority_reader": kwargs["authority_reader"],
        "config_reader": kwargs.get("config_reader"),
        "node_reader": kwargs["node_reader"],
        "authority_store": kwargs["authority_store"],
        "entity_store": kwargs["entity_store"],
    }
    return {
        "context": context,
        "plan": partial(plan_rollout, context),
        "stage_due": partial(stage_due_rollouts, context),
        "handle_ack": partial(handle_rollout_ack, context),
    }

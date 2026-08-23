# -*- coding: utf-8 -*-

"""Non-executing Master coordination kernel for control and Worker traffic."""

import copy
import time
import uuid

from functools import partial

from src.library._generic_system_contract_library import protocol_contracts
from src.library._generic_system_contract_library.protocol_contracts import make_envelope
from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library._generic_system_contract_library.protocol_contracts import certificate_uri
from src.library._generic_type_library import fingerprint, json_ready
from src.library._generic_system_contract_library.protocol_contracts import validate_worker_ack
from src.core.access_policy import authorize_control
from src.core.node_local_coordination import create_node_local_coordination


WORKER_COMMANDS = (
    "distributed.node.register",
    "distributed.node.heartbeat",
    "distributed.config.propose",
    "distributed.mirror.announce",
    "distributed.mirror.snapshot",
    "distributed.actuator.route",
    "distributed.actuator.result",
)


def _principal_subject(envelope):
    principal = envelope.get("principal")
    if isinstance(principal, dict):
        return str(principal.get("subject") or "unknown")
    return str(principal or "unknown")


def _require_control_authority(context):
    authority = _owned_authority(context)
    return None if authority else make_error(
        "not_config_authority", "command requires the active Master authority")


def _owned_authority(context):
    authority = context["authority_reader"]()
    owned = (
        authority
        and authority.get("lease_active")
        and authority.get("leader_instance_id") == context["instance_id"]
        and authority.get("lease_token")
    )
    return authority if owned else None


def _authority_write_fence(context, authority):
    return {
        "site_id": context["site_id"],
        "instance_id": context["instance_id"],
        "lease_token": authority["lease_token"],
        "authority_term": authority["authority_term"],
    }


def _operation_write_fence(context, authority):
    result = _authority_write_fence(context, authority)
    result["leader_lease_token"] = result.pop("lease_token")
    result["config_revision"] = authority["config_revision"]
    return result


def _owns_authority(context):
    return _require_control_authority(context) is None


def _publish_event(context, event_type, payload, **kwargs):
    publisher = context.get("event_publisher")
    if not callable(publisher):
        return False
    try:
        return bool(publisher(
            event_type, json_ready(payload),
            correlation_id=kwargs.get("correlation_id"),
            causation_id=kwargs.get("causation_id"),
            principal=json_ready(kwargs.get("principal")),
            target_service="proxy", target_instance_id="broadcast",
        ))
    except Exception:
        return False


def _configured_worker(context, node_id):
    """Return whether Registry policy enables this Worker identity.

    Node-local mode cannot derive admission from Worker mirrors because the
    first mirror is accepted only after admission. Its administrative
    allowlist therefore breaks that bootstrap cycle without importing Worker
    value authority into the Master. Legacy global mode keeps using the
    configured topology. The operational site remains bound by the active
    Master context, broker ACL and exact mTLS certificate URI.
    """
    normalized_node_id = str(node_id or "")
    if context["authority_mode"] == "node_local":
        return normalized_node_id in context["allowed_worker_node_ids"]
    snapshot = context["config_reader"]()
    config = snapshot.get("config_data") if isinstance(snapshot, dict) else {}
    for node in config.get("nodes") or () if isinstance(config, dict) else ():
        if not isinstance(node, dict) \
                or str(node.get("node_id") or "") != normalized_node_id:
            continue
        return (
            str(node.get("node_role") or "").lower() == "worker"
            and node.get("enabled") is not False
        )
    return False


def _fresh_broker_identity_proof(context, node_id):
    proof = context["broker_identity_proofs"].get(str(node_id))
    if not isinstance(proof, dict):
        return None
    age = time.monotonic() - float(proof.get("received_at_monotonic") or 0.0)
    if age < 0.0 or age > context["identity_proof_max_age_s"]:
        context["broker_identity_proofs"].pop(str(node_id), None)
        return None
    return proof


def _refresh_runtime(context):
    refresh = context.get("derived_refresh")
    return bool(refresh()) if callable(refresh) else False


def _auto_approve_worker_if_proven(context, node_id):
    if not context["auto_admit_broker_bound_workers"] or not _owns_authority(context):
        return None
    if not _configured_worker(context, node_id):
        return None
    proof = _fresh_broker_identity_proof(context, node_id)
    if proof is None:
        return None
    expected_identity = certificate_uri(context["site_id"], "worker", node_id)
    if proof.get("certificate_identity") != expected_identity:
        return make_error("identity_mismatch", "broker identity proof does not match worker")
    node = context["nodes"]["get"](node_id)
    if not node:
        return None
    if node.get("certificate_identity") != expected_identity:
        return make_error("identity_mismatch", "registered worker identity does not match proof")
    state = str(node.get("admission_state") or "").upper()
    if state == "APPROVED":
        return make_success(node=node, already_approved=True)
    if state != "PENDING":
        return make_error("authorization_denied", "automatic admission never overrides " + state)
    result = context["nodes"]["decide_admission"]({
        "node_id": node_id,
        "decision": "APPROVED",
        "reason": "broker-bound live mTLS heartbeat proof",
        "principal_subject": "system:broker-mtls-auto-admission",
    })
    if result.get("ok"):
        snapshot = context["config_reader"]()
        if snapshot:
            context["ownership"]["reconcile"](snapshot["config_data"])
        _publish_event(context, "node.admission", {
            "node_id": node_id,
            "decision": "APPROVED",
            "automatic": True,
            "proof": "broker-bound-live-mtls-heartbeat",
        }, principal={"subject": "system:broker-mtls-auto-admission"})
    return result


def handle_broker_identity_heartbeat(context, envelope):
    """Use one live ACL-bound heartbeat as an ephemeral mTLS identity proof."""
    if not context["auto_admit_broker_bound_workers"] or not _owns_authority(context):
        return False
    node_id = str(envelope.get("source_node") or "")
    if not node_id or envelope.get("broker_bound_topic_node") != node_id:
        return False
    if not _configured_worker(context, node_id):
        return False
    context["broker_identity_proofs"][node_id] = {
        "certificate_identity": certificate_uri(context["site_id"], "worker", node_id),
        "message_id": str(envelope.get("message_id") or ""),
        "received_at_monotonic": time.monotonic(),
    }
    decision = _auto_approve_worker_if_proven(context, node_id)
    return decision is None or bool(decision.get("ok"))


def _register_control_inbox(context, envelope):
    source = envelope["source"]
    record = {
        "site_id": context["site_id"],
        "source_service": source["service"],
        "source_instance_id": source["instance_id"],
        "message_id": envelope["message_id"],
        "event_type": envelope["event_type"],
        "payload_fingerprint": fingerprint(envelope["payload"]),
    }
    return record, context["authority_store"]["register_inbox"](record)


def handle_control_command(context, envelope):
    """Execute one persistent, idempotent Proxy command."""
    record, started = _register_control_inbox(context, envelope)
    if not started.get("ok"):
        _audit_control(context, envelope, started)
        return started
    if started.get("duplicate"):
        cached = started.get("record", {}).get("result")
        result = copy.deepcopy(cached) if isinstance(cached, dict) else make_success(duplicate=True)
        _audit_control(context, envelope, result)
        return result
    authorized = authorize_control(envelope, site_id=context["site_id"])
    if not authorized.get("ok"):
        context["authority_store"]["complete_inbox"](record, authorized)
        _audit_control(context, envelope, authorized)
        return authorized
    handlers = {
        "actuator.request": _control_actuator,
        "config.propose": _control_config,
        "config.rollback": _control_config_rollback,
        "node.admission.approve": _control_admission,
        "node.drain": _control_node_drain,
        "node.state.set": _control_node_state,
        "nodes.list": _control_nodes_list,
        "config.get": _control_config_get,
        "config.history": _control_config_history,
        "operation.get": _control_operation_get,
        "runtime.status": _control_runtime_status,
        "terminal.execute": _control_terminal,
    }
    handler = handlers.get(envelope["event_type"])
    try:
        result = handler(context, envelope) if callable(handler) else make_error(
            "invalid_payload", "control command is unsupported")
    except Exception:
        result = make_error(
            "internal_error", "control command failed before durable completion",
            retryable=True)
        try:
            _audit_control(context, envelope, result)
        except Exception:
            result["audit_persistence"] = "unavailable"
        return result
    retry_pending = bool(
        envelope["event_type"] == "actuator.request"
        and not result.get("ok")
        and result.get("retryable")
    )
    if not retry_pending:
        context["authority_store"]["complete_inbox"](record, result)
    _audit_control(context, envelope, result)
    return result


def _after_config_commit(context, result):
    if not result.get("ok") or not result.get("revision"):
        return False
    _refresh_runtime(context)
    snapshot = context["config_reader"]()
    if snapshot:
        context["ownership"]["reconcile"](snapshot["config_data"])
    rollouts = context.get("rollouts") or {}
    stage = rollouts.get("stage_due")
    result["rollout_staged"] = bool(stage(limit=64)) if callable(stage) else False
    return result["rollout_staged"]


def _control_config(context, envelope):
    if context.get("authority_mode", "node_local") == "node_local":
        return context["node_local"]["relay_external_proposal"](envelope)
    payload = envelope["payload"]
    result = context["config_authority"]["propose"]({
        "base_revision": payload["base_revision"],
        "changes": copy.deepcopy(payload["changes"]),
        "reason": payload.get("reason"),
        "rollout_targets": payload.get("rollout_targets"),
        "principal_subject": _principal_subject(envelope),
        "request_id": envelope["message_id"],
    })
    changed = result.get("changed") is not False
    if result.get("ok") and changed:
        _after_config_commit(context, result)
    elif result.get("ok"):
        result["rollout_staged"] = False
        result["rollout_required"] = False
    if result.get("ok") and changed and not result.get("duplicate"):
        _publish_event(context, "config.rollout", {
            "revision": result.get("revision"),
            "rollout_ids": result.get("rollout_ids") or (),
            "fingerprint": result.get("fingerprint"),
            "state": "staged" if result.get("targets") else "converged",
        }, correlation_id=envelope.get("correlation_id"),
           causation_id=envelope.get("message_id"), principal=envelope.get("principal"))
    return result


def _stage_worker_command(context, command):
    authority = _owned_authority(context)
    if not authority:
        return make_error(
            "not_config_authority",
            "actuator Outbox enqueue requires the active Master authority",
            retryable=True,
        )
    record = {
        "message_id": command["message_id"],
        "site_id": context["site_id"],
        "target_service": "worker",
        "target_instance_id": command["target_node"],
        "event_type": command["command"],
        "envelope": copy.deepcopy(command),
    }
    return context["authority_store"]["enqueue_outbox"](
        record, **_authority_write_fence(context, authority))


def _outbox_stage_failure(staged, operation):
    failure = copy.deepcopy(staged)
    code = str(failure.get("code") or "internal_error")
    if failure.get("retryable") or code in (
            "database_unavailable", "internal_error", "not_config_authority", "not_ready"):
        failure["retryable"] = True
    operation_id = operation.get("operation_id") if isinstance(operation, dict) else None
    if operation_id:
        failure["operation_id"] = str(operation_id)
    return failure


def _control_actuator(context, envelope):
    result = context["operations"]["request"](envelope)
    if not result.get("ok"):
        return result
    if result.get("worker_command"):
        staged = _stage_worker_command(context, result["worker_command"])
        if not staged.get("ok"):
            return _outbox_stage_failure(staged, result.get("operation"))
    operation = result["operation"]
    accepted = make_success(
        accepted=True, operation_id=operation["operation_id"],
        state=operation["state"], owner_node_id=operation["owner_node_id"],
        duplicate=bool(result.get("duplicate")),
    )
    if not result.get("duplicate"):
        _publish_event(
            context, "operation.accepted", accepted,
            correlation_id=envelope.get("correlation_id"),
            causation_id=envelope.get("message_id"), principal=envelope.get("principal"))
    return accepted


def _control_config_rollback(context, envelope):
    """Keep global rollback unavailable after Worker authority activation."""
    if context.get("authority_mode", "node_local") == "node_local":
        return make_error(
            "unsupported_operation_format",
            "node_local rollback requires a target Worker proposal with canonical operations",
        )
    payload = envelope["payload"]
    result = context["config_authority"]["rollback"]({
        "base_revision": payload["base_revision"],
        "target_revision": payload["target_revision"],
        "reason": payload.get("reason"),
        "rollout_targets": payload.get("rollout_targets"),
        "principal_subject": _principal_subject(envelope),
        "request_id": envelope["message_id"],
    })
    if result.get("ok"):
        _after_config_commit(context, result)
    if result.get("ok") and not result.get("duplicate"):
        _publish_event(context, "config.rollout", {
            "revision": result.get("revision"),
            "rollback_from_revision": payload["base_revision"],
            "rollback_target_revision": payload["target_revision"],
            "rollout_ids": result.get("rollout_ids") or (),
            "fingerprint": result.get("fingerprint"),
            "state": "staged" if result.get("targets") else "converged",
        }, correlation_id=envelope.get("correlation_id"),
           causation_id=envelope.get("message_id"),
           principal=envelope.get("principal"))
    return result

def _control_admission(context, envelope):
    authority_error = _require_control_authority(context)
    if authority_error:
        return authority_error
    payload = envelope["payload"]
    result = context["nodes"]["decide_admission"]({
        "node_id": payload["node_id"], "decision": "APPROVED",
        "reason": payload.get("reason"),
        "principal_subject": _principal_subject(envelope),
    })
    if result.get("ok"):
        snapshot = context["config_reader"]()
        if snapshot:
            context["ownership"]["reconcile"](snapshot["config_data"])
        _resync_worker(context, payload["node_id"])
        _publish_event(context, "node.admission", {
            "node_id": payload["node_id"], "decision": "APPROVED",
        }, correlation_id=envelope.get("correlation_id"),
           causation_id=envelope.get("message_id"), principal=envelope.get("principal"))
    return result


def _set_node_state(context, envelope, desired):
    authority_error = _require_control_authority(context)
    if authority_error:
        return authority_error
    payload = envelope["payload"]
    result = context["nodes"]["set_desired_state"]({
        "node_id": payload["node_id"], "desired_state": desired,
        "reason": payload.get("reason"),
        "principal_subject": _principal_subject(envelope),
    })
    if result.get("ok"):
        _publish_event(context, "node.desired_state", {
            "node_id": payload["node_id"], "desired_state": desired,
        }, correlation_id=envelope.get("correlation_id"),
           causation_id=envelope.get("message_id"), principal=envelope.get("principal"))
    return result


def _control_node_drain(context, envelope):
    return _set_node_state(context, envelope, "DRAINING")


def _control_node_state(context, envelope):
    return _set_node_state(
        context, envelope, str(envelope["payload"]["desired_state"]).upper())


def _control_operation_get(context, envelope):
    operation = context["operations"]["get"](envelope["payload"]["operation_id"])
    return make_success(operation=operation) if operation else make_error(
        "not_found", "operation is unknown")


def _control_nodes_list(context, _envelope):
    return make_success(nodes=context["nodes"]["list"]())


def _control_config_get(context, envelope):
    if context.get("authority_mode", "node_local") == "node_local":
        payload = envelope.get("payload") or {}
        scope = str(payload.get("scope") or "")
        if scope:
            mirror = context["authority_store"]["get_worker_mirror"](scope)
            return make_success(mirror=mirror) if mirror else make_error(
                "not_found", "Worker mirror scope is unknown")
        return make_success(
            mirrors=context["authority_store"]["list_worker_mirrors"]())
    snapshot = context["authority_store"]["get_config"](
        context["site_id"], revision=envelope["payload"].get("revision"))
    return make_success(config=snapshot) if snapshot else make_error(
        "not_found", "configuration revision is unknown")


def _control_config_history(context, envelope):
    """Return mirror heads in node_local; never expose global config history."""
    if context.get("authority_mode", "node_local") == "node_local":
        mirrors = context["authority_store"]["list_worker_mirrors"]()
        limit = max(1, min(200, int(envelope["payload"].get("limit", 50))))
        return make_success(history=tuple(mirrors[:limit]))
    history = context["authority_store"]["config_history"](
        context["site_id"], limit=envelope["payload"].get("limit", 50))
    return make_success(history=history)

def _control_runtime_status(context, _envelope):
    return make_success(runtime=context["runtime_snapshot"]())


def _control_terminal(context, envelope):
    principal = envelope.get("principal") if isinstance(envelope.get("principal"), dict) else {}
    return context["admin"]["execute"](
        envelope["payload"]["command"],
        principal_subject=_principal_subject(envelope),
        permissions=principal.get("permissions") or (),
    )


def _audit_control(context, envelope, result):
    source = envelope.get("source") if isinstance(envelope.get("source"), dict) else {}
    context["audit"]["append"]({
        "request_id": str(envelope.get("message_id") or uuid.uuid4()),
        "correlation_id": envelope.get("correlation_id"),
        "principal_subject": _principal_subject(envelope),
        "session_id": (envelope.get("principal") or {}).get("session_id")
        if isinstance(envelope.get("principal"), dict) else None,
        "instance_id": context["instance_id"], "site_id": context["site_id"],
        "resource": str(envelope.get("event_type") or "unknown"),
        "action": str(envelope.get("event_type") or "unknown"),
        "decision": "ALLOW" if result.get("ok") else "DENY",
        "reason_code": str(result.get("code") or "ok"),
        "config_revision": (context["authority_reader"]() or {}).get("config_revision"),
        "operation_id": result.get("operation_id"),
        "details": {
            "source_service": source.get("service"),
            "source_instance": source.get("instance_id"),
        },
    })


def _resync_worker(context, node_id):
    store = context.get("authority_store") or {}
    if context.get("authority_mode", "node_local") == "node_local":
        node = context["nodes"]["get"](node_id) or {}
        scope = "node:" + str(node_id)
        mirror = store["get_worker_mirror"](scope)
        exact = bool(
            mirror
            and int(node.get("node_replica_revision") or 0)
            == int(mirror.get("scope_revision") or 0)
            and str(node.get("node_replica_fingerprint") or "")
            == str(mirror.get("fingerprint") or "")
        )
        if exact:
            return False
        command = {
            "message_id": "mirror-request:" + str(uuid.uuid4()),
            "target_node": str(node_id),
            "command": "distributed.mirror.request",
            "payload": {
                "scope": scope,
                "authority_epoch": int((mirror or {}).get("authority_epoch") or 0),
                "scope_revision": int((mirror or {}).get("scope_revision") or 0),
                "fingerprint": str((mirror or {}).get("fingerprint") or ""),
            },
        }
        return bool(_stage_worker_command(context, command).get("ok"))
    ensure = store.get("ensure_node_rollouts")
    if not callable(ensure):
        return False
    authority = context["authority_reader"]() or {}
    store["converge_node_rollouts"](node_id)
    converge_proposals = store.get("converge_node_proposals")
    if callable(converge_proposals):
        converge_proposals(node_id)
    count = ensure(node_id, int(authority.get("authority_term") or 0))
    stage = (context.get("rollouts") or {}).get("stage_due")
    if callable(stage):
        stage(limit=64)
    return bool(count)

def handle_config_commit(context, result):
    """Refresh projections and stage persisted changed scopes after a commit."""
    return _after_config_commit(context, result)


def handle_legacy_registration(context, message):
    """Register v0082/v2 Workers and ensure their independent scope rollouts."""
    result = context["nodes"]["register"](message)
    if not result.get("ok") and result.get("code") == "authorization_denied":
        decision = _auto_approve_worker_if_proven(
            context, str(message.get("source_node") or ""))
        if decision and decision.get("ok"):
            result = context["nodes"]["register"](message)
    if result.get("ok"):
        _resync_worker(context, str(message.get("source_node") or ""))
    _publish_event(context, "node.registration", {
        "node_id": message.get("source_node"),
        "accepted": bool(result.get("ok")), "code": result.get("code"),
    }, principal={"subject": "worker:" + str(message.get("source_node") or "unknown")})
    return result


def handle_legacy_heartbeat(context, message):
    """Record Worker replica anchors and converge or restage current scopes."""
    result = context["nodes"]["heartbeat"](message)
    if result.get("ok"):
        _resync_worker(context, str(message.get("source_node") or ""))
        _publish_event(context, "node.heartbeat", {
            "node_id": message.get("source_node"),
            "observed_state": result.get("observed_state"),
        }, principal={"subject": "worker:" + str(message.get("source_node") or "unknown")})
    return result


def _legacy_proposal(context, message, payload, source, snapshot):
    prepared = context["proposal_ingest"]["record_legacy"](
        source, payload, snapshot["config_data"])
    if not prepared.get("ok"):
        return prepared
    if prepared.get("duplicate"):
        previous = prepared.get("result") or {}
        return copy.deepcopy(previous) if previous else make_success(duplicate=True)
    result = context["config_authority"]["propose_action_plan"]({
        "base_revision": payload.get("authority_revision"),
        "actions": copy.deepcopy(payload.get("actions") or []),
        "principal_subject": "worker:" + source,
        "request_id": prepared["operation_id"],
        "source_node_id": source,
    })
    context["proposal_ingest"]["complete"](prepared["operation_id"], result)
    return result


def _v2_proposal(context, payload, source):
    prepared = context["proposal_ingest"]["prepare_v2"](source, payload)
    if not prepared.get("ok"):
        return prepared
    result = context["config_authority"]["commit_prepared"]({
        "base_revision": prepared["base_global_revision"],
        "config": prepared["config"],
        "changes": copy.deepcopy(payload.get("operations") or []),
        "principal_subject": "worker:" + source,
        "request_id": prepared["operation_id"],
        "source_node_id": source,
        "reason": payload.get("reason") or "worker scope proposal",
        "rollout_targets": None,
    })
    context["proposal_ingest"]["complete"](prepared["operation_id"], result)
    return result


def handle_legacy_config_proposal(context, message):
    """Relay legacy Master mode or acknowledge a node-authoritative notice."""
    if context.get("authority_mode", "node_local") == "node_local":
        return context["node_local"]["accept_worker_local_notice"](message)
    source = str(message.get("source_node") or "")
    node = context["nodes"]["get"](source)
    if not node or node.get("admission_state") != "APPROVED":
        return make_error("authorization_denied", "worker is not admitted")
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    snapshot = context["config_reader"]()
    if not snapshot:
        return make_error("not_found", "authority config is unavailable")
    if payload.get("scope") and isinstance(payload.get("operations"), list):
        result = _v2_proposal(context, payload, source)
    elif payload.get("operation") == "action_plan":
        result = _legacy_proposal(context, message, payload, source, snapshot)
    else:
        return make_error("invalid_payload", "proposal shape is unsupported")
    changed = result.get("changed") is not False
    if result.get("ok") and result.get("revision") and changed:
        _after_config_commit(context, result)
        result["post_commit"] = {
            "authority_revision": result.get("revision"),
            "fingerprint": result.get("fingerprint"),
        }
        _publish_event(context, "config.rollout", {
            "revision": result.get("revision"),
            "rollout_ids": result.get("rollout_ids") or (),
            "fingerprint": result.get("fingerprint"), "state": "staged",
        }, principal={"subject": "worker:" + source})
    elif result.get("ok") and result.get("revision"):
        authority = context["authority_reader"]() or {}
        result["rollout_staged"] = False
        result["rollout_required"] = False
        result["post_commit"] = {
            "authority_revision": result.get("revision"),
            "fingerprint": result.get("fingerprint") or authority.get("config_fingerprint"),
        }
    return result


def handle_mirror_announce(context, message):
    """Compare a Worker mirror head without updating Mirror content."""
    return context["node_local"]["handle_mirror_announce"](message)


def handle_mirror_snapshot(context, message):
    """Persist validated Worker mirror content before exact acknowledgement."""
    return context["node_local"]["handle_mirror_snapshot"](message)


def handle_legacy_actuator_route(context, message):
    """Route a Worker-originated remote actuator request to its hardware owner."""
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    task = payload.get("task") if isinstance(payload.get("task"), dict) else {}
    metadata = task.get("meta") if isinstance(task.get("meta"), dict) else {}
    runtime_metadata = task.get("metadata") if isinstance(task.get("metadata"), dict) else {}
    runtime_metadata = runtime_metadata.get("cross_runtime") \
        if isinstance(runtime_metadata.get("cross_runtime"), dict) else {}
    operation_id = str(payload.get("operation_id") or task.get("task_uuid") or uuid.uuid4())
    authority = context["authority_reader"]() or {}
    envelope = make_envelope(
        "actuator.request",
        {"service": "worker", "instance_id": message["source_node"]},
        {"service": "master", "instance_id": context["instance_id"]},
        site_id=context["site_id"], idempotency_key=operation_id,
        principal={"subject": "worker:" + message["source_node"]},
        authority_term=authority.get("authority_term", 0),
        config_revision=authority.get("config_revision", 0),
        payload={
            "controller_uuid": task.get("controller_uuid"),
            "actuator_id": task.get("actuator_id"),
            "value": task.get("control_value"),
            "idempotent": bool(task.get("idempotent", False)),
            "device_command_id_supported": bool(task.get("device_command_id_supported", False)),
            "execution_scope": payload.get("execution_scope") or task.get(
                "execution_scope") or metadata.get("execution_scope") or runtime_metadata.get(
                "execution_scope"),
            "execution_epoch": payload.get("execution_epoch") or task.get("execution_epoch"),
            "cross_domain_id": task.get("cross_domain_id") or metadata.get(
                "cross_domain_id") or runtime_metadata.get("domain_id"),
            "cross_revision": metadata.get("cross_revision") or task.get("cross_revision"),
            "cross_fingerprint": metadata.get("cross_fingerprint") or task.get(
                "cross_fingerprint"),
            "distributed_protocol": payload.get("protocol"),
        }, ttl_s=60.0,
    )
    result = context["operations"]["request"](envelope)
    if result.get("ok") and result.get("worker_command"):
        staged = _stage_worker_command(context, result["worker_command"])
        if not staged.get("ok"):
            return _outbox_stage_failure(staged, result.get("operation"))
    return result


def handle_legacy_actuator_result(context, message):
    """Accept only the fenced owner Worker's terminal hardware result."""
    result = context["operations"]["apply_worker_result"](message)
    if result.get("ok"):
        authority = _owned_authority(context)
        if not authority:
            return make_error(
                "not_config_authority", "operation result arrived after authority loss")
        operation = result.get("operation") \
            if isinstance(result.get("operation"), dict) else {}
        operation_id = str(operation.get("operation_id") or "")
        authority_term = operation.get("authority_term")
        if not operation_id or authority_term is None:
            return make_error(
                "internal_error", "accepted operation result lacks persisted identity")
        execute_message_id = "actuator-execute:{}:{}".format(
            authority_term, operation_id)
        marked = context["authority_store"]["mark_outbox"](
            execute_message_id, state="ACKED", error_code=None,
            **_authority_write_fence(context, authority))
        if not marked:
            return make_error(
                "not_config_authority", "operation outbox ACK lost its authority fence")
        if not result.get("duplicate"):
            _publish_event(context, "operation.result", {
                "operation_id": operation_id,
                "terminal_state": operation.get("state"),
                "result": operation.get("result") or {},
            }, principal={
                "subject": "worker:" + str(operation.get("owner_node_id") or "unknown"),
            })
    return result


def _legacy_sensor_source_sequence(payload):
    """Return the strict monotone sensor-store sequence from supported Workers."""
    if not isinstance(payload, dict):
        return None
    sequence = payload.get("sequence")
    if sequence is None:
        sequence = payload.get("sensor_store_sequence")
    if isinstance(sequence, bool) or not isinstance(sequence, int) or sequence <= 0:
        return None
    return sequence


def handle_legacy_sensor_event(context, envelope):
    """Persist latest telemetry and route it; never evaluate a rule."""
    if str(envelope.get("event_type") or "").upper() != "SENSOR_VALUE_UPDATE":
        return True
    outer = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    payload = outer.get("payload") if isinstance(outer.get("payload"), dict) else outer
    source_sequence = _legacy_sensor_source_sequence(payload)
    if source_sequence is None:
        return False
    owner = context["ownership"]["resolve"](payload.get("controller_uuid"))
    if not owner or owner.get("owner_node_id") != envelope.get("source_node"):
        return False
    authority = context["authority_reader"]() or {}
    result = context["node_store"]["upsert_sensor"]({
        "site_id": context["site_id"],
        "controller_uuid": payload.get("controller_uuid"),
        "sensor_id": payload.get("sensor_id"),
        "source_node_id": envelope.get("source_node"),
        "authority_term": authority.get("authority_term"),
        "config_revision": authority.get("config_revision"),
        "value": copy.deepcopy(payload.get("value")),
        "quality": payload.get("quality", "unknown"),
        "source_sequence": source_sequence,
        "measured_at": payload.get("timestamp"),
    })
    if result.get("ok") and result.get("updated"):
        fact = copy.deepcopy(payload)
        fact["event_id"] = str(envelope.get("message_id") or uuid.uuid4())
        context["cross_router"]["route"](
            fact, source_node=envelope.get("source_node"))
        _publish_event(context, "sensor.value", {
            "controller_uuid": payload.get("controller_uuid"),
            "sensor_id": payload.get("sensor_id"), "value": payload.get("value"),
            "quality": payload.get("quality", "unknown"),
            "source_node_id": envelope.get("source_node"),
        }, principal={"subject": "worker:" + str(envelope.get("source_node") or "unknown")})
    return bool(result.get("ok"))


def _publish_operation_failure(context, operation_id, result):
    _publish_event(context, "operation.result", {
        "operation_id": operation_id,
        "terminal_state": "FAILED",
        "result": copy.deepcopy(result),
    }, principal={"subject": "worker:outbox-ack"})


def _apply_authority_transition_ack(context, record, result):
    """Advance A5/A6 only from an exact correlated Worker result."""
    event_type = str(record.get("event_type") or "")
    transitions = context.get("authority_transitions") or {}
    payload = (record.get("envelope") or {}).get("payload") or {}
    if event_type == protocol_contracts.AUTHORITY_TRANSFER:
        transfer = context["authority_store"]["get_authority_transfer"](
            payload.get("transfer_id"))
        if not transfer:
            return make_error("not_found", "Authority transfer record is unavailable")
        return transitions["complete_transfer"](transfer, result)
    if event_type == protocol_contracts.RESTORE_REQUEST:
        restore = context["authority_store"]["get_worker_restore"](
            payload.get("restore_id"))
        if not restore:
            return make_error("not_found", "Worker restore record is unavailable")
        return transitions["complete_restore_activation"](restore, result)
    if event_type == protocol_contracts.RESTORE_ACTIVATE:
        restore = context["authority_store"]["get_worker_restore"](
            payload.get("restore_id"))
        if not restore:
            return make_error("not_found", "Worker restore record is unavailable")
        return transitions["complete_restore_preflight"](restore, result)
    return None

def _publish_config_proposal_result(context, record, result):
    """Publish the exact Worker decision for one node-local relayed proposal."""
    if context.get("authority_mode", "node_local") != "node_local" \
            or record.get("event_type") != protocol_contracts.CONFIG_PROPOSE:
        return False
    command = record.get("envelope") \
        if isinstance(record.get("envelope"), dict) else {}
    payload = command.get("payload") \
        if isinstance(command.get("payload"), dict) else {}
    node_id = str(record.get("target_instance_id") or "")
    accepted = result.get("ok") is True
    proposal_id = str(
        result.get("proposal_id") or payload.get("proposal_id") or "")
    operation_id = str(
        result.get("operation_id") or payload.get("operation_id") or "")
    return _publish_event(context, "config.proposal.result", {
        "node_id": node_id,
        "scope": str(result.get("scope") or payload.get("scope") or ""),
        "relay_message_id": str(record.get("message_id") or ""),
        "proposal_id": proposal_id,
        "operation_id": operation_id,
        "accepted": accepted,
        "code": str(result.get("code") or (
            "accepted" if accepted else "proposal_rejected")),
        "authority_epoch": result.get("authority_epoch"),
        "scope_revision": result.get("scope_revision"),
        "fingerprint": result.get("fingerprint"),
        "result": copy.deepcopy(result),
    }, correlation_id=proposal_id or None,
       causation_id=proposal_id or None,
       principal={"subject": "worker:" + node_id})


def handle_legacy_ack(context, envelope):
    """Correlate a Worker ACK to durable outbox, rollout or operation state."""
    authority = _owned_authority(context)
    if not authority:
        return False
    record = context["authority_store"]["get_outbox"](envelope.get("message_id"))
    if not record or record.get("target_service") != "worker":
        return False
    command = record["envelope"]
    expected = {
        "source_node": context["legacy_coordinator_node_id"],
        "target_node": record["target_instance_id"],
        "message_id": record["message_id"], "command": record["event_type"],
    }
    checked = validate_worker_ack(
        envelope, expected, site_id=context["site_id"],
        authority_term=authority.get("authority_term"))
    if not checked.get("ok"):
        return False
    result = envelope.get("result") if isinstance(envelope.get("result"), dict) else {}
    transition_result = _apply_authority_transition_ack(context, record, result)
    if transition_result is not None and not transition_result.get("ok"):
        result = transition_result
    marked = context["authority_store"]["mark_outbox"](
        record["message_id"], state="ACKED" if result.get("ok") else "FAILED",
        error_code=result.get("code"),
        **_authority_write_fence(context, authority))
    if not marked:
        return False
    _publish_config_proposal_result(context, record, result)
    rollout_handler = (context.get("rollouts") or {}).get("handle_ack")
    if (
        context.get("authority_mode", "node_local") == "master_global"
        and callable(rollout_handler)
        and command.get("rollout_id")
    ):
        if not rollout_handler(
                command, result, **_authority_write_fence(context, authority)):
            return False
        _publish_event(context, "config.rollout", {
            "node_id": record["target_instance_id"],
            "rollout_id": command.get("rollout_id"),
            "state": "applied" if result.get("ok") else "failed",
            "result": result,
        }, principal={"subject": "worker:" + record["target_instance_id"]})
    if record["event_type"] == "distributed.actuator.execute":
        operation_id = command.get("payload", {}).get("operation_id")
        if result.get("ok"):
            context["operation_store"]["transition"](
                operation_id, "DISPATCHED", details={"worker_ack": True},
                **_operation_write_fence(context, authority))
        else:
            failure = copy.deepcopy(result)
            failure["ok"] = False
            failure.setdefault("code", "worker_rejected")
            failure.setdefault("error", "worker rejected actuator execution")
            moved = context["operation_store"]["transition"](
                operation_id, "FAILED", result=failure,
                details={"worker_ack": True, "code": failure["code"]},
                **_operation_write_fence(context, authority),
            )
            if moved.get("ok") and not moved.get("duplicate"):
                _publish_operation_failure(context, operation_id, failure)
    return True


def expire_due_operations(context, **kwargs):
    """Expire stale actuator intents and mirror their terminal result to Proxy."""
    result = context["operation_store"]["expire_due"](
        context["site_id"], limit=kwargs.get("limit", 64))
    for operation in result.get("operations") or ():
        _publish_event(context, "operation.result", {
            "operation_id": operation["operation_id"],
            "terminal_state": operation["state"],
            "result": operation.get("result") or {},
        }, principal={"subject": "system:operation-expiry"})
    return int(result.get("count") or 0)


def publish_due_outbox(context, **kwargs):
    """Stage scope rollouts, then publish a bounded durable outbox batch."""
    authority = context["authority_reader"]()
    owned = (
        authority and authority.get("lease_active")
        and authority.get("leader_instance_id") == context["instance_id"]
        and authority.get("lease_token")
    )
    if not owned:
        return 0
    stage = (context.get("rollouts") or {}).get("stage_due")
    if (
        context.get("authority_mode", "node_local") == "master_global"
        and callable(stage)
    ):
        stage(limit=kwargs.get("limit", 64))
    records = context["authority_store"]["due_outbox"](
        limit=kwargs.get("limit", 64), site_id=context["site_id"],
        instance_id=context["instance_id"], lease_token=authority["lease_token"],
        authority_term=authority["authority_term"],
        minimum_lease_remaining_s=kwargs.get("minimum_lease_remaining_s", 1.0),
    )
    published = 0
    for record in records:
        if record["target_service"] != "worker":
            continue
        command = record["envelope"]
        command_expiry = command.get("expires_at_s")
        if (
                record["event_type"] == "distributed.actuator.execute"
                and command_expiry is not None
                and float(command_expiry) <= time.time()):
            context["authority_store"]["mark_outbox"](
                record["message_id"], state="CANCELLED",
                error_code="operation_expired",
                **_authority_write_fence(context, authority))
            continue
        metadata = copy.deepcopy(command.get("metadata") or {})
        metadata["authority_term"] = int(authority["authority_term"])
        response = context["legacy_mqtt"]["publish_command"](
            command["target_node"], command["command"], payload=command["payload"],
            message_id=command["message_id"],
            principal=context["legacy_coordinator_node_id"],
            expires_at_s=command_expiry or time.time() + context["command_ttl_s"],
            metadata=metadata,
        )
        state = "SENT" if response.get("ok") else "PENDING"
        marked = context["authority_store"]["mark_outbox"](
            record["message_id"], state=state, retry_s=context["retry_s"],
            error_code=None if response.get("ok") else "transport_unavailable",
            **_authority_write_fence(context, authority))
        if not marked:
            continue
        if response.get("ok") and record["event_type"] == "distributed.actuator.execute":
            operation_id = command.get("payload", {}).get("operation_id")
            moved = context["operation_store"]["transition"](
                operation_id, "DISPATCHED",
                details={"transport_published": True, "message_id": record["message_id"]},
                **_operation_write_fence(context, authority))
            if not moved.get("ok"):
                continue
        published += int(bool(response.get("ok")))
    return published

def register_worker_handlers(context):
    """Bind v0082-compatible command names to the standalone Master."""
    bindings = {
        "distributed.node.register": handle_legacy_registration,
        "distributed.node.heartbeat": handle_legacy_heartbeat,
        "distributed.config.propose": handle_legacy_config_proposal,
        "distributed.mirror.announce": handle_mirror_announce,
        "distributed.mirror.snapshot": handle_mirror_snapshot,
        "distributed.actuator.route": handle_legacy_actuator_route,
        "distributed.actuator.result": handle_legacy_actuator_result,
    }
    for command, handler in bindings.items():
        context["legacy_mqtt"]["register_command_handler"](
            command, partial(handler, context))
    context["legacy_mqtt"]["register_ack_handler"](partial(handle_legacy_ack, context))
    context["legacy_mqtt"]["register_event_handler"](
        partial(handle_legacy_sensor_event, context))
    context["legacy_mqtt"]["register_identity_heartbeat_handler"](
        partial(handle_broker_identity_heartbeat, context))
    return True


def set_event_publisher(context, publisher):
    """Attach the Master-to-Proxy logical event port."""
    context["event_publisher"] = publisher
    return True


def create_coordinator_service(**kwargs):
    """Create the explicit non-executing coordinator capability dictionary."""
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "authority_mode": str(kwargs.get("authority_mode") or "node_local"),
        "allowed_worker_node_ids": tuple(sorted(
            str(node_id)
            for node_id in kwargs.get("worker_compatibility", {}).get(
                "allowed_worker_node_ids", ())
            if str(node_id)
        )),
        "legacy_coordinator_node_id": kwargs["legacy_coordinator_node_id"],
        "authority_store": kwargs["authority_store"],
        "node_store": kwargs["node_store"],
        "operation_store": kwargs["operation_store"],
        "audit": kwargs["audit"],
        "config_authority": kwargs["config_authority"],
        "config_reader": kwargs["config_reader"],
        "authority_reader": kwargs["authority_reader"],
        "nodes": kwargs["nodes"],
        "ownership": kwargs["ownership"],
        "operations": kwargs["operations"],
        "admin": kwargs["admin"],
        "legacy_mqtt": kwargs["legacy_mqtt"],
        "proposal_ingest": kwargs["proposal_ingest"],
        "rollouts": kwargs["rollouts"],
        "cross_router": kwargs["cross_router"],
        "derived_refresh": kwargs.get("derived_refresh"),
        "runtime_snapshot": kwargs["runtime_snapshot"],
        "command_ttl_s": float(kwargs.get("command_ttl_s", 60.0)),
        "retry_s": float(kwargs.get("retry_s", 2.0)),
        "auto_admit_broker_bound_workers": bool(
            kwargs.get("worker_compatibility", {}).get(
                "auto_admit_broker_bound_workers", False)),
        "identity_proof_max_age_s": max(
            1.0, float(kwargs.get("worker_compatibility", {}).get(
                "identity_proof_max_age_s", 45.0))),
        "broker_identity_proofs": {},
        "event_publisher": None,
        "node_local": None,
        "authority_transitions": None,
    }
    context["node_local"] = create_node_local_coordination(
        site_id=context["site_id"],
        instance_id=context["instance_id"],
        authority_reader=context["authority_reader"],
        node_reader=context["nodes"]["get"],
        mirror_store=context["authority_store"],
        stage_worker_command=partial(_stage_worker_command, context),
        derived_refresh=context.get("derived_refresh"),
    )
    register_worker_handlers(context)
    return {
        "context": context,
        "handle_control": partial(handle_control_command, context),
        "publish_due_outbox": partial(publish_due_outbox, context),
        "handle_config_commit": partial(handle_config_commit, context),
        "expire_operations": partial(expire_due_operations, context),
        "set_event_publisher": partial(set_event_publisher, context),
        "stage_worker_command": partial(_stage_worker_command, context),
        "snapshot": context["runtime_snapshot"],
    }

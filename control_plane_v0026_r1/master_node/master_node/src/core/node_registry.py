# -*- coding: utf-8 -*-

# master_node/src/core/node_registry.py

"""Worker admission and heartbeat policy above the PostgreSQL registry."""

import copy

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library._generic_system_contract_library.protocol_contracts import certificate_uri, normalize_token
from src.library._generic_system_contract_library.protocol_contracts import (
    PROTOCOL_V1,
    normalize_registration,
    selected_protocol,
)


def register_worker(context, message):
    """Register a broker-authenticated worker without auto-admission."""
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    source = normalize_token(message.get("source_node"), "source_node")
    claimed = normalize_token(payload.get("node_id"), "node_id")
    if source != claimed:
        return make_error("identity_mismatch", "source node and payload node differ")
    expected_identity = certificate_uri(context["site_id"], "worker", source)
    identity = message.get("certificate_identity")
    if not identity and context["trust_broker_acl_identity"]:
        identity = expected_identity
    if identity != expected_identity:
        return make_error(
            "identity_mismatch", "worker certificate identity does not match its node ID")
    try:
        normalized = normalize_registration(
            payload, message_protocol=payload.get("protocol") or PROTOCOL_V1)
    except (TypeError, ValueError) as exc:
        return make_error(str(exc), "worker registration contract is incompatible")
    request = {
        "site_id": context["site_id"],
        "node_id": source,
        "certificate_identity": identity,
        "software_version": payload.get("software_version"),
        "protocol_version": selected_protocol(normalized),
        "protocol_versions": normalized["protocol_versions"],
        "contracts_version": normalized["contracts_version"],
        "authority_term": payload.get("coordinator_term", 0),
        "config_revision": normalized["observed_global_revision"],
        "config_fingerprint": payload.get("fingerprint"),
        "capabilities": {
            "features": copy.deepcopy(normalized["capabilities"]),
            "reported_features": copy.deepcopy(payload.get("capabilities") or {}),
            "controller_uuids": copy.deepcopy(payload.get("controller_uuids") or []),
        },
        "runtime_nonce": payload.get("runtime_nonce"),
        "observed_global_revision": normalized["observed_global_revision"],
        "node_replica": normalized["node_replica"],
        "cross_replica": normalized["cross_replica"],
        "live_config_revision": normalized["live_config_revision"],
        "pending_proposal_count": normalized["pending_proposal_count"],
        "max_packet_size": normalized["max_packet_size"],
    }
    stored = context["store"]["register"](request)
    if not stored.get("ok"):
        return stored
    node = stored["node"]
    if node.get("admission_state") != "APPROVED":
        return make_error("authorization_denied", "worker is pending explicit admission")
    return _registration_result(context, node)


def _registration_result(context, node):
    """Return Registry fencing separately from Worker config authority."""
    authority = context["authority_reader"]() or {}
    floor_reader = context["store"].get("sensor_sequence_floor")
    sequence_floor = floor_reader(
        context["site_id"], node["node_id"]) if callable(floor_reader) else 0
    coordinator_term = int(authority.get("authority_term") or 0)
    if context.get("authority_mode", "node_local") == "node_local":
        mirror = context["scope_reader"]("node:" + str(node["node_id"])) \
            if callable(context.get("scope_reader")) else None
        return make_success(
            node_id=node["node_id"],
            coordinator_node_id=context["coordinator_node_id"],
            coordinator_term=coordinator_term,
            registry_term=coordinator_term,
            authority_mode="node_local",
            config_authority="worker",
            authority_revision=0,
            global_revision=0,
            fingerprint=None,
            mirror_head={
                "scope": (mirror or {}).get("scope"),
                "authority_epoch": int(
                    (mirror or {}).get("authority_epoch") or 0),
                "scope_revision": int(
                    (mirror or {}).get("scope_revision") or 0),
                "fingerprint": (mirror or {}).get("fingerprint"),
            },
            desired_state=node.get("desired_state"),
            observed_state=node.get("observed_state"),
            sensor_sequence_floor=int(sequence_floor or 0),
        )
    return make_success(
        node_id=node["node_id"],
        coordinator_node_id=context["coordinator_node_id"],
        coordinator_term=coordinator_term,
        authority_revision=int(authority.get("config_revision") or 0),
        fingerprint=authority.get("config_fingerprint"),
        global_revision=int(authority.get("config_revision") or 0),
        desired_state=node.get("desired_state"),
        observed_state=node.get("observed_state"),
        sensor_sequence_floor=int(sequence_floor or 0),
    )

def _node_replica_observed_state(context, node_id, normalized, reported_state):
    """Expose ACTIVE only after the exact independent Node shard is live."""
    state = str(reported_state or "ACTIVE").upper()
    if state != "ACTIVE" or not callable(context.get("scope_reader")):
        return state, None
    scope = "node:" + str(node_id)
    target = context["scope_reader"](scope)
    replica = normalized.get("node_replica") if isinstance(normalized, dict) else {}
    replica = replica if isinstance(replica, dict) else {}
    revision_matches = bool(
        target
        and int(replica.get("revision") or 0)
        == int(target.get("scope_revision") or 0)
    )
    converged = bool(
        target
        and str(replica.get("scope") or "") == scope
        and revision_matches
        and str(replica.get("fingerprint") or "") == str(target.get("fingerprint") or "")
    )
    detail = {
        "scope": scope,
        "converged": converged,
        "observed_revision": int(replica.get("revision") or 0),
        "target_revision": int((target or {}).get("scope_revision") or 0),
        "revision_matches": revision_matches,
        "fingerprint_matches": bool(
            target and replica.get("fingerprint") == target.get("fingerprint")),
    }
    return ("ACTIVE" if converged else "DEGRADED"), detail


def record_worker_heartbeat(context, message):
    """Validate an admitted worker heartbeat and return current desired state."""
    payload = message.get("payload") if isinstance(message.get("payload"), dict) else {}
    source = normalize_token(message.get("source_node"), "source_node")
    claimed = normalize_token(payload.get("node_id"), "node_id")
    if source != claimed:
        return make_error("identity_mismatch", "heartbeat identity differs")
    capability_update = None
    try:
        normalized = normalize_registration(
            payload, message_protocol=payload.get("protocol") or PROTOCOL_V1)
    except (TypeError, ValueError) as exc:
        return make_error(str(exc), "worker registration contract is incompatible")
    if "capabilities" in payload or "controller_uuids" in payload:
        capability_update = {
            "features": copy.deepcopy(normalized["capabilities"]),
            "reported_features": copy.deepcopy(payload.get("capabilities") or {}),
            "controller_uuids": copy.deepcopy(payload.get("controller_uuids") or []),
        }
    observed_state, sync_detail = _node_replica_observed_state(
        context, source, normalized, payload.get("state", "ACTIVE"))
    health = copy.deepcopy(payload.get("health") or {})
    if sync_detail is not None:
        health["master_node_scope_sync"] = sync_detail
    request = {
        "site_id": context["site_id"],
        "node_id": source,
        "message_id": str(message.get("message_id") or ""),
        "authority_term": payload.get("coordinator_term", 0),
        "config_revision": payload.get("authority_revision", payload.get("local_config_revision", 0)),
        "config_fingerprint": payload.get("fingerprint"),
        "observed_state": observed_state,
        "health": health,
        "capabilities": capability_update,
        "protocol_version": selected_protocol(normalized) if normalized else None,
        "protocol_versions": normalized["protocol_versions"] if normalized else None,
        "contracts_version": normalized["contracts_version"] if normalized else None,
        "observed_global_revision": normalized["observed_global_revision"] if normalized else None,
        "node_replica": normalized["node_replica"] if normalized else None,
        "cross_replica": normalized["cross_replica"] if normalized else None,
        "live_config_revision": normalized["live_config_revision"] if normalized else None,
        "pending_proposal_count": normalized["pending_proposal_count"] if normalized else None,
        "max_packet_size": normalized["max_packet_size"] if normalized else None,
        "runtime_nonce": payload.get("runtime_nonce"),
    }
    stored = context["store"]["heartbeat"](request)
    if not stored.get("ok"):
        return stored
    return _registration_result(context, stored["node"])


def decide_worker_admission(context, request):
    """Apply an authorized explicit admission decision."""
    prepared = copy.deepcopy(request)
    prepared["site_id"] = context["site_id"]
    return context["store"]["decide_admission"](prepared)


def set_worker_state(context, request):
    """Set desired state through the registry state machine."""
    prepared = copy.deepcopy(request)
    prepared["site_id"] = context["site_id"]
    return context["store"]["set_desired_state"](prepared)


def create_node_registry(**kwargs):
    """Create the functional worker-registry capability dictionary."""
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "coordinator_node_id": kwargs.get("coordinator_node_id") or kwargs["instance_id"],
        "authority_mode": str(kwargs.get("authority_mode") or "node_local"),
        "trust_broker_acl_identity": bool(kwargs.get("trust_broker_acl_identity", False)),
        "store": kwargs["store"],
        "authority_reader": kwargs["authority_reader"],
        "scope_reader": kwargs.get("scope_reader"),
    }
    return {
        "context": context,
        "register": partial(register_worker, context),
        "heartbeat": partial(record_worker_heartbeat, context),
        "decide_admission": partial(decide_worker_admission, context),
        "set_desired_state": partial(set_worker_state, context),
        "list": partial(kwargs["store"]["list"], context["site_id"]),
        "get": partial(kwargs["store"]["get"], context["site_id"]),
    }

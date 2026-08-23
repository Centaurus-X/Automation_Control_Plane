# -*- coding: utf-8 -*-

"""Execution-scope activation and epoch fencing without rule execution."""

import copy

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library._generic_system_contract_library.protocol_contracts import PROTOCOL_V2


def _execution_entities(scope_snapshot):
    """Return true execution anchors, not replicated dependency records.

    V9 process rows carry administrative ``owner_node_id`` metadata but remain
    shared definitions.  Execution is fenced exclusively by Rules and concrete
    process-state rows, which may partition one process across Node/Cross
    scopes without creating a second writer.
    """
    config = scope_snapshot.get("config") or {}
    values = []
    for section, id_field, prefix in (
        ("rules", "rule_id", "rule"),
        ("process_states", "state_id", "state"),
    ):
        for row in config.get(section) or []:
            if isinstance(row, dict) and row.get(id_field) is not None:
                values.append(prefix + ":" + str(row[id_field]))
    return tuple(sorted(set(values)))


def plan_activations(scope_snapshots, previous):
    """Create the active map and reject unsafe distributed scope movement."""
    prior = previous if isinstance(previous, dict) else {}
    result = {}
    for scope, snapshot in sorted(scope_snapshots.items()):
        for entity_key in _execution_entities(snapshot):
            if entity_key in result and result[entity_key]["active_scope"] != scope:
                raise ValueError("execution entity is assigned to multiple scopes: " + entity_key)
            old = prior.get(entity_key) if isinstance(prior.get(entity_key), dict) else {}
            epoch = int(old.get("execution_epoch") or 0)
            if old.get("active_scope") != scope or epoch < 1:
                epoch += 1
            result[entity_key] = {
                "entity_key": entity_key,
                "active_scope": scope,
                "execution_epoch": epoch,
                "state": "active",
            }
    moved = tuple(sorted(
        entity_key for entity_key, activation in result.items()
        if entity_key in prior
        and isinstance(prior[entity_key], dict)
        and prior[entity_key].get("state") == "active"
        and prior[entity_key].get("active_scope") != activation["active_scope"]
    ))
    removed = tuple(sorted(
        entity_key for entity_key, activation in prior.items()
        if isinstance(activation, dict)
        and activation.get("state") == "active"
        and entity_key not in result
    ))
    if moved or removed:
        raise ValueError(
            "online execution-scope migration requires staged READY/activate/retire; "
            "refusing unsafe immediate movement: " + ",".join(moved + removed))
    return result


def validate_execution_intent(activations, intent):
    """Fence one Cross-to-owner actuator intent by scope and epoch."""
    entity_key = str(intent.get("entity_key") or "")
    activation = activations.get(entity_key)
    if not activation or activation.get("state") != "active":
        return make_error("activation_missing", "execution entity is not active")
    if str(intent.get("execution_scope") or "") != activation["active_scope"]:
        return make_error("stale_execution_scope", "execution scope differs")
    if int(intent.get("execution_epoch") or -1) != int(activation["execution_epoch"]):
        return make_error("stale_execution_epoch", "execution epoch differs")
    return make_success(activation=copy.deepcopy(activation))


def validate_scope_intent(scope_snapshot, source_node, intent):
    """Validate legacy revision/fingerprint and v2 epoch Cross fences."""
    if not scope_snapshot or scope_snapshot.get("scope_kind") != "cross":
        return make_error("activation_missing", "Cross execution scope is not active")
    if str(scope_snapshot.get("owner_node_id") or "") != str(source_node):
        return make_error("not_authorized", "Cross intent source is not the scope owner")
    revision = intent.get("cross_revision")
    if revision is not None and int(revision) != int(scope_snapshot["scope_revision"]):
        return make_error("stale_execution_scope", "Cross scope revision differs")
    supplied_fingerprint = intent.get("cross_fingerprint")
    if supplied_fingerprint and supplied_fingerprint != scope_snapshot["fingerprint"]:
        return make_error("stale_execution_scope", "Cross scope fingerprint differs")
    supplied_epoch = intent.get("execution_epoch")
    if intent.get("distributed_protocol") == PROTOCOL_V2 and supplied_epoch is None:
        return make_error("stale_execution_epoch", "v2 Cross intent lacks its execution epoch")
    if supplied_epoch is not None and int(supplied_epoch) != int(scope_snapshot["execution_epoch"]):
        return make_error("stale_execution_epoch", "Cross execution epoch differs")
    return make_success(
        execution_scope=scope_snapshot["scope"],
        execution_epoch=int(scope_snapshot["execution_epoch"]),
    )

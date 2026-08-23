# -*- coding: utf-8 -*-

"""Durable owner-checked v2 and v0082 worker proposal ingestion."""

import copy

from functools import partial

from src.library._generic_system_contract_library import config_graph, entity_registry
from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library._generic_type_library import fingerprint
from src.library._generic_system_contract_library.config_sharding_contracts import apply_operations


WORKER_RUNTIME_SECTIONS = ("process_states",)
WORKER_RUNTIME_POINTERS = ("/value_id", "/parameters")
WORKER_FILE_SECTIONS = (
    "actuator_types", "actuators", "control_method_templates",
    "control_methods", "controllers", "filters", "local_node",
    "modbus_functions", "nodes", "parameter_generic_templates",
    "pid_configs", "process_modes", "process_states", "process_values",
    "processes", "rule_engine", "rules", "scaling_profiles",
    "schema_mappings", "sensor_types", "sensors", "site_controller_map",
    "sites", "timers", "virtual_controllers",
)


def _scope_owner(scope):
    text = str(scope or "")
    if text.startswith("node:"):
        return text.split(":", 1)[1]
    if text.startswith("cross:"):
        return text.split(":", 1)[1].split(".", 1)[0]
    return None


def _rule_by_id(config, rule_id):
    for rule in config.get("rules") or []:
        if isinstance(rule, dict) and str(rule.get("rule_id")) == str(rule_id):
            return rule
    return None


def _target_state_ids(config, actions):
    state_ids = set()
    process_ids = set()
    for item in actions:
        action = item.get("action") if isinstance(item, dict) else None
        action = action if isinstance(action, dict) else item
        if not isinstance(action, dict):
            raise ValueError("action-plan entry is invalid")
        state_ids.update(str(value) for value in action.get("state_ids") or ())
        process_ids.update(str(value) for value in action.get("p_ids") or ())
    for state in config.get("process_states") or []:
        if isinstance(state, dict) and str(state.get("p_id")) in process_ids:
            state_ids.add(str(state.get("state_id")))
    return state_ids


def authorize_legacy_action_plan(config, source_node, actions):
    """Authorize the exact v0082 action plan by explicit execution ownership."""
    if not isinstance(actions, list) or not actions:
        return make_error("invalid_payload", "action plan must be a non-empty list")
    try:
        state_ids = _target_state_ids(config, actions)
    except ValueError as exc:
        return make_error("invalid_payload", str(exc))
    if not state_ids:
        return make_error("invalid_payload", "action plan has no state target")
    graph = config_graph.build_graph(config)
    for item in actions:
        rule_id = item.get("rule_id") if isinstance(item, dict) else None
        if rule_id is None:
            continue
        rule = _rule_by_id(config, rule_id)
        if not rule or rule.get("deployment_status") not in ("active_node", "active_cross"):
            return make_error("not_authorized", "proposal rule is not active")
        if str(rule.get("execution_owner") or "") != str(source_node):
            return make_error("not_authorized", "proposal rule belongs to another execution owner")
    for state_id in state_ids:
        state = graph.get("states", {}).get(state_id)
        if not isinstance(state, dict):
            return make_error("invalid_payload", "proposal targets an unknown state")
        if state.get("errors"):
            return make_error("not_authorized", "proposal state has unresolved ownership")
        owners = {str(value) for value in state.get("node_ids") or ()}
        if not owners or str(source_node) not in owners:
            return make_error("not_authorized", "proposal targets another node's state")
    return make_success(state_ids=tuple(sorted(state_ids)))


def _translate_operations(config, operations):
    records = {
        (record["section"], record["entity_key"]): record
        for record in entity_registry.decompose(config)
    }
    translated = []
    for operation in operations:
        if not isinstance(operation, dict) or operation.get("op") != "replace_field":
            raise ValueError("Worker proposals support replace_field only")
        section = str(operation.get("section") or "")
        pointer = str(operation.get("json_pointer") or "")
        if section not in WORKER_RUNTIME_SECTIONS:
            raise ValueError("Worker proposal section is Master-only")
        if not any(pointer == allowed or pointer.startswith(allowed + "/") for allowed in WORKER_RUNTIME_POINTERS):
            raise ValueError("Worker proposal field is Master-only")
        identity = (section, str(operation.get("entity_key") or ""))
        if identity not in records:
            raise ValueError("Worker proposal entity is unknown")
        translated.append({
            "op": "replace_field",
            "section": section,
            "entity_key": identity[1],
            "json_pointer": "/payload" + pointer,
            # replace_field hashes the addressed scalar/object, not the
            # scope-local record position; stable identity resolves the
            # corresponding Master entity first.
            "expected_value_hash": operation.get("expected_value_hash"),
            "value": copy.deepcopy(operation.get("value")),
        })
    return translated


def _record_index(config):
    return {
        (record["section"], record["entity_key"]): record
        for record in entity_registry.decompose(config)
    }


def _record_controller_uuid(record):
    payload = record.get("payload") if isinstance(record, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    if payload.get("controller_uuid"):
        return str(payload["controller_uuid"])
    if payload.get("kind") == "group":
        header = payload.get("header") if isinstance(payload.get("header"), dict) else {}
        if header.get("controller_uuid"):
            return str(header["controller_uuid"])
    key = str(record.get("entity_key") or "") if isinstance(record, dict) else ""
    parts = key.split(":")
    return parts[1] if len(parts) > 2 and parts[0] in (
        "sensor", "sensor-group", "actuator", "actuator-group") else ""


def _translated_file_value(existing, supplied, section_records):
    value = copy.deepcopy(supplied)
    if not isinstance(value, dict):
        raise ValueError("file upsert value must be an entity record")
    if existing:
        for field in ("position", "group_position", "row_position"):
            value[field] = copy.deepcopy(existing.get(field))
        return value
    maximum = max(
        [int(record.get("position") or 0) for record in section_records] or [-1])
    value["position"] = maximum + 1
    if value.get("group_position") is not None:
        value["group_position"] = maximum + 1
    return value


def _translate_file_operations(config, operations, policy):
    maximum = min(500, max(1, int(policy.get("max_operations") or 128)))
    if not isinstance(operations, list) or not operations or len(operations) > maximum:
        raise ValueError("file proposal operation count is invalid")
    records = _record_index(config)
    translated = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("file proposal operation must be an object")
        section = str(operation.get("section") or "")
        identity = (section, str(operation.get("entity_key") or ""))
        kind = str(operation.get("op") or "")
        if section not in tuple(policy.get("allowed_sections") or WORKER_FILE_SECTIONS):
            raise ValueError("file proposal section is Master-only")
        existing = records.get(identity)
        item = {
            "op": kind, "section": identity[0], "entity_key": identity[1],
            "json_pointer": operation.get("json_pointer"),
            "expected_value_hash": fingerprint(existing) if existing else None,
            "value": copy.deepcopy(operation.get("value")),
            "value_hash": operation.get("value_hash"),
        }
        if kind == "delete_entity":
            if not policy.get("allow_deletes", True):
                raise ValueError("file proposal deletes are disabled")
            if existing is None:
                raise ValueError("file proposal deletes an unknown entity")
        elif kind == "upsert_entity":
            section_records = [
                record for record in records.values()
                if record.get("section") == section]
            item["value"] = _translated_file_value(
                existing, operation.get("value"), section_records)
            if (item["value"].get("section"), item["value"].get("entity_key")) != identity:
                raise ValueError("file proposal upsert identity differs")
            item["value_hash"] = fingerprint(item["value"])
        elif kind == "replace_field":
            if existing is None:
                raise ValueError("file proposal patches an unknown entity")
            pointer = str(operation.get("json_pointer") or "")
            item["json_pointer"] = pointer if pointer.startswith("/payload") \
                else "/payload" + pointer
        else:
            raise ValueError("file proposal operation is unsupported")
        translated.append(item)
    return translated


def _owner_route(config, controller_uuid):
    for route in config.get("site_controller_map") or []:
        if not isinstance(route, dict):
            continue
        if str(route.get("controller_uuid") or "") != str(controller_uuid):
            continue
        if str(route.get("routing_role") or "owner") == "owner":
            return str(route.get("node_id") or "")
    return ""


def _record_for_operation(index, operation):
    identity = (str(operation.get("section") or ""), str(operation.get("entity_key") or ""))
    value = operation.get("value")
    return value if isinstance(value, dict) else index.get(identity)


def _state_owned_by(config, source_node, entity_key):
    graph = config_graph.build_graph(config)
    state_id = str(entity_key).split(":", 1)[-1]
    state = graph.get("states", {}).get(state_id)
    return bool(
        isinstance(state, dict)
        and not state.get("errors")
        and str(source_node) in {str(value) for value in state.get("node_ids") or ()})


def _authorize_file_operations(before, candidate, source_node, operations):
    before_index = _record_index(before)
    candidate_index = _record_index(candidate)
    for operation in operations:
        section = str(operation.get("section") or "")
        entity_key = str(operation.get("entity_key") or "")
        identity = (section, entity_key)
        prior_record = before_index.get(identity)
        candidate_record = _record_for_operation(candidate_index, operation)
        record = prior_record or candidate_record
        payload = record.get("payload") if isinstance(record, dict) else {}
        payload = payload if isinstance(payload, dict) else {}
        if section == "nodes" and entity_key != "node:" + str(source_node):
            return make_error("not_authorized", "file proposal changes another node")
        if section == "local_node" and entity_key != "local-node:" + str(source_node):
            return make_error("not_authorized", "file proposal changes another local node")
        if section == "site_controller_map":
            route = payload
            if str(route.get("node_id") or "") != str(source_node):
                return make_error("not_authorized", "file proposal changes another route owner")
        if section in ("controllers", "sensors", "actuators"):
            controller_uuid = _record_controller_uuid(record)
            owner = _owner_route(before, controller_uuid) \
                if prior_record else _owner_route(candidate, controller_uuid)
            if not controller_uuid or owner != str(source_node):
                return make_error("not_authorized", "file proposal changes another controller")
        if section == "process_states":
            owned = _state_owned_by(before, source_node, entity_key) \
                if prior_record else _state_owned_by(candidate, source_node, entity_key)
            if not owned:
                return make_error("not_authorized", "file proposal changes another process state")
        if section == "processes":
            prior_payload = prior_record.get("payload") \
                if isinstance(prior_record, dict) else {}
            prior_payload = prior_payload if isinstance(prior_payload, dict) else {}
            candidate_payload = candidate_record.get("payload") \
                if isinstance(candidate_record, dict) else {}
            candidate_payload = candidate_payload \
                if isinstance(candidate_payload, dict) else {}
            prior_owner = str(prior_payload.get("owner_node_id") or "")
            candidate_owner = str(candidate_payload.get("owner_node_id") or "")
            if prior_record and prior_owner != str(source_node):
                return make_error("not_authorized", "file proposal changes another process")
            if candidate_record and candidate_owner != str(source_node):
                return make_error("not_authorized", "file proposal changes another process")
        if section == "rules":
            owner = str(payload.get("execution_owner") or payload.get("owner_node_id") or "")
            if owner != str(source_node):
                return make_error("not_authorized", "file proposal changes another rule")
    return make_success()


def prepare_v2_proposal(context, source_node, payload):
    """Persist, authorize and map v2 scope operations to global entities."""
    scope = str(payload.get("scope") or "")
    operation_id = str(payload.get("operation_id") or "")
    proposal_id = str(payload.get("proposal_id") or "proposal:" + operation_id)
    if not operation_id or _scope_owner(scope) != str(source_node):
        return make_error("not_authorized", "proposal source is not the scope owner")
    head = context["scope_head_reader"]().get(scope)
    if not head:
        return make_error("not_authorized", "proposal scope is not active")
    base_revision = int(payload.get("base_scope_revision") or -1)
    base_fingerprint = str(payload.get("base_scope_fingerprint") or "")
    record = {
        "proposal_id": proposal_id,
        "node_id": str(source_node),
        "scope": scope,
        "base_scope_revision": max(0, base_revision),
        "base_scope_fingerprint": base_fingerprint or "0" * 64,
        "operation_id": operation_id,
        "operations": copy.deepcopy(payload.get("operations") or []),
    }
    stored = context["record_proposal"](record)
    if not stored.get("ok"):
        return stored
    if stored.get("duplicate"):
        previous = stored["proposal"]
        result = copy.deepcopy(previous.get("result") or {})
        result.setdefault("ok", previous.get("state") in ("accepted", "converged"))
        result["status"] = "already_committed" if result.get("ok") else previous.get("state")
        return result
    if base_revision != int(head["scope_revision"]) or base_fingerprint != head["fingerprint"]:
        result = make_error(
            "rebase_required", "proposal baseline differs",
            current_scope_revision=int(head["scope_revision"]),
            current_scope_fingerprint=head["fingerprint"],
        )
        context["complete_proposal"](operation_id, "rebase_required", result)
        return result
    snapshot = context["config_reader"]()
    try:
        file_origin = payload.get("proposal_origin") == "worker_config_file"
        if file_origin and not context["file_policy"].get("enabled", False):
            raise ValueError("Worker file proposals are disabled")
        translated = _translate_file_operations(
            snapshot["config_data"], payload.get("operations") or [],
            context["file_policy"],
        ) if file_origin else _translate_operations(
            snapshot["config_data"], payload.get("operations") or [])
        candidate = apply_operations(snapshot["config_data"], translated)
    except ValueError as exc:
        result = make_error("rejected", str(exc))
        context["complete_proposal"](operation_id, "rejected", result)
        return result
    if file_origin:
        authorized = _authorize_file_operations(
            snapshot["config_data"], candidate, source_node,
            payload.get("operations") or [])
        if not authorized.get("ok"):
            context["complete_proposal"](operation_id, "not_authorized", authorized)
            return authorized
    return make_success(
        proposal_id=proposal_id,
        operation_id=operation_id,
        base_global_revision=int(snapshot["revision"]),
        config=candidate,
        scope=scope,
    )


def record_legacy_proposal(context, source_node, payload, config):
    """Persist and owner-check the exact v0082 action-plan proposal."""
    operation_id = str(payload.get("operation_id") or "legacy:" + fingerprint(payload))
    scope = "node:" + str(source_node)
    head = context["scope_head_reader"]().get(scope) or {}
    record = {
        "proposal_id": "proposal:" + operation_id,
        "node_id": str(source_node),
        "scope": scope,
        "base_scope_revision": int(head.get("scope_revision") or 0),
        "base_scope_fingerprint": str(head.get("fingerprint") or "0" * 64),
        "operation_id": operation_id,
        "operations": copy.deepcopy(payload.get("actions") or []),
    }
    stored = context["record_proposal"](record)
    if not stored.get("ok"):
        return stored
    if stored.get("duplicate"):
        previous = stored["proposal"]
        return make_success(
            duplicate=True, operation_id=operation_id,
            result=copy.deepcopy(previous.get("result") or {}),
        )
    authorized = authorize_legacy_action_plan(
        config, source_node, payload.get("actions") or [])
    if not authorized.get("ok"):
        context["complete_proposal"](operation_id, "not_authorized", authorized)
        return authorized
    authorized.update({
        "operation_id": operation_id,
        "proposal_id": record["proposal_id"],
        "scope": scope,
    })
    return authorized


def complete_ingest(context, operation_id, result):
    """Persist an accepted or rejected proposal result."""
    state = "accepted" if result.get("ok") else "rejected"
    if result.get("ok") and result.get("changed") is False:
        state = "already_committed"
    context["complete_proposal"](operation_id, state, result)
    return result


def create_proposal_ingest(**kwargs):
    """Create the proposal-ingest capability dictionary."""
    context = {
        "config_reader": kwargs["config_reader"],
        "scope_head_reader": kwargs["scope_head_reader"],
        "record_proposal": kwargs["record_proposal"],
        "complete_proposal": kwargs["complete_proposal"],
        "file_policy": copy.deepcopy(kwargs.get("file_policy") or {}),
    }
    return {
        "context": context,
        "prepare_v2": partial(prepare_v2_proposal, context),
        "record_legacy": partial(record_legacy_proposal, context),
        "complete": partial(complete_ingest, context),
    }

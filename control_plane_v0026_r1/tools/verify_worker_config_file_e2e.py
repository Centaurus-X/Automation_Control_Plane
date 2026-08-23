#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Fail-closed verifier for the remote Worker config-file E2E proof."""

import argparse
import hashlib
import json
import re

from pathlib import Path


EXPECTED = (
    ("policy_candidate", "worker_node_local", "policy_fallback"),
    ("policy_restore", "worker_node_local", "policy_fallback"),
    ("structural_candidate", "worker_node_local", "structural"),
    ("runtime_candidate", "worker_node_local", "runtime"),
    ("runtime_restore", "worker_node_local", "runtime"),
    ("structural_restore", "worker_node_local", "structural"),
)
EXPECTED_DIFFERENTIAL_OPERATIONS = {
    "policy_fallback": "scope_operations",
    "structural": "scope_operations",
    "runtime": "action_plan",
}
EXPECTED_COMMIT_KINDS = {
    "policy_fallback": "scope_operations",
    "structural": "scope_operations",
    "runtime": "action_plan",
}
EXPECTED_STRUCTURAL_SECTIONS = (
    "controllers", "sensors", "actuators", "rules",
    "process_states", "processes",
)

def items_fingerprint(value):
    payload = json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _entity_payload(operation):
    value = operation.get("value") if isinstance(operation, dict) else None
    payload = value.get("payload") if isinstance(value, dict) else None
    payload = payload if isinstance(payload, dict) else value
    if isinstance(payload, dict) and payload.get("kind") == "row" \
            and isinstance(payload.get("row"), dict):
        payload = payload["row"]
    return payload if isinstance(payload, dict) else {}


def _entity_container(operation):
    value = operation.get("value") if isinstance(operation, dict) else None
    payload = value.get("payload") if isinstance(value, dict) else None
    return payload if isinstance(payload, dict) else {}


def _single_section(items, section):
    selected = [item for item in items if isinstance(item, dict)
                and item.get("section") == section]
    if len(selected) != 1 or not str(selected[0].get("entity_key") or ""):
        raise RuntimeError(
            "Worker config-file E2E structural matrix path differs: " + section)
    return selected[0]


def _structural_differential(items, phase, context):
    matrix = context["matrix"]
    original = context["original"]
    bindings = context["bindings"]
    sections = [item.get("section") for item in items
                if isinstance(item, dict)]
    section_counts = {name: sections.count(name)
                      for name in EXPECTED_STRUCTURAL_SECTIONS}
    if len(sections) != len(items) \
            or any(not str(item.get("entity_key") or "") for item in items) \
            or any(section_counts[name] != 1
                   for name in EXPECTED_STRUCTURAL_SECTIONS
                   if name != "process_states") \
            or section_counts["process_states"] not in (1, 2) \
            or len(items) != sum(section_counts.values()):
        raise RuntimeError(
            "Worker config-file E2E structural differential paths differ")
    candidate = phase == "structural_candidate"
    if not candidate and phase != "structural_restore":
        raise RuntimeError("Worker config-file E2E structural phase differs")

    controller_uuid = str(matrix["new_sensor"].get("controller_uuid") or "")
    if not controller_uuid \
            or controller_uuid != str(
                matrix["new_actuator"].get("controller_uuid") or ""):
        raise RuntimeError("Worker config-file E2E controller binding is absent")

    controller = _single_section(items, "controllers")
    rule = _single_section(items, "rules")
    state_operations = [item for item in items
                        if item.get("section") == "process_states"]
    pid_candidates = [
        item for item in state_operations
        if _entity_payload(item).get("state_id")
        == matrix["pid_state"].get("state_id")
    ]
    if len(pid_candidates) != 1:
        raise RuntimeError("Worker config-file E2E PID matrix path differs")
    pid = pid_candidates[0]
    state_one_operations = [item for item in state_operations if item is not pid]
    if state_one_operations:
        state_one = state_one_operations[0]
        state_one_row = _entity_payload(state_one)
        expected_state_one = 2 if candidate else original.get("state_1_value_id")
        if state_one.get("op") != "upsert_entity" \
                or state_one.get("entity_key") != "state:1" \
                or state_one_row.get("state_id") != 1 \
                or state_one_row.get("value_id") != expected_state_one:
            raise RuntimeError("Worker config-file E2E state-1 phase diff differs")
    process = _single_section(items, "processes")
    sensor = _single_section(items, "sensors")
    actuator = _single_section(items, "actuators")
    upserts = (controller, rule, pid, process)
    if any(item.get("op") != "upsert_entity" for item in upserts):
        raise RuntimeError("Worker config-file E2E structural upsert proof differs")

    controller_row = _entity_payload(controller)
    original_tags = original.get("controller_tags")
    tags = controller_row.get("tags")
    if controller_row.get("controller_uuid") != controller_uuid \
            or controller.get("entity_key") \
            != "controller:" + controller_uuid \
            or not isinstance(original_tags, list) or not isinstance(tags, list):
        raise RuntimeError("Worker config-file E2E controller path differs")
    if candidate:
        if len(tags) != len(original_tags) + 1 \
                or tags[:-1] != original_tags or not str(tags[-1]) \
                or tags[-1] in original_tags:
            raise RuntimeError(
                "Worker config-file E2E controller candidate differs")
        bindings["marker"] = tags[-1]
        bindings["controller_key"] = controller["entity_key"]
    elif tags != original_tags \
            or controller.get("entity_key") != bindings.get("controller_key"):
        raise RuntimeError("Worker config-file E2E controller restore differs")

    sensor_row = _entity_payload(sensor)
    actuator_row = _entity_payload(actuator)
    sensor_container = _entity_container(sensor)
    actuator_container = _entity_container(actuator)
    expected_sensor_key = "sensor:{}:{}".format(
        controller_uuid, matrix["new_sensor"].get("sensor_id"))
    expected_actuator_key = "actuator:{}:{}".format(
        controller_uuid, matrix["new_actuator"].get("actuator_id"))
    if candidate:
        if sensor.get("op") != "upsert_entity" \
                or actuator.get("op") != "upsert_entity" \
                or sensor.get("entity_key") != expected_sensor_key \
                or actuator.get("entity_key") != expected_actuator_key \
                or (sensor_container.get("kind") == "row" and
                    sensor_container.get("group_key")
                    != "sensor-group:" + controller_uuid) \
                or (actuator_container.get("kind") == "row" and
                    actuator_container.get("group_key")
                    != "actuator-group:" + controller_uuid) \
                or sensor_row.get("controller_uuid") \
                not in (None, controller_uuid) \
                or sensor_row.get("sensor_id") \
                != matrix["new_sensor"].get("sensor_id") \
                or sensor_row.get("address") \
                != matrix["new_sensor"].get("address") \
                or actuator_row.get("controller_uuid") \
                not in (None, controller_uuid) \
                or actuator_row.get("actuator_id") \
                != matrix["new_actuator"].get("actuator_id") \
                or actuator_row.get("address") \
                != matrix["new_actuator"].get("address"):
            raise RuntimeError("Worker config-file E2E dynamic I/O diff differs")
        bindings["sensor_key"] = sensor["entity_key"]
        bindings["actuator_key"] = actuator["entity_key"]
    elif sensor.get("op") != "delete_entity" \
            or actuator.get("op") != "delete_entity" \
            or sensor.get("entity_key") != bindings.get("sensor_key") \
            or actuator.get("entity_key") != bindings.get("actuator_key"):
        raise RuntimeError("Worker config-file E2E dynamic I/O restore differs")

    rule_row = _entity_payload(rule)
    when = rule_row.get("when")
    expected_threshold = matrix["rule_threshold"][
        "after" if candidate else "before"]
    expected_enabled = False if candidate else original.get("rule_1_enabled")
    if rule_row.get("rule_id") != 1 or rule_row.get("enabled", True) \
            != expected_enabled or rule.get("entity_key") != "rule:1" \
            or not isinstance(when, list) or not when \
            or not isinstance(when[0], dict) \
            or when[0].get("value") != expected_threshold:
        raise RuntimeError("Worker config-file E2E rule differential differs")

    pid_row = _entity_payload(pid)
    pid_matrix = matrix["pid_state"]
    parameters = pid_row.get("parameters")
    expected_mode = pid_matrix["mode_id"]["after" if candidate else "before"]
    if pid_row.get("state_id") != pid_matrix.get("state_id") \
            or pid.get("entity_key") \
            != "state:" + str(pid_matrix.get("state_id")) \
            or pid_row.get("mode_id") != expected_mode \
            or pid_row.get("value_id") != pid_matrix["value_id"]["before"] \
            or not isinstance(parameters, dict) \
            or parameters.get("target_temp_c") \
            != pid_matrix["target_temp_c"]["before"]:
        raise RuntimeError("Worker config-file E2E PID structural diff differs")

    process_row = _entity_payload(process)
    process_matrix = matrix["process_data"]
    expected_location = process_matrix["process_location"][
        "after" if candidate else "before"]
    if process_row.get("p_id") != process_matrix.get("p_id") \
            or process.get("entity_key") \
            != "process:" + str(process_matrix.get("p_id")) \
            or process_row.get("process_location") != expected_location:
        raise RuntimeError("Worker config-file E2E process_data diff differs")

    for name, item in (("rule_key", rule), ("pid_key", pid),
                       ("process_key", process)):
        if candidate:
            bindings[name] = item["entity_key"]
        elif item.get("entity_key") != bindings.get(name):
            raise RuntimeError(
                "Worker config-file E2E structural restore identity differs")


def _policy_fallback_differential(items, phase, context):
    matrix = context["matrix"]
    pid = matrix["pid_state"]
    if phase not in ("policy_candidate", "policy_restore") \
            or len(items) != 1 or not isinstance(items[0], dict):
        raise RuntimeError("Worker config-file E2E policy fallback paths differ")
    operation = items[0]
    row = _entity_payload(operation)
    parameters = row.get("parameters") if isinstance(row, dict) else None
    target_key = "after" if phase == "policy_candidate" else "before"
    policy = matrix.get("operator_file_policy_fallback") or {}
    if operation.get("op") != "upsert_entity" \
            or operation.get("section") != "process_states" \
            or operation.get("entity_key") != "state:" + str(pid.get("state_id")) \
            or row.get("state_id") != pid.get("state_id") \
            or row.get("mode_id") != pid.get("mode_id", {}).get("before") \
            or row.get("value_id") != pid.get("value_id", {}).get("before") \
            or not isinstance(parameters, dict) \
            or parameters.get("target_temp_c") \
            != pid.get("target_temp_c", {}).get(target_key) \
            or policy.get("safety_code") != "required_mode" \
            or policy.get("active_mode_id") == policy.get("required_mode_id"):
        raise RuntimeError(
            "Worker config-file E2E required_mode policy fallback differs")


def _runtime_differential(items, phase, context):
    matrix = context["matrix"]
    original = context["original"]
    if len(items) != 2 or any(not isinstance(item, dict) for item in items):
        raise RuntimeError("Worker config-file E2E runtime action paths differ")
    by_state = {}
    for item in items:
        state_ids = item.get("state_ids")
        if not isinstance(state_ids, list) or len(state_ids) != 1:
            raise RuntimeError("Worker config-file E2E runtime state path differs")
        key = str(state_ids[0])
        if key in by_state:
            raise RuntimeError("Worker config-file E2E runtime state is duplicated")
        by_state[key] = item
    pid_matrix = matrix["pid_state"]
    state_one = by_state.get("1")
    pid = by_state.get(str(pid_matrix.get("state_id")))
    if state_one is None or pid is None or state_one is pid:
        raise RuntimeError("Worker config-file E2E runtime matrix paths differ")
    restore = phase == "runtime_restore"
    if not restore and phase != "runtime_candidate":
        raise RuntimeError("Worker config-file E2E runtime phase differs")
    state_one_value = 2 if restore else 1
    pid_value = pid_matrix["value_id"]["before" if restore else "after"]
    pid_target = pid_matrix["target_temp_c"]["before" if restore else "after"]
    if set(state_one) != {"state_ids", "value_id"} \
            or state_one.get("value_id") != state_one_value \
            or set(pid) != {"state_ids", "value_id", "parameters"} \
            or pid.get("value_id") != pid_value \
            or pid.get("parameters") != {"target_temp_c": pid_target}:
        raise RuntimeError("Worker config-file E2E runtime phase values differ")


def _strict_int(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) \
            or value < int(minimum):
        raise RuntimeError(str(label) + " is not a valid integer")
    return value


def _fingerprint_text(value, label):
    text = str(value or "")
    if re.fullmatch(r"[0-9a-f]{64}", text) is None:
        raise RuntimeError(str(label) + " is not a canonical fingerprint")
    return text


def _uuid_text(value, label):
    text = str(value or "")
    if re.fullmatch(
            r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
            r"[89ab][0-9a-f]{3}-[0-9a-f]{12}", text) is None:
        raise RuntimeError(str(label) + " is not a canonical UUID")
    return text


def _read_result(path):
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("Worker config-file E2E result is missing or unsafe")
    if path.stat().st_size > 1024 * 1024:
        raise RuntimeError("Worker config-file E2E result is unexpectedly large")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("status") != "PASSED":
        raise RuntimeError("Worker config-file E2E did not pass")
    if value.get("version") != "0.25.0-e2e.3" \
            or value.get("worker_version") != "automation_system_v0101" \
            or value.get("restore_confirmed") is not True:
        raise RuntimeError("Worker config-file E2E release contract differs")
    return value


def _core_proposal_proof(value, node_id):
    proof = value.get("core_node_local_proposal")
    if not isinstance(proof, dict):
        raise RuntimeError("Core node-local Worker proof is absent")
    operation_id = _uuid_text(
        proof.get("operation_id"), "Core operation identity")
    relay_message_id = str(proof.get("relay_message_id") or "")
    epoch = _strict_int(proof.get("authority_epoch"), "Core authority epoch")
    base_revision = _strict_int(
        proof.get("base_scope_revision"), "Core base scope revision")
    target_revision = _strict_int(
        proof.get("target_scope_revision"), "Core target scope revision", 1)
    replica_revision = _strict_int(
        proof.get("node_replica_revision"), "Core node replica revision", 1)
    target_fingerprint = _fingerprint_text(
        proof.get("target_scope_fingerprint"), "Core target fingerprint")
    replica_fingerprint = _fingerprint_text(
        proof.get("node_replica_fingerprint"), "Core replica fingerprint")
    _fingerprint_text(
        proof.get("worker_payload_sha256"), "Core Worker payload hash")
    _uuid_text(proof.get("journal_record_id"), "Core journal record identity")
    _uuid_text(proof.get("mirror_id"), "Core mirror identity")
    if proof.get("commit_origin") \
            != "canonical_client_proxy_master_relay_worker_local" \
            or proof.get("transport_command") \
            != "distributed.config.propose" \
            or proof.get("transport_mode") \
            != "canonical_scope_proposal" \
            or relay_message_id != "relay:" + operation_id \
            or proof.get("operation_format") \
            != "config_scope_operations_v1" \
            or _strict_int(
                proof.get("operation_count"), "Core operation count", 1) <= 0 \
            or target_revision != base_revision + 1 \
            or replica_revision != target_revision \
            or replica_fingerprint != target_fingerprint \
            or proof.get("target_scope") != "node:" + node_id \
            or proof.get("worker_inbox_state") != "DONE" \
            or proof.get("authority_journal_correlated") is not True \
            or proof.get("mirror_state") != "acked" \
            or proof.get("mirror_ack_correlated") is not True \
            or proof.get("current_run_correlated") is not True \
            or proof.get("worker_applied") is not True:
        raise RuntimeError("Core node-local Worker proof differs")
    return {
        "operation_id": operation_id,
        "authority_epoch": epoch,
        "base_scope_revision": base_revision,
        "target_scope_revision": target_revision,
        "target_scope_fingerprint": target_fingerprint,
    }


def _managed_restore(value):
    original_fingerprint = _fingerprint_text(
        value.get("original_fingerprint"), "Worker original fingerprint")
    final_fingerprint = _fingerprint_text(
        value.get("final_fingerprint"), "Worker final fingerprint")
    original = value.get("managed_original")
    final = value.get("managed_final")
    if original_fingerprint != final_fingerprint \
            or not isinstance(original, dict) or not isinstance(final, dict) \
            or original != final \
            or not isinstance(original.get("state_1_value_id"), int) \
            or isinstance(original.get("state_1_value_id"), bool) \
            or not isinstance(original.get("state_1_mode_id"), int) \
            or isinstance(original.get("state_1_mode_id"), bool) \
            or final.get("marker_present") is not False \
            or final.get("sensor_present") is not False \
            or final.get("actuator_present") is not False:
        raise RuntimeError("Worker config-file E2E managed restore differs")
    return {
        "original": original,
        "original_fingerprint": original_fingerprint,
        "final_fingerprint": final_fingerprint,
    }


def _matrix_proof(value, original):
    matrix = value.get("config_matrix")
    if not isinstance(matrix, dict) \
            or matrix.get("commit_paths") != ["worker_node_local"] \
            or matrix.get("commit_origin") \
            != "local_json_file_sem_node_authority" \
            or matrix.get("master_mirror_is_non_authoritative") is not True \
            or matrix.get("authority_journal_required") is not True \
            or matrix.get("exact_mirror_ack_required") is not True \
            or matrix.get("worker_live_ack") is not True \
            or matrix.get("restore_confirmed") is not True:
        raise RuntimeError("Worker config-file E2E authority matrix is absent")
    sensor = matrix.get("new_sensor")
    actuator = matrix.get("new_actuator")
    if not isinstance(sensor, dict) or not isinstance(actuator, dict) \
            or not isinstance(sensor.get("sensor_id"), int) \
            or isinstance(sensor.get("sensor_id"), bool) \
            or not isinstance(sensor.get("address"), int) \
            or isinstance(sensor.get("address"), bool) \
            or sensor.get("added") is not True \
            or sensor.get("restored") is not True \
            or not isinstance(actuator.get("actuator_id"), int) \
            or isinstance(actuator.get("actuator_id"), bool) \
            or not isinstance(actuator.get("address"), int) \
            or isinstance(actuator.get("address"), bool) \
            or actuator.get("added") is not True \
            or actuator.get("restored") is not True:
        raise RuntimeError("Worker config-file E2E dynamic I/O proof is absent")
    pid = matrix.get("pid_state")
    policy = matrix.get("operator_file_policy_fallback")
    threshold = matrix.get("rule_threshold")
    process_data = matrix.get("process_data")
    storage = matrix.get("storage_contract")
    if not isinstance(pid, dict) or not isinstance(policy, dict) \
            or policy.get("state_id") != pid.get("state_id") \
            or policy.get("safety_code") != "required_mode" \
            or policy.get("active_mode_id") \
            != pid.get("mode_id", {}).get("before") \
            or policy.get("required_mode_id") \
            != pid.get("mode_id", {}).get("after") \
            or policy.get("active_mode_id") == policy.get("required_mode_id") \
            or policy.get("candidate_target_temp_c") \
            != pid.get("target_temp_c", {}).get("after") \
            or policy.get("authority_scope") != "worker_node_local" \
            or policy.get("restored") is not True \
            or not isinstance(threshold, dict) \
            or not isinstance(process_data, dict) \
            or not isinstance(storage, dict) \
            or storage.get("version") != 9 \
            or storage.get("io_layout") != "flat" \
            or storage.get("primary") is not True \
            or not pid.get("state_id") \
            or pid.get("mode_id", {}).get("before") \
            == pid.get("mode_id", {}).get("after") \
            or pid.get("value_id", {}).get("before") \
            == pid.get("value_id", {}).get("after") \
            or pid.get("target_temp_c", {}).get("before") \
            == pid.get("target_temp_c", {}).get("after") \
            or threshold.get("before") == threshold.get("after") \
            or process_data.get("process_location", {}).get("before") \
            == process_data.get("process_location", {}).get("after") \
            or not isinstance(original.get("controller_tags"), list):
        raise RuntimeError("Worker config-file E2E PID/state matrix is absent")
    return matrix


def _actuator_proof(value):
    actuator = value.get("actuator_524")
    if not isinstance(actuator, dict) \
            or actuator.get("actuator_id") != 3 \
            or actuator.get("address") != 524 \
            or actuator.get("readback_verified") is not True \
            or actuator.get("new_log_evidence") is not True \
            or not isinstance(actuator.get("baseline_value"), int) \
            or isinstance(actuator.get("baseline_value"), bool) \
            or not isinstance(actuator.get("observed_before_restore"), int) \
            or isinstance(actuator.get("observed_before_restore"), bool) \
            or actuator.get("restored_value") \
            != actuator.get("baseline_value") \
            or actuator.get("restore_readback_verified") is not True \
            or not isinstance(actuator.get("write_performed"), bool) \
            or not isinstance(actuator.get("verified_noop"), bool) \
            or actuator.get("write_performed") \
            == actuator.get("verified_noop") \
            or re.fullmatch(
                r"[^:]+:[1-9][0-9]{0,4}",
                str(actuator.get("approved_endpoint") or "")) is None:
        raise RuntimeError("Worker config-file E2E actuator 524 proof is absent")
    return actuator


def _validate_transition_identity(transition, expected, node_id, state):
    route = expected[2]
    operation_id = str(transition.get("operation_id") or "")
    if re.fullmatch(r"file:[A-Za-z0-9._:-]+", operation_id) is None \
            or operation_id in state["operation_ids"]:
        raise RuntimeError("Worker file operation identity is invalid")
    state["operation_ids"].add(operation_id)
    commit_operation_id = str(transition.get("commit_operation_id") or "")
    expected_commit = operation_id + ":scope-fallback" \
        if route == "policy_fallback" else operation_id
    mirror_id = _uuid_text(
        transition.get("mirror_id"), "Worker transition mirror identity")
    journal_id = _uuid_text(
        transition.get("journal_record_id"),
        "Worker transition journal identity")
    if commit_operation_id != expected_commit \
            or mirror_id in state["mirror_ids"] \
            or journal_id in state["journal_ids"]:
        raise RuntimeError("Worker transition durable identity differs")
    state["mirror_ids"].add(mirror_id)
    state["journal_ids"].add(journal_id)
    epoch = _strict_int(
        transition.get("authority_epoch"), "Worker authority epoch")
    base_revision = _strict_int(
        transition.get("base_scope_revision"), "Worker base scope revision")
    revision = _strict_int(
        transition.get("scope_revision"), "Worker scope revision", 1)
    fingerprint = _fingerprint_text(
        transition.get("fingerprint"), "Worker transition fingerprint")
    if revision != base_revision + 1 \
            or epoch != state["authority_epoch"] \
            or transition.get("target_scope") != "node:" + node_id \
            or transition.get("target_scope_revision") != revision \
            or transition.get("target_scope_fingerprint") != fingerprint:
        raise RuntimeError("Worker transition scope tuple differs")
    if state["previous_revision"] is None:
        if base_revision < state["core_revision"]:
            raise RuntimeError("Worker file transitions predate the Core proof")
    elif base_revision != state["previous_revision"]:
        raise RuntimeError("Worker file scope revisions are not contiguous")
    wal_baseline = _fingerprint_text(
        transition.get("wal_baseline_fingerprint"),
        "Worker file-WAL baseline fingerprint")
    wal_candidate = _fingerprint_text(
        transition.get("wal_candidate_fingerprint"),
        "Worker file-WAL candidate fingerprint")
    if wal_baseline != state["previous_fingerprint"] \
            or route != "runtime" and wal_candidate != fingerprint:
        raise RuntimeError("Worker file-WAL fingerprint chain differs")
    state["previous_revision"] = revision
    state["previous_fingerprint"] = fingerprint
    return operation_id


def _validate_transition_differential(transition, expected, context):
    route = expected[2]
    items = transition.get("differential_items")
    items_hash = str(transition.get("differential_items_sha256") or "")
    full_hash = _fingerprint_text(
        transition.get("full_scope_operations_sha256"),
        "Worker full-operation hash")
    if not isinstance(items, list) or not items \
            or _strict_int(
                transition.get("differential_operation_count"),
                "Worker differential count", 1) != len(items) \
            or items_fingerprint(items) != items_hash \
            or not full_hash:
        raise RuntimeError("Worker exact differential proof differs")
    if route == "policy_fallback":
        _policy_fallback_differential(items, expected[0], context)
    elif route == "structural":
        _structural_differential(items, expected[0], context)
    elif route == "runtime":
        _runtime_differential(items, expected[0], context)
    else:
        raise RuntimeError("Worker config-file E2E file route differs")


def _transition_chain(value, node_id, core, matrix, managed):
    transitions = value.get("transitions")
    if not isinstance(transitions, list) or len(transitions) != len(EXPECTED):
        raise RuntimeError("Worker config-file E2E transition set differs")
    state = {
        "authority_epoch": core["authority_epoch"],
        "core_revision": core["target_scope_revision"],
        "previous_revision": None,
        "previous_fingerprint": managed["original_fingerprint"],
        "operation_ids": set(),
        "mirror_ids": set(),
        "journal_ids": set(),
    }
    context = {
        "matrix": matrix,
        "original": managed["original"],
        "bindings": {},
    }
    for transition, expected in zip(transitions, EXPECTED):
        if not isinstance(transition, dict) \
                or (transition.get("phase"), transition.get("authority_scope")) \
                != expected[:2] \
                or transition.get("file_route") != expected[2] \
                or transition.get("differential_operation") \
                != EXPECTED_DIFFERENTIAL_OPERATIONS[expected[2]] \
                or transition.get("file_commit_kind") \
                != EXPECTED_COMMIT_KINDS[expected[2]] \
                or transition.get("file_wal_mode") != "node_local" \
                or transition.get("observed_modes") != ["node_local"] \
                or transition.get("commit_origin") \
                != "local_json_file_sem_node_authority" \
                or transition.get("semantic_postcondition") is not True \
                or transition.get("local_commit_ok") is not True \
                or transition.get("wal_authority_correlated") is not True \
                or transition.get("authority_journal_correlated") is not True \
                or transition.get("journal_format") \
                != "authority_journal_v1" \
                or transition.get("mirror_state") != "acked" \
                or transition.get("mirror_ack_correlated") is not True \
                or transition.get("mirror_format") != "worker_mirror_v1" \
                or _strict_int(
                    transition.get("mirror_attempts"),
                    "Worker mirror attempts", 1) < 1:
            raise RuntimeError("Worker config-file E2E lacks durable convergence")
        _validate_transition_identity(
            transition, expected, node_id, state)
        _validate_transition_differential(transition, expected, context)
    if transitions[-1].get("fingerprint") != managed["final_fingerprint"]:
        raise RuntimeError("Worker structural restore fingerprint differs")
    return transitions


def _recovery_proof(value, transitions):
    recovery = value.get("recovery_journal")
    if not isinstance(recovery, dict) \
            or recovery.get("journal_version") != 3 \
            or recovery.get("status") != "COMPLETED" \
            or recovery.get("separate_from_product_file_wal") is not True \
            or recovery.get("prepared_before_first_mutation") is not True \
            or recovery.get("full_v9_baseline_recorded") is not True \
            or recovery.get("selectors_recorded") is not True \
            or recovery.get("physical_actuator_baseline_recorded") is not True \
            or recovery.get("physical_actuator_restore_confirmed") is not True \
            or recovery.get("baseline_fingerprint") \
            != value.get("original_fingerprint") \
            or recovery.get("final_fingerprint") \
            != value.get("final_fingerprint") \
            or recovery.get("restore_scope_revision") \
            != transitions[-1].get("scope_revision") \
            or recovery.get("restore_mirror_state") != "acked" \
            or recovery.get("restore_authority_journal_correlated") is not True \
            or recovery.get("restore_mirror_ack_correlated") is not True \
            or recovery.get("restore_confirmed") is not True \
            or not isinstance(
                recovery.get("previous_open_journal_recovered"), bool):
        raise RuntimeError("Worker config-file E2E recovery proof differs")
    return recovery


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", required=True, type=Path)
    arguments = parser.parse_args()
    value = _read_result(arguments.result)
    node_id = str(value.get("node_id") or "")
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", node_id) is None:
        raise RuntimeError("Worker config-file E2E node identity is invalid")
    core = _core_proposal_proof(value, node_id)
    managed = _managed_restore(value)
    matrix = _matrix_proof(value, managed["original"])
    _actuator_proof(value)
    transitions = _transition_chain(
        value, node_id, core, matrix, managed)
    _recovery_proof(value, transitions)
    print(
        "PASS Worker config-file E2E: Core relay + Worker node authority + "
        "SEM/File-WAL + authority journal + exact mirror ACK + V9 flat "
        "storage + dynamic sensor/actuator + controller/rule/process/PID "
        "matrix + actuator 524 + exact restore; scope_revisions={}".format(
            ",".join(str(item["scope_revision"]) for item in transitions)))
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

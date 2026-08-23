#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Verify the mandatory Authority-V5 Control Plane Core-E2E result contract."""

import argparse
import json
import os
import re
import sys

from pathlib import Path


REQUIRED_CHECKS = frozenset((
    "HTTPS-READY", "TLS-001", "TLS-002", "TLS-003", "AUTH",
    "SEC-001", "SEC-002", "SEC-003", "SEC-006", "WSS-CONTROL",
    "PATH-ORIGIN", "PTH-001", "PTH-002", "PTH-003", "PTH-004",
    "WSS-EVENTS", "CMD-READS", "RD-001", "RD-002", "RD-003",
    "HTTP-001", "HTTP-002", "WORKER-FN01", "EVT-001", "EVT-002",
    "EVT-003", "EVT-001A", "EVT-004", "EVT-004A", "SEC-005",
    "SEC-007", "SEC-008", "CMD-NEGATIVE", "SEC-004", "CFG-001",
    "CFG-002", "CFG-003", "CFG-004", "CFG-005", "CFG-006", "CFG-MATRIX",
    "CONFIG-DIFF", "ACT-001", "ACT-002", "ACT-003", "ACT-004",
    "PAR-001", "CROSS-WORKER", "ACT-RESTORE", "CFG-RESTORE",
    "SEC-LOG", "SEC-009",
))
FINGERPRINT_PATTERN = re.compile(r"[0-9a-f]{64}")
UUID_PATTERN = re.compile(
    r"[0-9a-f]{8}-[0-9a-f]{4}-[1-5][0-9a-f]{3}-"
    r"[89ab][0-9a-f]{3}-[0-9a-f]{12}")


def _load_json(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("E2E result root must be a JSON object")
    return value


def _required_text(path):
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise ValueError("version file is empty: " + str(path))
    return value


def _assert_equal(actual, expected, label):
    if actual != expected:
        raise AssertionError(
            "{} mismatch: expected {!r}, got {!r}".format(label, expected, actual))


def _strict_int(value, label, minimum=0):
    if isinstance(value, bool) or not isinstance(value, int) or value < minimum:
        raise AssertionError(label + " is not a valid integer")
    return value


def _fingerprint(value, label):
    normalized = str(value or "")
    if FINGERPRINT_PATTERN.fullmatch(normalized) is None:
        raise AssertionError(label + " is not a canonical fingerprint")
    return normalized


def _node_local_proposal_proof(matrix):
    proof = matrix.get("node_local_proposal_proof")
    proof = proof if isinstance(proof, dict) else {}
    relay_message_id = str(proof.get("relay_message_id") or "")
    relay_match = re.fullmatch(r"relay:(?P<operation>.+)", relay_message_id)
    operation_id = relay_match.group("operation") if relay_match is not None else ""
    if UUID_PATTERN.fullmatch(operation_id) is None:
        raise AssertionError("CFG-MATRIX relay identity is invalid")
    epoch = _strict_int(
        proof.get("authority_epoch"), "CFG-MATRIX authority epoch")
    base_revision = _strict_int(
        proof.get("base_scope_revision"), "CFG-MATRIX base scope revision")
    target_revision = _strict_int(
        proof.get("target_scope_revision"), "CFG-MATRIX target scope revision", 1)
    replica_revision = _strict_int(
        proof.get("node_replica_revision"), "CFG-MATRIX replica revision", 1)
    target_fingerprint = _fingerprint(
        proof.get("target_scope_fingerprint"), "CFG-MATRIX target fingerprint")
    replica_fingerprint = _fingerprint(
        proof.get("node_replica_fingerprint"), "CFG-MATRIX replica fingerprint")
    operation_count = _strict_int(
        proof.get("operation_count"), "CFG-MATRIX operation count", 1)
    if target_revision != base_revision + 1:
        raise AssertionError("CFG-MATRIX Worker scope revision is not monotonic")
    if replica_revision != target_revision \
            or replica_fingerprint != target_fingerprint:
        raise AssertionError("CFG-MATRIX Worker replica differs from local commit")
    if proof.get("operation_format") != "config_scope_operations_v1":
        raise AssertionError("CFG-MATRIX canonical operation format differs")
    return {
        "operation_id": operation_id,
        "relay_message_id": relay_message_id,
        "authority_epoch": epoch,
        "base_scope_revision": base_revision,
        "target_scope_revision": target_revision,
        "target_scope_fingerprint": target_fingerprint,
        "node_replica_revision": replica_revision,
        "node_replica_fingerprint": replica_fingerprint,
        "operation_format": "config_scope_operations_v1",
        "operation_count": operation_count,
    }


def _restore_proof(detail, check_name, context):
    proposal = context["proposal"]
    matrix = context["matrix"]
    node_id = context["node_id"]
    detail = detail if isinstance(detail, dict) else {}
    baseline = detail.get("baseline")
    baseline = baseline if isinstance(baseline, dict) else {}
    worker = detail.get("worker_scope")
    worker = worker if isinstance(worker, dict) else {}
    base_revision = _strict_int(
        baseline.get("scope_revision"), check_name + " baseline scope revision")
    base_node_revision = _strict_int(
        baseline.get("node_scope_revision"),
        check_name + " baseline node scope revision")
    scope_revision = _strict_int(
        detail.get("scope_revision"), check_name + " restored scope revision", 1)
    worker_revision = _strict_int(
        worker.get("revision"), check_name + " Worker scope revision", 1)
    observed_global = _strict_int(
        worker.get("observed_global_revision"),
        check_name + " observed global revision")
    baseline_fingerprint = _fingerprint(
        baseline.get("scope_fingerprint"), check_name + " baseline fingerprint")
    baseline_node_fingerprint = _fingerprint(
        baseline.get("node_scope_fingerprint"),
        check_name + " baseline node fingerprint")
    restored_fingerprint = _fingerprint(
        detail.get("scope_fingerprint"), check_name + " restored fingerprint")
    worker_fingerprint = _fingerprint(
        worker.get("fingerprint"), check_name + " Worker fingerprint")
    expected_fingerprint = _fingerprint(
        worker.get("expected_fingerprint"),
        check_name + " expected Worker fingerprint")
    allowed_modes = (
        "compensating_proposal", "already_restored",
        "persisted_restore_revalidation", "intent_not_applied",
    )
    _strict_int(detail.get("cas_retries"), check_name + " CAS retries")
    if detail.get("authority_mode") != "node_local" \
            or detail.get("restore_confirmed") is not True \
            or detail.get("actuator_restored_before_config") is not True \
            or detail.get("before_parallel_load") is not True \
            or detail.get("mode") not in allowed_modes \
            or base_revision != proposal["base_scope_revision"] \
            or base_node_revision != base_revision \
            or scope_revision <= proposal["target_scope_revision"] \
            or worker_revision < scope_revision \
            or baseline_node_fingerprint != baseline_fingerprint \
            or restored_fingerprint != baseline_fingerprint \
            or worker_fingerprint != restored_fingerprint \
            or expected_fingerprint != restored_fingerprint \
            or proposal["target_scope_fingerprint"] == restored_fingerprint \
            or str(worker.get("node_id") or "") != node_id \
            or str(worker.get("scope") or "") != "node:" + node_id \
            or observed_global < 0 \
            or detail.get("owned_values_restored") is not True \
            or detail.get("owned_paths") != matrix.get("owned_paths"):
        raise AssertionError(check_name + " exact node-local restore proof is absent")
    return {
        "authority_mode": "node_local",
        "baseline_scope_revision": base_revision,
        "baseline_fingerprint": baseline_fingerprint,
        "scope_revision": scope_revision,
        "scope_fingerprint": restored_fingerprint,
        "worker_revision": worker_revision,
        "node_id": node_id,
        "owned_paths": list(detail.get("owned_paths") or []),
    }


def _changed_pair(value, label, numeric=False):
    if not isinstance(value, dict) or "before" not in value or "after" not in value \
            or value.get("before") == value.get("after"):
        raise AssertionError("CFG-MATRIX {} change proof is absent".format(label))
    if numeric and any(
            not isinstance(item, (int, float)) or isinstance(item, bool)
            for item in (value.get("before"), value.get("after"))):
        raise AssertionError("CFG-MATRIX {} values are not numeric".format(label))
    return value


def _matrix_payload_proof(matrix):
    proof = matrix.get("matrix")
    proof = proof if isinstance(proof, dict) else {}
    marker = str(proof.get("marker") or "")
    controller_uuid = str(proof.get("controller_uuid") or "")
    if not controller_uuid or re.fullmatch(r"wss-v0025-[0-9a-f]{8}", marker) is None:
        raise AssertionError("CFG-MATRIX controller/marker proof is invalid")
    sensor_id = _strict_int(proof.get("sensor_id"), "CFG-MATRIX sensor ID", 1)
    sensor_address = _strict_int(
        proof.get("sensor_address"), "CFG-MATRIX sensor address")
    actuator_id = _strict_int(proof.get("actuator_id"), "CFG-MATRIX actuator ID", 1)
    actuator_address = _strict_int(
        proof.get("actuator_address"), "CFG-MATRIX actuator address")
    if not 60000 <= sensor_id <= 65535 or not 14 <= sensor_address <= 63 \
            or not 60000 <= actuator_id <= 65535 \
            or not 547 <= actuator_address <= 599 \
            or proof.get("pid_state_id") is None:
        raise AssertionError("CFG-MATRIX dynamic I/O/PID identity proof is invalid")
    _changed_pair(proof.get("mode_id"), "PID mode")
    _changed_pair(proof.get("value_id"), "PID value")
    _changed_pair(proof.get("pid_target_temp_c"), "PID target", numeric=True)
    _changed_pair(proof.get("rule_threshold"), "rule threshold", numeric=True)
    process = proof.get("process_data")
    process = process if isinstance(process, dict) else {}
    location = _changed_pair(process.get("process_location"), "process location")
    if process.get("p_id") is None \
            or not isinstance(location.get("before"), str) \
            or location.get("after") != location.get("before") + "|" + marker:
        raise AssertionError("CFG-MATRIX process_data proof is invalid")
    storage = proof.get("storage_contract")
    storage = storage if isinstance(storage, dict) else {}
    if storage.get("version") != 9 or storage.get("io_layout") != "flat":
        raise AssertionError("CFG-MATRIX nested V9 storage proof is invalid")
    return proof


def _owned_paths(matrix):
    owned_paths = matrix.get("owned_paths")
    patterns = (
        r"controllers\.[0-9]+\.tags",
        r"sensors",
        r"actuators",
        r"rules\.[0-9]+\.when\.[0-9]+\.value",
        r"process_states\.[0-9]+\.mode_id",
        r"process_states\.[0-9]+\.value_id",
        r"process_states\.[0-9]+\.parameters\.target_temp_c",
        r"processes\.[0-9]+\.process_location",
    )
    if not isinstance(owned_paths, list) or len(owned_paths) != len(patterns) \
            or len(set(owned_paths)) != len(patterns) \
            or any(sum(re.fullmatch(pattern, str(path)) is not None
                       for path in owned_paths) != 1 for pattern in patterns):
        raise AssertionError("CFG-MATRIX exact owned path set differs")
    return owned_paths


def _runtime_matrix_proof(matrix, checks):
    _matrix_payload_proof(matrix)
    _owned_paths(matrix)
    proposal = _node_local_proposal_proof(matrix)
    identity = matrix.get("commit_identity")
    if identity != ["operation", proposal["operation_id"]]:
        raise AssertionError("CFG-MATRIX operation identity differs from relay")
    revision_detail = checks.get("CFG-001", {}).get("detail")
    revision_detail = revision_detail if isinstance(revision_detail, dict) else {}
    replica_detail = checks.get("CFG-002", {}).get("detail")
    replica_detail = replica_detail if isinstance(replica_detail, dict) else {}
    node_id = str(replica_detail.get("node_id") or "")
    from_revision = _strict_int(
        revision_detail.get("from_revision"), "CFG-001 from revision")
    to_revision = _strict_int(
        revision_detail.get("to_revision"), "CFG-001 to revision", 1)
    replica_revision = _strict_int(
        replica_detail.get("node_replica_revision"),
        "CFG-002 replica revision", 1)
    replica_fingerprint = _fingerprint(
        replica_detail.get("node_replica_fingerprint"),
        "CFG-002 replica fingerprint")
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", node_id) is None \
            or from_revision != proposal["base_scope_revision"] \
            or to_revision != proposal["target_scope_revision"] \
            or replica_revision != proposal["node_replica_revision"] \
            or replica_fingerprint != proposal["node_replica_fingerprint"]:
        raise AssertionError("CFG-MATRIX Worker revision proof differs")
    restore_context = {
        "proposal": proposal, "matrix": matrix, "node_id": node_id}
    restore_proofs = [
        _restore_proof(
            checks.get(check_name, {}).get("detail"), check_name, restore_context)
        for check_name in ("CFG-006", "CFG-RESTORE")
    ]
    if restore_proofs[0] != restore_proofs[1]:
        raise AssertionError("CFG-006/CFG-RESTORE node-local proof differs")
    output = dict(proposal)
    output["node_id"] = node_id
    return output


def _write_private_json(path, value):
    target = path.expanduser().resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists() or target.is_symlink():
        raise OSError("node-local proposal proof output already exists: " + str(target))
    descriptor = os.open(str(target), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True)
            stream.write("\n")
    except Exception:
        try:
            target.unlink()
        except OSError:
            pass
        raise


def verify(report, expected_version):
    _assert_equal(report.get("version"), expected_version, "E2E version")
    _assert_equal(report.get("status"), "PASSED", "E2E status")
    _assert_equal(report.get("profile"), "core", "E2E profile")
    _assert_equal(report.get("transport"), "wss-primary", "E2E transport")
    _assert_equal(
        report.get("config_restore_confirmed"), True, "configuration restore")
    _assert_equal(
        report.get("actuator_restore_confirmed"), True, "actuator restore")
    worker = report.get("worker_fn01")
    if not isinstance(worker, dict) or worker.get("converged") is not True:
        raise AssertionError("Worker fn-01 convergence was not confirmed")
    checks = report.get("checks")
    if not isinstance(checks, dict):
        raise AssertionError("E2E checks object is missing")
    missing = sorted(REQUIRED_CHECKS.difference(checks))
    if missing:
        raise AssertionError("missing mandatory checks: " + ", ".join(missing))
    allowed_skips = {"CROSS-WORKER"}
    auth = checks.get("AUTH")
    auth_detail = auth.get("detail") if isinstance(auth, dict) else {}
    auth_detail = auth_detail if isinstance(auth_detail, dict) else {}
    if auth_detail.get("mode") == "bearer":
        allowed_skips.add("SEC-004")
    failed = {}
    for name in sorted(REQUIRED_CHECKS):
        item = checks.get(name)
        status = item.get("status") if isinstance(item, dict) else None
        if status == "PASSED":
            continue
        if status == "SKIPPED" and name in allowed_skips:
            continue
        failed[name] = status
    if failed:
        raise AssertionError("mandatory checks not passed: " + repr(failed))
    pagination = checks["EVT-004A"].get("detail")
    pagination = pagination if isinstance(pagination, dict) else {}
    if pagination.get("retained_stream_command_filter_removed") is not True:
        raise AssertionError("EVT-004A retained stream filter invariant failed")
    matrix = checks["CFG-MATRIX"].get("detail")
    matrix = matrix if isinstance(matrix, dict) else {}
    storage = matrix.get("storage_contract")
    storage = storage if isinstance(storage, dict) else {}
    if matrix.get("commit_path") != "client_wss_proxy_master_mqtt_worker" \
            or matrix.get("commit_origin") \
            != "canonical_client_proxy_master_relay_worker_local" \
            or matrix.get("file_proposal") is not False \
            or matrix.get("worker_live_ack") is not True \
            or matrix.get("node_patch_capability") is not True \
            or matrix.get("transport_expectation") \
            != "distributed.config.propose" \
            or storage.get("version") != 9 \
            or storage.get("io_layout") != "flat":
        raise AssertionError("CFG-MATRIX Authority-V5 commit-chain proof is incomplete")
    proposal_proof = _runtime_matrix_proof(matrix, checks)
    return {
        "run_id": report.get("run_id"),
        "version": expected_version,
        "check_count": len(REQUIRED_CHECKS),
        "allowed_skips": sorted(allowed_skips),
        "node_local_proposal_proof": proposal_proof,
    }


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--result", type=Path, required=True)
    parser.add_argument("--version-file", type=Path, required=True)
    parser.add_argument("--node-local-proposal-proof-output", type=Path)
    arguments = parser.parse_args()
    try:
        result_path = arguments.result.expanduser().resolve()
        version_path = arguments.version_file.expanduser().resolve()
        report = _load_json(result_path)
        expected_version = _required_text(version_path)
        summary = verify(report, expected_version)
        if arguments.node_local_proposal_proof_output is not None:
            _write_private_json(
                arguments.node_local_proposal_proof_output,
                summary["node_local_proposal_proof"])
    except (OSError, ValueError, json.JSONDecodeError, AssertionError) as error:
        print("FAILED Core-E2E result verification: " + str(error), file=sys.stderr)
        return 1
    print(
        "PASS Core-E2E: version={version}, checks={check_count}, "
        "Worker/Config/Aktor-Restore bestätigt, run_id={run_id}".format(**summary)
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Exercise Worker JSON edits through the Authority-V5 node-local path.

The script is streamed to the Worker host by ``rollout_automation.sh`` and
uses only the Python standard library plus the installed Worker contracts. It
proves one unambiguous authority chain for every file edit:

* the productive file-WAL binds the exact differential to the visible V9 file;
* the Worker SEM commits the node scope locally and advances its scope revision;
* the immutable authority journal binds that revision to the SEM operation;
* the Worker-to-Master mirror is acknowledged only for the same tuple;
* structural, policy-fallback and runtime edit routes are all exercised;
* actuator 3 / Modbus register 524 produces a new readback-verified write;
* all edits are restored through a new node-authoritative revision.

An E2E-only, fsync-backed recovery journal records the complete canonical V9
baseline before the first edit. It is deliberately separate from the Worker's
productive config-file WAL and can only close a mutating run after a new
durable local commit plus exact mirror ACK restores the exact baseline.

Unrelated runtime values are deliberately excluded from equality checks: they
may move while the file transition is in progress. Convergence is tied to the
durable SEM operation identity, journal record, mirror tuple and targeted
semantic postconditions.
"""

import argparse
import copy
import hashlib
import json
import os
import re
import socket
import sqlite3
import stat
import struct
import sys
import tempfile
import time
import uuid

from pathlib import Path


VERSION = "0.25.0-e2e.3"
EXPECTED_WORKER_VERSION = "automation_system_v0101"
RECOVERY_JOURNAL_VERSION = 3
RECOVERY_TERMINAL_STATES = ("COMPLETED", "RECOVERED")
RECOVERY_STATES = (
    "PREPARED", "MUTATING", "RECOVERING", "FAILED",
) + RECOVERY_TERMINAL_STATES
EXPECTED_TRANSITIONS = (
    ("policy_candidate", "worker_node_local", "policy_fallback"),
    ("policy_restore", "worker_node_local", "policy_fallback"),
    ("structural_candidate", "worker_node_local", "structural"),
    ("runtime_candidate", "worker_node_local", "runtime"),
    ("runtime_restore", "worker_node_local", "runtime"),
    ("structural_restore", "worker_node_local", "structural"),
)
EXPECTED_DIFFERENTIAL_PAYLOADS = {
    "policy_fallback": ("scope_operations", "operations"),
    "structural": ("scope_operations", "operations"),
    "runtime": ("action_plan", "actions"),
}
_STORAGE_VERIFIER = None
_RUNTIME_VERIFIER = None
_PROJECT_STORAGE = None
_NORMALIZE_RUNTIME = None
_SCOPE_DIFF = None
_LOCAL_RUNTIME_ACTIONS = None
_RUNTIME_OVERLAY_MATCHES = None


def _canonical_bytes(value):
    return json.dumps(
        value, ensure_ascii=False, allow_nan=False, sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _required_transition_route(request):
    route = str(request.get("expected_file_route") or "")
    if route not in EXPECTED_DIFFERENTIAL_PAYLOADS:
        raise RuntimeError("local JSON transition has no explicit file route")
    return route

def _differential_payload_items(payload, expected_route):
    expected = EXPECTED_DIFFERENTIAL_PAYLOADS.get(expected_route)
    if expected is None:
        raise RuntimeError(
            "unsupported local JSON file route: " + str(expected_route))
    expected_operation, item_key = expected
    items = payload.get(item_key) if isinstance(payload, dict) else None
    if not isinstance(payload, dict) \
            or payload.get("operation") != expected_operation \
            or not isinstance(items, list) or not items:
        raise RuntimeError(
            "local JSON {} commit did not use differential {}/{}".format(
                expected_route, expected_operation, item_key))
    return expected_operation, items


def _restore_context(original, details):
    return {
        "original": copy.deepcopy(original),
        "controller_uuid": str(details["controller_uuid"]),
        "marker": str(details["marker"]),
        "timeout_s": float(details["timeout_s"]),
        "pid_state_id": details["pid_state_id"],
        "sensor_id": details["sensor_id"],
        "actuator_id": details["actuator_id"],
    }


def _fingerprint(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _items_fingerprint(value):
    return hashlib.sha256(_canonical_bytes(value)).hexdigest()


def _read_object(path, label):
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise RuntimeError(label + " is not a regular non-symlink file")
    if details.st_size <= 0 or details.st_size > 64 * 1024 * 1024:
        raise RuntimeError(label + " has an invalid size")
    with path.open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError(label + " is not a JSON object")
    return value


def _require_v9_storage(config, label):
    meta = config.get("meta") if isinstance(config, dict) else None
    if not isinstance(meta, list) or len(meta) != 1 \
            or not isinstance(meta[0], dict) or meta[0].get("version") != 9:
        raise RuntimeError(label + " is not canonical V9 storage")
    for section in ("sensors", "actuators"):
        rows = config.get(section)
        if not isinstance(rows, list) or any(
                not isinstance(row, dict) or "rows" in row for row in rows):
            raise RuntimeError(label + " has non-flat V9 " + section)
    if _STORAGE_VERIFIER is None:
        raise RuntimeError("Worker canonical storage verifier is not bound")
    report = _STORAGE_VERIFIER(config)
    if not isinstance(report, dict) or report.get("status") != "OK":
        raise RuntimeError(label + " failed the Worker canonical V9 contract")
    return config


def _bind_worker_storage_verifier(worker_root):
    resolved = Path(worker_root).resolve()
    source_text = str(resolved)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    from src.library import _generic_type_library as type_library
    from src.library._generic_system_contract_library import scope_diff
    from src.core import config_file_sync
    module_path = Path(type_library.__file__).resolve()
    diff_path = Path(scope_diff.__file__).resolve()
    sync_path = Path(config_file_sync.__file__).resolve()
    if any(resolved not in path.parents for path in (
            module_path, diff_path, sync_path)):
        raise RuntimeError("Worker config proof capability escaped the Worker root")
    global _STORAGE_VERIFIER, _RUNTIME_VERIFIER, _PROJECT_STORAGE
    global _NORMALIZE_RUNTIME, _SCOPE_DIFF, _LOCAL_RUNTIME_ACTIONS
    global _RUNTIME_OVERLAY_MATCHES
    _STORAGE_VERIFIER = type_library.verify_storage_config_data
    _RUNTIME_VERIFIER = type_library.verify_runtime_config_data
    _PROJECT_STORAGE = type_library.project_config_for_storage
    _NORMALIZE_RUNTIME = type_library.normalize_config_for_runtime
    def runtime_scope_diff(before, after):
        return scope_diff.diff_configs(
            before, after, representation="runtime")

    _SCOPE_DIFF = runtime_scope_diff
    _LOCAL_RUNTIME_ACTIONS = config_file_sync._local_runtime_actions
    _RUNTIME_OVERLAY_MATCHES = config_file_sync._runtime_overlay_matches


def _require_v9_runtime(config, label):
    if not isinstance(config, dict):
        raise RuntimeError(label + " is not a runtime config object")
    meta = config.get("meta")
    if not isinstance(meta, list) or len(meta) != 1 \
            or not isinstance(meta[0], dict) or meta[0].get("version") != 9:
        raise RuntimeError(label + " is not canonical V9 runtime")
    for section in ("sensors", "actuators"):
        groups = config.get(section)
        if not isinstance(groups, list) or any(
                not isinstance(group, dict)
                or not isinstance(group.get("rows"), list)
                for group in groups):
            raise RuntimeError(label + " has non-grouped V9 " + section)
    if _RUNTIME_VERIFIER is None:
        raise RuntimeError("Worker canonical runtime verifier is not bound")
    report = _RUNTIME_VERIFIER(config)
    if not isinstance(report, dict) or report.get("status") != "OK":
        raise RuntimeError(label + " failed the Worker canonical runtime contract")
    return config


def _strict_non_negative_int(value, label):
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise RuntimeError(str(label) + " must be a non-negative integer")
    return value


def _canonical_uuid(value, label):
    text = str(value or "")
    try:
        parsed = uuid.UUID(text)
    except (AttributeError, ValueError) as error:
        raise RuntimeError(str(label) + " is not a canonical UUID") from error
    if str(parsed) != text:
        raise RuntimeError(str(label) + " is not a canonical UUID")
    return text


def _valid_fingerprint(value):
    return re.fullmatch(r"[0-9a-f]{64}", str(value or "")) is not None


def _validated_wal_evidence(wal, transition):
    baseline = transition["baseline"]
    target = transition["target"]
    authority = transition["authority"]
    expected_scope = authority["scope"]
    expected_route = authority["route"]
    target_fingerprint = _fingerprint(target)
    last_operation = wal.get("last_operation") \
        if isinstance(wal, dict) else None
    if wal.get("state") != "IDLE" or not isinstance(last_operation, dict) \
            or str(last_operation.get("candidate_fingerprint") or "") \
            != target_fingerprint:
        return None
    if expected_scope != "worker_node_local":
        raise RuntimeError("file transition does not use Worker node authority")
    if _SCOPE_DIFF is None or _LOCAL_RUNTIME_ACTIONS is None \
            or _PROJECT_STORAGE is None or _NORMALIZE_RUNTIME is None \
            or _RUNTIME_OVERLAY_MATCHES is None:
        raise RuntimeError("Worker differential proof capabilities are not bound")

    baseline_storage = _require_v9_storage(
        baseline, "local JSON transition baseline")
    target_storage = _require_v9_storage(
        target, "local JSON transition target")
    baseline_runtime = _require_v9_runtime(
        _NORMALIZE_RUNTIME(baseline_storage, representation="storage"),
        "local JSON transition runtime baseline")
    candidate_runtime = _require_v9_runtime(
        _NORMALIZE_RUNTIME(target_storage, representation="storage"),
        "local JSON transition runtime candidate")
    idle_runtime = _require_v9_runtime(
        wal.get("baseline_config"), "terminal file-WAL runtime snapshot")
    idle_storage = _require_v9_storage(
        _PROJECT_STORAGE(idle_runtime),
        "terminal file-WAL storage projection")
    baseline_fingerprint = _fingerprint(baseline_storage)
    committed_fingerprint = _fingerprint(idle_storage)
    if str(wal.get("baseline_fingerprint") or "") \
            != committed_fingerprint \
            or str(last_operation.get("baseline_fingerprint") or "") \
            != baseline_fingerprint \
            or _canonical_bytes(candidate_runtime) != _canonical_bytes(
                _NORMALIZE_RUNTIME(target_storage, representation="storage")):
        raise RuntimeError("terminal file-WAL runtime/storage proof differs")

    operations = list(_SCOPE_DIFF(baseline_runtime, candidate_runtime))
    actions = _LOCAL_RUNTIME_ACTIONS(
        baseline_runtime, candidate_runtime, operations)
    action_items = list(actions or ())
    if not operations \
            or last_operation.get("operations") != operations \
            or last_operation.get("actions") != action_items:
        raise RuntimeError("terminal file-WAL does not contain the exact diff")

    expected_operation, item_key = EXPECTED_DIFFERENTIAL_PAYLOADS.get(
        expected_route, (None, None))
    if expected_operation is None:
        raise RuntimeError("unsupported local JSON file route")
    commit_kind = str(last_operation.get("commit_kind") or "")
    if expected_route == "policy_fallback":
        if not action_items or commit_kind != "scope_operations":
            raise RuntimeError("required_mode fallback did not use scope operations")
        differential_items = operations
    elif expected_route == "structural":
        if actions is not None or commit_kind != "scope_operations":
            raise RuntimeError("structural file edit was misclassified")
        differential_items = operations
    elif expected_route == "runtime":
        if not action_items or commit_kind != "action_plan" \
                or not _RUNTIME_OVERLAY_MATCHES(
                    idle_runtime, candidate_runtime, action_items):
            raise RuntimeError("runtime file edit has no exact action plan")
        differential_items = action_items
    else:
        raise RuntimeError("unsupported local JSON file route")
    if item_key not in ("operations", "actions") or not differential_items:
        raise RuntimeError("file transition differential payload is empty")
    if expected_route != "runtime" \
            and _canonical_bytes(idle_storage) != _canonical_bytes(target_storage):
        raise RuntimeError("scope-operation file commit differs from its target")

    operation_id = str(last_operation.get("operation_id") or "")
    if re.fullmatch(r"file:[A-Za-z0-9._:-]+", operation_id) is None:
        raise RuntimeError("file-WAL operation identity is malformed")
    commit_operation_id = operation_id + ":scope-fallback" \
        if expected_route == "policy_fallback" else operation_id
    if last_operation.get("mode") != "node_local":
        raise RuntimeError("file-WAL mode is not node_local")

    scope_baseline = last_operation.get("scope_baseline")
    scope_baseline = scope_baseline if isinstance(scope_baseline, dict) else {}
    expected_node_scope = "node:" + str(authority["node_id"])
    base_epoch = _strict_non_negative_int(
        scope_baseline.get("authority_epoch"), "file-WAL authority epoch")
    base_revision = _strict_non_negative_int(
        scope_baseline.get("revision"), "file-WAL base scope revision")
    base_fingerprint = str(scope_baseline.get("fingerprint") or "")
    if scope_baseline.get("scope") != expected_node_scope \
            or not _valid_fingerprint(base_fingerprint) \
            or base_fingerprint != baseline_fingerprint:
        raise RuntimeError("file-WAL scope baseline differs from the exact file")

    authority_commit = last_operation.get("authority_commit")
    authority_commit = authority_commit \
        if isinstance(authority_commit, dict) else {}
    epoch = _strict_non_negative_int(
        authority_commit.get("authority_epoch"), "commit authority epoch")
    observed_base = _strict_non_negative_int(
        authority_commit.get("observed_base_scope_revision"),
        "commit base scope revision")
    scope_revision = _strict_non_negative_int(
        authority_commit.get("scope_revision"), "commit scope revision")
    fingerprint = str(authority_commit.get("fingerprint") or "")
    mirror_id = _canonical_uuid(
        authority_commit.get("mirror_id"), "commit mirror identity")
    if authority_commit.get("authority_mode") != "node_local" \
            or authority_commit.get("scope") != expected_node_scope \
            or authority_commit.get("status") \
            != "node_authoritative_committed" \
            or authority_commit.get("changed") is not True \
            or authority_commit.get("commit_kind") != commit_kind \
            or epoch != base_epoch or observed_base != base_revision \
            or scope_revision != base_revision + 1 \
            or fingerprint != committed_fingerprint:
        raise RuntimeError("terminal file-WAL authority receipt differs")

    sem_result = last_operation.get("sem_result")
    sem_result = sem_result if isinstance(sem_result, dict) else {}
    post_commit = sem_result.get("post_commit")
    post_commit = post_commit if isinstance(post_commit, dict) else {}
    if sem_result.get("ok") is not True or sem_result.get("changed") is not True \
            or post_commit.get("ok") is not True \
            or post_commit.get("status") != "node_authoritative_committed" \
            or post_commit.get("authority_epoch") != epoch \
            or post_commit.get("scope_revision") != scope_revision \
            or post_commit.get("fingerprint") != fingerprint \
            or post_commit.get("mirror_id") != mirror_id \
            or wal.get("last_result") != sem_result:
        raise RuntimeError("terminal file-WAL SEM result differs from authority")
    return {
        "operation_id": operation_id,
        "commit_operation_id": commit_operation_id,
        "file_route": expected_route,
        "mode": "node_local",
        "commit_kind": commit_kind,
        "baseline_fingerprint": baseline_fingerprint,
        "candidate_fingerprint": target_fingerprint,
        "differential_operation": expected_operation,
        "differential_items": copy.deepcopy(differential_items),
        "differential_items_sha256": _items_fingerprint(differential_items),
        "full_scope_operations_sha256": _items_fingerprint(operations),
        "authority_epoch": epoch,
        "base_scope_revision": base_revision,
        "scope_revision": scope_revision,
        "scope": expected_node_scope,
        "fingerprint": fingerprint,
        "mirror_id": mirror_id,
    }


def _database_rows(database, statement, parameters):
    uri = "file:{}?mode=ro".format(database.as_posix())
    connection = sqlite3.connect(uri, uri=True, timeout=2.0)
    connection.row_factory = sqlite3.Row
    try:
        return connection.execute(statement, tuple(parameters)).fetchall()
    finally:
        connection.close()


def _authority_journal_evidence(database, expected):
    rows = _database_rows(
        database,
        """
        SELECT record_id,kind,scope,authority_epoch,scope_revision,
               execution_epoch,fingerprint,record_json,created_at
          FROM authority_journal
         WHERE kind='scope_commit' AND scope=? AND authority_epoch=?
           AND scope_revision=? AND fingerprint=?
         ORDER BY created_at,record_id
        """,
        (
            expected["scope"], expected["authority_epoch"],
            expected["scope_revision"], expected["fingerprint"],
        ),
    )
    matches = []
    for row in rows:
        record = json.loads(row["record_json"] or "{}")
        payload = record.get("payload") if isinstance(record, dict) else None
        payload = payload if isinstance(payload, dict) else {}
        if payload.get("operation_id") != expected["operation_id"]:
            continue
        if record.get("format") != "authority_journal_v1" \
                or record.get("record_id") != row["record_id"] \
                or record.get("kind") != row["kind"] \
                or record.get("scope") != row["scope"] \
                or record.get("authority_epoch") != row["authority_epoch"] \
                or record.get("scope_revision") != row["scope_revision"] \
                or record.get("execution_epoch") != row["execution_epoch"] \
                or record.get("fingerprint") != row["fingerprint"] \
                or not isinstance(record.get("created_at_s"), (int, float)) \
                or isinstance(record.get("created_at_s"), bool):
            raise RuntimeError("authority journal row differs from record JSON")
        expected_source = expected.get("source")
        expected_principal = expected.get("principal")
        if expected_source is not None \
                and payload.get("source") != expected_source:
            raise RuntimeError("authority journal SEM source differs")
        if expected_principal is not None \
                and payload.get("principal") != expected_principal:
            raise RuntimeError("authority journal principal differs")
        if isinstance(payload.get("sem_revision"), bool) \
                or not isinstance(payload.get("sem_revision"), int) \
                or payload.get("sem_revision") <= 0:
            raise RuntimeError("authority journal SEM revision is invalid")
        matches.append({
            "record_id": str(row["record_id"]),
            "format": str(record["format"]),
            "event_type": str(payload.get("event_type") or ""),
            "source": payload.get("source"),
            "principal": payload.get("principal"),
            "sem_revision": int(payload["sem_revision"]),
        })
    if len(matches) != 1:
        raise RuntimeError("exact authority journal commit is absent or duplicated")
    return matches[0]


def _mirror_evidence(database, expected):
    rows = _database_rows(
        database,
        """
        SELECT mirror_id,scope,authority_epoch,scope_revision,fingerprint,
               state,attempts,record_json,created_at,updated_at
          FROM mirror_outbox WHERE mirror_id=? LIMIT 2
        """,
        (expected["mirror_id"],),
    )
    if len(rows) != 1:
        raise RuntimeError("exact Worker mirror record is absent or duplicated")
    row = rows[0]
    record = json.loads(row["record_json"] or "{}")
    snapshot = record.get("snapshot") if isinstance(record, dict) else None
    snapshot = snapshot if isinstance(snapshot, dict) else {}
    snapshot_config = snapshot.get("config")
    snapshot_fingerprint = _fingerprint(
        _PROJECT_STORAGE(snapshot_config)) \
        if isinstance(snapshot_config, dict) \
        and callable(_PROJECT_STORAGE) else ""
    if row["state"] != "acked" \
            or record.get("format") != "worker_mirror_v1" \
            or record.get("mirror_id") != row["mirror_id"] \
            or record.get("command") != "distributed.mirror.snapshot" \
            or record.get("state") != "pending" \
            or record.get("scope") != row["scope"] \
            or record.get("authority_epoch") != row["authority_epoch"] \
            or record.get("scope_revision") != row["scope_revision"] \
            or record.get("fingerprint") != row["fingerprint"] \
            or row["scope"] != expected["scope"] \
            or row["authority_epoch"] != expected["authority_epoch"] \
            or row["scope_revision"] != expected["scope_revision"] \
            or row["fingerprint"] != expected["fingerprint"] \
            or not isinstance(row["attempts"], int) or row["attempts"] < 1 \
            or snapshot.get("format") != "authority_snapshot_v1" \
            or snapshot.get("scope") != expected["scope"] \
            or snapshot.get("authority_epoch") != expected["authority_epoch"] \
            or snapshot.get("scope_revision") != expected["scope_revision"] \
            or snapshot.get("fingerprint") != expected["fingerprint"] \
            or not isinstance(snapshot_config, dict) \
            or snapshot_fingerprint != expected["fingerprint"]:
        raise RuntimeError("Worker mirror ACK tuple differs from local commit")
    return {
        "mirror_id": str(row["mirror_id"]),
        "format": str(record["format"]),
        "state": str(row["state"]),
        "attempts": int(row["attempts"]),
        "snapshot_format": str(snapshot["format"]),
    }

def _atomic_write(path, value):
    payload = _canonical_bytes(value) + b"\n"
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=".worker-file-e2e-", suffix=".tmp", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        _fsync_directory(path.parent)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _fsync_directory(path):
    if hasattr(os, "O_DIRECTORY"):
        directory_fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)


def _state(config, state_id):
    for item in config.get("process_states") or ():
        if isinstance(item, dict) and str(item.get("state_id")) == str(state_id):
            return item
    raise RuntimeError("process state {} is absent".format(state_id))


def _process(config, process_id):
    for item in config.get("processes") or ():
        if isinstance(item, dict) and str(item.get("p_id")) == str(process_id):
            return item
    raise RuntimeError("process {} is absent".format(process_id))


def _rule(config, rule_id):
    for item in config.get("rules") or ():
        if isinstance(item, dict) and str(item.get("rule_id")) == str(rule_id):
            return item
    raise RuntimeError("rule {} is absent".format(rule_id))


def _owned_controller(config, node_id, controller_uuid):
    owners = {
        str(item.get("controller_uuid"))
        for item in config.get("site_controller_map") or ()
        if isinstance(item, dict)
        and str(item.get("node_id")) == str(node_id)
        and str(item.get("routing_role") or "owner") == "owner"
    }
    for controller in config.get("controllers") or ():
        if not isinstance(controller, dict):
            continue
        identity = str(controller.get("controller_uuid") or "")
        if identity == str(controller_uuid) and identity in owners:
            tags = controller.get("tags")
            if isinstance(tags, list) and all(isinstance(item, str) for item in tags):
                return controller
    raise RuntimeError(
        "actuator-524 controller is not safely taggable or owned by this Worker")


def _controller_by_uuid(config, controller_uuid):
    for controller in config.get("controllers") or ():
        if isinstance(controller, dict) and str(
                controller.get("controller_uuid")) == str(controller_uuid):
            return controller
    raise RuntimeError("owned controller disappeared")


def _io_group(config, section, controller_uuid):
    rows = [
        row for row in config.get(section) or ()
        if isinstance(row, dict) and "rows" not in row
        and str(row.get("controller_uuid") or "") == str(controller_uuid)
    ]
    if rows:
        return {"rows": rows}
    raise RuntimeError("owned controller has no flat V9 {} entities".format(section))


def _append_io_entity(config, section, row):
    id_field = "sensor_id" if section == "sensors" else "actuator_id"
    rows = config.get(section)
    if not isinstance(rows, list) or not isinstance(row, dict) or "rows" in row:
        raise RuntimeError("cannot append a non-flat V9 I/O entity")
    identity = (str(row.get("controller_uuid") or ""), str(row.get(id_field)))
    if not identity[0] or not identity[1] or any(
            isinstance(item, dict)
            and (str(item.get("controller_uuid") or ""), str(item.get(id_field))) == identity
            for item in rows):
        raise RuntimeError("V9 I/O entity identity is missing or duplicated")
    same_controller = [
        index for index, item in enumerate(rows)
        if isinstance(item, dict)
        and str(item.get("controller_uuid") or "") == identity[0]
    ]
    insert_at = same_controller[-1] + 1 if same_controller else len(rows)
    rows.insert(insert_at, copy.deepcopy(row))


def _remove_io_entity(config, selector, io_id):
    section, controller_uuid = selector
    id_field = "sensor_id" if section == "sensors" else "actuator_id"
    rows = config.get(section)
    if not isinstance(rows, list):
        raise RuntimeError("V9 I/O section is absent")
    config[section] = [
        row for row in rows
        if not isinstance(row, dict)
        or str(row.get("controller_uuid") or "") != str(controller_uuid)
        or str(row.get(id_field)) != str(io_id)
    ]


def _io_rows(config, section):
    for group in config.get(section) or ():
        if not isinstance(group, dict):
            continue
        rows = group.get("rows")
        if isinstance(rows, list):
            for row in rows:
                if isinstance(row, dict):
                    resolved = dict(row)
                    for key in ("controller_uuid", "controller_key", "node_id"):
                        if resolved.get(key) is None and group.get(key) is not None:
                            resolved[key] = group.get(key)
                    yield resolved
        else:
            yield group


def _actuator_rows(config):
    return _io_rows(config, "actuators")


def _rule_target_state_ids(config):
    result = set()
    for rule in config.get("rules") or ():
        if not isinstance(rule, dict) or rule.get("enabled") is False \
                or rule.get("activation_state") == "inactive":
            continue
        for section in ("do", "else_do"):
            for action in rule.get(section) or ():
                if isinstance(action, dict):
                    result.update(str(value) for value in action.get("state_ids") or ())
    return result


def _required_mode_id(config):
    engines = [
        item for item in config.get("rule_engine") or ()
        if isinstance(item, dict) and item.get("enabled") is not False
        and item.get("activation_state") != "inactive"
        and item.get("required_mode_id") is not None
    ]
    if len(engines) != 1:
        raise RuntimeError("file-E2E requires one enabled RuleEngine mode policy")
    return engines[0]["required_mode_id"]


def _stable_pid_state(config, controller_uuid):
    targeted = _rule_target_state_ids(config)
    for state in config.get("process_states") or ():
        if not isinstance(state, dict) or str(state.get("state_id")) in targeted:
            continue
        strategy = state.get("control_strategy")
        parameters = state.get("parameters")
        refs = state.get("io_refs")
        if not isinstance(strategy, dict) or strategy.get("type") != "PID" \
                or not isinstance(parameters, dict) \
                or not isinstance(parameters.get("target_temp_c"), (int, float)) \
                or isinstance(parameters.get("target_temp_c"), bool) \
                or not isinstance(refs, dict):
            continue
        identities = {
            str(item.get("controller_uuid") or "")
            for section in ("sensors", "actuators")
            for item in refs.get(section) or ()
            if isinstance(item, dict)
        }
        if identities == {str(controller_uuid)}:
            return state
    raise RuntimeError("no stable PID state exists for the file-E2E matrix")


def _next_unused(used, start, maximum):
    value = int(start)
    while value in used and value <= int(maximum):
        value += 1
    if value > int(maximum):
        raise RuntimeError("no bounded E2E identifier/address is available")
    return value


def _matrix_context(config, controller_uuid, marker):
    sensor_group = _io_group(config, "sensors", controller_uuid)
    actuator_group = _io_group(config, "actuators", controller_uuid)
    sensor_ids = {int(row.get("sensor_id")) for row in sensor_group["rows"]}
    sensor_addresses = {int(row.get("address")) for row in sensor_group["rows"]}
    actuator_ids = {int(row.get("actuator_id")) for row in actuator_group["rows"]}
    actuator_addresses = {int(row.get("address")) for row in actuator_group["rows"]}
    sensor_id = _next_unused(sensor_ids, 60000, 65535)
    actuator_id = _next_unused(actuator_ids, 60000, 65535)
    sensor_row = copy.deepcopy(sensor_group["rows"][-1])
    sensor_row.update({
        "sensor_id": sensor_id,
        "address": _next_unused(sensor_addresses, 14, 63),
        "sensor_group": "E2E_" + marker,
        "bus_type": "modbus",
        "function_type": "read_holding_registers",
    })
    actuator_row = copy.deepcopy(actuator_group["rows"][-1])
    actuator_row.update({
        "actuator_id": actuator_id,
        "address": _next_unused(actuator_addresses, 547, 599),
        "actuator_group": "E2E_" + marker,
        "bus_type": "modbus",
        "function_type": "write_register",
    })
    pid = _stable_pid_state(config, controller_uuid)
    required_mode = _required_mode_id(config)
    modes = [
        item.get("mode_id") for item in config.get("process_modes") or ()
        if isinstance(item, dict) and item.get("mode_id") is not None
    ]
    values = [
        item.get("value_id") for item in config.get("process_values") or ()
        if isinstance(item, dict) and item.get("value_id") is not None
    ]
    next_mode = required_mode if required_mode in modes else None
    next_value = next(
        (value for value in values if str(value) != str(pid.get("value_id"))), None)
    threshold = (_rule(config, 1).get("when") or [{}])[0].get("value")
    process = _process(config, pid.get("p_id"))
    process_location = str(process.get("process_location") or "")
    if next_mode is None or next_value is None \
            or not isinstance(threshold, (int, float)) or isinstance(threshold, bool):
        raise RuntimeError("file-E2E runtime matrix has no safe alternate values")
    if str(pid.get("mode_id")) == str(required_mode):
        raise RuntimeError("file-E2E PID state does not exercise required_mode routing")
    return {
        "pid_state_id": pid.get("state_id"),
        "required_mode_id": required_mode,
        "mode_before": pid.get("mode_id"),
        "mode_after": next_mode,
        "value_before": pid.get("value_id"),
        "value_after": next_value,
        "target_before": pid["parameters"]["target_temp_c"],
        "target_after": 400.0,
        "threshold_before": threshold,
        "threshold_after": float(threshold) + 0.5,
        "process_id": process.get("p_id"),
        "process_location_before": process_location,
        "process_location_after": process_location + "|" + marker,
        "sensor_id": sensor_id,
        "sensor_address": sensor_row["address"],
        "sensor_row": sensor_row,
        "actuator_id": actuator_id,
        "actuator_address": actuator_row["address"],
        "actuator_row": actuator_row,
    }


def _io_present(config, details):
    id_field = "sensor_id" if details["section"] == "sensors" else "actuator_id"
    for row in _io_rows(config, details["section"]):
        if str(row.get("controller_uuid") or "") == str(details["controller_uuid"]) \
                and str(row.get(id_field)) == str(details["io_id"]):
            return True
    return False


def _verify_actuator_524_contract(config):
    state = _state(config, 1)
    references = (state.get("io_refs") or {}).get("actuators") or ()
    reference = next(
        (item for item in references if isinstance(item, dict)
         and str(item.get("actuator_id")) == "3"), None)
    if not reference:
        raise RuntimeError("state 1 does not reference actuator 3")
    controller_uuid = str(reference.get("controller_uuid") or "")
    for actuator in _actuator_rows(config):
        if (str(actuator.get("controller_uuid") or "") == controller_uuid
                and str(actuator.get("actuator_id")) == "3"
                and int(actuator.get("address") or -1) == 524):
            return {"controller_uuid": controller_uuid, "actuator_id": 3,
                    "address": 524, "function_type": "write_register"}
    raise RuntimeError("actuator 3 / register 524 mapping is absent")


def _modbus_target(config, actuator, approved_endpoint):
    candidates = [
        item for item in config.get("virtual_controllers") or ()
        if isinstance(item, dict)
        and str(item.get("controller_uuid") or "")
        == str(actuator["controller_uuid"])
        and item.get("virt_active") is True
    ]
    if len(candidates) != 1:
        raise RuntimeError("actuator 524 has no unique active virtual endpoint")
    endpoint = candidates[0]
    host = str(endpoint.get("host") or "")
    port = int(endpoint.get("port") or 0)
    if host != str(approved_endpoint.get("host")) \
            or port != int(approved_endpoint.get("port") or 0):
        raise RuntimeError(
            "actuator 524 endpoint differs from the approved Modbus fixture")
    controller = _controller_by_uuid(config, actuator["controller_uuid"])
    unit_value = controller.get("unit_id")
    if unit_value is None:
        unit_value = controller.get("device_id")
    if unit_value is None:
        unit_value = controller.get("unit", 1)
    unit_id = int(str(unit_value), 0) if isinstance(unit_value, str) \
        else int(unit_value)
    if not 1 <= port <= 65535 or not 0 <= unit_id <= 255:
        raise RuntimeError("actuator 524 Modbus target is invalid")
    return {
        "host": host,
        "port": port,
        "unit_id": unit_id,
        "controller_uuid": str(actuator["controller_uuid"]),
        "actuator_id": int(actuator["actuator_id"]),
        "address": int(actuator["address"]),
        "function_type": str(actuator["function_type"]),
    }


def _receive_exact(connection, size):
    result = bytearray()
    while len(result) < int(size):
        chunk = connection.recv(int(size) - len(result))
        if not chunk:
            raise RuntimeError("Modbus fixture closed the connection")
        result.extend(chunk)
    return bytes(result)


def _modbus_exchange(target, pdu):
    transaction_id = int(time.monotonic_ns() % 65535) + 1
    request = struct.pack(
        ">HHHB", transaction_id, 0, len(pdu) + 1, target["unit_id"]
    ) + pdu
    with socket.create_connection(
            (target["host"], target["port"]), timeout=5.0) as connection:
        connection.settimeout(5.0)
        connection.sendall(request)
        header = _receive_exact(connection, 7)
        response_transaction, protocol_id, length, unit_id = struct.unpack(
            ">HHHB", header)
        if response_transaction != transaction_id or protocol_id != 0 \
                or unit_id != target["unit_id"] or not 2 <= length <= 254:
            raise RuntimeError("Modbus fixture returned an invalid MBAP header")
        response = _receive_exact(connection, length - 1)
    if response[0] & 0x80:
        raise RuntimeError("Modbus fixture returned exception " + str(response[1]))
    return response


def _read_modbus_register(target):
    response = _modbus_exchange(
        target, struct.pack(">BHH", 3, target["address"], 1))
    if len(response) != 4 or response[0] != 3 or response[1] != 2:
        raise RuntimeError("Modbus register read response differs")
    return int(struct.unpack(">H", response[2:4])[0])


def _write_modbus_register(target, value):
    if target.get("function_type") != "write_register" \
            or not isinstance(value, int) or isinstance(value, bool) \
            or not 0 <= value <= 65535:
        raise RuntimeError("Modbus register restore value is unsafe")
    request = struct.pack(">BHH", 6, target["address"], value)
    response = _modbus_exchange(target, request)
    if response != request:
        raise RuntimeError("Modbus register restore echo differs")
    observed = _read_modbus_register(target)
    if observed != value:
        raise RuntimeError("Modbus register restore readback differs")
    return observed


def _managed_projection(config, details):
    state = _state(config, 1)
    pid = _state(config, details["pid_state_id"])
    rule = _rule(config, 1)
    controller = _controller_by_uuid(config, details["controller_uuid"])
    process = _process(config, pid.get("p_id"))
    when = rule.get("when") or [{}]
    return {
        "state_1_value_id": state.get("value_id"),
        "state_1_mode_id": state.get("mode_id"),
        "rule_1_enabled": rule.get("enabled", True),
        "rule_1_threshold": when[0].get("value"),
        "pid_state_id": pid.get("state_id"),
        "pid_mode_id": pid.get("mode_id"),
        "pid_value_id": pid.get("value_id"),
        "pid_target_temp_c": (pid.get("parameters") or {}).get("target_temp_c"),
        "process_id": process.get("p_id"),
        "process_location": process.get("process_location"),
        "controller_tags": list(controller.get("tags") or ()),
        "marker_present": details["marker"] in (controller.get("tags") or ()),
        "sensor_present": _io_present(config, {
            "section": "sensors",
            "controller_uuid": details["controller_uuid"],
            "io_id": details["sensor_id"],
        }),
        "actuator_present": _io_present(config, {
            "section": "actuators",
            "controller_uuid": details["controller_uuid"],
            "io_id": details["actuator_id"],
        }),
    }


def _matches(config, expected):
    required = (
        "controller_uuid", "marker", "pid_state_id", "sensor_id", "actuator_id",
    )
    if any(expected.get(key) is None for key in required):
        return False
    projection = _managed_projection(config, expected)
    for key, value in expected.items():
        if key in projection and projection[key] != value:
            return False
    return True


def _scope_head_evidence(database, node_id):
    scope = "node:" + str(node_id)
    rows = _database_rows(
        database,
        """
        SELECT scope,authority_mode,authority_epoch,scope_revision,
               execution_epoch,fingerprint,locked,
               node_authoritative_committed
          FROM scope_heads WHERE scope=? LIMIT 2
        """,
        (scope,),
    )
    if len(rows) != 1:
        raise RuntimeError("Worker node authority head is absent or duplicated")
    row = rows[0]
    epoch = _strict_non_negative_int(
        row["authority_epoch"], "scope-head authority epoch")
    revision = _strict_non_negative_int(
        row["scope_revision"], "scope-head revision")
    execution_epoch = _strict_non_negative_int(
        row["execution_epoch"], "scope-head execution epoch")
    fingerprint = str(row["fingerprint"] or "")
    if row["scope"] != scope or row["authority_mode"] != "node_local" \
            or row["locked"] != 0 \
            or row["node_authoritative_committed"] != 1 \
            or not _valid_fingerprint(fingerprint):
        raise RuntimeError("Worker node authority head violates Authority V5")
    return {
        "scope": scope,
        "authority_mode": "node_local",
        "authority_epoch": epoch,
        "scope_revision": revision,
        "execution_epoch": execution_epoch,
        "fingerprint": fingerprint,
    }


def _latest_revision(database, node_id):
    return _scope_head_evidence(database, node_id)["scope_revision"]


def _mirror_state(database, mirror_id):
    rows = _database_rows(
        database,
        "SELECT state FROM mirror_outbox WHERE mirror_id=? LIMIT 2",
        (str(mirror_id),),
    )
    if len(rows) > 1:
        raise RuntimeError("Worker mirror identity is duplicated")
    return str(rows[0]["state"]) if rows else ""


def _node_local_relay_proof(database, expected, node_id):
    operation_id = _canonical_uuid(
        expected.get("operation_id"), "Core node-local operation identity")
    relay_message_id = str(expected.get("relay_message_id") or "")
    if relay_message_id != "relay:" + operation_id:
        raise RuntimeError("Core relay identity differs from operation identity")
    scope = "node:" + str(node_id)
    rows = _database_rows(
        database,
        """
        SELECT message_id,payload_hash,state,result_json,created_at,updated_at
          FROM inbox
         WHERE command='distributed.config.propose' AND message_id=?
         LIMIT 2
        """,
        (relay_message_id,),
    )
    if len(rows) != 1:
        raise RuntimeError("current Core proposal has no unique Worker inbox row")
    row = rows[0]
    result = json.loads(row["result_json"] or "{}")
    post_commit = result.get("post_commit") \
        if isinstance(result, dict) else None
    post_commit = post_commit if isinstance(post_commit, dict) else {}
    payload_hash = str(row["payload_hash"] or "")
    epoch = expected["authority_epoch"]
    target_revision = expected["target_scope_revision"]
    fingerprint = expected["target_scope_fingerprint"]
    mirror_id = _canonical_uuid(
        post_commit.get("mirror_id"), "Core proposal mirror identity")
    if row["state"] != "DONE" \
            or not _valid_fingerprint(payload_hash) \
            or not isinstance(result, dict) or result.get("ok") is not True \
            or result.get("code") != "accepted" \
            or result.get("status") != "accepted" \
            or result.get("operation_id") != operation_id \
            or result.get("principal") != "master_relay" \
            or result.get("authority_epoch") != epoch \
            or result.get("scope_revision") != target_revision \
            or result.get("fingerprint") != fingerprint \
            or post_commit.get("ok") is not True \
            or post_commit.get("status") \
            != "node_authoritative_committed" \
            or post_commit.get("authority_epoch") != epoch \
            or post_commit.get("scope_revision") != target_revision \
            or post_commit.get("fingerprint") != fingerprint:
        raise RuntimeError("Core proposal Worker inbox result differs")
    journal = _authority_journal_evidence(database, {
        "scope": scope,
        "authority_epoch": epoch,
        "scope_revision": target_revision,
        "fingerprint": fingerprint,
        "operation_id": operation_id,
        "source": "external_proposal",
        "principal": "master_relay",
    })
    mirror = _mirror_evidence(database, {
        "mirror_id": mirror_id,
        "scope": scope,
        "authority_epoch": epoch,
        "scope_revision": target_revision,
        "fingerprint": fingerprint,
    })
    return {
        "commit_origin": "canonical_client_proxy_master_relay_worker_local",
        "transport_command": "distributed.config.propose",
        "transport_mode": "canonical_scope_proposal",
        "operation_id": operation_id,
        "relay_message_id": relay_message_id,
        "operation_format": expected["operation_format"],
        "operation_count": expected["operation_count"],
        "authority_epoch": epoch,
        "base_scope_revision": expected["base_scope_revision"],
        "target_scope": scope,
        "target_scope_revision": target_revision,
        "target_scope_fingerprint": fingerprint,
        "node_replica_revision": expected["node_replica_revision"],
        "node_replica_fingerprint": expected["node_replica_fingerprint"],
        "worker_inbox_state": str(row["state"]),
        "worker_payload_sha256": payload_hash,
        "journal_record_id": journal["record_id"],
        "authority_journal_correlated": True,
        "mirror_id": mirror["mirror_id"],
        "mirror_state": mirror["state"],
        "mirror_ack_correlated": True,
        "current_run_correlated": True,
        "worker_applied": True,
    }


def _transition(paths, target, request):
    phase = request["phase"]
    expected_scope = request["expected_scope"]
    expected_route = _required_transition_route(request)
    minimum_revision = int(request["minimum_revision"])
    timeout_s = float(request["timeout_s"])
    expected = dict(request["expected"])
    baseline = _require_v9_storage(
        _read_object(paths["config"], "Worker config"),
        "local JSON transition baseline")
    _require_v9_storage(target, "local JSON transition target")
    meta = target.get("meta") or ()
    target_node_id = str(meta[0].get("node_id") or "") \
        if meta and isinstance(meta[0], dict) else ""
    if not target_node_id or expected_scope != "worker_node_local":
        raise RuntimeError("local JSON transition has no Worker node authority")
    _atomic_write(paths["config"], target)
    deadline = time.monotonic() + timeout_s
    last_summary = {}
    while time.monotonic() < deadline:
        try:
            wal = _read_object(paths["wal"], "file-proposal WAL")
            evidence = _validated_wal_evidence(wal, {
                "baseline": baseline,
                "target": target,
                "authority": {
                    "scope": expected_scope,
                    "route": expected_route,
                    "node_id": target_node_id,
                },
            })
            visible = _require_v9_storage(
                _read_object(paths["config"], "Worker config"),
                "Worker config")
            semantic_match = _matches(visible, expected)
            last_summary = {
                "wal_state": str(wal.get("state") or ""),
                "last_operation_id": str(
                    (wal.get("last_operation") or {}).get("operation_id") or ""),
                "semantic_match": semantic_match,
                "scope_revision": (
                    evidence.get("scope_revision") if evidence else None),
                "mirror_state": (
                    _mirror_state(paths["database"], evidence["mirror_id"])
                    if evidence else ""),
            }
            if evidence is not None and semantic_match:
                if evidence["scope_revision"] <= minimum_revision:
                    raise RuntimeError(
                        "file transition did not advance the node scope revision")
                mirror_state = _mirror_state(
                    paths["database"], evidence["mirror_id"])
                if mirror_state != "acked":
                    time.sleep(0.2)
                    continue
                source = "worker_local_automation" \
                    if evidence["commit_kind"] == "action_plan" \
                    else "external_proposal"
                journal_expected = {
                    "scope": evidence["scope"],
                    "authority_epoch": evidence["authority_epoch"],
                    "scope_revision": evidence["scope_revision"],
                    "fingerprint": evidence["fingerprint"],
                    "operation_id": evidence["commit_operation_id"],
                    "source": source,
                }
                if source == "external_proposal":
                    journal_expected["principal"] = "worker_file_sync"
                journal = _authority_journal_evidence(
                    paths["database"], journal_expected)
                mirror = _mirror_evidence(paths["database"], {
                    "mirror_id": evidence["mirror_id"],
                    "scope": evidence["scope"],
                    "authority_epoch": evidence["authority_epoch"],
                    "scope_revision": evidence["scope_revision"],
                    "fingerprint": evidence["fingerprint"],
                })
                differential_items = evidence["differential_items"]
                return {
                    "phase": phase,
                    "authority_scope": expected_scope,
                    "authority_epoch": evidence["authority_epoch"],
                    "base_scope_revision": evidence["base_scope_revision"],
                    "scope_revision": evidence["scope_revision"],
                    "fingerprint": evidence["fingerprint"],
                    "operation_id": evidence["operation_id"],
                    "commit_operation_id": evidence["commit_operation_id"],
                    "mirror_id": evidence["mirror_id"],
                    "mirror_state": mirror["state"],
                    "mirror_attempts": mirror["attempts"],
                    "observed_modes": ["node_local"],
                    "semantic_postcondition": True,
                    "local_commit_ok": True,
                    "authority_journal_correlated": True,
                    "journal_record_id": journal["record_id"],
                    "journal_format": journal["format"],
                    "mirror_ack_correlated": True,
                    "mirror_format": mirror["format"],
                    "commit_origin": "local_json_file_sem_node_authority",
                    "file_route": evidence["file_route"],
                    "file_wal_mode": evidence["mode"],
                    "file_commit_kind": evidence["commit_kind"],
                    "wal_authority_correlated": True,
                    "wal_baseline_fingerprint": evidence[
                        "baseline_fingerprint"],
                    "wal_candidate_fingerprint": evidence[
                        "candidate_fingerprint"],
                    "differential_operation": evidence[
                        "differential_operation"],
                    "differential_operation_count": len(differential_items),
                    "differential_items": copy.deepcopy(differential_items),
                    "differential_items_sha256": evidence[
                        "differential_items_sha256"],
                    "full_scope_operations_sha256": evidence[
                        "full_scope_operations_sha256"],
                    "target_scope": evidence["scope"],
                    "target_scope_revision": evidence["scope_revision"],
                    "target_scope_fingerprint": evidence["fingerprint"],
                }
            last_result = wal.get("last_result")
            if isinstance(last_result, dict) and last_result.get("ok") is False \
                    and wal.get("state") == "IDLE":
                raise RuntimeError(
                    "file proposal was rejected: {} {}".format(
                        last_result.get("code"),
                        last_result.get("message") or ""))
        except (FileNotFoundError, json.JSONDecodeError, sqlite3.OperationalError):
            pass
        time.sleep(0.2)
    raise RuntimeError(
        "{} did not converge within {:.1f}s: {}".format(
            phase, timeout_s, json.dumps(last_summary, sort_keys=True)))

def _log_anchor(path):
    details = path.lstat()
    if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
        raise RuntimeError("Worker log is not a regular non-symlink file")
    return {"device": details.st_dev, "inode": details.st_ino,
            "offset": details.st_size}


def _wait_actuator_524(path, anchor, timeout_s):
    pattern = re.compile(rb"WRITE: aid=3 addr=524 .*ok=True")
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        try:
            details = path.lstat()
            if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
                raise RuntimeError("Worker log became unsafe")
            same_file = (details.st_dev, details.st_ino) == (
                anchor["device"], anchor["inode"])
            offset = anchor["offset"] if same_file and details.st_size >= anchor[
                "offset"] else 0
            with path.open("rb") as stream:
                stream.seek(offset)
                payload = stream.read(4 * 1024 * 1024)
            if pattern.search(payload):
                return {
                    "actuator_id": 3,
                    "address": 524,
                    "readback_verified": True,
                    "new_log_evidence": True,
                }
        except FileNotFoundError:
            pass
        time.sleep(0.25)
    raise RuntimeError(
        "actuator 3 / register 524 produced no new readback-verified write")


def _restore_target(paths, context):
    current = _read_object(paths["config"], "Worker config")
    current_state_1 = _state(current, 1)
    original_state_1 = _state(context["original"], 1)
    current_state_1["value_id"] = original_state_1.get("value_id")
    current_state_1["mode_id"] = original_state_1.get("mode_id")
    _rule(current, 1)["enabled"] = _rule(
        context["original"], 1).get("enabled", True)
    _rule(current, 1)["when"][0]["value"] = _rule(
        context["original"], 1)["when"][0].get("value")
    current_pid = _state(current, context["pid_state_id"])
    original_pid = _state(context["original"], context["pid_state_id"])
    current_pid["mode_id"] = original_pid.get("mode_id")
    current_pid["value_id"] = original_pid.get("value_id")
    current_pid.setdefault("parameters", {})["target_temp_c"] = (
        original_pid.get("parameters") or {}).get("target_temp_c")
    current_process = _process(current, current_pid.get("p_id"))
    original_process = _process(context["original"], original_pid.get("p_id"))
    current_process["process_location"] = original_process.get("process_location")
    controller = _controller_by_uuid(current, context["controller_uuid"])
    original_controller = _controller_by_uuid(
        context["original"], context["controller_uuid"])
    controller["tags"] = copy.deepcopy(original_controller.get("tags") or [])
    for section, id_field, io_id in (
        ("sensors", "sensor_id", context["sensor_id"]),
        ("actuators", "actuator_id", context["actuator_id"]),
    ):
        _remove_io_entity(
            current, (section, context["controller_uuid"]), io_id)
    return current


def _static_restore_matches(config, context):
    details = {
        "controller_uuid": context["controller_uuid"],
        "marker": context["marker"],
        "pid_state_id": context["pid_state_id"],
        "sensor_id": context["sensor_id"],
        "actuator_id": context["actuator_id"],
    }
    current = _managed_projection(config, details)
    original = _managed_projection(context["original"], details)
    return _canonical_bytes(current) == _canonical_bytes(original)


def _restore_details(context):
    return {
        "controller_uuid": context["controller_uuid"],
        "marker": context["marker"],
        "pid_state_id": context["pid_state_id"],
        "sensor_id": context["sensor_id"],
        "actuator_id": context["actuator_id"],
    }


def _restore_expectation(context):
    details = _restore_details(context)
    expected = _managed_projection(context["original"], details)
    expected.update(details)
    return expected


def _validate_recovery_journal(journal, node_id):
    if not isinstance(journal, dict) \
            or journal.get("journal_version") != RECOVERY_JOURNAL_VERSION \
            or journal.get("status") not in RECOVERY_STATES \
            or str(journal.get("node_id") or "") != str(node_id) \
            or re.fullmatch(r"[0-9a-f]{32}", str(
                journal.get("run_id") or "")) is None:
        raise RuntimeError("Worker file-E2E recovery journal header is invalid")
    baseline = _require_v9_storage(
        journal.get("baseline_config"), "recovery-journal V9 baseline")
    baseline_fingerprint = str(journal.get("baseline_fingerprint") or "")
    if re.fullmatch(r"[0-9a-f]{64}", baseline_fingerprint) is None \
            or _fingerprint(baseline) != baseline_fingerprint:
        raise RuntimeError("Worker file-E2E recovery baseline fingerprint differs")
    meta = baseline.get("meta") or ()
    baseline_node = str(meta[0].get("node_id") or "") \
        if meta and isinstance(meta[0], dict) else ""
    if baseline_node != str(node_id):
        raise RuntimeError("Worker file-E2E recovery baseline node differs")
    selectors = journal.get("selectors")
    selector_names = {
        "controller_uuid", "marker", "pid_state_id", "sensor_id", "actuator_id",
    }
    if not isinstance(selectors, dict) or set(selectors) != selector_names \
            or not str(selectors.get("controller_uuid") or "") \
            or re.fullmatch(
                r"worker-file-e2e-[0-9a-f]{10}",
                str(selectors.get("marker") or "")) is None \
            or any(selectors.get(name) is None for name in (
                "pid_state_id", "sensor_id", "actuator_id")):
        raise RuntimeError("Worker file-E2E recovery selectors are invalid")
    details = dict(selectors)
    baseline_projection = _managed_projection(baseline, details)
    if _canonical_bytes(journal.get("baseline_owned_projection")) \
            != _canonical_bytes(baseline_projection):
        raise RuntimeError("Worker file-E2E recovery owned projection differs")
    actuator_restore = journal.get("actuator_restore")
    if not isinstance(actuator_restore, dict) \
            or not isinstance(actuator_restore.get("may_be_mutated"), bool) \
            or not isinstance(actuator_restore.get("restore_confirmed"), bool):
        raise RuntimeError("Worker file-E2E actuator recovery state is invalid")
    target = actuator_restore.get("target")
    original_value = actuator_restore.get("original_value")
    if target is None:
        if original_value is not None or actuator_restore["may_be_mutated"]:
            raise RuntimeError("Worker file-E2E actuator baseline is incomplete")
    elif not isinstance(target, dict) \
            or set(target) != {
                "host", "port", "unit_id", "controller_uuid", "actuator_id",
                "address", "function_type",
            } \
            or target.get("controller_uuid") != selectors["controller_uuid"] \
            or target.get("actuator_id") != 3 \
            or target.get("address") != 524 \
            or target.get("function_type") != "write_register" \
            or not isinstance(original_value, int) \
            or isinstance(original_value, bool) \
            or not 0 <= original_value <= 65535:
        raise RuntimeError("Worker file-E2E actuator recovery target differs")
    baseline_revision = journal.get("baseline_scope_revision")
    latest_revision = journal.get("latest_scope_revision")
    if not isinstance(baseline_revision, int) or isinstance(
            baseline_revision, bool) or baseline_revision < 0 \
            or not isinstance(latest_revision, int) or isinstance(
                latest_revision, bool) or latest_revision < baseline_revision \
            or not isinstance(journal.get("transitions"), list):
        raise RuntimeError("Worker file-E2E recovery revision history is invalid")
    final_fingerprint = journal.get("final_fingerprint")
    if final_fingerprint is not None and re.fullmatch(
            r"[0-9a-f]{64}", str(final_fingerprint)) is None:
        raise RuntimeError("Worker file-E2E recovery final fingerprint is invalid")
    if journal.get("restore_proof") is not None \
            and not isinstance(journal.get("restore_proof"), dict):
        raise RuntimeError("Worker file-E2E recovery authority proof is invalid")
    if journal.get("status") in RECOVERY_TERMINAL_STATES \
            and (journal.get("restore_confirmed") is not True
                 or final_fingerprint != baseline_fingerprint
                 or actuator_restore.get("may_be_mutated") is True):
        raise RuntimeError("terminal Worker file-E2E recovery journal is incomplete")
    return journal


def _read_recovery_journal(paths, node_id):
    if paths["recovery"].is_symlink():
        raise RuntimeError("Worker file-E2E recovery journal is a symlink")
    if not paths["recovery"].exists():
        return None
    return _validate_recovery_journal(
        _read_object(paths["recovery"], "Worker file-E2E recovery journal"),
        node_id,
    )


def _write_recovery_journal(paths, journal):
    journal["updated_at_s"] = time.time()
    _atomic_write(paths["recovery"], journal)
    durable = _read_object(
        paths["recovery"], "Worker file-E2E recovery journal")
    if _canonical_bytes(durable) != _canonical_bytes(journal):
        raise RuntimeError("Worker file-E2E recovery journal was not durable")


def _archive_recovery_journal(paths, journal):
    source = paths["recovery"]
    target = source.with_name(source.name + ".closed")
    os.replace(source, target)
    _fsync_directory(source.parent)
    return target


def _create_recovery_journal(paths, seed):
    node_id = seed["node_id"]
    original = seed["original"]
    selectors = seed["selectors"]
    revision = seed["revision"]
    if paths["recovery"].exists() or paths["recovery"].is_symlink():
        raise RuntimeError("open Worker file-E2E recovery journal was not handled")
    now = time.time()
    journal = {
        "journal_version": RECOVERY_JOURNAL_VERSION,
        "run_id": uuid.uuid4().hex,
        "node_id": str(node_id),
        "status": "PREPARED",
        "created_at_s": now,
        "updated_at_s": now,
        "baseline_config": copy.deepcopy(original),
        "baseline_fingerprint": _fingerprint(original),
        "baseline_scope_revision": int(revision),
        "latest_scope_revision": int(revision),
        "selectors": copy.deepcopy(selectors),
        "baseline_owned_projection": _managed_projection(original, selectors),
        "actuator_restore": copy.deepcopy(seed.get("actuator_restore") or {
            "target": None,
            "original_value": None,
            "may_be_mutated": False,
            "restore_confirmed": False,
        }),
        "pending_phase": None,
        "transitions": [],
        "recovery_scope_floor": None,
        "restore_proof": None,
        "restore_confirmed": False,
        "final_fingerprint": None,
        "failure": None,
    }
    _write_recovery_journal(paths, journal)
    return _validate_recovery_journal(journal, node_id)


def _journal_begin_phase(paths, journal, phase):
    journal["status"] = "MUTATING"
    journal["pending_phase"] = str(phase)
    _write_recovery_journal(paths, journal)


def _journal_mark_actuator_pending(paths, journal):
    recovery = journal["actuator_restore"]
    if not isinstance(recovery.get("target"), dict) \
            or not isinstance(recovery.get("original_value"), int):
        raise RuntimeError("physical actuator baseline is not journaled")
    recovery["may_be_mutated"] = True
    recovery["restore_confirmed"] = False
    _write_recovery_journal(paths, journal)


def _restore_journal_actuator(paths, journal, approved_endpoint):
    recovery = journal["actuator_restore"]
    if recovery.get("may_be_mutated") is not True:
        return {"skipped": True, "restore_confirmed": True}
    target = recovery.get("target")
    if not isinstance(target, dict) \
            or not isinstance(approved_endpoint, dict) \
            or target.get("host") != str(approved_endpoint.get("host")) \
            or int(target.get("port") or 0) != int(
                approved_endpoint.get("port") or 0):
        raise RuntimeError("journaled actuator endpoint is not approved")
    original_value = recovery.get("original_value")
    observed_before_restore = _read_modbus_register(target)
    verified_noop = observed_before_restore == original_value
    write_performed = False
    if not verified_noop:
        _write_modbus_register(target, original_value)
        write_performed = True
    # Never close the durable recovery journal based on a write response or on
    # the pre-restore observation.  A separate physical readback is the commit
    # point for both the write and verified-noop paths.
    restored_value = _read_modbus_register(target)
    if restored_value != original_value:
        raise RuntimeError(
            "Worker file-E2E physical actuator restore readback differs "
            "from the journaled baseline")
    recovery["may_be_mutated"] = False
    recovery["restore_confirmed"] = True
    _write_recovery_journal(paths, journal)
    return {
        "baseline_value": original_value,
        "observed_before_restore": observed_before_restore,
        "restored_value": restored_value,
        "restore_readback_verified": True,
        "write_performed": write_performed,
        "verified_noop": verified_noop,
        "approved_endpoint": "{}:{}".format(
            target["host"], target["port"]),
    }


def _journal_record_transition(paths, journal, proof):
    journal["transitions"].append(copy.deepcopy(proof))
    journal["latest_scope_revision"] = max(
        int(journal["latest_scope_revision"]),
        int(proof.get("scope_revision") or 0),
    )
    journal["pending_phase"] = None
    if proof.get("phase") in ("structural_restore", "emergency_restore"):
        journal["restore_proof"] = copy.deepcopy(proof)
    _write_recovery_journal(paths, journal)


def _finish_recovery_journal(paths, journal, completion):
    status = completion["status"]
    final_config = completion["final_config"]
    proof = completion.get("proof")
    if status not in RECOVERY_TERMINAL_STATES:
        raise RuntimeError("invalid Worker file-E2E terminal recovery state")
    final_fingerprint = _fingerprint(final_config)
    if final_fingerprint != journal.get("baseline_fingerprint"):
        raise RuntimeError("Worker file-E2E final fingerprint differs from baseline")
    if journal["actuator_restore"].get("may_be_mutated") is True:
        raise RuntimeError("Worker file-E2E physical actuator restore is incomplete")
    journal["status"] = status
    journal["pending_phase"] = None
    journal["restore_confirmed"] = True
    journal["final_fingerprint"] = final_fingerprint
    journal["failure"] = None
    if proof is not None:
        journal["restore_proof"] = copy.deepcopy(proof)
    _write_recovery_journal(paths, journal)


def _fail_recovery_journal(paths, journal, error):
    journal["status"] = "FAILED"
    journal["restore_confirmed"] = False
    journal["failure"] = str(error)[:2048]
    _write_recovery_journal(paths, journal)


def _restore_context_from_journal(journal, timeout_s):
    selectors = journal["selectors"]
    return _restore_context(copy.deepcopy(journal["baseline_config"]), {
        "controller_uuid": selectors["controller_uuid"],
        "marker": selectors["marker"],
        "timeout_s": float(timeout_s),
        "pid_state_id": selectors["pid_state_id"],
        "sensor_id": selectors["sensor_id"],
        "actuator_id": selectors["actuator_id"],
    })


def _context_node_id(context):
    meta = context["original"].get("meta") or ()
    return str(meta[0].get("node_id") or "") \
        if meta and isinstance(meta[0], dict) else ""


def _restore_proof_is_new_local_commit(proof, context, minimum_revision):
    if not isinstance(proof, dict):
        return False
    node_id = _context_node_id(context)
    return proof.get("mirror_state") == "acked" \
        and proof.get("semantic_postcondition") is True \
        and proof.get("local_commit_ok") is True \
        and proof.get("authority_journal_correlated") is True \
        and proof.get("mirror_ack_correlated") is True \
        and int(proof.get("scope_revision") or 0) > int(minimum_revision) \
        and proof.get("authority_scope") == "worker_node_local" \
        and proof.get("target_scope") == "node:" + node_id \
        and str(proof.get("fingerprint") or "") \
        == _fingerprint(context["original"])


def _mirror_for_scope_commit(database, expected):
    rows = _database_rows(
        database,
        """
        SELECT mirror_id FROM mirror_outbox
         WHERE scope=? AND authority_epoch=? AND scope_revision=?
           AND fingerprint=?
         LIMIT 2
        """,
        (
            expected["scope"], expected["authority_epoch"],
            expected["scope_revision"], expected["fingerprint"],
        ),
    )
    if len(rows) != 1:
        raise RuntimeError("restore scope commit has no unique mirror record")
    mirror_id = str(rows[0]["mirror_id"])
    return _mirror_evidence(database, {
        "mirror_id": mirror_id,
        "scope": expected["scope"],
        "authority_epoch": expected["authority_epoch"],
        "scope_revision": expected["scope_revision"],
        "fingerprint": expected["fingerprint"],
    })


def _restore_journal_records(database, scope, minimum_revision):
    return _database_rows(
        database,
        """
        SELECT record_json FROM authority_journal
         WHERE kind='scope_commit' AND scope=? AND scope_revision>?
         ORDER BY scope_revision DESC,created_at DESC,record_id DESC
        """,
        (scope, int(minimum_revision)),
    )


def _journal_restore_proof(database, row, query):
    context = query["context"]
    minimum_revision = int(query["minimum_revision"])
    try:
        record = json.loads(row["record_json"] or "{}")
    except (TypeError, json.JSONDecodeError):
        return None
    payload = record.get("payload") if isinstance(record, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    node_id = _context_node_id(context)
    scope = "node:" + node_id
    target_fingerprint = _fingerprint(context["original"])
    operation_id = str(payload.get("operation_id") or "")
    marker = ":{}:".format(target_fingerprint[:12])
    revision = record.get("scope_revision")
    epoch = record.get("authority_epoch")
    if record.get("kind") != "scope_commit" \
            or record.get("scope") != scope \
            or record.get("fingerprint") != target_fingerprint \
            or isinstance(revision, bool) or not isinstance(revision, int) \
            or revision <= int(minimum_revision) \
            or isinstance(epoch, bool) or not isinstance(epoch, int) or epoch < 0 \
            or re.fullmatch(r"file:[A-Za-z0-9._:-]+", operation_id) is None \
            or marker not in operation_id:
        return None
    try:
        journal = _authority_journal_evidence(database, {
            "scope": scope,
            "authority_epoch": epoch,
            "scope_revision": revision,
            "fingerprint": target_fingerprint,
            "operation_id": operation_id,
            "source": "external_proposal",
            "principal": "worker_file_sync",
        })
        mirror = _mirror_for_scope_commit(database, {
            "scope": scope,
            "authority_epoch": epoch,
            "scope_revision": revision,
            "fingerprint": target_fingerprint,
        })
    except (RuntimeError, sqlite3.OperationalError):
        return None
    return {
        "phase": "emergency_restore",
        "authority_scope": "worker_node_local",
        "authority_epoch": epoch,
        "base_scope_revision": revision - 1,
        "scope_revision": revision,
        "fingerprint": target_fingerprint,
        "operation_id": operation_id,
        "commit_operation_id": operation_id,
        "mirror_id": mirror["mirror_id"],
        "mirror_state": mirror["state"],
        "mirror_attempts": mirror["attempts"],
        "semantic_postcondition": True,
        "local_commit_ok": True,
        "authority_journal_correlated": True,
        "journal_record_id": journal["record_id"],
        "journal_format": journal["format"],
        "mirror_ack_correlated": True,
        "mirror_format": mirror["format"],
        "commit_origin": "local_json_file_sem_node_authority",
        "file_route": "structural",
        "file_wal_mode": "node_local",
        "file_commit_kind": "scope_operations",
        "target_scope": scope,
        "target_scope_revision": revision,
        "target_scope_fingerprint": target_fingerprint,
        "recovery_journal_correlated": True,
    }


def _proof_scope_revision(proof):
    return int(proof.get("scope_revision") or 0)


def _find_restore_proof(paths, query):
    context = query["context"]
    minimum_revision = int(query["minimum_revision"])
    operation_id = str(query.get("operation_id") or "")
    scope = "node:" + _context_node_id(context)
    candidates = []
    for row in _restore_journal_records(
            paths["database"], scope, minimum_revision):
        proof = _journal_restore_proof(paths["database"], row, query)
        if proof is None:
            continue
        if operation_id and proof.get("operation_id") != operation_id:
            continue
        candidates.append(proof)
    if not candidates:
        return None
    return max(candidates, key=_proof_scope_revision)


def _recorded_restore_floor(journal):
    restore = journal.get("restore_proof") or {}
    operation_id = str(restore.get("operation_id") or "")
    revisions = [int(journal.get("baseline_scope_revision") or 0)]
    for proof in journal.get("transitions") or ():
        if isinstance(proof, dict) and str(proof.get("operation_id") or "") \
                != operation_id:
            revisions.append(int(proof.get("scope_revision") or 0))
    return max(revisions)


def _durable_restore_proof(paths, context, journal):
    recorded = journal.get("restore_proof")
    if isinstance(recorded, dict):
        floor = _recorded_restore_floor(journal)
        proof = _find_restore_proof(paths, {
            "context": context,
            "minimum_revision": floor,
            "operation_id": str(recorded.get("operation_id") or ""),
        })
        if proof is not None \
                and int(proof["scope_revision"]) == int(
                    recorded.get("scope_revision") or 0) \
                and _restore_proof_is_new_local_commit(
                    proof, context, floor):
            return proof
    floor = int(journal.get("latest_scope_revision") or 0)
    proof = _find_restore_proof(paths, {
        "context": context,
        "minimum_revision": floor,
    })
    if _restore_proof_is_new_local_commit(proof, context, floor):
        return proof
    return None

def _wait_for_idle_product_file_wal(paths, timeout_s):
    deadline = time.monotonic() + float(timeout_s)
    last_state = ""
    while time.monotonic() < deadline:
        try:
            wal = _read_object(paths["wal"], "file-proposal WAL")
            visible = _require_v9_storage(
                _read_object(paths["config"], "Worker config"),
                "Worker config")
            last_state = str(wal.get("state") or "")
            if last_state == "IDLE":
                return wal, visible
        except (FileNotFoundError, json.JSONDecodeError, sqlite3.OperationalError):
            pass
        time.sleep(0.2)
    raise RuntimeError(
        "fail-closed: product File-WAL did not become idle; state=" + last_state)


def _emergency_restore(paths, context, journal):
    journal["status"] = "RECOVERING"
    journal["pending_phase"] = "emergency_restore"
    _write_recovery_journal(paths, journal)
    wal, visible = _wait_for_idle_product_file_wal(
        paths, context["timeout_s"])
    target_fingerprint = _fingerprint(context["original"])
    exact_visible = _fingerprint(visible) == target_fingerprint \
        and _static_restore_matches(visible, context)
    if wal.get("state") == "IDLE" and exact_visible:
        proof = _durable_restore_proof(paths, context, journal)
        if proof is None:
            raise RuntimeError(
                "fail-closed: static restore has no new local Authority commit and mirror ACK")
        journal["restore_proof"] = copy.deepcopy(proof)
        journal["latest_scope_revision"] = max(
            int(journal["latest_scope_revision"]),
            int(proof["scope_revision"]),
        )
        _write_recovery_journal(paths, journal)
        return proof
    minimum_revision = max(
        int(journal.get("latest_scope_revision") or 0),
        _latest_revision(paths["database"], _context_node_id(context)),
    )
    journal["recovery_scope_floor"] = minimum_revision
    _write_recovery_journal(paths, journal)
    proof = _transition(paths, copy.deepcopy(context["original"]), {
        "phase": "emergency_restore",
        "expected_scope": "worker_node_local",
        "expected_file_route": "structural",
        "minimum_revision": minimum_revision,
        "timeout_s": context["timeout_s"],
        "expected": _restore_expectation(context),
    })
    final_config = _require_v9_storage(
        _read_object(paths["config"], "Worker config"), "Worker config")
    final_wal = _read_object(paths["wal"], "file-proposal WAL")
    if not _restore_proof_is_new_local_commit(
            proof, context, minimum_revision) \
            or final_wal.get("state") != "IDLE" \
            or _fingerprint(final_config) != target_fingerprint \
            or not _static_restore_matches(final_config, context):
        raise RuntimeError(
            "fail-closed: emergency restore lacks exact local commit and mirror ACK")
    _journal_record_transition(paths, journal, proof)
    return proof


def _first_intent_not_applied(journal, state, context):
    wal = state["wal"]
    visible = state["visible"]
    baseline_fingerprint = str(journal.get("baseline_fingerprint") or "")
    wal_baseline = wal.get("baseline_config") \
        if isinstance(wal, dict) else None
    return bool(
        journal.get("status") == "MUTATING"
        and journal.get("pending_phase") == "policy_candidate"
        and not journal.get("transitions")
        and isinstance(wal_baseline, dict)
        and wal.get("state") == "IDLE"
        and str(wal.get("baseline_fingerprint") or "")
        == baseline_fingerprint
        and _fingerprint(_PROJECT_STORAGE(wal_baseline)) == baseline_fingerprint
        and _fingerprint(visible) == baseline_fingerprint
        and _static_restore_matches(visible, context)
        and _managed_projection(visible, journal["selectors"])
        == journal.get("baseline_owned_projection")
    )


def _recover_existing_journal(paths, node_id, options):
    timeout_s = float(options["timeout_s"])
    approved_endpoint = options.get("approved_endpoint")
    journal = _read_recovery_journal(paths, node_id)
    if journal is None:
        return None
    if journal["status"] in RECOVERY_TERMINAL_STATES:
        return _archive_recovery_journal(paths, journal)
    context = _restore_context_from_journal(journal, timeout_s)
    if journal["status"] == "PREPARED" \
            and not journal["transitions"] \
            and journal.get("pending_phase") is None:
        visible = _require_v9_storage(
            _read_object(paths["config"], "Worker config"), "Worker config")
        wal = _read_object(paths["wal"], "file-proposal WAL")
        if wal.get("state") != "IDLE" \
                or _fingerprint(visible) != journal["baseline_fingerprint"] \
                or not _static_restore_matches(visible, context):
            error = RuntimeError(
                "fail-closed: PREPARED journal differs before first mutation")
            _fail_recovery_journal(paths, journal, error)
            raise error
        _finish_recovery_journal(paths, journal, {
            "status": "COMPLETED",
            "final_config": visible,
        })
        return _archive_recovery_journal(paths, journal)
    wal, visible = _wait_for_idle_product_file_wal(paths, timeout_s)
    if _first_intent_not_applied(journal, {
            "wal": wal, "visible": visible,
    }, context):
        _finish_recovery_journal(paths, journal, {
            "status": "RECOVERED",
            "final_config": visible,
        })
        return _archive_recovery_journal(paths, journal)
    try:
        if journal["actuator_restore"].get("may_be_mutated") is True:
            if not isinstance(approved_endpoint, dict):
                raise RuntimeError(
                    "physical actuator recovery requires an approved endpoint")
            _restore_journal_actuator(paths, journal, approved_endpoint)
        proof = _emergency_restore(paths, context, journal)
        final_config = _require_v9_storage(
            _read_object(paths["config"], "Worker config"), "Worker config")
        _finish_recovery_journal(paths, journal, {
            "status": "RECOVERED",
            "final_config": final_config,
            "proof": proof,
        })
    except Exception as error:
        _fail_recovery_journal(paths, journal, error)
        raise RuntimeError(
            "open Worker file-E2E recovery journal could not be recovered") from error
    return _archive_recovery_journal(paths, journal)


def _safe_home_file(value, label, require_existing=True):
    home = Path.home().resolve()
    text = str(value)
    if text.startswith("$HOME/"):
        candidate = Path.home() / text[len("$HOME/"):]
    else:
        candidate = Path(text).expanduser()
        if not candidate.is_absolute():
            candidate = Path.home() / candidate
    if candidate.is_symlink():
        raise RuntimeError(label + " must not be a symlink")
    path = candidate.resolve()
    if path == home or home not in path.parents:
        raise RuntimeError(label + " must be a non-symlink descendant of HOME")
    if require_existing and not path.is_file():
        raise RuntimeError(label + " is missing")
    return path


def _paths(arguments):
    worker = _safe_home_file(arguments.worker_root, "Worker root", False)
    if not worker.is_dir():
        raise RuntimeError("Worker root is missing")
    version_file = worker / "VERSION"
    if version_file.read_text(encoding="utf-8").strip() != EXPECTED_WORKER_VERSION:
        raise RuntimeError("installed Worker version differs from the E2E contract")
    config = worker / arguments.config_relative
    wal = worker / "_runtime" / ("config_file_sync_{}.json".format(arguments.node_id))
    database = worker / "_runtime" / (
        "distributed_operations_{}.sqlite3".format(arguments.node_id))
    for path, label in ((config, "Worker config"), (wal, "file-proposal WAL"),
                        (database, "Worker authority database")):
        resolved = path.resolve()
        if worker not in resolved.parents or resolved.is_symlink() or not resolved.is_file():
            raise RuntimeError(label + " is missing, unsafe or outside the Worker root")
    runtime = (worker / "_runtime").resolve()
    if worker not in runtime.parents or runtime.is_symlink() or not runtime.is_dir():
        raise RuntimeError("Worker runtime directory is missing or unsafe")
    recovery = runtime / (
        "worker_config_file_e2e_recovery_{}.json".format(arguments.node_id))
    if recovery.exists() or recovery.is_symlink():
        details = recovery.lstat()
        if stat.S_ISLNK(details.st_mode) or not stat.S_ISREG(details.st_mode):
            raise RuntimeError("Worker file-E2E recovery journal is unsafe")
    worker_log = _safe_home_file(arguments.worker_log, "Worker log")
    return {"worker": worker, "config": config, "wal": wal,
            "database": database, "log": worker_log,
            "recovery": recovery}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker-root", required=True)
    parser.add_argument("--worker-log", required=True)
    parser.add_argument("--node-id", default="fn-01")
    parser.add_argument(
        "--config-relative",
        default="_config/config_data/device_state_data_v9_node_fn-01.json")
    parser.add_argument("--timeout", type=float, default=180.0)
    parser.add_argument("--core-operation-id", required=True)
    parser.add_argument("--core-relay-message-id", required=True)
    parser.add_argument("--core-authority-epoch", required=True, type=int)
    parser.add_argument("--core-base-scope-revision", required=True, type=int)
    parser.add_argument("--core-target-scope-revision", required=True, type=int)
    parser.add_argument("--core-target-scope-fingerprint", required=True)
    parser.add_argument("--core-node-replica-revision", required=True, type=int)
    parser.add_argument("--core-node-replica-fingerprint", required=True)
    parser.add_argument("--core-operation-format", required=True)
    parser.add_argument("--core-operation-count", required=True, type=int)
    parser.add_argument("--approved-actuator-host", default="192.168.0.3")
    parser.add_argument("--approved-actuator-port", type=int, default=5020)
    arguments = parser.parse_args()
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", arguments.node_id) is None:
        raise RuntimeError("node ID is invalid")
    if not 30.0 <= arguments.timeout <= 600.0:
        raise RuntimeError("timeout must be between 30 and 600 seconds")
    if not arguments.approved_actuator_host \
            or not 1 <= arguments.approved_actuator_port <= 65535:
        raise RuntimeError("approved Modbus actuator fixture is invalid")
    approved_endpoint = {
        "host": arguments.approved_actuator_host,
        "port": arguments.approved_actuator_port,
    }
    core_expected = {
        "operation_id": arguments.core_operation_id,
        "relay_message_id": arguments.core_relay_message_id,
        "authority_epoch": arguments.core_authority_epoch,
        "base_scope_revision": arguments.core_base_scope_revision,
        "target_scope_revision": arguments.core_target_scope_revision,
        "target_scope_fingerprint": arguments.core_target_scope_fingerprint,
        "node_replica_revision": arguments.core_node_replica_revision,
        "node_replica_fingerprint": arguments.core_node_replica_fingerprint,
        "operation_format": arguments.core_operation_format,
        "operation_count": arguments.core_operation_count,
    }
    operation_id = _canonical_uuid(
        core_expected["operation_id"], "Core operation identity")
    if core_expected["relay_message_id"] != "relay:" + operation_id \
            or core_expected["authority_epoch"] < 0 \
            or core_expected["base_scope_revision"] < 0 \
            or core_expected["target_scope_revision"] \
            != core_expected["base_scope_revision"] + 1 \
            or core_expected["node_replica_revision"] \
            != core_expected["target_scope_revision"] \
            or not _valid_fingerprint(
                core_expected["target_scope_fingerprint"]) \
            or core_expected["node_replica_fingerprint"] \
            != core_expected["target_scope_fingerprint"] \
            or core_expected["operation_format"] \
            != "config_scope_operations_v1" \
            or core_expected["operation_count"] <= 0:
        raise RuntimeError("Core node-local proposal expectation is invalid")
    paths = _paths(arguments)
    _bind_worker_storage_verifier(paths["worker"])
    recovered_journal_archive = _recover_existing_journal(
        paths, arguments.node_id, {
            "timeout_s": arguments.timeout,
            "approved_endpoint": approved_endpoint,
        })
    original = _require_v9_storage(
        _read_object(paths["config"], "Worker config"), "Worker config")
    actuator_contract = _verify_actuator_524_contract(original)
    controller = _owned_controller(
        original, arguments.node_id, actuator_contract["controller_uuid"])
    controller_uuid = str(controller.get("controller_uuid") or "")
    actuator_target = _modbus_target(
        original, actuator_contract, approved_endpoint,
    )
    physical_baseline = _read_modbus_register(actuator_target)
    marker = "worker-file-e2e-" + uuid.uuid4().hex[:10]
    matrix = _matrix_context(original, controller_uuid, marker)
    restore_context = _restore_context(original, {
        "controller_uuid": controller_uuid,
        "marker": marker,
        "timeout_s": arguments.timeout,
        "pid_state_id": matrix["pid_state_id"],
        "sensor_id": matrix["sensor_id"],
        "actuator_id": matrix["actuator_id"],
    })
    projection_details = {
        "controller_uuid": controller_uuid,
        "marker": marker,
        "pid_state_id": matrix["pid_state_id"],
        "sensor_id": matrix["sensor_id"],
        "actuator_id": matrix["actuator_id"],
    }
    original_projection = _managed_projection(original, projection_details)
    initial_wal = _read_object(paths["wal"], "file-proposal WAL")
    if initial_wal.get("state") != "IDLE":
        raise RuntimeError("file-proposal WAL is not idle before E2E")
    core_node_local_proposal = _node_local_relay_proof(
        paths["database"], core_expected, arguments.node_id)

    revision = _latest_revision(paths["database"], arguments.node_id)
    if revision < core_expected["target_scope_revision"]:
        raise RuntimeError("Worker scope head predates the current Core proof")
    journal = _create_recovery_journal(paths, {
        "node_id": arguments.node_id,
        "original": original,
        "selectors": projection_details,
        "revision": revision,
        "actuator_restore": {
            "target": actuator_target,
            "original_value": physical_baseline,
            "may_be_mutated": False,
            "restore_confirmed": False,
        },
    })
    transitions = []
    restored = False
    mutation_started = False
    actuator_proof = None
    try:
        policy_candidate = _read_object(paths["config"], "Worker config")
        policy_pid = _state(policy_candidate, matrix["pid_state_id"])
        policy_pid.setdefault("parameters", {})["target_temp_c"] = matrix[
            "target_after"]
        _journal_begin_phase(paths, journal, EXPECTED_TRANSITIONS[0][0])
        mutation_started = True
        proof = _transition(paths, policy_candidate, {
            "phase": EXPECTED_TRANSITIONS[0][0],
            "expected_scope": EXPECTED_TRANSITIONS[0][1],
            "expected_file_route": EXPECTED_TRANSITIONS[0][2],
            "minimum_revision": revision,
            "timeout_s": arguments.timeout,
            "expected": {
                "state_1_value_id": original_projection["state_1_value_id"],
                "rule_1_enabled": original_projection["rule_1_enabled"],
                "rule_1_threshold": original_projection["rule_1_threshold"],
                "pid_state_id": matrix["pid_state_id"],
                "pid_mode_id": matrix["mode_before"],
                "pid_value_id": matrix["value_before"],
                "pid_target_temp_c": matrix["target_after"],
                "process_location": matrix["process_location_before"],
                "marker_present": False,
                "marker": marker,
                "controller_uuid": controller_uuid,
                "sensor_id": matrix["sensor_id"],
                "actuator_id": matrix["actuator_id"],
                "sensor_present": False,
                "actuator_present": False,
            },
        })
        transitions.append(proof)
        _journal_record_transition(paths, journal, proof)
        revision = proof["scope_revision"]

        policy_restore = _read_object(paths["config"], "Worker config")
        restore_policy_pid = _state(policy_restore, matrix["pid_state_id"])
        restore_policy_pid.setdefault("parameters", {})["target_temp_c"] = matrix[
            "target_before"]
        _journal_begin_phase(paths, journal, EXPECTED_TRANSITIONS[1][0])
        proof = _transition(paths, policy_restore, {
            "phase": EXPECTED_TRANSITIONS[1][0],
            "expected_scope": EXPECTED_TRANSITIONS[1][1],
            "expected_file_route": EXPECTED_TRANSITIONS[1][2],
            "minimum_revision": revision,
            "timeout_s": arguments.timeout,
            "expected": {
                "state_1_value_id": original_projection["state_1_value_id"],
                "rule_1_enabled": original_projection["rule_1_enabled"],
                "rule_1_threshold": original_projection["rule_1_threshold"],
                "pid_state_id": matrix["pid_state_id"],
                "pid_mode_id": matrix["mode_before"],
                "pid_value_id": matrix["value_before"],
                "pid_target_temp_c": matrix["target_before"],
                "process_location": matrix["process_location_before"],
                "marker_present": False,
                "marker": marker,
                "controller_uuid": controller_uuid,
                "sensor_id": matrix["sensor_id"],
                "actuator_id": matrix["actuator_id"],
                "sensor_present": False,
                "actuator_present": False,
            },
        })
        transitions.append(proof)
        _journal_record_transition(paths, journal, proof)
        revision = proof["scope_revision"]

        structural_candidate = _read_object(paths["config"], "Worker config")
        _rule(structural_candidate, 1)["enabled"] = False
        _rule(structural_candidate, 1)["when"][0]["value"] = matrix[
            "threshold_after"]
        _state(structural_candidate, 1)["value_id"] = 2
        _state(structural_candidate, matrix["pid_state_id"])["mode_id"] = matrix[
            "mode_after"]
        _process(structural_candidate, matrix["process_id"])["process_location"] = matrix[
            "process_location_after"]
        _append_io_entity(
            structural_candidate, "sensors", matrix["sensor_row"])
        _append_io_entity(
            structural_candidate, "actuators", matrix["actuator_row"])
        structural_controller = _controller_by_uuid(
            structural_candidate, controller_uuid)
        structural_controller["tags"] = list(
            structural_controller.get("tags") or ()) + [marker]
        _journal_begin_phase(paths, journal, EXPECTED_TRANSITIONS[2][0])
        mutation_started = True
        proof = _transition(paths, structural_candidate, {
            "phase": EXPECTED_TRANSITIONS[2][0],
            "expected_scope": EXPECTED_TRANSITIONS[2][1],
            "expected_file_route": EXPECTED_TRANSITIONS[2][2],
            "minimum_revision": revision,
            "timeout_s": arguments.timeout,
            "expected": {
                "state_1_value_id": 2,
                "rule_1_enabled": False,
                "rule_1_threshold": matrix["threshold_after"],
                "pid_state_id": matrix["pid_state_id"],
                "pid_mode_id": matrix["mode_after"],
                "pid_value_id": matrix["value_before"],
                "pid_target_temp_c": matrix["target_before"],
                "process_location": matrix["process_location_after"],
                "marker_present": True,
                "marker": marker,
                "controller_uuid": controller_uuid,
                "sensor_id": matrix["sensor_id"],
                "actuator_id": matrix["actuator_id"],
                "sensor_present": True,
                "actuator_present": True,
            },
        })
        transitions.append(proof)
        _journal_record_transition(paths, journal, proof)
        revision = proof["scope_revision"]

        anchor = _log_anchor(paths["log"])
        runtime_candidate = _read_object(paths["config"], "Worker config")
        _state(runtime_candidate, 1)["value_id"] = 1
        runtime_pid = _state(runtime_candidate, matrix["pid_state_id"])
        runtime_pid["value_id"] = matrix["value_after"]
        runtime_pid.setdefault("parameters", {})["target_temp_c"] = matrix[
            "target_after"]
        _journal_mark_actuator_pending(paths, journal)
        _journal_begin_phase(paths, journal, EXPECTED_TRANSITIONS[3][0])
        proof = _transition(paths, runtime_candidate, {
            "phase": EXPECTED_TRANSITIONS[3][0],
            "expected_scope": EXPECTED_TRANSITIONS[3][1],
            "expected_file_route": EXPECTED_TRANSITIONS[3][2],
            "minimum_revision": revision,
            "timeout_s": arguments.timeout,
            "expected": {
                "state_1_value_id": 1,
                "rule_1_enabled": False,
                "rule_1_threshold": matrix["threshold_after"],
                "pid_state_id": matrix["pid_state_id"],
                "pid_mode_id": matrix["mode_after"],
                "pid_value_id": matrix["value_after"],
                "pid_target_temp_c": matrix["target_after"],
                "process_location": matrix["process_location_after"],
                "marker_present": True,
                "marker": marker,
                "controller_uuid": controller_uuid,
                "sensor_id": matrix["sensor_id"],
                "actuator_id": matrix["actuator_id"],
                "sensor_present": True,
                "actuator_present": True,
            },
        })
        transitions.append(proof)
        _journal_record_transition(paths, journal, proof)
        revision = proof["scope_revision"]
        actuator_proof = _wait_actuator_524(
            paths["log"], anchor, arguments.timeout)
        actuator_proof.update(_restore_journal_actuator(
            paths, journal, approved_endpoint,
        ))

        runtime_restore = _read_object(paths["config"], "Worker config")
        _state(runtime_restore, 1)["value_id"] = 2
        restore_pid = _state(runtime_restore, matrix["pid_state_id"])
        restore_pid["value_id"] = matrix["value_before"]
        restore_pid.setdefault("parameters", {})["target_temp_c"] = matrix[
            "target_before"]
        _journal_begin_phase(paths, journal, EXPECTED_TRANSITIONS[4][0])
        proof = _transition(paths, runtime_restore, {
            "phase": EXPECTED_TRANSITIONS[4][0],
            "expected_scope": EXPECTED_TRANSITIONS[4][1],
            "expected_file_route": EXPECTED_TRANSITIONS[4][2],
            "minimum_revision": revision,
            "timeout_s": arguments.timeout,
            "expected": {
                "state_1_value_id": 2,
                "rule_1_enabled": False,
                "rule_1_threshold": matrix["threshold_after"],
                "pid_state_id": matrix["pid_state_id"],
                "pid_mode_id": matrix["mode_after"],
                "pid_value_id": matrix["value_before"],
                "pid_target_temp_c": matrix["target_before"],
                "process_location": matrix["process_location_after"],
                "marker_present": True,
                "marker": marker,
                "controller_uuid": controller_uuid,
                "sensor_id": matrix["sensor_id"],
                "actuator_id": matrix["actuator_id"],
                "sensor_present": True,
                "actuator_present": True,
            },
        })
        transitions.append(proof)
        _journal_record_transition(paths, journal, proof)
        revision = proof["scope_revision"]

        structural_restore = _restore_target(paths, restore_context)
        restore_authority_floor = revision
        _journal_begin_phase(paths, journal, EXPECTED_TRANSITIONS[5][0])
        proof = _transition(paths, structural_restore, {
            "phase": EXPECTED_TRANSITIONS[5][0],
            "expected_scope": EXPECTED_TRANSITIONS[5][1],
            "expected_file_route": EXPECTED_TRANSITIONS[5][2],
            "minimum_revision": revision,
            "timeout_s": arguments.timeout,
            "expected": _restore_expectation(restore_context),
        })
        transitions.append(proof)
        _journal_record_transition(paths, journal, proof)
        revision = proof["scope_revision"]
        final_config = _require_v9_storage(
            _read_object(paths["config"], "Worker config"), "Worker config")
        original_fingerprint = _fingerprint(original)
        final_fingerprint = _fingerprint(final_config)
        restore_verified = _static_restore_matches(
            final_config, restore_context) \
            and original_fingerprint == final_fingerprint \
            and _restore_proof_is_new_local_commit(
                proof, restore_context, restore_authority_floor)
        if not restore_verified:
            raise RuntimeError(
                "exact Worker V9 baseline/Authority restore was not proven")
        _finish_recovery_journal(paths, journal, {
            "status": "COMPLETED",
            "final_config": final_config,
            "proof": proof,
        })
        restored = True
    finally:
        if not restored:
            if not mutation_started:
                visible = _require_v9_storage(
                    _read_object(paths["config"], "Worker config"),
                    "Worker config")
                wal = _read_object(paths["wal"], "file-proposal WAL")
                if wal.get("state") != "IDLE" \
                        or _fingerprint(visible) != _fingerprint(original) \
                        or not _static_restore_matches(visible, restore_context):
                    error = RuntimeError(
                        "fail-closed: pre-mutation recovery baseline differs")
                    _fail_recovery_journal(paths, journal, error)
                    raise error
                _finish_recovery_journal(paths, journal, {
                    "status": "COMPLETED",
                    "final_config": visible,
                })
                restored = True
            else:
                try:
                    _restore_journal_actuator(
                        paths, journal, approved_endpoint,
                    )
                    recovery_proof = _emergency_restore(
                        paths, restore_context, journal)
                    recovered_config = _require_v9_storage(
                        _read_object(paths["config"], "Worker config"),
                        "Worker config")
                    _finish_recovery_journal(paths, journal, {
                        "status": "RECOVERED",
                        "final_config": recovered_config,
                        "proof": recovery_proof,
                    })
                    restored = True
                except Exception as recovery_error:
                    _fail_recovery_journal(paths, journal, recovery_error)
                    raise RuntimeError(
                        "emergency config restore did not converge") \
                        from recovery_error

    if not restored:
        raise RuntimeError("emergency config restore did not converge")
    final_config = _require_v9_storage(
        _read_object(paths["config"], "Worker config"), "Worker config")
    original_fingerprint = _fingerprint(original)
    final_fingerprint = _fingerprint(final_config)
    if original_fingerprint != final_fingerprint:
        raise RuntimeError("final Worker V9 fingerprint differs from baseline")
    result = {
        "status": "PASSED",
        "version": VERSION,
        "worker_version": EXPECTED_WORKER_VERSION,
        "node_id": arguments.node_id,
        "original_fingerprint": original_fingerprint,
        "final_fingerprint": final_fingerprint,
        "managed_original": original_projection,
        "managed_final": _managed_projection(final_config, projection_details),
        "config_matrix": {
            "commit_paths": ["worker_node_local"],
            "commit_origin": "local_json_file_sem_node_authority",
            "master_mirror_is_non_authoritative": True,
            "authority_journal_required": True,
            "exact_mirror_ack_required": True,
            "operator_file_policy_fallback": {
                "state_id": matrix["pid_state_id"],
                "safety_code": "required_mode",
                "active_mode_id": matrix["mode_before"],
                "required_mode_id": matrix["required_mode_id"],
                "candidate_target_temp_c": matrix["target_after"],
                "authority_scope": "worker_node_local",
                "restored": True,
            },
            "new_sensor": {
                "controller_uuid": controller_uuid,
                "sensor_id": matrix["sensor_id"],
                "address": matrix["sensor_address"],
                "added": True,
                "restored": True,
            },
            "new_actuator": {
                "controller_uuid": controller_uuid,
                "actuator_id": matrix["actuator_id"],
                "address": matrix["actuator_address"],
                "added": True,
                "restored": True,
            },
            "pid_state": {
                "state_id": matrix["pid_state_id"],
                "mode_id": {
                    "before": matrix["mode_before"],
                    "after": matrix["mode_after"],
                },
                "value_id": {
                    "before": matrix["value_before"],
                    "after": matrix["value_after"],
                },
                "target_temp_c": {
                    "before": matrix["target_before"],
                    "after": matrix["target_after"],
                },
            },
            "rule_threshold": {
                "before": matrix["threshold_before"],
                "after": matrix["threshold_after"],
            },
            "process_data": {
                "p_id": matrix["process_id"],
                "process_location": {
                    "before": matrix["process_location_before"],
                    "after": matrix["process_location_after"],
                },
            },
            "storage_contract": {
                "version": 9,
                "io_layout": "flat",
                "primary": True,
            },
            "worker_live_ack": True,
            "restore_confirmed": True,
        },
        "restore_confirmed": True,
        "recovery_journal": {
            "journal_version": RECOVERY_JOURNAL_VERSION,
            "status": journal["status"],
            "separate_from_product_file_wal": True,
            "prepared_before_first_mutation": True,
            "full_v9_baseline_recorded": True,
            "selectors_recorded": True,
            "physical_actuator_baseline_recorded": isinstance(
                journal["actuator_restore"].get("target"), dict),
            "physical_actuator_restore_confirmed": journal[
                "actuator_restore"].get("restore_confirmed") is True,
            "baseline_fingerprint": journal["baseline_fingerprint"],
            "final_fingerprint": journal["final_fingerprint"],
            "restore_scope_revision": int(
                (journal.get("restore_proof") or {}).get(
                    "scope_revision") or 0),
            "restore_mirror_state": (
                journal.get("restore_proof") or {}).get("mirror_state"),
            "restore_authority_journal_correlated": (
                journal.get("restore_proof") or {}).get(
                    "authority_journal_correlated") is True,
            "restore_mirror_ack_correlated": (
                journal.get("restore_proof") or {}).get(
                    "mirror_ack_correlated") is True,
            "restore_confirmed": journal["restore_confirmed"],
            "previous_open_journal_recovered": bool(
                recovered_journal_archive),
        },
        "actuator_524": actuator_proof,
        "actuator_contract": actuator_contract,
        "core_node_local_proposal": core_node_local_proposal,
        "transitions": transitions,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as error:
        print("FAILED Worker config-file E2E: " + str(error), file=sys.stderr)
        raise SystemExit(1) from error

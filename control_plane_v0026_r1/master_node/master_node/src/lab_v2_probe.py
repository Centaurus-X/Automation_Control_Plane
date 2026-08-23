# -*- coding: utf-8 -*-

"""State-based acceptance probe for the real two-Worker Master-v2 lab."""

import json
import os
import sys
import sysconfig
import time

from src.library._generic_system_contract_library.protocol_contracts import (
    CONTRACTS_VERSION, OPERATION_FORMAT, PROTOCOL_V2,
)
from src.library.postgresql import create_pool, env_database_options


CROSS_CONTROLLER = "d11032fa-33db-56de-a4ed-7e16cca3ded7"
EXPECTED_SCOPES = (
    "cross:fn-01.default",
    "node:fn-01",
    "node:fn-02",
)
COMMON_REQUIRED_CAPABILITIES = (
    OPERATION_FORMAT,
    "canonical_config_v9",
    "authority_epoch_v1",
    "authority_transfer_v1",
    "mirror_v1",
    "restore_quarantine_v1",
    "node_full",
    "node_patch",
    "cross_runtime",
    "cross_full",
    "cross_patch",
    "chunk_reassembly",
    "config_proposals",
    "cross_fact_consumer",
    "remote_actuator",
)
REQUIRED_CAPABILITIES = {
    "fn-01": COMMON_REQUIRED_CAPABILITIES,
    "fn-02": COMMON_REQUIRED_CAPABILITIES,
}
FORBIDDEN_CAPABILITIES = {
    "fn-01": (),
    "fn-02": (),
}

def _read_state(connection):
    authority = connection.execute(
        "SELECT site_id,authority_term,config_revision,config_fingerprint,"
        "leader_instance_id,lease_until>clock_timestamp() AS lease_active "
        "FROM master_control.authority_state WHERE site_id='site-main'"
    ).fetchone()
    nodes = connection.execute(
        "SELECT node_id,admission_state,desired_state,observed_state,"
        "protocol_version,protocol_versions,contracts_version,capabilities,max_packet_size,"
        "observed_global_revision,live_config_revision,pending_proposal_count,"
        "node_replica_scope,node_replica_revision,node_replica_fingerprint,"
        "cross_replica_scope,cross_replica_revision,cross_replica_fingerprint,"
        "last_seen_at>clock_timestamp()-(60*interval '1 second') AS online "
        "FROM master_control.nodes WHERE site_id='site-main' ORDER BY node_id"
    ).fetchall()
    heads = connection.execute(
        "SELECT scope,scope_kind,owner_node_id,scope_revision,global_revision,"
        "fingerprint,contracts_version,execution_epoch "
        "FROM master_control.scope_heads ORDER BY scope"
    ).fetchall()
    rollouts = connection.execute(
        "SELECT rollout_id,node_id,scope,target_scope_revision,target_fingerprint,"
        "transfer_id,state,attempts,detail "
        "FROM master_control.shard_rollouts WHERE state!='superseded' "
        "ORDER BY scope,target_scope_revision DESC"
    ).fetchall()
    outbox = connection.execute(
        "SELECT event_type,count(*) AS count FROM master_control.outbox "
        "WHERE target_service='worker' GROUP BY event_type ORDER BY event_type"
    ).fetchall()
    scope_ops = connection.execute(
        "SELECT scope,target_scope_revision,count(*) AS count,"
        "bool_and(expected_value_hash IS NOT NULL) AS expected_hashes,"
        "bool_and(value_hash IS NOT NULL OR operation='delete_entity') AS result_hashes "
        "FROM master_control.scope_ops GROUP BY scope,target_scope_revision "
        "ORDER BY scope,target_scope_revision"
    ).fetchall()
    proposals = connection.execute(
        "SELECT state,count(*) AS count FROM master_control.worker_proposals GROUP BY state"
    ).fetchall()
    sensors = connection.execute(
        "SELECT controller_uuid::text AS controller_uuid,sensor_id,source_node_id,quality,value "
        "FROM master_control.sensor_latest WHERE site_id='site-main' "
        "AND controller_uuid=%s::uuid ORDER BY sensor_id",
        (CROSS_CONTROLLER,),
    ).fetchall()
    cross_operations = connection.execute(
        "SELECT operation_id,source_service,source_instance_id,controller_uuid::text AS "
        "controller_uuid,actuator_id,owner_node_id,state,execution_scope,execution_epoch,result "
        "FROM master_control.actuator_operations WHERE site_id='site-main' "
        "AND controller_uuid=%s::uuid AND owner_node_id='fn-02' "
        "ORDER BY created_at DESC LIMIT 20",
        (CROSS_CONTROLLER,),
    ).fetchall()
    return {
        "authority": dict(authority) if authority else None,
        "nodes": [dict(row) for row in nodes],
        "scope_heads": [dict(row) for row in heads],
        "rollouts": [dict(row) for row in rollouts],
        "outbox": [dict(row) for row in outbox],
        "scope_ops": [dict(row) for row in scope_ops],
        "proposals": [dict(row) for row in proposals],
        "cross_sensors": [dict(row) for row in sensors],
        "cross_operations": [dict(row) for row in cross_operations],
    }


def _current_rollouts(rows):
    result = {}
    for row in rows:
        result.setdefault(row["scope"], row)
    return result


def _runtime_errors():
    errors = []
    if sys.version_info[:2] != (3, 14):
        errors.append("Master acceptance probe is not running on Python 3.14")
    if sysconfig.get_config_var("Py_GIL_DISABLED") != 1:
        errors.append("Master runtime is not a CPython free-threaded build")
    checker = getattr(sys, "_is_gil_enabled", None)
    if not callable(checker) or checker():
        errors.append("Master acceptance probe has the GIL enabled")
    return errors


def _capability_errors(node_id, node):
    errors = []
    protocols = set(node.get("protocol_versions") or [])
    if node.get("protocol_version") != PROTOCOL_V2 or PROTOCOL_V2 not in protocols:
        errors.append(node_id + " did not negotiate distributed v2")
    if node.get("contracts_version") != CONTRACTS_VERSION:
        errors.append(node_id + " did not negotiate contracts " + CONTRACTS_VERSION)
    features = (node.get("capabilities") or {}).get("features") or {}
    missing = [
        name for name in REQUIRED_CAPABILITIES.get(node_id, ())
        if features.get(name) is not True
    ]
    if missing:
        errors.append(node_id + " is missing v2 capabilities: " + ",".join(missing))
    forbidden = [
        name for name in FORBIDDEN_CAPABILITIES.get(node_id, ())
        if features.get(name) is not False
    ]
    if forbidden:
        errors.append(
            node_id + " advertises unsupported capabilities: "
            + ",".join(forbidden))
    if features.get("master_automation_execution") is not False:
        errors.append(node_id + " does not explicitly forbid Master automation execution")
    return errors

def _evaluate(state, phase):
    errors = _runtime_errors()
    authority = state.get("authority") or {}
    minimum_revision = 2 if phase in ("patched", "recovered") else 1
    if not authority.get("lease_active") or int(authority.get("config_revision") or 0) < minimum_revision:
        errors.append("Master authority is not active at the expected revision")
    heads = {row["scope"]: row for row in state["scope_heads"]}
    if tuple(sorted(heads)) != EXPECTED_SCOPES:
        errors.append("scope heads differ from the lab-v2 Node/Cross partition")
    nodes = {row["node_id"]: row for row in state["nodes"]}
    for node_id in ("fn-01", "fn-02"):
        node = nodes.get(node_id)
        if not node:
            errors.append(node_id + " is not registered")
            continue
        if node.get("admission_state") != "APPROVED":
            errors.append(node_id + " is not certificate-bound and approved")
        if node.get("desired_state") != "ACTIVE" or not node.get("online"):
            errors.append(node_id + " is not active and online")
        errors.extend(_capability_errors(node_id, node))
        node_head = heads.get("node:" + node_id) or {}
        if (
                node.get("node_replica_scope") != "node:" + node_id
                or int(node.get("node_replica_revision") or 0) != int(
                    node_head.get("scope_revision") or -1)
                or node.get("node_replica_fingerprint") != node_head.get("fingerprint")):
            errors.append(node_id + " Node replica has not converged exactly")
    cross_node = nodes.get("fn-01") or {}
    cross_head = heads.get("cross:fn-01.default") or {}
    if (
            cross_node.get("cross_replica_scope") != "cross:fn-01.default"
            or int(cross_node.get("cross_replica_revision") or 0) != int(
                cross_head.get("scope_revision") or -1)
            or cross_node.get("cross_replica_fingerprint") != cross_head.get("fingerprint")):
        errors.append("fn-01 Cross replica has not converged exactly")
    if (nodes.get("fn-02") or {}).get("cross_replica_scope"):
        errors.append("fn-02 received a foreign Cross replica")
    current = _current_rollouts(state["rollouts"])
    for scope in EXPECTED_SCOPES:
        rollout = current.get(scope)
        head = heads.get(scope) or {}
        if (
                not rollout or rollout.get("state") != "converged"
                or int(rollout.get("target_scope_revision") or 0) != int(
                    head.get("scope_revision") or -1)):
            errors.append(scope + " current rollout is not converged")
            continue
        detail = rollout.get("detail") or {}
        owner = nodes.get(str(head.get("owner_node_id") or "")) or {}
        features = (owner.get("capabilities") or {}).get("features") or {}
        patch_capability = "cross_patch" \
            if head.get("scope_kind") == "cross" else "node_patch"
        expected_mode = "patch" if (
            phase in ("patched", "recovered")
            and features.get(patch_capability) is True
        ) else "full"
        if detail.get("mode") != expected_mode or detail.get("protocol") != PROTOCOL_V2:
            errors.append(scope + " did not use the expected v2 " + expected_mode + " path")
        if rollout.get("transfer_id") and features.get("chunk_reassembly") is not True:
            errors.append(scope + " used Chunk transport without Worker capability")
    event_counts = {row["event_type"]: int(row["count"]) for row in state["outbox"]}
    if phase in ("patched", "recovered"):
        if event_counts.get("distributed.config.patch", 0) < 2:
            errors.append("both Node Patch commands are not persisted")
        fn01_features = (
            (nodes.get("fn-01") or {}).get("capabilities") or {}
        ).get("features") or {}
        if fn01_features.get("cross_patch") is True:
            if event_counts.get("distributed.cross.patch", 0) < 1:
                errors.append("the Cross Patch command is not persisted")
        elif event_counts.get("distributed.cross.apply", 0) < 2:
            errors.append("the capability-driven Cross Full fallback is not persisted")
        latest_ops = [
            row for row in state["scope_ops"]
            if int(row["target_scope_revision"]) == int(
                (heads.get(row["scope"]) or {}).get("scope_revision") or -1)
        ]
        if len(latest_ops) != 3 or not all(
                row.get("expected_hashes") and row.get("result_hashes") for row in latest_ops):
            errors.append("latest Node/Cross Patch journals lack expected/result hashes")
    if phase == "recovered":
        proposal_counts = {
            row["state"]: int(row["count"])
            for row in state["proposals"]
        }
        if proposal_counts.get("converged", 0) < 1:
            errors.append("outage-time local-first proposal has not converged through its baseline")
    if not any(row.get("source_node_id") == "fn-02" for row in state["cross_sensors"]):
        errors.append("fn-02 telemetry from the remote Cross dependency is missing")
    confirmed = [
        row for row in state["cross_operations"]
        if row.get("source_service") == "worker"
        and row.get("source_instance_id") == "fn-01"
        and row.get("owner_node_id") == "fn-02"
        and row.get("state") == "CONFIRMED"
        and str(row.get("execution_scope") or "").startswith("cross")
        and int(row.get("execution_epoch") or 0) > 0
    ]
    if not confirmed:
        errors.append("no fenced fn-01 -> fn-02 Cross actuator readback was confirmed")
    return tuple(errors)


def main():
    phase = str(os.environ.get("LAB_V2_EXPECT_PHASE") or "patched").lower()
    if phase not in ("initial", "patched", "recovered"):
        raise ValueError("LAB_V2_EXPECT_PHASE must be initial, patched or recovered")
    timeout_s = max(1.0, float(os.environ.get("LAB_V2_CONVERGENCE_TIMEOUT_S", "240")))
    deadline = time.monotonic() + timeout_s
    database = create_pool(env_database_options())
    state = None
    errors = ("probe has not run",)
    try:
        while time.monotonic() < deadline:
            state = database["transaction"](
                _read_state, isolation_level="REPEATABLE READ")
            errors = _evaluate(state, phase)
            if not errors:
                break
            time.sleep(1.0)
    finally:
        database["close"]()
    result = {
        "ok": not errors,
        "release": "automation_control_plane_release_v0026_r1",
        "profile": "lab_v2",
        "phase": phase,
        "runtime": "CPython-3.14t/no-GIL",
        "worker_compatibility": (
            "automation_system_v0101+distributed-v2/"
            "contracts-2.0/type-schema-2.0.0/node-V9/cross-V8"
        ),
        "errors": list(errors),
        "state": state,
    }
    print(json.dumps(result, ensure_ascii=False, sort_keys=True, default=str))
    return 0 if result["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

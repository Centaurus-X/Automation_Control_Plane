#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Regression tests for the fail-closed Authority-V5 Core-E2E verifier."""

import copy

from verify_core_e2e_result import REQUIRED_CHECKS, verify


OPERATION_ID = "cce259be-d2e1-4db7-bbe6-f8c7a00d2840"
TARGET_FINGERPRINT = "b" * 64
BASELINE_FINGERPRINT = "c" * 64


def _restore(owned_paths, mode):
    return {
        "authority_mode": "node_local",
        "restore_confirmed": True,
        "actuator_restored_before_config": True,
        "before_parallel_load": True,
        "mode": mode,
        "cas_retries": 0,
        "baseline": {
            "scope_revision": 0,
            "scope_fingerprint": BASELINE_FINGERPRINT,
            "node_scope_revision": 0,
            "node_scope_fingerprint": BASELINE_FINGERPRINT,
        },
        "scope_revision": 2,
        "scope_fingerprint": BASELINE_FINGERPRINT,
        "owned_paths": copy.deepcopy(owned_paths),
        "owned_values_restored": True,
        "worker_scope": {
            "node_id": "fn-01",
            "scope": "node:fn-01",
            "revision": 2,
            "fingerprint": BASELINE_FINGERPRINT,
            "expected_fingerprint": BASELINE_FINGERPRINT,
            "observed_global_revision": 0,
        },
    }


def _report():
    checks = {
        name: {"status": "PASSED", "detail": {}}
        for name in REQUIRED_CHECKS
    }
    checks["AUTH"]["detail"] = {"mode": "session"}
    checks["EVT-004A"]["detail"] = {
        "retained_stream_command_filter_removed": True,
    }
    checks["CFG-001"]["detail"] = {
        "from_revision": 0,
        "to_revision": 1,
    }
    checks["CFG-002"]["detail"] = {
        "node_id": "fn-01",
        "node_replica_revision": 1,
        "node_replica_fingerprint": TARGET_FINGERPRINT,
    }
    owned_paths = [
        "controllers.0.tags",
        "sensors",
        "actuators",
        "rules.0.when.0.value",
        "process_states.1.mode_id",
        "process_states.1.value_id",
        "process_states.1.parameters.target_temp_c",
        "processes.0.process_location",
    ]
    checks["CFG-006"]["detail"] = _restore(
        owned_paths, "compensating_proposal")
    checks["CFG-RESTORE"]["detail"] = _restore(
        owned_paths, "persisted_restore_revalidation")
    checks["CFG-MATRIX"]["detail"] = {
        "commit_path": "client_wss_proxy_master_mqtt_worker",
        "commit_origin": "canonical_client_proxy_master_relay_worker_local",
        "commit_identity": ["operation", OPERATION_ID],
        "file_proposal": False,
        "transport_expectation": "distributed.config.propose",
        "node_patch_capability": True,
        "node_local_proposal_proof": {
            "relay_message_id": "relay:" + OPERATION_ID,
            "authority_epoch": 0,
            "base_scope_revision": 0,
            "target_scope_revision": 1,
            "target_scope_fingerprint": TARGET_FINGERPRINT,
            "node_replica_revision": 1,
            "node_replica_fingerprint": TARGET_FINGERPRINT,
            "operation_format": "config_scope_operations_v1",
            "operation_count": 6,
        },
        "storage_contract": {"version": 9, "io_layout": "flat"},
        "owned_paths": owned_paths,
        "matrix": {
            "controller_uuid": "ctrl-wago-1",
            "marker": "wss-v0025-1234abcd",
            "sensor_id": 60000,
            "sensor_address": 14,
            "actuator_id": 60000,
            "actuator_address": 547,
            "pid_state_id": 2,
            "mode_id": {"before": 1, "after": 2},
            "value_id": {"before": 1, "after": 2},
            "pid_target_temp_c": {"before": 200.0, "after": 201.25},
            "rule_threshold": {"before": 540.0, "after": 540.5},
            "process_data": {
                "p_id": 1,
                "process_location": {
                    "before": "lab",
                    "after": "lab|wss-v0025-1234abcd",
                },
            },
            "storage_contract": {"version": 9, "io_layout": "flat"},
        },
        "worker_live_ack": True,
    }
    return {
        "run_id": "test-core-result",
        "version": "0.25.0-e2e.3",
        "status": "PASSED",
        "profile": "core",
        "transport": "wss-primary",
        "config_restore_confirmed": True,
        "actuator_restore_confirmed": True,
        "worker_fn01": {"converged": True},
        "checks": checks,
    }


def _assert_rejected(report):
    try:
        verify(report, "0.25.0-e2e.3")
    except AssertionError:
        return
    raise AssertionError("invalid Core-E2E report was accepted")


def _proof(report):
    return report["checks"]["CFG-MATRIX"]["detail"][
        "node_local_proposal_proof"]


def _restore_details(report):
    return (
        report["checks"]["CFG-006"]["detail"],
        report["checks"]["CFG-RESTORE"]["detail"],
    )


def main():
    report = _report()
    summary = verify(report, "0.25.0-e2e.3")
    proof = summary["node_local_proposal_proof"]
    assert proof["target_scope_revision"] == 1
    assert proof["operation_id"] == OPERATION_ID
    assert proof["node_id"] == "fn-01"

    missing_path = _report()
    missing_path["checks"]["CFG-MATRIX"]["detail"]["owned_paths"].pop()
    _assert_rejected(missing_path)

    unchanged_pid = _report()
    pair = unchanged_pid["checks"]["CFG-MATRIX"]["detail"]["matrix"][
        "pid_target_temp_c"]
    pair["after"] = pair["before"]
    _assert_rejected(unchanged_pid)

    wrong_replica = _report()
    wrong_replica["checks"]["CFG-002"]["detail"][
        "node_replica_fingerprint"] = "d" * 64
    _assert_rejected(wrong_replica)

    wrong_identity = _report()
    wrong_identity["checks"]["CFG-MATRIX"]["detail"][
        "commit_identity"][1] = "5aa4bab4-4a80-48a1-98ab-60bc6cc37bb4"
    _assert_rejected(wrong_identity)

    wrong_relay = _report()
    _proof(wrong_relay)["relay_message_id"] = (
        "relay:5aa4bab4-4a80-48a1-98ab-60bc6cc37bb4")
    _assert_rejected(wrong_relay)

    wrong_format = _report()
    _proof(wrong_format)["operation_format"] = "legacy_patch"
    _assert_rejected(wrong_format)

    skipped_revision = _report()
    _proof(skipped_revision)["target_scope_revision"] = 2
    _proof(skipped_revision)["node_replica_revision"] = 2
    skipped_revision["checks"]["CFG-001"]["detail"]["to_revision"] = 2
    skipped_revision["checks"]["CFG-002"]["detail"][
        "node_replica_revision"] = 2
    _assert_rejected(skipped_revision)

    boolean_epoch = _report()
    _proof(boolean_epoch)["authority_epoch"] = True
    _assert_rejected(boolean_epoch)

    wrong_process = _report()
    wrong_process["checks"]["CFG-MATRIX"]["detail"]["matrix"][
        "process_data"]["process_location"]["after"] = "unrelated"
    _assert_rejected(wrong_process)

    boolean_restore = _report()
    boolean_restore["checks"]["CFG-RESTORE"]["detail"] = {
        "restore_confirmed": True}
    boolean_restore["checks"]["CFG-006"]["detail"] = {
        "restore_confirmed": True}
    _assert_rejected(boolean_restore)

    stale_restore_revision = _report()
    for detail in _restore_details(stale_restore_revision):
        detail["scope_revision"] = 1
        detail["worker_scope"]["revision"] = 1
    _assert_rejected(stale_restore_revision)

    worker_scope_drift = _report()
    for detail in _restore_details(worker_scope_drift):
        detail["worker_scope"]["expected_fingerprint"] = "0" * 64
    _assert_rejected(worker_scope_drift)

    foreign_restore_node = _report()
    for detail in _restore_details(foreign_restore_node):
        detail["worker_scope"].update({
            "node_id": "fn-02", "scope": "node:fn-02"})
    _assert_rejected(foreign_restore_node)

    restore_path_drift = _report()
    for detail in _restore_details(restore_path_drift):
        detail["owned_paths"].pop()
    _assert_rejected(restore_path_drift)

    inconsistent_restore_checks = _report()
    inconsistent_restore_checks["checks"]["CFG-006"]["detail"][
        "scope_revision"] = 3
    inconsistent_restore_checks["checks"]["CFG-006"]["detail"][
        "worker_scope"]["revision"] = 3
    _assert_rejected(inconsistent_restore_checks)

    embedded_revision = _report()
    _proof(embedded_revision)["target_scope_revision"] = 2
    _assert_rejected(embedded_revision)

    wrong_transport = _report()
    wrong_transport["checks"]["CFG-MATRIX"]["detail"][
        "transport_expectation"] = "distributed.config.patch"
    _assert_rejected(wrong_transport)

    print(
        "PASS Authority-V5 Core-E2E verifier rejects stale proposal/restore proofs")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
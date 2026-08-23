#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Regression tests for the persistent Worker config-file E2E journal."""

import copy
import importlib.util
import json
import os
import sqlite3
import stat
import sys
import tempfile

from pathlib import Path


def _load_module():
    sys.dont_write_bytecode = True
    script = Path(__file__).resolve().parent.parent / "integration_test" \
        / "e2e_v0025" / "worker_config_file_e2e.py"
    specification = importlib.util.spec_from_file_location(
        "worker_config_file_e2e_recovery_test", script)
    module = importlib.util.module_from_spec(specification)
    specification.loader.exec_module(module)
    return module


def _storage_ok(_config):
    return {"status": "OK"}


def _identity(value):
    return copy.deepcopy(value)


def _baseline():
    return {
        "meta": [{"version": 9, "node_id": "fn-01"}],
        "site_controller_map": [{
            "controller_uuid": "ctrl-wago-1",
            "node_id": "fn-01",
            "routing_role": "owner",
        }],
        "controllers": [{
            "controller_uuid": "ctrl-wago-1",
            "tags": ["baseline"],
        }],
        "sensors": [],
        "actuators": [],
        "rules": [{
            "rule_id": 1,
            "enabled": True,
            "when": [{"value": 540.0}],
        }],
        "process_states": [
            {"state_id": 1, "mode_id": 7, "value_id": 2},
            {
                "state_id": 21,
                "p_id": 1,
                "mode_id": 1,
                "value_id": 1,
                "parameters": {"target_temp_c": 200.0},
            },
        ],
        "processes": [{"p_id": 1, "process_location": "lab"}],
    }


def _selectors():
    return {
        "controller_uuid": "ctrl-wago-1",
        "marker": "worker-file-e2e-0123456789",
        "pid_state_id": 21,
        "sensor_id": 60000,
        "actuator_id": 60000,
    }


def _write_json(path, value):
    path.write_text(
        json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def _create_empty_outbox(path):
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            "CREATE TABLE outbox("
            "message_id TEXT,state TEXT,payload_json TEXT,result_json TEXT,"
            "created_at REAL,updated_at REAL,command TEXT)"
        )
        connection.commit()
    finally:
        connection.close()


def _context(module, baseline):
    selectors = _selectors()
    return module._restore_context(copy.deepcopy(baseline), {
        "controller_uuid": selectors["controller_uuid"],
        "marker": selectors["marker"],
        "timeout_s": 30.0,
        "pid_state_id": selectors["pid_state_id"],
        "sensor_id": selectors["sensor_id"],
        "actuator_id": selectors["actuator_id"],
    })


def _assert_exact_projection(module):
    baseline = _baseline()
    context = _context(module, baseline)
    assert module._static_restore_matches(copy.deepcopy(baseline), context)
    for state_field in ("value_id", "mode_id"):
        changed = copy.deepcopy(baseline)
        module._state(changed, 1)[state_field] += 1
        assert not module._static_restore_matches(changed, context)
    changed = copy.deepcopy(baseline)
    module._controller_by_uuid(changed, "ctrl-wago-1")["tags"].append("drift")
    assert not module._static_restore_matches(changed, context)


def _assert_authority_gate(module):
    baseline = _baseline()
    context = _context(module, baseline)
    proof = {
        "mirror_state": "acked",
        "semantic_postcondition": True,
        "local_commit_ok": True,
        "authority_journal_correlated": True,
        "mirror_ack_correlated": True,
        "scope_revision": 8,
        "authority_scope": "worker_node_local",
        "target_scope": "node:fn-01",
        "fingerprint": module._fingerprint(baseline),
    }
    assert module._restore_proof_is_new_local_commit(proof, context, 7)
    stale = dict(proof)
    stale["scope_revision"] = 7
    assert not module._restore_proof_is_new_local_commit(stale, context, 7)
    unacked = dict(proof)
    unacked["mirror_state"] = "pending"
    assert not module._restore_proof_is_new_local_commit(
        unacked, context, 7)
    unjournaled = dict(proof)
    unjournaled["authority_journal_correlated"] = False
    assert not module._restore_proof_is_new_local_commit(
        unjournaled, context, 7)


def _assert_persistent_journal(module):
    baseline = _baseline()
    selectors = _selectors()
    with tempfile.TemporaryDirectory(
            prefix="worker-file-e2e-recovery-") as directory:
        root = Path(directory)
        config = root / "device_state_data_v9_node_fn-01.json"
        wal = root / "config_file_sync_fn-01.json"
        database = root / "distributed_operations_fn-01.sqlite3"
        recovery = root / "worker_config_file_e2e_recovery_fn-01.json"
        _write_json(config, baseline)
        idle_wal = {
            "version": 1,
            "state": "IDLE",
            "node_id": "fn-01",
            "baseline_config": baseline,
            "baseline_fingerprint": module._fingerprint(baseline),
        }
        _write_json(wal, idle_wal)
        _create_empty_outbox(database)
        paths = {
            "config": config,
            "wal": wal,
            "database": database,
            "recovery": recovery,
        }
        product_wal_before = wal.read_bytes()
        journal = module._create_recovery_journal(paths, {
            "node_id": "fn-01",
            "original": baseline,
            "selectors": selectors,
            "revision": 7,
        })
        durable = module._read_recovery_journal(paths, "fn-01")
        assert durable["status"] == "PREPARED"
        assert durable["baseline_config"] == baseline
        assert durable["baseline_fingerprint"] == module._fingerprint(baseline)
        assert durable["selectors"] == selectors
        assert durable["baseline_owned_projection"] \
            == module._managed_projection(baseline, selectors)
        if os.name != "nt":
            assert stat.S_IMODE(recovery.stat().st_mode) == 0o600
        archive = module._recover_existing_journal(
            paths, "fn-01", {"timeout_s": 30.0})
        assert archive.is_file() and not recovery.exists()
        assert wal.read_bytes() == product_wal_before

        journal = module._create_recovery_journal(paths, {
            "node_id": "fn-01",
            "original": baseline,
            "selectors": selectors,
            "revision": 7,
        })
        journal["status"] = "MUTATING"
        journal["pending_phase"] = "policy_candidate"
        module._write_recovery_journal(paths, journal)
        context = module._restore_context_from_journal(journal, 30.0)
        assert module._first_intent_not_applied(
            journal, {"wal": idle_wal, "visible": baseline}, context)
        pending_wal = copy.deepcopy(idle_wal)
        pending_wal["state"] = "PENDING"
        assert not module._first_intent_not_applied(
            journal, {"wal": pending_wal, "visible": baseline}, context)
        wrong_wal = copy.deepcopy(idle_wal)
        wrong_wal["baseline_fingerprint"] = "0" * 64
        assert not module._first_intent_not_applied(
            journal, {"wal": wrong_wal, "visible": baseline}, context)
        changed = copy.deepcopy(baseline)
        changed["processes"][0]["process_location"] = "changed"
        assert not module._first_intent_not_applied(
            journal, {"wal": idle_wal, "visible": changed}, context)
        archive = module._recover_existing_journal(
            paths, "fn-01", {"timeout_s": 30.0})
        recovered = module._read_object(
            archive, "closed Worker file-E2E recovery journal")
        assert recovered["status"] == "RECOVERED"
        assert recovered["restore_confirmed"] is True
        assert recovered["restore_proof"] is None
        assert wal.read_bytes() == product_wal_before


def _assert_physical_actuator_restore(module):
    baseline = _baseline()
    selectors = _selectors()
    target = {
        "host": "192.168.0.3",
        "port": 5020,
        "unit_id": 1,
        "controller_uuid": selectors["controller_uuid"],
        "actuator_id": 3,
        "address": 524,
        "function_type": "write_register",
    }
    with tempfile.TemporaryDirectory(
            prefix="worker-file-e2e-actuator-") as directory:
        paths = {"recovery": Path(directory) / "recovery.json"}
        journal = module._create_recovery_journal(paths, {
            "node_id": "fn-01",
            "original": baseline,
            "selectors": selectors,
            "revision": 7,
            "actuator_restore": {
                "target": target,
                "original_value": 7,
                "may_be_mutated": False,
                "restore_confirmed": False,
            },
        })
        module._journal_mark_actuator_pending(paths, journal)
        original_read = module._read_modbus_register
        original_write = module._write_modbus_register

        reads = iter((9, 7))

        def fake_read(_target):
            return next(reads)

        def fake_write(_target, value):
            assert value == 7
            return value

        try:
            module._read_modbus_register = fake_read
            module._write_modbus_register = fake_write
            proof = module._restore_journal_actuator(
                paths, journal, {"host": "192.168.0.3", "port": 5020})
        finally:
            module._read_modbus_register = original_read
            module._write_modbus_register = original_write
        assert proof["baseline_value"] == 7
        assert proof["observed_before_restore"] == 9
        assert proof["restored_value"] == 7
        assert proof["restore_readback_verified"] is True
        assert proof["write_performed"] is True
        assert proof["verified_noop"] is False
        durable = module._read_recovery_journal(paths, "fn-01")
        assert durable["actuator_restore"]["may_be_mutated"] is False
        assert durable["actuator_restore"]["restore_confirmed"] is True

    with tempfile.TemporaryDirectory(
            prefix="worker-file-e2e-actuator-noop-") as directory:
        paths = {"recovery": Path(directory) / "recovery.json"}
        journal = module._create_recovery_journal(paths, {
            "node_id": "fn-01",
            "original": baseline,
            "selectors": selectors,
            "revision": 7,
            "actuator_restore": {
                "target": target,
                "original_value": 7,
                "may_be_mutated": False,
                "restore_confirmed": False,
            },
        })
        module._journal_mark_actuator_pending(paths, journal)
        original_read = module._read_modbus_register
        original_write = module._write_modbus_register
        reads = iter((7, 7))
        write_calls = []

        def fake_read(_target):
            return next(reads)

        def forbidden_write(_target, value):
            write_calls.append(value)
            raise AssertionError("verified-noop restore must not write")

        try:
            module._read_modbus_register = fake_read
            module._write_modbus_register = forbidden_write
            proof = module._restore_journal_actuator(
                paths, journal, {"host": "192.168.0.3", "port": 5020})
        finally:
            module._read_modbus_register = original_read
            module._write_modbus_register = original_write
        assert write_calls == []
        assert proof["write_performed"] is False
        assert proof["verified_noop"] is True
        durable = module._read_recovery_journal(paths, "fn-01")
        assert durable["actuator_restore"]["may_be_mutated"] is False
        assert durable["actuator_restore"]["restore_confirmed"] is True

    with tempfile.TemporaryDirectory(
            prefix="worker-file-e2e-actuator-drift-") as directory:
        paths = {"recovery": Path(directory) / "recovery.json"}
        journal = module._create_recovery_journal(paths, {
            "node_id": "fn-01",
            "original": baseline,
            "selectors": selectors,
            "revision": 7,
            "actuator_restore": {
                "target": target,
                "original_value": 7,
                "may_be_mutated": False,
                "restore_confirmed": False,
            },
        })
        module._journal_mark_actuator_pending(paths, journal)
        original_read = module._read_modbus_register
        original_write = module._write_modbus_register
        reads = iter((7, 8))

        def fake_read(_target):
            return next(reads)

        try:
            module._read_modbus_register = fake_read
            module._write_modbus_register = forbidden_write
            try:
                module._restore_journal_actuator(
                    paths, journal, {"host": "192.168.0.3", "port": 5020})
            except RuntimeError as exc:
                assert "readback differs" in str(exc)
            else:
                raise AssertionError("drifting verified-noop restore was accepted")
        finally:
            module._read_modbus_register = original_read
            module._write_modbus_register = original_write
        durable = module._read_recovery_journal(paths, "fn-01")
        assert durable["actuator_restore"]["may_be_mutated"] is True
        assert durable["actuator_restore"]["restore_confirmed"] is False


def main():
    module = _load_module()
    module._STORAGE_VERIFIER = _storage_ok
    module._PROJECT_STORAGE = _identity
    _assert_exact_projection(module)
    _assert_authority_gate(module)
    _assert_persistent_journal(module)
    _assert_physical_actuator_restore(module)
    print(
        "PASS Worker file-E2E recovery journal: atomic baseline + exact "
        "projection + first-intent recovery + fail-closed local Authority/ACK gate")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

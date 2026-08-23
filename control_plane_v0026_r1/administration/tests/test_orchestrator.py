#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Functional unit tests for the additive administration orchestrator."""

import contextlib
import io
import json
import os
import sys
import tempfile

from argparse import Namespace
from functools import partial


TEST_DIR = os.path.dirname(os.path.abspath(__file__))
ADMIN_DIR = os.path.dirname(TEST_DIR)
if ADMIN_DIR not in sys.path:
    sys.path.insert(0, ADMIN_DIR)

import orchestrate
import component_adapter


def write_json(path, value):
    orchestrate.atomic_write_json(path, value)


def read_json_lines(path):
    with open(path, "r", encoding="utf-8") as handle:
        return [json.loads(line) for line in handle if line.strip()]


def command_action(fake_path, timeout_s=60, idempotent=True):
    return {
        "command": [
            sys.executable,
            fake_path,
            "{state_dir}/fake-adapter.jsonl",
            "{action}",
            "{component}",
            "{target_id}",
        ],
        "timeout_s": timeout_s,
        "idempotent": idempotent,
    }


def component_entry(name, fake_path):
    index = orchestrate.COMPONENT_ORDER.index(name)
    dependencies = [] if index == 0 else [orchestrate.COMPONENT_ORDER[index - 1]]
    action = command_action(fake_path)
    return {
        "depends_on": dependencies,
        "target_role": "worker" if name == "worker" else "control_plane",
        "rollout_strategy": "canary_batches" if name == "worker" else "serial",
        "reversible": True,
        "actions": {
            "preflight": action,
            "backup": action,
            "apply": action,
            "ready": action,
            "status": action,
            "test": action,
            "restart": action,
            "rollback": action,
        },
    }


def fixture_documents(root, execution_mode="component_adapters"):
    release_root = os.path.join(root, "release")
    os.makedirs(release_root, exist_ok=True)
    config_path = os.path.join(release_root, "config.json")
    write_json(config_path, {"meta": [{"version": 9}], "value": 1})
    sha256s_path = os.path.join(release_root, "SHA256SUMS")
    with open(sha256s_path, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(orchestrate.sha256_file(config_path) + "  ./config.json\n")
    fake_path = os.path.join(TEST_DIR, "fake_adapter.py")
    fake_hash = orchestrate.sha256_file(fake_path)
    inventory = {
        "schema_version": 1,
        "environment": "unit-lab",
        "release_root": release_root,
        "state_dir": os.path.join(root, "state"),
        "targets": [
            {
                "target_id": "worker-03", "role": "worker", "host": "worker-03.invalid",
                "node_id": "fn-03", "components": ["worker"],
            },
            {
                "target_id": "control-plane-01", "role": "control_plane",
                "host": "control-plane.invalid",
                "components": [
                    "proxy", "database", "master", "mqtt",
                    "mcp_controller_server",
                ],
            },
            {
                "target_id": "worker-01", "role": "worker", "host": "worker-01.invalid",
                "node_id": "fn-01", "components": ["worker"],
            },
            {
                "target_id": "worker-02", "role": "worker", "host": "worker-02.invalid",
                "node_id": "fn-02", "components": ["worker"],
            },
        ],
        "config_files": [
            {
                "config_id": "canonical-v9",
                "path": config_path,
                "sha256": orchestrate.sha256_file(config_path),
            }
        ],
        "rollout": {
            "worker_canary_count": 1,
            "worker_batch_size": 2,
            "lock_ttl_s": 300,
        },
    }
    components = {
        name: component_entry(name, fake_path) for name in orchestrate.COMPONENT_ORDER
    }
    registry = {
        "schema_version": 1,
        "execution_mode": execution_mode,
        "required_component_order": list(orchestrate.COMPONENT_ORDER),
        "components": components,
        "operations": {
            "status": command_action(fake_path),
            "test": command_action(fake_path),
        },
    }
    legacy_apply = command_action(fake_path, idempotent=False)
    legacy_apply["command"][1] = "{legacy_backend}"
    legacy_apply["command"].extend((
        "--release", "{release_root}",
        "--worker-archive", "{worker_archive}",
    ))
    registry["legacy_full"] = {
        "apply": legacy_apply,
        "ready": command_action(fake_path),
        "test": command_action(fake_path),
    }
    if execution_mode == "legacy_full_once":
        for component in components.values():
            component.pop("actions", None)
            component["reversible"] = False
    manifest = {
        "schema_version": 1,
        "release": {"name": "control-plane", "version": "0.19.0", "revision": "r2"},
        "worker": {
            "name": "automation_system", "version": "0.88.0",
            "revision": "v0088", "artifact_id": "fake-adapter",
        },
        "compatibility": {"node_storage": [9], "legacy_import": [5], "cross_storage": [8]},
        "artifacts": [
            {"artifact_id": "fake-adapter", "path": fake_path, "sha256": fake_hash}
        ],
        "legacy_backend": {
            "path": fake_path,
            "sha256": fake_hash,
            "component_order": list(orchestrate.COMPONENT_ORDER),
        },
        "required_tests": ["readiness", "static"],
    }
    inventory_path = os.path.join(root, "inventory.json")
    registry_path = os.path.join(root, "registry.json")
    manifest_path = os.path.join(root, "manifest.json")
    write_json(inventory_path, inventory)
    write_json(registry_path, registry)
    write_json(manifest_path, manifest)
    return {
        "inventory": inventory,
        "registry": registry,
        "manifest": manifest,
        "inventory_path": inventory_path,
        "registry_path": registry_path,
        "manifest_path": manifest_path,
        "config_path": config_path,
        "sha256s_path": sha256s_path,
        "state_dir": inventory["state_dir"],
    }


def build_fixture_plan(fixture):
    return orchestrate.build_plan(
        fixture["inventory_path"], fixture["registry_path"], fixture["manifest_path"],
    )


def apply_components(plan):
    return [
        (step["component"], step["target"]["target_id"])
        for step in plan["steps"] if step["phase"] == "apply"
    ]


def test_deterministic_plan_and_dag_order():
    with tempfile.TemporaryDirectory(prefix="admin-plan-") as root:
        fixture = fixture_documents(root)
        first = build_fixture_plan(fixture)
        fixture["inventory"]["targets"].reverse()
        write_json(fixture["inventory_path"], fixture["inventory"])
        second = build_fixture_plan(fixture)
        assert first["plan_id"] == second["plan_id"]
        assert first["worker_rollout"] == {
            "canary": ["worker-01"],
            "batches": [["worker-02", "worker-03"]],
            "batch_size": 2,
        }
        assert apply_components(first) == [
            ("database", "control-plane-01"),
            ("mqtt", "control-plane-01"),
            ("master", "control-plane-01"),
            ("proxy", "control-plane-01"),
            ("mcp_controller_server", "control-plane-01"),
            ("worker", "worker-01"),
            ("worker", "worker-02"),
            ("worker", "worker-03"),
        ]
        assert len({step["idempotency_key"] for step in first["steps"]}) == len(first["steps"])
        assert first["release_integrity"]["entry_count"] == 1
        assert len(first["release_integrity"]["full_release_digest"]) == 64


def test_partial_component_target_selection_and_operation_scope():
    with tempfile.TemporaryDirectory(prefix="admin-partial-") as root:
        fixture = fixture_documents(root)
        plan = orchestrate.build_plan(
            fixture["inventory_path"], fixture["registry_path"],
            fixture["manifest_path"], selected_components=["worker"],
            selected_targets=["worker-02"], worker_batch_size=1,
        )
        assert plan["selected_components"] == ["worker"]
        assert [target["target_id"] for target in plan["targets"]] == ["worker-02"]
        assert plan["lock_targets"] == ["worker-02"]
        assert plan["worker_rollout"] == {
            "canary": ["worker-02"], "batches": [], "batch_size": 1,
        }
        assert apply_components(plan) == [("worker", "worker-02")]
        for operation_name in orchestrate.OPERATION_NAMES:
            steps = plan["operation_steps"][operation_name]
            assert [(step["component"], step["target"]["target_id"])
                    for step in steps] == [("worker", "worker-02")]

        cp_plan = orchestrate.build_plan(
            fixture["inventory_path"], fixture["registry_path"],
            fixture["manifest_path"], selected_components=["master,proxy"],
        )
        assert cp_plan["selected_components"] == ["master", "proxy"]
        assert apply_components(cp_plan) == [
            ("master", "control-plane-01"),
            ("proxy", "control-plane-01"),
        ]
        assert cp_plan["worker_rollout"]["canary"] == []


def test_partial_live_status_test_and_restart_use_adapter_targets():
    with tempfile.TemporaryDirectory(prefix="admin-partial-ops-") as root:
        fixture = fixture_documents(root)
        plan = orchestrate.build_plan(
            fixture["inventory_path"], fixture["registry_path"],
            fixture["manifest_path"], selected_components=["master"],
        )
        checkpoint = orchestrate.apply_plan(plan)
        assert checkpoint["status"] == "complete"
        for operation_name in orchestrate.OPERATION_NAMES:
            operation_id = "restart-test-001" if operation_name == "restart" else None
            result = orchestrate.run_plan_operation(
                plan, operation_name, operation_id=operation_id,
            )
            assert result["ok"]
            assert len(result["results"]) == 1
        try:
            orchestrate.run_plan_operation(plan, "restart")
        except ValueError as exc:
            assert "operation_id" in str(exc)
        else:
            raise AssertionError("restart without stable operation_id was accepted")
        records = read_json_lines(os.path.join(
            fixture["state_dir"], "fake-adapter.jsonl",
        ))
        assert ("status", "master", "control-plane-01") in [
            (item["action"], item["component"], item["target"])
            for item in records
        ]
        assert ("restart", "master", "control-plane-01") in [
            (item["action"], item["component"], item["target"])
            for item in records
        ]


def test_cli_partial_plan_and_external_output_enforcement():
    with tempfile.TemporaryDirectory(prefix="admin-cli-plan-") as root:
        fixture = fixture_documents(root)
        output = os.path.join(root, "plans", "worker-02.json")
        with contextlib.redirect_stdout(io.StringIO()):
            result = orchestrate.main([
                "plan", "--inventory", fixture["inventory_path"],
                "--registry", fixture["registry_path"],
                "--manifest", fixture["manifest_path"],
                "--component", "worker", "--target", "worker-02",
                "--output", output,
            ])
        assert result == 0
        plan = orchestrate.load_plan(output)
        assert plan["selected_components"] == ["worker"]
        assert plan["lock_targets"] == ["worker-02"]

        inside = os.path.join(root, "release", "forbidden-plan.json")
        with contextlib.redirect_stdout(io.StringIO()):
            result = orchestrate.main([
                "plan", "--inventory", fixture["inventory_path"],
                "--registry", fixture["registry_path"],
                "--manifest", fixture["manifest_path"],
                "--output", inside,
            ])
        assert result == 2
        assert not os.path.exists(inside)


def test_apply_resume_fencing_evidence_and_redaction():
    with tempfile.TemporaryDirectory(prefix="admin-apply-") as root:
        fixture = fixture_documents(root)
        plan = build_fixture_plan(fixture)
        runtime_env = {"ADMIN_FAKE_EMIT_SECRET": "1"}
        checkpoint = orchestrate.apply_plan(plan, runtime_env=runtime_env)
        assert checkpoint["status"] == "complete"
        log_path = os.path.join(fixture["state_dir"], "fake-adapter.jsonl")
        first_records = read_json_lines(log_path)
        assert first_records
        assert all(record["fencing_token"] > 0 for record in first_records)
        assert all(record["idempotency_key"] for record in first_records)
        resumed = orchestrate.apply_plan(plan, resume=True, runtime_env=runtime_env)
        assert resumed["status"] == "complete"
        assert len(read_json_lines(log_path)) == len(first_records)
        evidence_dir = os.path.join(fixture["state_dir"], "evidence")
        evidence_text = ""
        for name in os.listdir(evidence_dir):
            with open(os.path.join(evidence_dir, name), "r", encoding="utf-8") as handle:
                evidence_text += handle.read()
        with open(os.path.join(fixture["state_dir"], "audit.jsonl"), "r", encoding="utf-8") as handle:
            audit_text = handle.read()
        assert "super-secret-test-value" not in evidence_text
        assert "super-secret-test-value" not in audit_text
        assert "<redacted>" in evidence_text
        redacted = orchestrate.redact_text('{"password":"visible"} https://user:visible@host.invalid')
        assert "visible" not in redacted
        operation = orchestrate.run_plan_operation(plan, "test", runtime_env=runtime_env)
        assert operation["ok"]


def test_config_drift_gate_fails_closed():
    with tempfile.TemporaryDirectory(prefix="admin-drift-") as root:
        fixture = fixture_documents(root)
        plan = build_fixture_plan(fixture)
        write_json(fixture["config_path"], {"meta": [{"version": 9}], "value": 2})
        failed = False
        try:
            orchestrate.verify_drift_gate(plan)
        except ValueError as exc:
            failed = "drift" in str(exc) or "SHA-256" in str(exc)
        assert failed


def test_root_sha256sums_and_external_state_are_fail_closed():
    with tempfile.TemporaryDirectory(prefix="admin-root-integrity-") as root:
        fixture = fixture_documents(root)
        plan = build_fixture_plan(fixture)
        with open(fixture["sha256s_path"], "a", encoding="utf-8") as stream:
            stream.write("0" * 64 + "  ./missing.json\n")
        try:
            orchestrate.verify_drift_gate(plan)
        except ValueError as exc:
            assert "SHA256SUMS" in str(exc) or "missing" in str(exc)
        else:
            raise AssertionError("root SHA256SUMS drift was accepted")

    with tempfile.TemporaryDirectory(prefix="admin-state-inside-") as root:
        fixture = fixture_documents(root)
        fixture["inventory"]["state_dir"] = os.path.join(
            root, "release", "runtime", "administration",
        )
        write_json(fixture["inventory_path"], fixture["inventory"])
        try:
            build_fixture_plan(fixture)
        except ValueError as exc:
            assert "outside the immutable release root" in str(exc)
        else:
            raise AssertionError("state inside immutable release was accepted")


def test_failure_rolls_back_only_backed_up_reversible_steps():
    with tempfile.TemporaryDirectory(prefix="admin-rollback-") as root:
        fixture = fixture_documents(root)
        plan = build_fixture_plan(fixture)
        checkpoint = orchestrate.apply_plan(
            plan,
            runtime_env={"ADMIN_FAKE_FAIL_MATCH": "apply:worker:worker-02"},
        )
        assert checkpoint["status"] == "failed_rolled_back"
        assert not checkpoint["manual_recovery"]
        records = read_json_lines(os.path.join(fixture["state_dir"], "fake-adapter.jsonl"))
        rollback_targets = [
            (record["component"], record["target"])
            for record in records if record["action"] == "rollback"
        ]
        assert rollback_targets == [
            ("worker", "worker-02"),
            ("worker", "worker-01"),
            ("mcp_controller_server", "control-plane-01"),
            ("proxy", "control-plane-01"),
            ("master", "control-plane-01"),
            ("mqtt", "control-plane-01"),
            ("database", "control-plane-01"),
        ]
        assert "rollback_ready" not in {record["action"] for record in records}


def test_terminal_predecessor_requires_explicit_archival():
    with tempfile.TemporaryDirectory(prefix="admin-terminal-archive-") as root:
        fixture = fixture_documents(root)
        first = orchestrate.build_plan(
            fixture["inventory_path"], fixture["registry_path"],
            fixture["manifest_path"], selected_components=["database"],
        )
        assert orchestrate.apply_plan(first)["status"] == "complete"
        second = orchestrate.build_plan(
            fixture["inventory_path"], fixture["registry_path"],
            fixture["manifest_path"], selected_components=["mqtt"],
        )
        assert first["plan_id"] != second["plan_id"]
        try:
            orchestrate.apply_plan(second)
        except ValueError as exc:
            assert "archive-terminal" in str(exc)
        else:
            raise AssertionError("different predecessor plan was overwritten")

        result = orchestrate.apply_plan(second, archive_terminal=True)
        assert result["status"] == "complete"
        archive = os.path.join(
            fixture["state_dir"], "__OLD__", first["plan_id"],
        )
        archived = orchestrate.read_json(
            os.path.join(archive, "checkpoint.json"),
        )
        handoff = orchestrate.read_json(os.path.join(archive, "handoff.json"))
        assert archived["status"] == "complete"
        assert handoff["superseding_plan_id"] == second["plan_id"]
        current = orchestrate.load_checkpoint(fixture["state_dir"])
        assert current["plan_id"] == second["plan_id"]
        assert os.path.isdir(os.path.join(fixture["state_dir"], "locks"))


def test_manual_recovery_predecessor_cannot_be_archived():
    with tempfile.TemporaryDirectory(prefix="admin-manual-archive-") as root:
        fixture = fixture_documents(root)
        first = orchestrate.build_plan(
            fixture["inventory_path"], fixture["registry_path"],
            fixture["manifest_path"], selected_components=["database"],
        )
        checkpoint = orchestrate._new_checkpoint(first, "manual-owner")
        checkpoint["status"] = "failed_manual_recovery"
        checkpoint["manual_recovery"] = [{"reason": "operator_required"}]
        orchestrate.write_checkpoint(fixture["state_dir"], checkpoint)
        second = orchestrate.build_plan(
            fixture["inventory_path"], fixture["registry_path"],
            fixture["manifest_path"], selected_components=["mqtt"],
        )
        try:
            orchestrate.apply_plan(second, archive_terminal=True)
        except ValueError as exc:
            assert "safely archivable" in str(exc)
        else:
            raise AssertionError("manual recovery predecessor was archived")
        current = orchestrate.load_checkpoint(fixture["state_dir"])
        assert current["plan_id"] == first["plan_id"]
        assert not os.path.exists(os.path.join(fixture["state_dir"], "__OLD__"))


def test_legacy_backend_is_planned_and_run_exactly_once():
    with tempfile.TemporaryDirectory(prefix="admin-legacy-") as root:
        fixture = fixture_documents(root, execution_mode="legacy_full_once")
        plan = build_fixture_plan(fixture)
        legacy_steps = [
            step for step in plan["steps"]
            if step["component"] == "legacy_full" and step["phase"] == "apply"
        ]
        assert len(legacy_steps) == 1
        checkpoint = orchestrate.apply_plan(plan)
        assert checkpoint["status"] == "complete"
        log_path = os.path.join(fixture["state_dir"], "fake-adapter.jsonl")
        records = read_json_lines(log_path)
        count = len(records)
        legacy_apply = next(record for record in records
                            if record["action"] == "apply")
        assert legacy_apply["release_root"] == plan["release_root"]
        assert legacy_apply["worker_archive"] == plan["artifacts"][0]["path"]
        orchestrate.apply_plan(plan, resume=True)
        assert len(read_json_lines(log_path)) == count
        rolled = orchestrate.rollback_plan(plan)
        assert rolled["status"] == "manual_recovery"
        assert rolled["manual_recovery"][0]["reason"] == "step_not_reversible"


def test_legacy_backend_rejects_partial_selection():
    with tempfile.TemporaryDirectory(prefix="admin-legacy-partial-") as root:
        fixture = fixture_documents(root, execution_mode="legacy_full_once")
        try:
            orchestrate.build_plan(
                fixture["inventory_path"], fixture["registry_path"],
                fixture["manifest_path"], selected_components=["proxy"],
            )
        except ValueError as exc:
            assert "cannot execute a partial selection" in str(exc)
        else:
            raise AssertionError("legacy backend accepted a partial selection")

    with tempfile.TemporaryDirectory(prefix="admin-adapter-fallback-") as root:
        fixture = fixture_documents(root, execution_mode="component_adapters")
        plan = orchestrate.build_plan(
            fixture["inventory_path"], fixture["registry_path"],
            fixture["manifest_path"], force_legacy_full_once=True,
        )
        assert plan["execution_mode"] == "legacy_full_once"
        assert len([step for step in plan["steps"]
                    if step["component"] == "legacy_full"
                    and step["phase"] == "apply"]) == 1


def test_indeterminate_legacy_backend_is_never_repeated():
    with tempfile.TemporaryDirectory(prefix="admin-legacy-crash-") as root:
        fixture = fixture_documents(root, execution_mode="legacy_full_once")
        plan = build_fixture_plan(fixture)
        legacy_step = plan["steps"][0]
        checkpoint = orchestrate._new_checkpoint(plan, "resume-owner")
        checkpoint["status"] = "applying"
        checkpoint["active_step"] = {
            "step_id": legacy_step["step_id"],
            "idempotent": False,
            "started_at": orchestrate.utc_now(),
        }
        orchestrate.write_checkpoint(fixture["state_dir"], checkpoint)
        resumed = orchestrate.apply_plan(plan, resume=True)
        assert resumed["status"] == "failed_manual_recovery"
        assert resumed["manual_recovery"][0]["reason"] == "non_idempotent_result_indeterminate"
        log_path = os.path.join(fixture["state_dir"], "fake-adapter.jsonl")
        assert not os.path.exists(log_path)


def test_unresolved_placeholders_cannot_apply():
    with tempfile.TemporaryDirectory(prefix="admin-placeholder-") as root:
        fixture = fixture_documents(root)
        fixture["inventory"]["config_files"][0]["sha256"] = "__TEST_CONFIG_SHA256__"
        write_json(fixture["inventory_path"], fixture["inventory"])
        plan = orchestrate.build_plan(
            fixture["inventory_path"], fixture["registry_path"], fixture["manifest_path"],
            allow_placeholders=True,
        )
        assert plan["has_placeholders"]
        failed = False
        try:
            orchestrate.apply_plan(plan)
        except ValueError as exc:
            failed = "placeholder" in str(exc)
        assert failed


def test_inline_credentials_are_rejected():
    with tempfile.TemporaryDirectory(prefix="admin-credentials-") as root:
        fixture = fixture_documents(root)
        fixture["inventory"]["ssh_password"] = "must-not-be-inline"
        write_json(fixture["inventory_path"], fixture["inventory"])
        failed = False
        try:
            build_fixture_plan(fixture)
        except ValueError as exc:
            failed = "secret material" in str(exc)
        assert failed


def test_lock_fencing_token_is_monotonic():
    with tempfile.TemporaryDirectory(prefix="admin-lock-") as root:
        first = orchestrate.acquire_lock(root, "environment--unit", "p1", "o1", 300)
        assert orchestrate.release_lock(
            root, "environment--unit", "o1", first["fencing_token"],
        )
        second = orchestrate.acquire_lock(root, "environment--unit", "p2", "o2", 300)
        assert second["fencing_token"] > first["fencing_token"]
        assert orchestrate.release_lock(
            root, "environment--unit", "o2", second["fencing_token"],
        )


def test_state_dir_override_cannot_bypass_plan_locks():
    with tempfile.TemporaryDirectory(prefix="admin-state-root-") as root:
        fixture = fixture_documents(root)
        plan = build_fixture_plan(fixture)
        alternate = os.path.join(root, "alternate-lock-root")
        for operation in (
                partial(orchestrate.apply_plan, plan, state_dir=alternate),
                partial(orchestrate.run_plan_operation, plan, "status",
                        state_dir=alternate),
                partial(orchestrate.rollback_plan, plan, state_dir=alternate),
                partial(orchestrate.plan_status, plan, state_dir=alternate)):
            try:
                operation()
            except ValueError as exc:
                assert "immutable plan lock root" in str(exc)
            else:
                raise AssertionError("alternate administration lock root was accepted")


def test_plan_tamper_is_rejected():
    with tempfile.TemporaryDirectory(prefix="admin-tamper-") as root:
        fixture = fixture_documents(root)
        plan = build_fixture_plan(fixture)
        plan["steps"][0]["timeout_s"] += 1
        failed = False
        try:
            orchestrate.validate_plan(plan)
        except ValueError as exc:
            failed = "plan_id" in str(exc)
        assert failed


def test_operational_component_adapter_contract_self_test():
    assert component_adapter.self_test() == 0
    formatted = orchestrate._format_command(
        ["{target_options}"], {"target_options": '{"transport":"local"}'},
    )
    assert formatted == ['{"transport":"local"}']


def test_operational_component_adapter_waits_for_runtime_health():
    original_check = component_adapter._check_runtime_services
    original_sleep = component_adapter.time.sleep
    original_timeout = component_adapter.RUNTIME_READINESS_TIMEOUT_S
    attempts = []
    sleeps = []

    def transient_check(_arguments):
        attempts.append("check")
        if len(attempts) == 1:
            raise component_adapter.AdapterError("health is starting")

    try:
        component_adapter._check_runtime_services = transient_check
        component_adapter.time.sleep = sleeps.append
        component_adapter._wait_runtime_services(object())
        assert attempts == ["check", "check"]
        assert sleeps == [component_adapter.RUNTIME_READINESS_POLL_S]

        attempts.clear()
        component_adapter.RUNTIME_READINESS_TIMEOUT_S = 0.0
        try:
            component_adapter._wait_runtime_services(object())
        except component_adapter.AdapterError as exc:
            assert "timed out" in str(exc)
        else:
            raise AssertionError("runtime readiness timeout was not fail-closed")
    finally:
        component_adapter._check_runtime_services = original_check
        component_adapter.time.sleep = original_sleep
        component_adapter.RUNTIME_READINESS_TIMEOUT_S = original_timeout


def test_operational_component_adapter_hands_off_before_database_apply():
    with tempfile.TemporaryDirectory(prefix="admin-database-handoff-") as root:
        os.makedirs(os.path.join(root, "postgres_system"))
        arguments = Namespace(
            release_root=root, component="database", host="localhost", action="apply",
        )
        events = []
        original_assert = component_adapter._assert_local_target
        original_handoff = component_adapter._handoff_live_database_secrets
        original_tls_handoff = component_adapter._handoff_live_database_tls
        original_script = component_adapter._script
        original_run = component_adapter.run

        def fake_run(command, **kwargs):
            del kwargs
            events.append(("deploy", tuple(command)))
            return ""

        def ignore_local_target(_host):
            return None

        def record_handoff(_arguments):
            events.append(("handoff", ()))

        def record_tls_handoff(_arguments, profile):
            events.append(("tls-handoff", (profile,)))

        def verified_script(_arguments, _name):
            return "/verified/deploy"

        try:
            component_adapter._assert_local_target = ignore_local_target
            component_adapter._handoff_live_database_secrets = record_handoff
            component_adapter._handoff_live_database_tls = record_tls_handoff
            component_adapter._script = verified_script
            component_adapter.run = fake_run
            component_adapter.control_plane_action(
                arguments,
                {"transport": "local", "deployment_profile": "test"},
            )
        finally:
            component_adapter._assert_local_target = original_assert
            component_adapter._handoff_live_database_secrets = original_handoff
            component_adapter._handoff_live_database_tls = original_tls_handoff
            component_adapter._script = original_script
            component_adapter.run = original_run
        assert events == [
            ("handoff", ()),
            ("tls-handoff", ("test",)),
            ("deploy", ("/verified/deploy",)),
        ]

def test_operational_component_adapter_configures_database_tls_handoff():
    with tempfile.TemporaryDirectory(prefix="admin-database-tls-") as root:
        directory = os.path.join(
            root, "postgres_system", "runtime-tls-handoff",
        )
        os.makedirs(directory)
        names = {
            "POSTGRES_CA_SOURCE": "ca.crt",
            "POSTGRES_PRIMARY_CERT_SOURCE": "primary.crt",
            "POSTGRES_PRIMARY_KEY_SOURCE": "primary.key",
            "POSTGRES_STANDBY_CERT_SOURCE": "standby.crt",
            "POSTGRES_STANDBY_KEY_SOURCE": "standby.key",
        }
        for name in tuple(names.values()) + (".live_postgres_tls_handoff",):
            with open(os.path.join(directory, name), "w", encoding="utf-8") as stream:
                stream.write("verified runtime material\n")
        arguments = Namespace(release_root=root, component="database")
        previous = {name: os.environ.get(name) for name in names}
        try:
            for name in names:
                os.environ.pop(name, None)
            component_adapter._configure_database_tls_handoff_environment(
                arguments,
            )
            for environment_name, name in names.items():
                assert os.environ[environment_name] == os.path.join(
                    directory, name,
                )
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def test_operational_component_adapter_database_test_is_non_deploying():
    with tempfile.TemporaryDirectory(prefix="admin-database-test-tls-") as root:
        os.makedirs(os.path.join(root, "postgres_system"))
        arguments = Namespace(
            release_root=root, component="database", host="localhost",
            action="test",
        )
        events = []
        original_assert = component_adapter._assert_local_target
        original_configure = (
            component_adapter._configure_database_tls_handoff_environment
        )
        original_script = component_adapter._script
        original_run = component_adapter.run

        def fake_run(command, **kwargs):
            del kwargs
            events.append(("test", tuple(command)))
            return ""

        def ignore_local_target(_host):
            return None

        def reject_tls_environment(_arguments):
            raise AssertionError("live database test reused cached TLS handoff")

        def verified_script(_arguments, name):
            events.append(("script", (name,)))
            return "/verified/" + name

        try:
            component_adapter._assert_local_target = ignore_local_target
            component_adapter._configure_database_tls_handoff_environment = (
                reject_tls_environment
            )
            component_adapter._script = verified_script
            component_adapter.run = fake_run
            component_adapter.control_plane_action(
                arguments,
                {"transport": "local", "deployment_profile": "test"},
            )
        finally:
            component_adapter._assert_local_target = original_assert
            component_adapter._configure_database_tls_handoff_environment = (
                original_configure
            )
            component_adapter._script = original_script
            component_adapter.run = original_run
        assert events == [
            ("script", ("test_running.sh",)),
            ("test", ("/verified/test_running.sh",)),
        ]


def test_operational_component_adapter_hands_off_mqtt_trust_and_binding():
    with tempfile.TemporaryDirectory(prefix="admin-mqtt-handoff-") as root:
        os.makedirs(os.path.join(root, "mqtt_broker"))
        arguments = Namespace(
            release_root=root, component="mqtt", host="localhost",
            action="apply",
        )
        options = {
            "transport": "local",
            "deployment_profile": "test",
            "mqtt_bind_address": "192.0.2.10",
            "mqtt_test_host": "192.0.2.10",
            "mqtt_test_broker_ip": "192.0.2.10",
        }
        events = []
        environment_names = (
            "MQTT_BIND_ADDRESS", "MQTT_TEST_HOST", "MQTT_TEST_BROKER_IP",
        )
        previous_environment = {
            name: os.environ.get(name) for name in environment_names
        }
        original_assert = component_adapter._assert_local_target
        original_handoff = component_adapter._handoff_live_mqtt_pki
        original_script = component_adapter._script
        original_run = component_adapter.run

        def fake_run(command, **kwargs):
            del kwargs
            events.append(("deploy", tuple(command)))
            return ""

        def ignore_local_target(_host):
            return None

        def record_handoff(_arguments, profile):
            events.append(("handoff", (profile,)))

        def verified_script(_arguments, _name):
            return "/verified/deploy"

        try:
            component_adapter._assert_local_target = ignore_local_target
            component_adapter._handoff_live_mqtt_pki = record_handoff
            component_adapter._script = verified_script
            component_adapter.run = fake_run
            component_adapter.control_plane_action(arguments, options)
            assert os.environ["MQTT_BIND_ADDRESS"] == "192.0.2.10"
            assert os.environ["MQTT_TEST_HOST"] == "192.0.2.10"
            assert os.environ["MQTT_TEST_BROKER_IP"] == "192.0.2.10"
        finally:
            component_adapter._assert_local_target = original_assert
            component_adapter._handoff_live_mqtt_pki = original_handoff
            component_adapter._script = original_script
            component_adapter.run = original_run
            for name, value in previous_environment.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
        assert events == [
            ("handoff", ("test",)),
            ("deploy", ("/verified/deploy",)),
        ]


def test_operational_component_adapter_configures_isolated_mcp_runtime():
    with tempfile.TemporaryDirectory(prefix="admin-mcp-runtime-") as root:
        release_root = os.path.join(root, "release")
        auth_root = os.path.join(root, "external")
        pki_root = os.path.join(release_root, "mqtt_broker", "test-pki")
        client_root = os.path.join(pki_root, "clients", "mcp-controller")
        os.makedirs(os.path.join(pki_root, "ca"))
        os.makedirs(client_root)
        os.makedirs(auth_root)
        files = (
            os.path.join(pki_root, "ca", "ca.crt"),
            os.path.join(client_root, "mqtt.crt"),
            os.path.join(client_root, "mqtt.key"),
        )
        for path in files:
            with open(path, "w", encoding="utf-8") as stream:
                stream.write("verified test material\n")
        os.chmod(files[2], 0o600)
        auth_path = os.path.join(auth_root, "mcp_auth.json")
        with open(auth_path, "w", encoding="utf-8") as stream:
            stream.write('{"tokens":[]}\n')
        os.chmod(auth_path, 0o600)
        arguments = Namespace(
            release_root=release_root,
            component="mcp_controller_server",
        )
        environment_names = (
            "AUTOMATION_SITE_ID", "MCP_BIND_ADDRESS", "MCP_HTTP_PORT",
            "MCP_MQTT_SOURCE_INSTANCE_ID", "MCP_AUTH_SOURCE",
            "MCP_MQTT_CA_SOURCE", "MCP_MQTT_CERT_SOURCE",
            "MCP_MQTT_KEY_SOURCE",
        )
        previous = {name: os.environ.get(name) for name in environment_names}
        try:
            component_adapter._configure_mcp_environment(arguments, {
                "deployment_profile": "test",
                "site_id": "site-main",
                "mcp_bind_address": "127.0.0.1",
                "mcp_http_port": "8080",
                "mcp_source_instance_id": "mcp-controller-01",
                "mcp_auth_source": auth_path,
            })
            assert os.environ["AUTOMATION_SITE_ID"] == "site-main"
            assert os.environ["MCP_BIND_ADDRESS"] == "127.0.0.1"
            assert os.environ["MCP_HTTP_PORT"] == "8080"
            assert os.environ["MCP_MQTT_SOURCE_INSTANCE_ID"] == "mcp-controller-01"
            assert os.environ["MCP_AUTH_SOURCE"] == auth_path
            assert os.environ["MCP_MQTT_CA_SOURCE"] == files[0]
            assert os.environ["MCP_MQTT_CERT_SOURCE"] == files[1]
            assert os.environ["MCP_MQTT_KEY_SOURCE"] == files[2]

            failed = False
            try:
                component_adapter._configure_mcp_environment(arguments, {
                    "deployment_profile": "production",
                    "mcp_auth_source": auth_path,
                    "mcp_mqtt_ca_source": files[0],
                })
            except component_adapter.AdapterError as exc:
                failed = "complete set" in str(exc)
            assert failed
        finally:
            for name, value in previous.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value


def test_operational_component_adapter_stages_worker_pki_before_stop():
    arguments = Namespace(action="apply")
    events = []
    original_stage = component_adapter._stage_worker_pki
    original_lifecycle = component_adapter._lifecycle
    original_install = component_adapter._install_worker

    def record_stage(_arguments, _options):
        events.append("stage-pki")

    def record_lifecycle(_arguments, _options, action):
        events.append(action)

    def record_install(_arguments, _options):
        events.append("install-worker")

    try:
        component_adapter._stage_worker_pki = record_stage
        component_adapter._lifecycle = record_lifecycle
        component_adapter._install_worker = record_install
        component_adapter.worker_action(arguments, {})
    finally:
        component_adapter._stage_worker_pki = original_stage
        component_adapter._lifecycle = original_lifecycle
        component_adapter._install_worker = original_install

    assert events == [
        "stage-pki",
        "stop-compatible",
        "install-worker",
        "install-start",
    ]


def test_operational_component_adapter_fencing_and_idempotency():
    with tempfile.TemporaryDirectory(prefix="admin-adapter-state-") as root:
        arguments = Namespace(
            state_dir=root, target_id="worker-01", component="worker",
            plan_id="a" * 64, step_id="restart-01",
            idempotency_key="a" * 64 + ":restart:maintenance-01",
            fencing_token=2, action="restart",
        )
        path, replay = component_adapter.claim_mutation(arguments)
        assert not replay
        component_adapter.finish_mutation(path, arguments, "complete")
        _path, replay = component_adapter.claim_mutation(arguments)
        assert replay
        first_key = arguments.idempotency_key
        arguments.idempotency_key += "-new"
        arguments.fencing_token = 1
        try:
            component_adapter.claim_mutation(arguments)
        except component_adapter.AdapterError as exc:
            assert "stale" in str(exc)
        else:
            raise AssertionError("stale component adapter fencing token was accepted")
        arguments.fencing_token = 3
        path, replay = component_adapter.claim_mutation(arguments)
        assert not replay
        component_adapter.finish_mutation(path, arguments, "complete")
        arguments.idempotency_key = first_key
        arguments.fencing_token = 4
        _path, replay = component_adapter.claim_mutation(arguments)
        assert replay


def run_tests():
    tests = (
        test_deterministic_plan_and_dag_order,
        test_partial_component_target_selection_and_operation_scope,
        test_partial_live_status_test_and_restart_use_adapter_targets,
        test_cli_partial_plan_and_external_output_enforcement,
        test_apply_resume_fencing_evidence_and_redaction,
        test_config_drift_gate_fails_closed,
        test_root_sha256sums_and_external_state_are_fail_closed,
        test_failure_rolls_back_only_backed_up_reversible_steps,
        test_terminal_predecessor_requires_explicit_archival,
        test_manual_recovery_predecessor_cannot_be_archived,
        test_legacy_backend_is_planned_and_run_exactly_once,
        test_legacy_backend_rejects_partial_selection,
        test_indeterminate_legacy_backend_is_never_repeated,
        test_unresolved_placeholders_cannot_apply,
        test_inline_credentials_are_rejected,
        test_lock_fencing_token_is_monotonic,
        test_state_dir_override_cannot_bypass_plan_locks,
        test_plan_tamper_is_rejected,
        test_operational_component_adapter_contract_self_test,
        test_operational_component_adapter_waits_for_runtime_health,
        test_operational_component_adapter_hands_off_before_database_apply,
        test_operational_component_adapter_configures_database_tls_handoff,
        test_operational_component_adapter_database_test_is_non_deploying,
        test_operational_component_adapter_hands_off_mqtt_trust_and_binding,
        test_operational_component_adapter_configures_isolated_mcp_runtime,
        test_operational_component_adapter_stages_worker_pki_before_stop,
        test_operational_component_adapter_fencing_and_idempotency,
    )
    for test in tests:
        test()
        print("PASS " + test.__name__)
    print("PASS all administration orchestrator tests: " + str(len(tests)))
    return 0


if __name__ == "__main__":
    sys.exit(run_tests())

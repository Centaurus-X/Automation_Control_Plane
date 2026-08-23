#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Isolated regression tests for the autonomous rollout boundary."""

import io
import json
import os
import shutil
import stat
import subprocess
import tempfile

from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import credential_reader
import read_rollout_config
import ssh_askpass
import write_rollout_summary

from verify_core_e2e_result import REQUIRED_CHECKS, verify


ROOT = Path(__file__).resolve().parent.parent
TOOLS = ROOT / "tools"
SCRIPT = ROOT / "rollout_automation.sh"
REMOTE = TOOLS / "worker_remote_lifecycle.sh"
CREATE_ADMIN = ROOT / "proxy_gateway" / "create_admin.sh"


def run(command, **kwargs):
    return subprocess.run(
        command,
        cwd=kwargs.get("cwd", ROOT),
        env=kwargs.get("env"),
        input=kwargs.get("input"),
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )


def write_json(path, value, mode=0o600):
    path.write_text(json.dumps(value) + "\n", encoding="utf-8")
    path.chmod(mode)


def expect_value_error(callable_object, expected_fragment):
    try:
        callable_object()
    except ValueError as error:
        assert expected_fragment in str(error), str(error)
        return
    raise AssertionError("expected ValueError containing: " + expected_fragment)


def test_credentials(root):
    nested = root / "nested.json"
    write_json(nested, {
        "ssh": {"philo": {"password": "worker-secret"}},
        "control_plane": {"admin": {"password": "admin-secret"}},
    })
    assert credential_reader.load_credential(
        nested, ("philo", "ssh.philo")) == "worker-secret"
    assert credential_reader.load_credential(
        nested, ("admin", "control_plane.admin")) == "admin-secret"

    flat = root / "flat.json"
    write_json(flat, {"philo": "flat-worker", "admin": "flat-admin"})
    assert credential_reader.load_credential(flat, ("philo",)) == "flat-worker"
    assert credential_reader.load_credential(flat, ("admin",)) == "flat-admin"

    for unsafe_mode in (0o644, 0o700):
        unsafe = root / ("unsafe-{:o}.json".format(unsafe_mode))
        write_json(unsafe, {"admin": "never-print"}, unsafe_mode)
        expect_value_error(
            partial_path_call(credential_reader.load_credential, unsafe, ("admin",)),
            "permissions",
        )

    duplicate = root / "duplicate.json"
    duplicate.write_text('{"admin":"first","admin":"second"}\n', encoding="utf-8")
    duplicate.chmod(0o600)
    expect_value_error(
        partial_path_call(credential_reader.load_credential, duplicate, ("admin",)),
        "duplicate JSON key",
    )

    control = root / "control.json"
    write_json(control, {"admin": "line\nbreak"})
    expect_value_error(
        partial_path_call(credential_reader.load_credential, control, ("admin",)),
        "control characters",
    )

    symlink_target = root / "symlink-target.json"
    write_json(symlink_target, {"admin": "hidden"})
    symlink = root / "symlink.json"
    symlink.symlink_to(symlink_target)
    expect_value_error(
        partial_path_call(credential_reader.load_credential, symlink, ("admin",)),
        "symbolic link",
    )

    expect_value_error(
        partial_path_call(credential_reader.load_credential, root / "missing.json", ("admin",)),
        "not found",
    )


def partial_path_call(function, path, keys):
    from functools import partial
    return partial(function, path, keys)


def test_rollout_configuration(root):
    example = ROOT / "rollout_automation.json.example"
    document = read_rollout_config._read_document(example)
    values = dict(read_rollout_config.validate_document(document))
    assert values["NODE_ID"] == "fn-01"
    assert values["PROXY_HTTPS_PORT"] == "8444"
    assert values["WORKER_SSH"] == "philo@192.168.0.80"
    assert values["WORKER_ARCHIVE"] == "$HOME/automation_system_v0101.zip"
    assert values["WORKER_EXPECTED_VERSION"] == "automation_system_v0101"
    assert values["WORKER_ARCHIVE_SHA256"] == (
        "c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467"
    )
    assert values["WORKER_MANIFEST_SHA256"] == (
        "699c29f9cf58b1bd6bd7407e879b3b5b3f41ad978343037a1dc33a75ec25026d"
    )
    assert values["WORKER_CONFIG_DRIFT_POLICY"] == "preserve"
    assert values["REUSE_COMPATIBLE_REVISIONS"] == "v0019-r5"

    unknown = root / "unknown-config.json"
    write_json(unknown, {"UNSUPPORTED": "value"})
    expect_value_error(
        partial_config_call(unknown), "unknown rollout configuration keys")

    duplicate = root / "duplicate-config.json"
    duplicate.write_text('{"NODE_ID":"fn-01","NODE_ID":"fn-02"}\n', encoding="utf-8")
    expect_value_error(partial_config_call(duplicate), "duplicate JSON key")

    invalid_port = root / "invalid-port.json"
    write_json(invalid_port, {"PROXY_HTTPS_PORT": 65536})
    expect_value_error(partial_config_call(invalid_port), "port range")

    boolean_port = root / "boolean-port.json"
    write_json(boolean_port, {"PROXY_HTTPS_PORT": True})
    expect_value_error(partial_config_call(boolean_port), "positive integer")

    invalid_manifest = root / "invalid-manifest.json"
    write_json(invalid_manifest, {"WORKER_MANIFEST_SHA256": "abc"})
    expect_value_error(partial_config_call(invalid_manifest), "64 lowercase")

    invalid_drift_policy = root / "invalid-drift-policy.json"
    write_json(invalid_drift_policy, {"WORKER_CONFIG_DRIFT_POLICY": "ignore"})
    expect_value_error(
        partial_config_call(invalid_drift_policy), "preserve or strict"
    )

    invalid_revisions = root / "invalid-revisions.json"
    write_json(invalid_revisions, {"REUSE_COMPATIBLE_REVISIONS": "v0017-r1,latest"})
    expect_value_error(
        partial_config_call(invalid_revisions), "invalid revision"
    )


def validate_config_path(path):
    return read_rollout_config.validate_document(
        read_rollout_config._read_document(path))


def partial_config_call(path):
    from functools import partial
    return partial(validate_config_path, path)


def test_askpass(root):
    credentials = root / "askpass.json"
    write_json(credentials, {"ssh": {"philo": {"password": "askpass-secret"}}})
    previous_file = os.environ.get("AUTOMATION_CREDENTIALS_FILE")
    previous_key = os.environ.get("AUTOMATION_SSH_CREDENTIAL_KEY")
    os.environ["AUTOMATION_CREDENTIALS_FILE"] = str(credentials)
    os.environ["AUTOMATION_SSH_CREDENTIAL_KEY"] = "philo"
    standard_output = io.StringIO()
    standard_error = io.StringIO()
    try:
        with redirect_stdout(standard_output), redirect_stderr(standard_error):
            result = ssh_askpass.main()
    finally:
        restore_environment("AUTOMATION_CREDENTIALS_FILE", previous_file)
        restore_environment("AUTOMATION_SSH_CREDENTIAL_KEY", previous_key)
    assert result == 0
    assert standard_output.getvalue() == "askpass-secret\n"
    assert "askpass-secret" not in standard_error.getvalue()


def restore_environment(name, previous):
    if previous is None:
        os.environ.pop(name, None)
    else:
        os.environ[name] = previous


def test_summary_writer(root):
    output = root / "logs" / "summary.json"
    report = {
        "run_id": "test-run",
        "status": "PASSED",
        "exit_code": 0,
        "stage": "complete",
        "release": {"version": "0.17.0", "revision": "v0017-r13"},
        "runtime": {"version": "0.17.0", "revision": "v0017-r1"},
        "deployment_mode": "reuse",
        "site_id": "site-main",
        "node_id": "fn-01",
        "worker_target": "philo@192.0.2.80",
        "log_directory": str(output.parent),
    }
    write_rollout_summary._write_atomic(output, report)
    loaded = json.loads(output.read_text(encoding="utf-8"))
    assert loaded == report
    assert "password" not in output.read_text(encoding="utf-8").lower()
    assert stat.S_IMODE(output.stat().st_mode) == 0o600


def restore_proof(owned_paths, mode):
    baseline_fingerprint = "c" * 64
    return {
        "authority_mode": "node_local",
        "restore_confirmed": True,
        "actuator_restored_before_config": True,
        "before_parallel_load": True,
        "mode": mode,
        "cas_retries": 0,
        "baseline": {
            "scope_revision": 0,
            "scope_fingerprint": baseline_fingerprint,
            "node_scope_revision": 0,
            "node_scope_fingerprint": baseline_fingerprint,
        },
        "scope_revision": 2,
        "scope_fingerprint": baseline_fingerprint,
        "owned_paths": list(owned_paths),
        "owned_values_restored": True,
        "worker_scope": {
            "node_id": "fn-01",
            "scope": "node:fn-01",
            "revision": 2,
            "fingerprint": baseline_fingerprint,
            "expected_fingerprint": baseline_fingerprint,
            "observed_global_revision": 0,
        },
    }


def passed_report(version):
    checks = {}
    for name in REQUIRED_CHECKS:
        checks[name] = {"status": "PASSED", "detail": {}}
    checks["AUTH"]["detail"] = {"mode": "password"}
    checks["EVT-004A"]["detail"] = {
        "retained_stream_command_filter_removed": True,
    }
    operation_id = "cce259be-d2e1-4db7-bbe6-f8c7a00d2840"
    target_fingerprint = "b" * 64
    checks["CFG-001"]["detail"] = {
        "from_revision": 0,
        "to_revision": 1,
    }
    checks["CFG-002"]["detail"] = {
        "node_id": "fn-01",
        "node_replica_revision": 1,
        "node_replica_fingerprint": target_fingerprint,
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
    checks["CFG-006"]["detail"] = restore_proof(
        owned_paths, "compensating_proposal")
    checks["CFG-RESTORE"]["detail"] = restore_proof(
        owned_paths, "persisted_restore_revalidation")
    checks["CFG-MATRIX"]["detail"] = {
        "commit_path": "client_wss_proxy_master_mqtt_worker",
        "commit_origin": 
            "canonical_client_proxy_master_relay_worker_local",
        "commit_identity": ["operation", operation_id],
        "file_proposal": False,
        "transport_expectation": "distributed.config.propose",
        "node_patch_capability": True,
        "node_local_proposal_proof": {
            "relay_message_id": "relay:" + operation_id,
            "authority_epoch": 0,
            "base_scope_revision": 0,
            "target_scope_revision": 1,
            "target_scope_fingerprint": target_fingerprint,
            "node_replica_revision": 1,
            "node_replica_fingerprint": target_fingerprint,
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
    checks["CROSS-WORKER"]["status"] = "SKIPPED"
    return {
        "version": version,
        "status": "PASSED",
        "profile": "core",
        "transport": "wss-primary",
        "run_id": "test-run",
        "worker_fn01": {"converged": True},
        "config_restore_confirmed": True,
        "actuator_restore_confirmed": True,
        "checks": checks,
    }


def test_e2e_verifier():
    version = "0.25.0-e2e.3"
    report = passed_report(version)
    summary = verify(report, version)
    assert summary["check_count"] == len(REQUIRED_CHECKS)
    assert summary["node_local_proposal_proof"]["operation_id"] \
        == "cce259be-d2e1-4db7-bbe6-f8c7a00d2840"

    bearer = passed_report(version)
    bearer["checks"]["AUTH"]["detail"] = {"mode": "bearer"}
    bearer["checks"]["SEC-004"]["status"] = "SKIPPED"
    verify(bearer, version)

    failed = passed_report(version)
    failed["checks"]["CFG-RESTORE"]["status"] = "FAILED"
    try:
        verify(failed, version)
    except AssertionError:
        pass
    else:
        raise AssertionError("failed mandatory E2E check was accepted")

    wrong_relay = passed_report(version)
    wrong_relay["checks"]["CFG-MATRIX"]["detail"][
        "node_local_proposal_proof"]["relay_message_id"] = "relay:foreign"
    try:
        verify(wrong_relay, version)
    except AssertionError:
        pass
    else:
        raise AssertionError("foreign node-local relay was accepted")

    skipped_revision = passed_report(version)
    proof = skipped_revision["checks"]["CFG-MATRIX"]["detail"][
        "node_local_proposal_proof"]
    proof["target_scope_revision"] = 2
    proof["node_replica_revision"] = 2
    try:
        verify(skipped_revision, version)
    except AssertionError:
        pass
    else:
        raise AssertionError("non-monotonic node-local revision was accepted")

def write_docker_stub(root):
    binary_dir = root / "bin"
    binary_dir.mkdir()
    stub = binary_dir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "printf '%s\\n' \"$@\" >\"$DOCKER_ARGS_FILE\"\n"
        "cat >\"$DOCKER_STDIN_FILE\"\n"
        "if [[ \"${STUB_DOCKER_MODE:-success}\" == conflict ]]; then\n"
        "  echo 'conflict: username already exists' >&2\n"
        "  exit 1\n"
        "fi\n"
        "echo created\n",
        encoding="utf-8",
    )
    stub.chmod(0o755)
    return binary_dir


def test_admin_bootstrap(root):
    binary_dir = write_docker_stub(root)
    args_file = root / "docker-args.txt"
    stdin_file = root / "docker-stdin.txt"
    environment = os.environ.copy()
    environment["PATH"] = str(binary_dir) + os.pathsep + environment["PATH"]
    environment["DOCKER_ARGS_FILE"] = str(args_file)
    environment["DOCKER_STDIN_FILE"] = str(stdin_file)
    environment["STUB_DOCKER_MODE"] = "success"
    secret = "admin-secret-not-in-arguments"
    result = run(
        [
            "bash", str(CREATE_ADMIN), "--username", "admin",
            "--password-stdin", "--allow-existing",
        ],
        env=environment,
        input=secret + "\n",
    )
    assert result.returncode == 0, result.stderr
    arguments = args_file.read_text(encoding="utf-8")
    assert "--password-file\n/dev/stdin\n" in arguments
    assert "--ensure-existing-password\n" in arguments
    assert secret not in arguments
    assert stdin_file.read_text(encoding="utf-8") == secret + "\n"
    assert secret not in result.stdout and secret not in result.stderr

    environment["STUB_DOCKER_MODE"] = "conflict"
    rejected = run(
        [
            "bash", str(CREATE_ADMIN), "--username", "admin",
            "--password-stdin", "--allow-existing",
        ],
        env=environment,
        input=secret + "\n",
    )
    assert rejected.returncode != 0
    assert "conflict: username already exists" in rejected.stderr
    assert secret not in rejected.stdout and secret not in rejected.stderr


def test_script_contract(root):
    for script in (SCRIPT, REMOTE, CREATE_ADMIN):
        result = run(["bash", "-n", str(script)])
        assert result.returncode == 0, result.stderr

    help_result = run(["bash", str(SCRIPT), "--help"])
    assert help_result.returncode == 0, help_result.stderr
    for expected in (
        "--reuse", "--fresh", "--upgrade", "--release", "--archive",
        "--list-releases", "--worker-archive", "recoverably install",
        "Previous Worker directories",
    ):
        assert expected in help_result.stdout

    remote_help = run(["bash", str(REMOTE), "--help"])
    assert remote_help.returncode == 0, remote_help.stderr
    assert "install-start" in remote_help.stdout
    assert "verify-active" in remote_help.stdout
    assert "stop-compatible" in remote_help.stdout

    text = SCRIPT.read_text(encoding="utf-8")
    assert "automation_system_v0101.zip" in text
    assert 'WORKER_PATH="${WORKER_PATH%/*}/$WORKER_EXPECTED_VERSION"' in text
    assert "scp " not in text
    assert "unzip " not in text
    assert "integration_test/CURRENT_E2E" in text
    assert "$HOME/.config/automation-control-plane/rollout_automation.json" in text
    assert "immutable release tree" in text
    assert "move rollout_automation.json" in text
    assert "tools/worker_remote_lifecycle.sh" in text
    assert "ControlMaster" in text
    assert "ControlPersist=yes" in text
    assert "ControlPersist=180" not in text
    assert "ensure_ssh_master" in text
    assert "-o BatchMode=yes" in text
    assert "-o NumberOfPasswordPrompts=0" in text
    assert (
        "if ! setsid -w env \\\n"
        "      DISPLAY=automation-rollout:0 \\\n"
        "      PYTHONDONTWRITEBYTECODE=1 \\\n"
        "      SSH_ASKPASS_REQUIRE=force \\\n"
        '      SSH_ASKPASS="$root_dir/tools/ssh_askpass.py" \\\n'
    ) in text
    assert "--password-file /dev/stdin" in CREATE_ADMIN.read_text(encoding="utf-8")
    assert '--result "$e2e_result" --version-file "$e2e_version_file"' in text
    assert 'e2e_result_dir="${E2E_RESULT_DIR:-$e2e_dir/results}"' in text
    assert 'e2e_result="$e2e_result_dir/e2e-${e2e_serial}-result.json"' in text
    assert 'if [[ -d "$e2e_result_dir" ]]; then' in text
    assert 'cp -a "$e2e_result_dir/." "$log_dir/e2e-results/"' in text
    assert 'cp -a "$e2e_dir/results/."' not in text
    remote_text = REMOTE.read_text(encoding="utf-8")
    assert "canonical v0101 manifest" in remote_text
    assert "expected_manifest_sha256" in remote_text
    assert "is_mutable_deployment_file" in remote_text
    assert "immutable package integrity failed" in remote_text
    assert "preserved Worker deployment configuration drift" in remote_text
    assert "verify|verify-active|stop-compatible|stop|restart|install-start|status|diagnose" in remote_text
    assert "tools/install_worker_release.py" in text
    assert "recoverably install Worker v0101 release" in text
    assert "REUSE_PRESERVE_WORKER" in text
    assert "verify active Worker identity without restart" in text
    assert "Active Worker continuity: PASS" in remote_text
    assert "Worker package baseline" in text
    assert "REUSE_COMPATIBLE_REVISIONS" in text
    assert "AUTONOMY CONTRACT" in text
    assert "numerically" in text and "newest revision" in text
    assert "bootstrap_discover_release" in text
    assert "bootstrap_install_release" in text
    assert "AUTOMATION_ROLLOUT_INTERNAL" in text
    assert (
        'PYTHONDONTWRITEBYTECODE=1 python3 \\\n'
        '    "$root_dir/tools/verify_worker_config_file_e2e.py"'
    ) in text
    assert (
        'export PYTHONPYCACHEPREFIX='
        '"/tmp/automation_system_pycache_$(id -u)"'
    ) in text
    assert "replica journal reset" in REMOTE.read_text(encoding="utf-8")
    assert "replica-backups" not in REMOTE.read_text(encoding="utf-8")
    assert "log-archive" not in REMOTE.read_text(encoding="utf-8")

    internal_environment = os.environ.copy()
    internal_environment["AUTOMATION_ROLLOUT_INTERNAL"] = "1"
    isolated_home = root / "isolated-rollout-home"
    isolated_home.mkdir()
    internal_environment["HOME"] = str(isolated_home)
    invalid = run(
        ["bash", str(SCRIPT), "unsupported-action"],
        env=internal_environment,
    )
    assert invalid.returncode == 64
    assert "Unknown action" in invalid.stderr

    unsupported_node = run(
        ["bash", str(SCRIPT), "run", "--node-id", "fn-02"],
        env=internal_environment,
    )
    assert unsupported_node.returncode == 65
    assert "fixed fn-01 certificate" in unsupported_node.stderr

    missing_config_value = run(
        ["bash", str(SCRIPT), "--config"],
        env=internal_environment,
    )
    assert missing_config_value.returncode == 64
    assert "requires a value" in missing_config_value.stderr


def main():
    temporary = Path(tempfile.mkdtemp(prefix="rollout-automation-test-"))
    try:
        test_credentials(temporary)
        test_rollout_configuration(temporary)
        test_askpass(temporary)
        test_summary_writer(temporary)
        test_e2e_verifier()
        test_admin_bootstrap(temporary)
        test_script_contract(temporary)
    finally:
        shutil.rmtree(temporary)
    print("PASS autonomous rollout, protected credentials, Worker reuse and complete E2E result contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

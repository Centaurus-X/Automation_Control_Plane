#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Static and fixture regression for authority-mode-aware Worker convergence."""

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile


FINGERPRINT_A = "a" * 64
FINGERPRINT_B = "b" * 64


def _require_fragments(source, fragments, contract_name):
    for fragment in fragments:
        if fragment not in source:
            raise AssertionError(
                "missing {} contract fragment: {}".format(
                    contract_name, fragment))


def _assert_source_contract(source):
    required = (
        "read_authority_mode",
        "node_local|master_global",
        "read_node_local_state",
        "master_control.worker_mirror_heads",
        "master_control.worker_mirror_snapshots",
        "mirror.source_node_id",
        "mirror.owner_node_id",
        "jsonb_typeof(snapshot.snapshot)='object'",
        "BLOCKED immutable_mirror_conflict",
        "read_master_global_state",
        "master_control.scope_heads",
        "LEFT JOIN master_control.authority_state authority",
        "THEN authority.authority_term ELSE 0 END::text",
        "WORKER_ADMISSION_PROGRESS_INTERVAL_S",
    )
    _require_fragments(source, required, "admission")
    node_local = source[
        source.index("read_node_local_state"):
        source.index("read_master_global_state")
    ]
    if "scope_heads" in node_local or "shard_rollouts" in node_local:
        raise AssertionError(
            "node_local convergence still depends on a Master-owned shard")
    obsolete = "n.authority_term::text || '|'"
    if obsolete in source:
        raise AssertionError(
            "node telemetry term is still used as the Registry lease oracle")


def _assert_diagnosis_contract(source):
    required = (
        "diagnose_node_local",
        "Autoritativer Worker-Mirror-Head",
        "master_control.worker_mirror_heads",
        "master_control.worker_mirror_snapshots",
        "distributed.mirror.request",
        "distributed.mirror.snapshot",
        "BLOCKER immutable_mirror_conflict",
        "niemals automatisch überschreiben",
        "diagnose_master_global",
        "BLOCKER stale_replica: Worker-Revision",
        "worker_runtime_replica_recovery",
    )
    _require_fragments(source, required, "diagnosis")
    for marker in ("Ã", "Â", "�"):
        if marker in source:
            raise AssertionError("diagnosis contains mojibake marker: " + marker)


def _write_fake_docker(directory):
    path = pathlib.Path(directory) / "docker"
    path.write_text(
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "printf '%s\\n' \"${ADMISSION_FIXTURE_ROW:?missing fixture row}\"\n",
        encoding="utf-8",
    )
    path.chmod(0o700)
    return path


def _run_fixture(context, row, authority_mode):
    environment = os.environ.copy()
    environment["PATH"] = context["bin_dir"] + os.pathsep + environment["PATH"]
    environment["ADMISSION_FIXTURE_ROW"] = row
    environment["AUTOMATION_SITE_ID"] = "site-main"
    environment["MASTER_CONFIG_AUTHORITY"] = authority_mode
    command = [str(context["script"]), "fn-01", "1"]
    if os.name == "nt":
        candidates = (
            os.environ.get("BASH_EXE"),
            shutil.which("bash"),
            r"C:\Program Files\Git\bin\bash.exe",
        )
        bash_path = next(
            (candidate for candidate in candidates
             if candidate and pathlib.Path(candidate).is_file()),
            None,
        )
        if not bash_path:
            raise AssertionError("bash is required for the shell fixture")
        command.insert(0, bash_path)
    return subprocess.run(
        command,
        cwd=str(context["root"]),
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
        timeout=5,
    )


def _node_local_row(**overrides):
    values = {
        "admission": "APPROVED",
        "desired": "ACTIVE",
        "observed": "ACTIVE",
        "heartbeats": "1",
        "term": "7",
        "node_scope": "node:fn-01",
        "node_revision": "4",
        "node_fingerprint": FINGERPRINT_A,
        "mirror_scope": "node:fn-01",
        "mirror_source": "fn-01",
        "mirror_owner": "fn-01",
        "authority_epoch": "3",
        "mirror_revision": "4",
        "execution_epoch": "0",
        "mirror_fingerprint": FINGERPRINT_A,
        "snapshots": "1",
        "fresh": "fresh",
    }
    values.update(overrides)
    order = (
        "admission", "desired", "observed", "heartbeats", "term",
        "node_scope", "node_revision", "node_fingerprint", "mirror_scope",
        "mirror_source", "mirror_owner", "authority_epoch",
        "mirror_revision", "execution_epoch", "mirror_fingerprint",
        "snapshots", "fresh",
    )
    return "|".join(str(values[key]) for key in order)


def _master_global_row(**overrides):
    values = {
        "admission": "APPROVED",
        "desired": "ACTIVE",
        "observed": "ACTIVE",
        "heartbeats": "1",
        "term": "7",
        "global_revision": "9",
        "node_revision": "4",
        "head_revision": "4",
        "node_fingerprint": FINGERPRINT_A,
        "head_fingerprint": FINGERPRINT_A,
        "fresh": "fresh",
    }
    values.update(overrides)
    order = (
        "admission", "desired", "observed", "heartbeats", "term",
        "global_revision", "node_revision", "head_revision",
        "node_fingerprint", "head_fingerprint", "fresh",
    )
    return "|".join(str(values[key]) for key in order)


def _test_node_local_exact_mirror(context):
    completed = _run_fixture(context, _node_local_row(), "node_local")
    if completed.returncode != 0:
        raise AssertionError("exact Worker Mirror was rejected: " + completed.stderr)
    if "authoritative Node snapshot is mirrored exactly" not in completed.stdout:
        raise AssertionError("Node-Local success did not reach the Mirror branch")


def _test_node_local_inactive_registry_lease(context):
    completed = _run_fixture(
        context, _node_local_row(term="0"), "node_local")
    if completed.returncode == 0:
        raise AssertionError("expired or absent Registry lease was accepted")
    if "registry-term" not in completed.stderr:
        raise AssertionError("Node-Local failure does not name the Registry term")


def _test_node_local_immutable_conflict(context):
    completed = _run_fixture(
        context,
        _node_local_row(mirror_fingerprint=FINGERPRINT_B),
        "node_local",
    )
    if completed.returncode != 2:
        raise AssertionError(
            "same-revision Mirror conflict was not blocked immediately: "
            + completed.stderr)
    if "never overwrite the Worker authority" not in completed.stderr:
        raise AssertionError("Mirror conflict lacks the Worker authority boundary")


def _test_master_global_exact_shard(context):
    completed = _run_fixture(
        context, _master_global_row(), "master_global")
    if completed.returncode != 0:
        raise AssertionError("exact Master shard was rejected: " + completed.stderr)
    if "Master-assigned Node shard converged" not in completed.stdout:
        raise AssertionError("Master-Global success did not reach the shard branch")


def _test_master_global_divergent_replica_fails_fast(context):
    completed = _run_fixture(
        context,
        _master_global_row(
            observed="DEGRADED",
            node_revision="5",
            head_revision="4",
            node_fingerprint=FINGERPRINT_B,
        ),
        "master_global",
    )
    if completed.returncode != 2:
        raise AssertionError(
            "newer divergent Worker replica was not blocked immediately: "
            + completed.stderr)
    if "clean Worker replica baseline" not in completed.stderr:
        raise AssertionError("stale replica diagnostic lacks its recovery boundary")


def main():
    root = pathlib.Path(__file__).resolve().parents[1]
    script = root / "test_worker_auto_admission.sh"
    source = script.read_text(encoding="utf-8")
    _assert_source_contract(source)
    _assert_diagnosis_contract(
        (root / "diagnose_worker_sync.sh").read_text(encoding="utf-8"))
    with tempfile.TemporaryDirectory(prefix="admission-static-") as temporary:
        _write_fake_docker(temporary)
        context = {"root": root, "script": script, "bin_dir": temporary}
        tests = (
            _test_node_local_exact_mirror,
            _test_node_local_inactive_registry_lease,
            _test_node_local_immutable_conflict,
            _test_master_global_exact_shard,
            _test_master_global_divergent_replica_fails_fast,
        )
        for test_function in tests:
            test_function(context)
    print("PASS authority-mode-aware Worker convergence (5 scenarios)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

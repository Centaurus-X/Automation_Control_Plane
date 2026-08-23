#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Regression tests for autonomous release discovery, install and delegation."""

import hashlib
import os
import shutil
import stat
import subprocess
import tempfile
import zipfile

from functools import partial
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LAUNCHER_SOURCE = ROOT / "rollout_automation.sh"
ACTIVE_E2E = (
    ROOT / "integration_test" / "CURRENT_E2E"
).read_text(encoding="utf-8").strip()
RUNTIME_COPY_BOUNDARIES = frozenset((
    Path("mqtt_broker/test-pki"),
    Path("mqtt_broker/secrets"),
    Path("mqtt_broker/runtime-config"),
    Path("mqtt_broker/runtime-pki-handoff"),
    Path("postgres_system/secrets"),
    Path("postgres_system/backup"),
    Path("postgres_system/runtime-tls-handoff"),
    Path("postgres_system/tls"),
    Path("postgres_system/test_ca_private"),
    Path("master_node/secrets"),
    Path("proxy_gateway/secrets"),
    Path("proxy_gateway/test-pki"),
    Path("integration_test/runtime"),
    Path("integration_test") / ACTIVE_E2E / "results",
    Path("integration_test") / ACTIVE_E2E / "runtime",
))
RUNTIME_COPY_SUFFIXES = (".pyc", ".pyo", ".log")


def sha256(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(partial(stream.read, 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def write_executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def write_manifest(root):
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    lines = [
        "{}  ./{}".format(sha256(path), path.relative_to(root).as_posix())
        for path in files
    ]
    (root / "SHA256SUMS").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n"
    )


def create_release_tree(parent, revision, version):
    directory_name = "automation_control_plane_release_" + revision.replace("-", "_")
    root = parent / directory_name
    (root / "tools").mkdir(parents=True)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "RELEASE_REVISION").write_text(revision + "\n", encoding="utf-8")
    (root / "rollout_automation.json.example").write_text(
        "{}\n", encoding="utf-8", newline="\n"
    )
    write_executable(
        root / "tools" / "verify_checksums.sh",
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "root=\"$(cd \"$(dirname \"${BASH_SOURCE[0]}\")/..\" && pwd -P)\"\n"
        "cd \"$root\"\n"
        "sha256sum --check --strict SHA256SUMS >/dev/null\n",
    )
    write_executable(
        root / "rollout_automation.sh",
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "test \"${AUTOMATION_ROLLOUT_INTERNAL:-0}\" = 1\n"
        "{\n"
        "  printf 'revision=%s\\n' \"$(<\"$(dirname \"${BASH_SOURCE[0]}\")/RELEASE_REVISION\")\"\n"
        "  printf 'installation=%s\\n' \"${AUTOMATION_ROLLOUT_INSTALL_ACTION:-missing}\"\n"
        "  printf 'source=%s\\n' \"${AUTOMATION_ROLLOUT_SELECTED_SOURCE:-missing}\"\n"
        "  printf 'script=%s\\n' \"$(readlink -f -- \"${BASH_SOURCE[0]}\")\"\n"
        "  for argument in \"$@\"; do printf 'arg=%s\\n' \"$argument\"; done\n"
        "} >\"${BOOTSTRAP_TEST_LOG:?}\"\n",
    )
    write_manifest(root)
    return root


def create_archive(tree, destination):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(tree.rglob("*")):
            relative = Path(tree.name) / path.relative_to(tree)
            if path.is_dir():
                info = zipfile.ZipInfo(relative.as_posix() + "/")
                info.create_system = 3
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
                archive.writestr(info, b"")
            else:
                info = zipfile.ZipInfo(relative.as_posix())
                info.create_system = 3
                info.external_attr = (
                    stat.S_IFREG | stat.S_IMODE(path.stat().st_mode)
                ) << 16
                archive.writestr(info, path.read_bytes())
    sidecar = Path(str(destination) + ".sha256")
    sidecar.write_text(
        "{}  {}\n".format(sha256(destination), destination.name),
        encoding="utf-8",
        newline="\n",
    )
    return destination


def run_launcher(launcher, environment, *arguments):
    return subprocess.run(
        ["sh", str(launcher), *arguments],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )


def assert_argument(log_text, value):
    assert "arg=" + value + "\n" in log_text, log_text


def runtime_source_copy_ignored(source_root, directory, names):
    relative_directory = Path(directory).relative_to(source_root)
    ignored = []
    for name in names:
        relative = relative_directory / name
        if (
            relative in RUNTIME_COPY_BOUNDARIES
            or name == "__pycache__"
            or name.endswith(RUNTIME_COPY_SUFFIXES)
        ):
            ignored.append(name)
    return ignored


def copy_release_source(source, destination):
    shutil.copytree(
        source,
        destination,
        ignore=partial(runtime_source_copy_ignored, source),
    )


def assert_runtime_handoff_manifest_boundary(temporary):
    live_source = temporary / "runtime-copy-source"
    copy_release_source(ROOT, live_source)
    runtime_config = live_source / "mqtt_broker" / "runtime-config"
    runtime_config.mkdir(mode=0o750)
    runtime_config_file = runtime_config / "mosquitto.conf"
    runtime_config_file.write_text(
        "runtime-only protected configuration\n",
        encoding="utf-8",
        newline="\n",
    )
    runtime_config_file.chmod(0)
    runtime_config.chmod(0)

    candidate = temporary / "runtime-handoff-manifest-boundary"
    try:
        copy_release_source(live_source, candidate)
    finally:
        runtime_config.chmod(0o750)
        runtime_config_file.chmod(0o600)
    assert not (candidate / "mqtt_broker" / "runtime-config").exists()

    handoff = candidate / "mqtt_broker" / "runtime-pki-handoff"
    handoff.mkdir(mode=0o700)
    (handoff / ".live_mqtt_pki_handoff").write_text(
        "verified runtime material\n", encoding="utf-8", newline="\n"
    )
    (handoff / "mqtt_ca.crt").write_text(
        "runtime-only certificate fixture\n", encoding="utf-8", newline="\n"
    )
    backup = candidate / "postgres_system" / "backup"
    backup.mkdir(mode=0o750)
    (backup / "automation_platform_test.dump").write_bytes(
        b"runtime-only PostgreSQL backup fixture\n"
    )
    (backup / "automation_platform_test.dump.sha256").write_text(
        "runtime-only digest fixture\n", encoding="utf-8", newline="\n"
    )
    tls_handoff = candidate / "postgres_system" / "runtime-tls-handoff"
    tls_handoff.mkdir(mode=0o700)
    (tls_handoff / ".live_postgres_tls_handoff").write_text(
        "verified runtime material\n", encoding="utf-8", newline="\n"
    )
    for name in (
        "ca.crt",
        "primary.crt",
        "primary.key",
        "standby.crt",
        "standby.key",
    ):
        (tls_handoff / name).write_text(
            "runtime-only TLS fixture\n", encoding="utf-8", newline="\n"
        )
    verifier = candidate / "tools" / "verify_checksums.sh"
    permitted = subprocess.run(
        ["bash", str(verifier)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    assert permitted.returncode == 0, permitted.stdout

    (candidate / "UNEXPECTED_RUNTIME_FILE").write_text(
        "must remain fail-closed\n", encoding="utf-8", newline="\n"
    )
    rejected = subprocess.run(
        ["bash", str(verifier)],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=60,
        check=False,
    )
    assert rejected.returncode != 0, rejected.stdout


def main():
    temporary = Path(tempfile.mkdtemp(prefix="rollout-bootstrap-r13-"))
    try:
        assert_runtime_handoff_manifest_boundary(temporary)
        home = temporary / "home"
        search = home / "releases"
        source_trees = temporary / "source-trees"
        install_root = home / "installed"
        home.mkdir()
        search.mkdir()
        source_trees.mkdir()
        install_root.mkdir(mode=0o700)

        credentials = home / "credentials.json"
        credentials.write_text(
            '{"philo":"worker-secret","admin":"administrator-secret"}\n',
            encoding="utf-8",
            newline="\n",
        )
        credentials.chmod(0o600)

        launcher = home / "rollout_automation.sh"
        shutil.copy2(LAUNCHER_SOURCE, launcher)
        launcher.chmod(0o700)

        r1 = create_release_tree(source_trees, "v0017-r1", "0.17.0")
        r2 = create_release_tree(source_trees, "v0017-r2", "0.17.0")
        r3 = create_release_tree(source_trees, "v0017-r3", "0.17.0")
        r4 = create_release_tree(source_trees, "v0017-r4", "0.17.0")
        r5 = create_release_tree(source_trees, "v0017-r5", "0.17.0")
        r6 = create_release_tree(source_trees, "v0017-r6", "0.17.0")
        r13 = create_release_tree(source_trees, "v0017-r13", "0.17.0")
        v18 = create_release_tree(source_trees, "v0018-r3", "0.18.0")

        # Extracted old releases coexist exactly as required by the rollout contract.
        shutil.copytree(r1, search / r1.name)
        shutil.copytree(r2, search / r2.name)
        create_archive(r3, search / "automation_control_plane_release_v0017_r3.zip")
        create_archive(r4, search / "automation_control_plane_release_v0017_r4.zip")
        create_archive(r5, search / "automation_control_plane_release_v0017_r5.zip")
        create_archive(r6, search / "automation_control_plane_release_v0017_r6.zip")
        create_archive(r13, search / "automation_control_plane_release_v0017_r13.zip")
        create_archive(v18, search / "automation_control_plane_release_v0018_r3.zip")

        log = home / "delegation.log"
        environment = os.environ.copy()
        environment.update({
            "HOME": str(home),
            "BOOTSTRAP_TEST_LOG": str(log),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        })

        latest = run_launcher(
            launcher, environment,
            "--search-dir", str(search),
            "--install-root", str(install_root),
        )
        assert latest.returncode == 0, latest.stdout
        delegated = log.read_text(encoding="utf-8")
        assert "revision=v0018-r3\n" in delegated, delegated
        assert "installation=installed\n" in delegated, delegated
        assert_argument(delegated, "--fresh")
        assert_argument(
            delegated,
            str(home / ".config" / "automation-control-plane" /
                "rollout_automation.json"),
        )
        installed_v18 = install_root / "automation_control_plane_release_v0018_r3"
        assert installed_v18.is_dir()
        assert not any(install_root.glob(".automation-release-displaced.*"))
        assert not any(install_root.glob(".automation-release-stage.*"))
        assert stat.S_IMODE((home / ".config" / "automation-control-plane" /
            "credentials.json").stat().st_mode) == 0o600

        target_mtime = installed_v18.stat().st_mtime_ns
        repeated = run_launcher(
            launcher, environment,
            "--search-dir", str(search),
            "--install-root", str(install_root),
        )
        assert repeated.returncode == 0, repeated.stdout
        delegated = log.read_text(encoding="utf-8")
        assert "revision=v0018-r3\n" in delegated
        assert "installation=reused\n" in delegated
        assert_argument(delegated, "--reuse")
        assert installed_v18.stat().st_mtime_ns == target_mtime

        collision_parent = temporary / "collision-source"
        collision_parent.mkdir()
        colliding_v18 = create_release_tree(collision_parent, "v0018-r3", "0.18.0")
        (colliding_v18 / "COLLISION.txt").write_text(
            "different content under the same immutable revision\n",
            encoding="utf-8",
            newline="\n",
        )
        write_manifest(colliding_v18)
        collision_archive = create_archive(
            colliding_v18,
            home / "automation_control_plane_release_v0018_r3.zip",
        )
        collision = run_launcher(
            launcher, environment,
            "--archive", str(collision_archive),
            "--install-root", str(install_root),
            "--install-only",
        )
        assert collision.returncode != 0, collision.stdout
        assert "immutable release revision collision: v0018-r3" in collision.stdout
        assert installed_v18.stat().st_mtime_ns == target_mtime

        selected_version = run_launcher(
            launcher, environment,
            "--search-dir", str(search),
            "--install-root", str(install_root),
            "--release", "0.17.0",
        )
        assert selected_version.returncode == 0, selected_version.stdout
        delegated = log.read_text(encoding="utf-8")
        assert "revision=v0017-r13\n" in delegated, delegated
        assert_argument(delegated, "--fresh")

        pinned = run_launcher(
            launcher, environment,
            "--search-dir", str(search),
            "--install-root", str(install_root),
            "--release", "v0017-r1",
            "--reuse",
        )
        assert pinned.returncode == 0, pinned.stdout
        delegated = log.read_text(encoding="utf-8")
        assert "revision=v0017-r1\n" in delegated, delegated
        assert_argument(delegated, "--reuse")
        assert "arg=--fresh\n" not in delegated

        listed = run_launcher(
            launcher, environment,
            "--search-dir", str(search),
            "--list-releases",
        )
        assert listed.returncode == 0, listed.stdout
        positions = [
            listed.stdout.index("v0018-r3  "),
            listed.stdout.index("v0017-r13  "),
            listed.stdout.index("v0017-r6  "),
            listed.stdout.index("v0017-r5  "),
            listed.stdout.index("v0017-r4  "),
            listed.stdout.index("v0017-r3  "),
            listed.stdout.index("v0017-r2  "),
            listed.stdout.index("v0017-r1  "),
        ]
        assert positions == sorted(positions), listed.stdout

        install_only_log_mtime = log.stat().st_mtime_ns
        install_only = run_launcher(
            launcher, environment,
            "--search-dir", str(search),
            "--install-root", str(install_root),
            "--release", "v0017-r2",
            "--install-only",
        )
        assert install_only.returncode == 0, install_only.stdout
        assert "PASS: release v0017-r2 is installed" in install_only.stdout
        assert log.stat().st_mtime_ns == install_only_log_mtime

        v19 = create_release_tree(source_trees, "v0019-r5", "0.19.0")
        bad_sidecar = search / "automation_control_plane_release_v0019_r5.zip"
        create_archive(v19, bad_sidecar)
        Path(str(bad_sidecar) + ".sha256").write_text(
            "{}  {}\n".format("0" * 64, bad_sidecar.name), encoding="utf-8"
        )
        rejected_hash = run_launcher(
            launcher, environment,
            "--archive", str(bad_sidecar),
            "--install-root", str(install_root),
            "--install-only",
        )
        assert rejected_hash.returncode != 0, rejected_hash.stdout
        assert "SHA-256 mismatch" in rejected_hash.stdout, rejected_hash.stdout

        malicious = search / "automation_control_plane_release_v0025_r1.zip"
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("../escaped", b"unsafe")
        Path(str(malicious) + ".sha256").write_text(
            "{}  {}\n".format(sha256(malicious), malicious.name), encoding="utf-8"
        )
        rejected_path = run_launcher(
            launcher, environment,
            "--archive", str(malicious),
            "--install-root", str(install_root),
            "--install-only",
        )
        assert rejected_path.returncode != 0, rejected_path.stdout
        assert "unsafe ZIP member" in rejected_path.stdout, rejected_path.stdout
        assert not (temporary / "escaped").exists()

        print(
            "PASS rollout bootstrap r13: sh re-exec, numeric-UID ownership, "
            "metadata-ordered latest selection, "
            "future-release precedence, explicit pinning, idempotent installation, "
            "same-revision content collision rejection, "
            "runtime PKI/TLS handoffs and PostgreSQL backup manifest boundaries, "
            "external configuration/credentials, "
            "checksum and ZIP path fencing"
        )
        return 0
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

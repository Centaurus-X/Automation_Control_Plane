#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Executable regression tests for the package-preserving Worker lifecycle."""

import hashlib
import os
import shutil
import signal
import subprocess
import tempfile
import time

from functools import partial
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "tools" / "worker_remote_lifecycle.sh"
EXPECTED_VERSION = "automation_system_v0101"


def write_executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(partial(stream.read, 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def write_manifest(root):
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    lines = [
        "{}  ./{}".format(sha256(path), path.relative_to(root).as_posix())
        for path in files
    ]
    manifest = root / "SHA256SUMS"
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    return sha256(manifest)


def stop_pid(pid):
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    for _ in range(100):
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.02)


def process_alive(pid):
    try:
        os.kill(pid, 0)
        state = Path("/proc/{}/stat".format(pid)).read_text(
            encoding="utf-8"
        ).split()[2]
    except (ProcessLookupError, FileNotFoundError, PermissionError, IndexError):
        return False
    return state != "Z"


def prepare_worker(home):
    worker = (
        home / "python-skripte_22" / "__NEW_FILES__" /
        "automation_system_v0101"
    )
    (worker / "src").mkdir(parents=True)
    (worker / "_runtime").mkdir()
    (worker / "_config" / "certs").mkdir(parents=True)
    (worker / "_config" / "application").mkdir(parents=True)
    (worker / "_config" / "config_data").mkdir(parents=True)
    (worker / "VERSION").write_text(
        EXPECTED_VERSION + "\n", encoding="utf-8", newline="\n"
    )
    (worker / "src" / "__init__.py").write_text("", encoding="utf-8")
    source = worker / "src" / "sync_xserver_main.py"
    source.write_text(
        "import signal\n"
        "import time\n"
        "running = True\n"
        "def stop(signum, frame):\n"
        "    global running\n"
        "    running = False\n"
        "signal.signal(signal.SIGTERM, stop)\n"
        "while running:\n"
        "    time.sleep(0.05)\n",
        encoding="utf-8",
        newline="\n",
    )
    app_config = worker / "_config" / "application" / "app_config.yaml"
    app_config.write_text(
        "active_profile: worker_simulation\n",
        encoding="utf-8",
        newline="\n",
    )
    config_data = worker / "_config" / "config_data" / "node.json"
    config_data.write_text("{}\n", encoding="utf-8", newline="\n")
    (worker / "_config" / "certs" / "README.md").write_text(
        "Runtime certificates are injected.\n",
        encoding="utf-8",
        newline="\n",
    )
    write_executable(
        worker / "run_worker.sh",
        "#!/usr/bin/env bash\n"
        "set -Eeuo pipefail\n"
        "if [[ \"${1:-}\" == --preflight ]]; then\n"
        "  echo '{\"ok\":true,\"profile\":\"mock\"}'\n"
        "  exit 0\n"
        "fi\n"
        "exec python3 -m src.sync_xserver_main\n",
    )
    write_executable(
        worker / "install_mqtt_identity.sh",
        """#!/usr/bin/env bash
set -Eeuo pipefail
source_dir=
site_id=
node_id=
while (($#)); do
  case "$1" in
    --source-dir) source_dir=$2; shift 2 ;;
    --site-id) site_id=$2; shift 2 ;;
    --node-id) node_id=$2; shift 2 ;;
    *) exit 64 ;;
  esac
done
install -d -m 0700 _config/certs
install -m 0644 "$source_dir/ca.crt" _config/certs/mqtt_ca.crt
install -m 0644 "$source_dir/mqtt.crt" "_config/certs/worker_${node_id}.crt"
install -m 0600 "$source_dir/mqtt.key" "_config/certs/worker_${node_id}.key"
echo "Worker MQTT identity installed: urn:automation:${site_id}:worker:${node_id}"
""",
    )
    manifest_sha256 = write_manifest(worker)
    return worker, source, app_config, config_data, manifest_sha256


def prepare_fake_openssl(base):
    binary_dir = base / "bin"
    binary_dir.mkdir()
    write_executable(
        binary_dir / "openssl",
        """#!/usr/bin/env bash
set -Eeuo pipefail
case "${1:-}" in
  verify)
    echo "${*: -1}: OK"
    ;;
  x509)
    if [[ " $* " == *" -ext subjectAltName "* ]]; then
      echo 'X509v3 Subject Alternative Name:'
      echo '    URI:urn:automation:site-main:worker:fn-01'
    elif [[ " $* " == *" -pubkey "* ]]; then
      echo 'MOCK-PUBLIC-KEY'
    else
      echo 'subject=CN=mock'
      echo 'issuer=CN=mock-ca'
      echo 'notBefore=Jul 19 00:00:00 2026 GMT'
      echo 'notAfter=Jul 19 00:00:00 2028 GMT'
      echo 'sha256 Fingerprint=00:11'
    fi
    ;;
  pkey)
    if [[ " $* " == *" -pubout "* ]]; then
      echo 'MOCK-PUBLIC-KEY'
    fi
    ;;
  sha256)
    cat >/dev/null
    echo 'SHA2-256(stdin)= mock-public-key-digest'
    ;;
  *)
    echo "unexpected openssl command: $*" >&2
    exit 2
    ;;
esac
""",
    )
    return binary_dir


def run_lifecycle(environment, manifest_sha256, action, reset="no", policy="preserve"):
    return subprocess.run(
        [
            "bash", str(SCRIPT), action,
            "python-skripte_22/__NEW_FILES__/automation_system_v0101",
            "site-main", "fn-01", "worker_simulation",
            ".cache/automation-control-plane/pki/site-main/fn-01",
            ".local/state/automation-worker/site-main/fn-01/worker.log",
            reset, EXPECTED_VERSION, manifest_sha256, policy,
        ],
        env=environment,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=90,
        check=False,
    )


def main():
    temporary = Path(tempfile.mkdtemp(prefix="worker-lifecycle-r3-"))
    pids = []
    log_handle = None
    try:
        home = temporary / "home"
        home.mkdir()
        worker, source, app_config, config_data, manifest_sha256 = prepare_worker(home)
        original_source = source.read_text(encoding="utf-8")
        binary_dir = prepare_fake_openssl(temporary)
        real_readlink = shutil.which("readlink")
        if not real_readlink:
            raise AssertionError("readlink is required")
        readlink_script = """#!/usr/bin/env bash
set -Eeuo pipefail
if [[ "${1:-}" == -f && "${2:-}" =~ ^/proc/([1-9][0-9]*)/cwd$ ]]; then
  candidate_pid="${BASH_REMATCH[1]}"
  expected_pid=""
  if [[ -r "${MOCK_WORKER_PID_FILE:?}" ]]; then
    expected_pid="$(<"$MOCK_WORKER_PID_FILE")"
  fi
  if [[ "$candidate_pid" == "$expected_pid" ]]; then
    printf '%s\n' "${MOCK_WORKER_DIR:?}"
    exit 0
  fi
fi
exec __REAL_READLINK__ "$@"
""".replace("__REAL_READLINK__", real_readlink)
        write_executable(binary_dir / "readlink", readlink_script)
        state_dir = (
            home / ".local" / "state" / "automation-worker" /
            "site-main" / "fn-01"
        )
        environment = os.environ.copy()
        environment.update({
            "HOME": str(home),
            "PATH": str(binary_dir) + os.pathsep + environment["PATH"],
            "MOCK_WORKER_DIR": str(worker),
            "MOCK_WORKER_PID_FILE": str(state_dir / "worker.pid"),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
        })

        app_config.write_text(
            "active_profile: worker_simulation\ncommissioned: true\n",
            encoding="utf-8",
            newline="\n",
        )
        untracked_config = config_data.parent / "commissioned-extra.json"
        untracked_config.write_text(
            "{\"commissioned\": true}\n",
            encoding="utf-8",
            newline="\n",
        )
        preserved = run_lifecycle(
            environment, manifest_sha256, "verify", policy="preserve"
        )
        assert preserved.returncode == 0, preserved.stdout
        assert "immutable package: PASS" in preserved.stdout
        assert "configuration: PRESERVED" in preserved.stdout
        assert "preserved untracked Worker deployment configuration" in preserved.stdout

        strict = run_lifecycle(
            environment, manifest_sha256, "verify", policy="strict"
        )
        assert strict.returncode == 65, strict.stdout
        assert "strict package baseline" in strict.stdout

        source.write_text(
            original_source + "# immutable tamper\n",
            encoding="utf-8",
            newline="\n",
        )
        tampered = run_lifecycle(
            environment, manifest_sha256, "verify", policy="preserve"
        )
        assert tampered.returncode == 65, tampered.stdout
        assert "./src/sync_xserver_main.py: FAILED" in tampered.stdout
        source.write_text(original_source, encoding="utf-8", newline="\n")

        unsafe_config = config_data.parent / "unexpected.py"
        unsafe_config.write_text("raise RuntimeError('unsafe')\n", encoding="utf-8")
        unsafe = run_lifecycle(
            environment, manifest_sha256, "verify", policy="preserve"
        )
        assert unsafe.returncode == 65, unsafe.stdout
        assert "unsafe untracked files" in unsafe.stdout
        assert "./_config/config_data/unexpected.py" in unsafe.stdout
        unsafe_config.unlink()

        symlink_path = config_data.parent / "linked.json"
        symlink_path.symlink_to(config_data.name)
        linked = run_lifecycle(
            environment, manifest_sha256, "verify", policy="preserve"
        )
        assert linked.returncode == 65, linked.stdout
        assert "symbolic links are forbidden" in linked.stdout
        symlink_path.unlink()

        project_cache = worker / "src" / "__pycache__"
        project_cache.mkdir()
        (project_cache / "stale.pyc").write_bytes(b"bytecode\n")
        cached = run_lifecycle(
            environment, manifest_sha256, "verify", policy="preserve"
        )
        assert cached.returncode == 65, cached.stdout
        assert "Python bytecode cache exists inside the Worker project" in cached.stdout
        shutil.rmtree(project_cache)

        wrong_anchor = run_lifecycle(
            environment, "0" * 64, "verify", policy="preserve"
        )
        assert wrong_anchor.returncode == 65, wrong_anchor.stdout
        assert "not the canonical" in wrong_anchor.stdout

        initial_log = temporary / "initial.log"
        log_handle = initial_log.open("w", encoding="utf-8")
        initial = subprocess.Popen(
            [str(worker / "run_worker.sh")],
            cwd=worker,
            env=environment,
            stdout=log_handle,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        pids.append(initial.pid)
        time.sleep(0.3)
        if initial.poll() is not None:
            raise AssertionError("initial mock Worker did not start")

        state_dir.mkdir(parents=True, exist_ok=True)
        (state_dir / "worker.pid").write_text(
            str(initial.pid) + "\n", encoding="utf-8", newline="\n"
        )
        journal = worker / "_runtime" / "distributed_operations_fn-01.sqlite3"
        wal = Path(str(journal) + "-wal")
        shm = Path(str(journal) + "-shm")
        journal.write_text("journal\n", encoding="utf-8")
        wal.write_text("wal\n", encoding="utf-8")
        shm.write_text("shm\n", encoding="utf-8")

        stage = (
            home / ".cache" / "automation-control-plane" / "pki" /
            "site-main" / "fn-01"
        )
        stage.mkdir(parents=True)
        (stage / "ca.crt").write_text("MOCK-CA\n", encoding="utf-8")
        (stage / "mqtt.crt").write_text("MOCK-CERT\n", encoding="utf-8")
        (stage / "mqtt.key").write_text("MOCK-KEY\n", encoding="utf-8")

        installed = run_lifecycle(
            environment, manifest_sha256, "install-start", "yes", "preserve"
        )
        assert installed.returncode == 0, installed.stdout
        assert "configuration: PRESERVED" in installed.stdout
        assert "Worker MQTT identity installation: PASS" in installed.stdout
        assert "Worker project pycache policy: PASS" in installed.stdout
        initial.wait(timeout=5)
        if process_alive(initial.pid):
            raise AssertionError("initial Worker remained alive")
        if stage.exists():
            raise AssertionError("PKI stage was not removed")
        assert "Worker replica journal reset: removed 3 local file(s)." in installed.stdout
        for path in (journal, wal, shm):
            if path.exists():
                raise AssertionError("replica file was not reset: " + str(path))
        if (state_dir / "replica-backups").exists():
            raise AssertionError("backup-free lifecycle created replica-backups")

        cert_dir = worker / "_config" / "certs"
        assert (cert_dir / "mqtt_ca.crt").read_text(
            encoding="utf-8"
        ) == "MOCK-CA\n"
        assert (cert_dir / "worker_fn-01.crt").read_text(
            encoding="utf-8"
        ) == "MOCK-CERT\n"
        assert (cert_dir / "worker_fn-01.key").read_text(
            encoding="utf-8"
        ) == "MOCK-KEY\n"

        new_pid = int((state_dir / "worker.pid").read_text(
            encoding="utf-8"
        ).strip())
        pids.append(new_pid)
        assert new_pid != initial.pid and process_alive(new_pid)

        active = run_lifecycle(
            environment, manifest_sha256, "verify-active", policy="preserve"
        )
        assert active.returncode == 0, active.stdout
        assert "Installed Worker MQTT identity: PASS" in active.stdout
        assert "Worker project pycache policy: PASS" in active.stdout
        assert "Active Worker continuity: PASS; PID={}".format(new_pid) in active.stdout
        assert int((state_dir / "worker.pid").read_text(
            encoding="utf-8"
        ).strip()) == new_pid
        assert process_alive(new_pid)

        stopped = run_lifecycle(
            environment, manifest_sha256, "stop-compatible", policy="preserve"
        )
        assert stopped.returncode == 0, stopped.stdout
        assert "Compatible Worker processes stopped cleanly" in stopped.stdout

        inactive = run_lifecycle(
            environment, manifest_sha256, "verify-active", policy="preserve"
        )
        assert inactive.returncode == 70, inactive.stdout
        assert "requires exactly one active process; found 0" in inactive.stdout

        print(
            "PASS Worker lifecycle r3: canonical manifest anchor, mutable config "
            "preservation, untracked-config and symlink fencing, exact immutable "
            "drift diagnosis, PKI install, backup-free replica reset and "
            "controlled restart, project-pycache fencing and restart-free "
            "active-Worker reuse"
        )
        return 0
    finally:
        if log_handle is not None:
            log_handle.close()
        for pid in pids:
            stop_pid(pid)
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

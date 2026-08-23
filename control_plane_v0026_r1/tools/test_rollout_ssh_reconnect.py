#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Mocked regression test for SSH ControlMaster loss and reconstruction."""

import json
import os
import shutil
import subprocess
import tempfile

from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
SOURCE_SCRIPT = ROOT / "rollout_automation.sh"
SOURCE_TOOLS = ROOT / "tools"


def write_executable(path, content):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8", newline="\n")
    path.chmod(0o755)


def prepare_release(base):
    release = base / "automation_control_plane_release_v0017"
    (release / "tools").mkdir(parents=True)
    (release / "integration_test" / "e2e_v0017").mkdir(parents=True)
    (release / "master_node").mkdir()
    (release / "proxy_gateway").mkdir()
    shutil.copy2(SOURCE_SCRIPT, release / "rollout_automation.sh")
    for name in (
        "credential_reader.py",
        "read_rollout_config.py",
        "ssh_askpass.py",
        "worker_remote_lifecycle.sh",
        "write_rollout_summary.py",
    ):
        shutil.copy2(SOURCE_TOOLS / name, release / "tools" / name)
    (release / "VERSION").write_text("0.17.0\n", encoding="utf-8")
    (release / "RELEASE_REVISION").write_text("v0017-r13\n", encoding="utf-8")
    (release / "integration_test" / "CURRENT_E2E").write_text(
        "e2e_v0017\n", encoding="utf-8"
    )
    (release / "integration_test" / "e2e_v0017" / "VERSION").write_text(
        "0.17.0-e2e.1\n", encoding="utf-8"
    )
    write_executable(
        release / "tools" / "verify_checksums.sh",
        "#!/usr/bin/env bash\nset -Eeuo pipefail\necho 'PASS mock checksums'\n",
    )
    write_executable(
        release / "rollout.sh",
        "#!/usr/bin/env bash\nset -Eeuo pipefail\necho \"PASS mock rollout $*\"\n",
    )
    write_executable(
        release / "master_node" / "test_runtime_state.sh",
        "#!/usr/bin/env bash\nset -Eeuo pipefail\necho 'PASS mock runtime'\n",
    )
    write_executable(
        release / "diagnose_worker_sync.sh",
        "#!/usr/bin/env bash\nset -Eeuo pipefail\necho 'PASS mock sync diagnosis'\n",
    )
    write_executable(
        release / "logs_all.sh",
        "#!/usr/bin/env bash\nset -Eeuo pipefail\necho 'PASS mock service logs'\n",
    )
    return release


def prepare_ssh_stub(binary_dir):
    write_executable(
        binary_dir / "ssh",
        r'''#!/usr/bin/env bash
set -Eeuo pipefail
socket=""
operation=""
master=0
args=("$@")
index=0
while ((index < ${#args[@]})); do
  case "${args[$index]}" in
    -S)
      socket="${args[$((index + 1))]}"
      index=$((index + 2))
      ;;
    -O)
      operation="${args[$((index + 1))]}"
      index=$((index + 2))
      ;;
    -M)
      master=1
      index=$((index + 1))
      ;;
    -o|-i|-F|-p|-l|-J)
      index=$((index + 2))
      ;;
    -N|-f)
      index=$((index + 1))
      ;;
    -*)
      index=$((index + 1))
      ;;
    *)
      target="${args[$index]}"
      index=$((index + 1))
      break
      ;;
  esac
done

if [[ -n "$operation" ]]; then
  case "$operation" in
    check)
      [[ -S "$socket" && -s "$socket.pid" ]]
      pid="$(<"$socket.pid")"
      kill -0 "$pid" 2>/dev/null
      ;;
    exit)
      if [[ -s "$socket.pid" ]]; then
        pid="$(<"$socket.pid")"
        kill "$pid" 2>/dev/null || true
      fi
      rm -f -- "$socket" "$socket.pid"
      ;;
    *) exit 2 ;;
  esac
  exit 0
fi

if ((master == 1)); then
  test -n "$socket"
  rm -f -- "$socket" "$socket.pid"
  nohup python3 - "$socket" >/dev/null 2>&1 <<'PY_SOCKET' &
import os
import socket
import sys
import time
path = sys.argv[1]
try:
    os.unlink(path)
except FileNotFoundError:
    pass
server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
server.bind(path)
server.listen(1)
while True:
    time.sleep(1)
PY_SOCKET
  pid=$!
  printf '%s\n' "$pid" >"$socket.pid"
  for _ in $(seq 1 100); do
    [[ -S "$socket" ]] && break
    sleep 0.01
  done
  [[ -S "$socket" ]]
  printf '%s\n' "$socket" >"${MOCK_SOCKET_RECORD:?}"
  count=0
  [[ -s "${MOCK_MASTER_COUNT:?}" ]] && count="$(<"$MOCK_MASTER_COUNT")"
  printf '%s\n' "$((count + 1))" >"$MOCK_MASTER_COUNT"
  exit 0
fi

cat >/dev/null || true
echo 'Worker status: STOPPED'
echo 'Replica journal: clean or not yet created'
echo 'Worker certificate: missing'
''',
    )


def prepare_docker_stub(binary_dir):
    write_executable(
        binary_dir / "docker",
        r'''#!/usr/bin/env bash
set -Eeuo pipefail
expire_master_once() {
  [[ -e "${MOCK_EXPIRED_MARKER:?}" ]] && return 0
  [[ -s "${MOCK_SOCKET_RECORD:?}" ]] || return 0
  socket="$(<"$MOCK_SOCKET_RECORD")"
  if [[ -s "$socket.pid" ]]; then
    pid="$(<"$socket.pid")"
    kill "$pid" 2>/dev/null || true
  fi
  rm -f -- "$socket" "$socket.pid"
  : >"$MOCK_EXPIRED_MARKER"
}
case "${1:-}" in
  info)
    exit 0
    ;;
  ps)
    exit 0
    ;;
  compose)
    case "${2:-}" in
      version) exit 0 ;;
      ps)
        service="${@: -1}"
        printf 'container-%s\n' "$service"
        exit 0
        ;;
    esac
    ;;
  inspect)
    format="${3:-}"
    expire_master_once
    if [[ "$format" == *'image.version'* ]]; then
      echo '0.17.0'
    elif [[ "$format" == *'image.revision'* ]]; then
      echo 'v0017-r1'
    else
      echo 'mock'
    fi
    exit 0
    ;;
esac
echo "unexpected docker invocation: $*" >&2
exit 2
''',
    )


def main():
    temporary = Path(tempfile.mkdtemp(prefix="rollout-ssh-reconnect-"))
    try:
        release = prepare_release(temporary)
        binary_dir = temporary / "bin"
        binary_dir.mkdir()
        prepare_ssh_stub(binary_dir)
        prepare_docker_stub(binary_dir)
        home = temporary / "home"
        home.mkdir()
        config = home / "rollout.json"
        config.write_text(
            json.dumps({
                "WORKER_SSH": "philo@192.0.2.80",
                "WORKER_EXPECTED_VERSION": "automation_system_v0082",
                "WORKER_MANIFEST_SHA256": (
                    "73b67bc917c659e09667781149e2c4f3dbfc2660e9184be378ec127c69e9d2e1"
                ),
                "WORKER_CONFIG_DRIFT_POLICY": "preserve",
                "REUSE_COMPATIBLE_REVISIONS":
                    "v0017-r1,v0017-r2,v0017-r3,v0017-r4,v0017-r5,v0017-r6,v0017-r7,v0017-r8,v0017-r9,v0017-r10,v0017-r11,v0017-r12,v0017-r13",
                "LOG_ROOT": str(home / "logs"),
            }) + "\n",
            encoding="utf-8",
        )
        config.chmod(0o600)
        socket_record = temporary / "socket-record"
        master_count = temporary / "master-count"
        expired_marker = temporary / "expired"
        environment = os.environ.copy()
        environment.update({
            "HOME": str(home),
            "PATH": str(binary_dir) + os.pathsep + environment["PATH"],
            "MOCK_SOCKET_RECORD": str(socket_record),
            "MOCK_MASTER_COUNT": str(master_count),
            "MOCK_EXPIRED_MARKER": str(expired_marker),
            "PYTHONDONTWRITEBYTECODE": "1",
            "PYTHONNOUSERSITE": "1",
            "AUTOMATION_ROLLOUT_INTERNAL": "1",
        })
        result = subprocess.run(
            [
                "bash", str(release / "rollout_automation.sh"),
                "status", "--reuse", "--config", str(config),
            ],
            cwd=release,
            env=environment,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=60,
            check=False,
        )
        assert result.returncode == 0, result.stdout
        assert "ControlMaster was lost; rebuilding it non-interactively" in result.stdout
        assert "Compatible runtime reuse" in result.stdout
        assert master_count.read_text(encoding="utf-8").strip() == "2"
        assert "password:" not in result.stdout.lower()
        summaries = list((home / "logs").rglob("summary.json"))
        assert len(summaries) == 1, summaries
        summary = json.loads(summaries[0].read_text(encoding="utf-8"))
        assert summary["status"] == "PASSED", summary
        assert summary["release"]["revision"] == "v0017-r13", summary
        assert summary["runtime"]["revision"] == "v0017-r1", summary
        print(
            "PASS rollout SSH r3: indefinite ControlMaster, loss detection, "
            "non-interactive reconstruction and compatible r1 runtime reuse"
        )
        return 0
    finally:
        shutil.rmtree(temporary, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())

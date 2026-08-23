#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"

expect_status() {
  local expected="$1"
  shift
  set +e
  "$@" >/dev/null 2>&1
  local actual="$?"
  set -e
  [[ "$actual" == "$expected" ]] || {
    echo "Expected status $expected, got $actual: $*" >&2
    exit 1
  }
}

temporary_dir="$(mktemp -d)"
port_holder_pid=""
cleanup() {
  if [[ -n "$port_holder_pid" ]]; then
    kill "$port_holder_pid" 2>/dev/null || true
    wait "$port_holder_pid" 2>/dev/null || true
  fi
  find "$temporary_dir" -mindepth 1 -depth -delete 2>/dev/null || true
  rmdir "$temporary_dir" 2>/dev/null || true
}
trap cleanup EXIT
cp -p "$root_dir/rollout.sh" "$temporary_dir/rollout.sh"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'printf "lab"' \
  'printf " <%s>" "$@"' \
  'printf "\\n"' \
  > "$temporary_dir/deploy_test_lab.sh"
chmod 0755 "$temporary_dir/deploy_test_lab.sh"
[[ "$("$temporary_dir/rollout.sh" --fresh --profile lab_v2)" \
    == 'lab <--fresh> <--profile> <lab_v2>' ]]
[[ "$("$temporary_dir/rollout.sh" --upgrade)" == 'lab <--upgrade>' ]]
[[ "$("$temporary_dir/rollout.sh" --fresh --purge-host --profile lab_v2)" \
    == 'lab <--fresh> <--purge-host> <--profile> <lab_v2>' ]]

"$root_dir/deploy_test_lab.sh" --help >/dev/null
"$root_dir/deploy_production.sh" --help >/dev/null
expect_status 64 "$root_dir/rollout.sh" --unknown
expect_status 64 "$root_dir/rollout.sh"
expect_status 64 "$root_dir/rollout.sh" test
expect_status 64 "$root_dir/deploy_test_lab.sh"
expect_status 64 "$root_dir/deploy_test_lab.sh" --fresh --upgrade
expect_status 64 "$root_dir/deploy_test_lab.sh" --upgrade --fresh
expect_status 64 "$root_dir/deploy_test_lab.sh" --upgrade --purge-host
expect_status 64 "$root_dir/deploy_test_lab.sh" --fresh --profile unsupported
expect_status 64 env -u CONTROL_PLANE_STATE_SOURCE \
  "$root_dir/deploy_test_lab.sh" --upgrade
expect_status 64 "$root_dir/deploy_test_lab.sh" --unknown
expect_status 64 "$root_dir/deploy_production.sh"
expect_status 64 env -u CONTROL_PLANE_CONFIRM_PURGE \
  "$root_dir/deploy_production.sh" --fresh
expect_status 64 env -u CONTROL_PLANE_STATE_SOURCE \
  "$root_dir/deploy_production.sh" --upgrade

grep -F 'deployment_mode=""' "$root_dir/deploy_test_lab.sh" >/dev/null
for dispatcher in rollout.sh deploy_test_lab.sh deploy_production.sh; do
  grep -F 'unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES' \
    "$root_dir/$dispatcher" >/dev/null
done
grep -F 'Choose exactly one deployment mode: --fresh or --upgrade.' \
  "$root_dir/deploy_test_lab.sh" >/dev/null
grep -F 'prepare_control_plane_images' "$root_dir/deploy_test_lab.sh" >/dev/null
grep -F 'purge_control_plane_for_fresh_install' \
  "$root_dir/scripts/control_plane_lifecycle.sh" >/dev/null
grep -F 'CONTROL_PLANE_CONFIRM_HOST_PURGE' \
  "$root_dir/scripts/control_plane_lifecycle.sh" >/dev/null
grep -F -- '--purge-host is supported only with --fresh.' \
  "$root_dir/deploy_test_lab.sh" >/dev/null

scoped_image_purge="$(sed -n \
  '/^purge_control_plane_images()/,/^}/p' \
  "$root_dir/scripts/control_plane_lifecycle.sh")"
scoped_fresh_purge="$(sed -n \
  '/^purge_control_plane_for_fresh_install()/,/^}/p' \
  "$root_dir/scripts/control_plane_lifecycle.sh")"
dedicated_host_purge="$(sed -n \
  '/^purge_dedicated_docker_host()/,/^}/p' \
  "$root_dir/scripts/control_plane_lifecycle.sh")"
[[ "$scoped_image_purge" == *'docker image rm'* ]]
[[ "$scoped_image_purge" != *'docker image prune'* ]]
[[ "$scoped_fresh_purge" != *'docker builder prune'* ]]
[[ "$scoped_fresh_purge" != *'docker system prune'* ]]
[[ "$dedicated_host_purge" == *'CONTROL_PLANE_CONFIRM_HOST_PURGE'* ]]
[[ "$dedicated_host_purge" == *'docker image prune --all --force'* ]]
[[ "$dedicated_host_purge" == *'docker builder prune --all --force'* ]]
[[ "$dedicated_host_purge" == *'docker system prune --all --force --volumes'* ]]

fresh_purge_line="$(grep -n 'run_rollout_stage "1/11 fresh purge"' \
  "$root_dir/deploy_test_lab.sh" | cut -d: -f1)"
fresh_preflight_line="$(grep -n 'run_rollout_stage "2/11 port preflight"' \
  "$root_dir/deploy_test_lab.sh" | cut -d: -f1)"
fresh_validation_line="$(grep -n 'run_rollout_stage "3/11 containerized static validation"' \
  "$root_dir/deploy_test_lab.sh" | cut -d: -f1)"
fresh_build_line="$(grep -n 'run_rollout_stage "4/11 clean-room image staging"' \
  "$root_dir/deploy_test_lab.sh" | cut -d: -f1)"
[[ -n "$fresh_purge_line" && -n "$fresh_preflight_line" \
    && -n "$fresh_validation_line" && -n "$fresh_build_line" ]]
(( fresh_purge_line < fresh_preflight_line ))
(( fresh_preflight_line < fresh_validation_line ))
(( fresh_validation_line < fresh_build_line ))

upgrade_validation_line="$(grep -n 'run_rollout_stage "1/11 containerized static validation"' \
  "$root_dir/deploy_test_lab.sh" | cut -d: -f1)"
upgrade_build_line="$(grep -n 'run_rollout_stage "2/11 clean-room image staging"' \
  "$root_dir/deploy_test_lab.sh" | cut -d: -f1)"
upgrade_prepare_line="$(grep -n 'run_rollout_stage "3/11 upgrade deployment preparation"' \
  "$root_dir/deploy_test_lab.sh" | cut -d: -f1)"
[[ -n "$upgrade_validation_line" && -n "$upgrade_build_line" \
    && -n "$upgrade_prepare_line" ]]
(( upgrade_validation_line < upgrade_build_line ))
(( upgrade_build_line < upgrade_prepare_line ))

source "$root_dir/scripts/control_plane_lifecycle.sh"
port_file="$temporary_dir/port"
ready_file="$temporary_dir/ready"
python3 - "$port_file" "$ready_file" <<'PY' &
import socket
import sys
import time


listener = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
listener.bind(("127.0.0.1", 0))
listener.listen()
with open(sys.argv[1], "w", encoding="ascii") as handle:
    handle.write(str(listener.getsockname()[1]))
with open(sys.argv[2], "w", encoding="ascii") as handle:
    handle.write("ready\n")
time.sleep(0.75)
listener.close()
PY
port_holder_pid="$!"
for unused in 1 2 3 4 5 6 7 8 9 10; do
  [[ -s "$ready_file" ]] && break
  sleep 0.1
done
[[ -s "$ready_file" && -s "$port_file" ]]
held_port="$(<"$port_file")"
CONTROL_PLANE_PORT_PROBE_ATTEMPTS=2 \
CONTROL_PLANE_PORT_PROBE_RETRY_SECONDS=0.01 \
  expect_status 1 verify_tcp_ports_available "127.0.0.1:$held_port"
CONTROL_PLANE_PORT_PROBE_ATTEMPTS=20 \
CONTROL_PLANE_PORT_PROBE_RETRY_SECONDS=0.1 \
  verify_tcp_ports_available "127.0.0.1:$held_port" >/dev/null
wait "$port_holder_pid"
port_holder_pid=""

if grep -E 'run_in_host_validation_env|host_validation_environment|python3-venv' \
    "$root_dir/deploy_test_lab.sh" "$root_dir/test_static_all.sh" >/dev/null; then
  echo "Lab rollout still depends on the legacy host venv." >&2
  exit 1
fi
if grep -E 'prepare_worker_v0082|AUTOMATION_WORKER_V0082_STAGED|WORKER_V0082_IMAGE' \
    "$root_dir/deploy_test_lab.sh" \
    "$root_dir/scripts/control_plane_lifecycle.sh" >/dev/null; then
  echo "Control Plane rollout still stages a Worker package." >&2
  exit 1
fi

echo "PASS explicit modes, shared-host scoped Fresh, bounded post-purge port retry, confirmed host purge, purge-before-validation/build order, no-venv path and Worker boundary."

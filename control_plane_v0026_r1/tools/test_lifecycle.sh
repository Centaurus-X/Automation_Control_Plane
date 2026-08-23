#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_dir="$(mktemp -d)"
call_log="$temporary_dir/docker-calls"
cleanup() {
  find "$temporary_dir" -mindepth 1 -depth -delete 2>/dev/null || true
  rmdir "$temporary_dir" 2>/dev/null || true
}
trap cleanup EXIT

source "$root_dir/scripts/control_plane_lifecycle.sh"

mock_reset() {
  mock_container_ids=()
  mock_container_projects=()
  mock_container_names=()
  mock_container_running=()
  mock_container_active=()
  mock_volume_names=()
  mock_volume_projects=()
  mock_volume_active=()
  mock_network_names=()
  mock_network_projects=()
  mock_network_active=()
  mock_image_references=()
  mock_image_active=()
  mock_stop_keeps_running=0
  mock_rm_keeps_containers=0
}

mock_log_docker() {
  local argument
  for argument in "$@"; do
    printf '%s ' "$argument" >> "$call_log"
  done
  printf '\n' >> "$call_log"
}

mock_project_filter() {
  local argument
  for argument in "$@"; do
    case "$argument" in
      label=com.docker.compose.project=*)
        printf '%s\n' "${argument#label=com.docker.compose.project=}"
        return 0
        ;;
    esac
  done
  return 0
}

mock_reference_filter() {
  local argument
  for argument in "$@"; do
    case "$argument" in
      reference=*)
        printf '%s\n' "${argument#reference=}"
        return 0
        ;;
    esac
  done
  return 0
}

mock_mark_containers_stopped() {
  local argument
  local index
  for argument in "$@"; do
    for index in "${!mock_container_ids[@]}"; do
      if [[ "$argument" == "${mock_container_ids[$index]}" ]]; then
        mock_container_running[$index]=0
      fi
    done
  done
}

mock_remove_containers() {
  local argument
  local index
  (( mock_rm_keeps_containers == 0 )) || return 0
  for argument in "$@"; do
    for index in "${!mock_container_ids[@]}"; do
      if [[ "$argument" == "${mock_container_ids[$index]}" ]]; then
        mock_container_active[$index]=0
        mock_container_running[$index]=0
      fi
    done
  done
}

# Replace Docker only inside this test process. The mock retains resource state
# so post-purge verification exercises the same transitions as the real CLI.
docker() {
  local argument
  local filter=""
  local index
  local name
  local pattern=""
  mock_log_docker "$@"
  if [[ "$1" == "ps" ]]; then
    filter="$(mock_project_filter "$@")"
    for index in "${!mock_container_ids[@]}"; do
      (( mock_container_active[$index] == 1 )) || continue
      [[ -z "$filter" || "${mock_container_projects[$index]}" == "$filter" ]] || continue
      if [[ "$2" == "-q" ]]; then
        (( mock_container_running[$index] == 1 )) || continue
      fi
      if [[ "$2" == "-a" ]]; then
        printf '%s %s\n' \
          "${mock_container_ids[$index]}" "${mock_container_names[$index]}"
      else
        printf '%s\n' "${mock_container_ids[$index]}"
      fi
    done
    return 0
  fi
  if [[ "$1" == "stop" ]]; then
    (( mock_stop_keeps_running == 1 )) || mock_mark_containers_stopped "$@"
    return 0
  fi
  if [[ "$1" == "kill" ]]; then
    mock_mark_containers_stopped "$@"
    return 0
  fi
  if [[ "$1" == "rm" ]]; then
    mock_remove_containers "$@"
    return 0
  fi
  if [[ "$1" == "volume" && "$2" == "ls" ]]; then
    filter="$(mock_project_filter "$@")"
    for index in "${!mock_volume_names[@]}"; do
      (( mock_volume_active[$index] == 1 )) || continue
      [[ -z "$filter" || "${mock_volume_projects[$index]}" == "$filter" ]] || continue
      printf '%s\n' "${mock_volume_names[$index]}"
    done
    return 0
  fi
  if [[ "$1" == "volume" && "$2" == "inspect" ]]; then
    name="$3"
    for index in "${!mock_volume_names[@]}"; do
      if (( mock_volume_active[$index] == 1 )) \
          && [[ "${mock_volume_names[$index]}" == "$name" ]]; then
        return 0
      fi
    done
    return 1
  fi
  if [[ "$1" == "volume" && "$2" == "rm" ]]; then
    for argument in "${@:3}"; do
      for index in "${!mock_volume_names[@]}"; do
        [[ "${mock_volume_names[$index]}" == "$argument" ]] \
          && mock_volume_active[$index]=0
      done
    done
    return 0
  fi
  if [[ "$1" == "network" && "$2" == "ls" ]]; then
    filter="$(mock_project_filter "$@")"
    for index in "${!mock_network_names[@]}"; do
      (( mock_network_active[$index] == 1 )) || continue
      [[ -z "$filter" || "${mock_network_projects[$index]}" == "$filter" ]] || continue
      printf '%s\n' "${mock_network_names[$index]}"
    done
    return 0
  fi
  if [[ "$1" == "network" && "$2" == "inspect" ]]; then
    name="$3"
    for index in "${!mock_network_names[@]}"; do
      if (( mock_network_active[$index] == 1 )) \
          && [[ "${mock_network_names[$index]}" == "$name" ]]; then
        return 0
      fi
    done
    return 1
  fi
  if [[ "$1" == "network" && "$2" == "rm" ]]; then
    for argument in "${@:3}"; do
      for index in "${!mock_network_names[@]}"; do
        [[ "${mock_network_names[$index]}" == "$argument" ]] \
          && mock_network_active[$index]=0
      done
    done
    return 0
  fi
  if [[ "$1" == "image" && "$2" == "ls" ]]; then
    pattern="$(mock_reference_filter "$@")"
    for index in "${!mock_image_references[@]}"; do
      (( mock_image_active[$index] == 1 )) || continue
      if [[ -n "$pattern" ]]; then
        [[ "${mock_image_references[$index]}" == ${pattern%\*}* ]] || continue
      fi
      printf '%s\n' "${mock_image_references[$index]}"
    done
    return 0
  fi
  if [[ "$1" == "image" && "$2" == "rm" ]]; then
    for argument in "${@:3}"; do
      for index in "${!mock_image_references[@]}"; do
        [[ "${mock_image_references[$index]}" == "$argument" ]] \
          && mock_image_active[$index]=0
      done
    done
    return 0
  fi
  if [[ "$1" == "image" && "$2" == "prune" ]]; then
    if [[ " $* " == *' --all '* ]]; then
      for index in "${!mock_image_references[@]}"; do
        mock_image_active[$index]=0
      done
    fi
    return 0
  fi
  return 0
}

mock_reset
mock_container_ids=(container-a container-b)
mock_container_projects=(automation-test automation-test)
mock_container_names=(automation-test-a automation-test-b)
mock_container_running=(1 1)
mock_container_active=(1 1)
mock_stop_keeps_running=1
CONTROL_PLANE_STOP_TIMEOUT=7 stop_project_containers automation-test >/dev/null
grep -F 'stop --time 7 container-a container-b ' "$call_log" >/dev/null
grep -F 'kill container-a container-b ' "$call_log" >/dev/null
grep -F 'rm -f container-a container-b ' "$call_log" >/dev/null
[[ "${mock_container_active[*]}" == "0 0" ]]

# A Docker removal that reports success but leaves a container behind must
# fail the fresh rollout instead of producing a false clean-state result.
mock_reset
mock_container_ids=(residual-container)
mock_container_projects=(automation-test)
mock_container_names=(automation-test-residual)
mock_container_running=(0)
mock_container_active=(1)
mock_rm_keeps_containers=1
set +e
stop_project_containers automation-test >/dev/null 2>&1
residual_status="$?"
set -e
[[ "$residual_status" == "70" ]]

CONTROL_PLANE_LOCK_DIR="$temporary_dir/rollout.lock"
acquire_control_plane_lock
set +e
acquire_control_plane_lock >/dev/null 2>&1
lock_status="$?"
set -e
[[ "$lock_status" == "75" ]]
release_control_plane_lock
[[ ! -e "$CONTROL_PLANE_LOCK_DIR" ]]

mock_reset
mock_container_ids=(compose-old legacy-old)
mock_container_projects=(automation-master legacy-project)
mock_container_names=(automation-master-master-node-1 automation-proxy-v0005-1)
mock_container_running=(0 0)
mock_container_active=(1 1)
mock_volume_names=(automation-master_master_runtime old-labelled-volume)
mock_volume_projects=(legacy-project automation-proxy)
mock_volume_active=(1 1)
mock_network_names=(automation-proxy-old automation_control automation_database)
mock_network_projects=(automation-proxy legacy-project legacy-project)
mock_network_active=(1 1 1)
mock_image_references=(
  automation-master-node:0.11.0
  automation-control-plane-validation:0.15.0
  automation-worker-node:0.82.0
)
mock_image_active=(1 1 1)

fake_release="$temporary_dir/release"
mkdir -p "$fake_release/mqtt_broker/secrets" \
  "$fake_release/postgres_system/secrets" \
  "$fake_release/master_node/__pycache__" "$fake_release/proxy_gateway" \
  "$fake_release/integration_test/e2e_v0017/results"
printf '0.17.0\n' > "$fake_release/VERSION"
printf 'e2e_v0017\n' > "$fake_release/integration_test/CURRENT_E2E"
printf 'generated\n' > "$fake_release/mqtt_broker/secrets/test-secret"
printf 'generated\n' > "$fake_release/postgres_system/secrets/test-secret"
printf 'generated\n' > "$fake_release/integration_test/e2e_v0017/results/result.json"
printf 'bytecode\n' > "$fake_release/master_node/__pycache__/stale.pyc"
purge_control_plane_for_fresh_install "$fake_release" >/dev/null
[[ ! -e "$fake_release/mqtt_broker/secrets" ]]
[[ ! -e "$fake_release/postgres_system/secrets" ]]
[[ ! -e "$fake_release/master_node/__pycache__" ]]
[[ ! -e "$fake_release/integration_test/e2e_v0017/results" ]]
grep -F 'rm -f legacy-old ' "$call_log" >/dev/null
grep -F 'volume rm old-labelled-volume ' "$call_log" >/dev/null
grep -F 'volume rm automation-master_master_runtime ' "$call_log" >/dev/null
grep -F 'network rm automation-proxy-old ' "$call_log" >/dev/null
grep -F 'network rm automation_control ' "$call_log" >/dev/null
grep -F 'network rm automation_database ' "$call_log" >/dev/null
grep -F 'image rm automation-control-plane-validation:0.15.0 automation-master-node:0.11.0 ' \
  "$call_log" >/dev/null
if grep -E 'image prune|builder prune|system prune' "$call_log" >/dev/null; then
  echo "Scoped Fresh used a host-global Docker prune operation." >&2
  exit 1
fi
if grep -E 'rm .*automation-worker|image rm .*automation-worker' "$call_log" >/dev/null; then
  echo "Fresh Control Plane lifecycle touched a Worker resource." >&2
  exit 1
fi
[[ "${mock_image_active[2]}" == "1" ]]

unsafe_root="$temporary_dir/not-a-release"
mkdir -p "$unsafe_root/mqtt_broker/secrets"
printf 'preserve\n' > "$unsafe_root/mqtt_broker/secrets/sentinel"
set +e
purge_control_plane_runtime_state "$unsafe_root" >/dev/null 2>&1
unsafe_status="$?"
set -e
[[ "$unsafe_status" == "64" ]]
[[ -f "$unsafe_root/mqtt_broker/secrets/sentinel" ]]

set +e
CONTROL_PLANE_CONFIRM_HOST_PURGE=NO purge_dedicated_docker_host >/dev/null 2>&1
host_purge_status="$?"
set -e
[[ "$host_purge_status" == "64" ]]
if grep -F 'system prune --all --force --volumes ' "$call_log" >/dev/null; then
  echo "Unconfirmed dedicated-host purge reached Docker." >&2
  exit 1
fi

# The destructive host-wide path requires both the explicit mode flag at the
# caller and this exact confirmation. Once confirmed, its claims are verified
# against stateful container, volume and image listings.
mock_reset
mock_container_ids=(unrelated-container)
mock_container_projects=(unrelated-project)
mock_container_names=(unrelated-service)
mock_container_running=(1)
mock_container_active=(1)
mock_volume_names=(unrelated-volume)
mock_volume_projects=(unrelated-project)
mock_volume_active=(1)
mock_image_references=(unrelated-image:latest)
mock_image_active=(1)
CONTROL_PLANE_CONFIRM_HOST_PURGE=PURGE purge_dedicated_docker_host >/dev/null
grep -F 'network prune --force ' "$call_log" >/dev/null
grep -F 'image prune --all --force ' "$call_log" >/dev/null
grep -F 'builder prune --all --force ' "$call_log" >/dev/null
grep -F 'system prune --all --force --volumes ' "$call_log" >/dev/null
[[ "${mock_container_active[0]}" == "0" ]]
[[ "${mock_volume_active[0]}" == "0" ]]
[[ "${mock_image_active[0]}" == "0" ]]

set +e
prepare_control_plane_deployment upgrade "$fake_release" "" >/dev/null 2>&1
upgrade_status="$?"
set -e
[[ "$upgrade_status" == "64" ]]

echo "PASS stateful lifecycle, shared-host scoped Fresh, confirmed full-host purge, safety refusals and rollout lock checks."

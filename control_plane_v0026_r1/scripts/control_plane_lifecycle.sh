#!/usr/bin/env bash

# Shared lifecycle helpers for the five integrated Control Plane Compose projects.

CONTROL_PLANE_PROJECTS=(
  automation-mcp-controller automation-proxy automation-master automation-mqtt
  automation-postgres
)
CONTROL_PLANE_IMAGE_REPOSITORIES=(
  automation-python-free-threaded
  automation-postgres
  automation-master-node
  automation-proxy-gateway
  automation-mcp-controller-server
  automation-control-plane-validation
)
CONTROL_PLANE_NAMED_VOLUMES=(
  automation-mqtt_mosquitto_data
  automation-postgres_postgres_primary_data
  automation-postgres_postgres_standby_data
  automation-postgres_postgres_tls_runtime
  automation-postgres_postgres_standby_tls_runtime
  automation-postgres_postgres_wal_archive
  automation-master_master_runtime
  automation-proxy_proxy_runtime
  automation-mcp-controller_mcp_controller_state
)
CONTROL_PLANE_LOCK_DIR="${CONTROL_PLANE_LOCK_DIR:-/tmp/automation-control-plane-rollout.lock}"

require_control_plane_tools() {
  command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; return 69; }
  command -v openssl >/dev/null 2>&1 || { echo "openssl is required" >&2; return 69; }
  command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; return 69; }
  docker compose version >/dev/null
  docker info >/dev/null
}

validate_proxy_deployment_config() {
  local root_dir="$1"
  command -v python3 >/dev/null 2>&1 || { echo "python3 is required" >&2; return 69; }
  PYTHONDONTWRITEBYTECODE=1 \
  PYTHONPATH="$root_dir/proxy_gateway/proxy_gateway:$root_dir/proxy_gateway/proxy_gateway/src" \
  python3 -c 'import sys; from system.bootstrap_system_interface import load_gateway_config; load_gateway_config(sys.argv[1])' \
    "$root_dir/proxy_gateway/proxy_gateway/_config/application/gateway_config.json"
}

validate_control_plane_compose_files() {
  local root_dir="$1"
  local package
  for package in mqtt_broker postgres_system master_node proxy_gateway mcp_controller_server; do
    (cd "$root_dir/$package" && docker compose config --quiet)
  done
}

prepare_control_plane_images() {
  local root_dir="$1"
  local broker_image="${MOSQUITTO_IMAGE:-eclipse-mosquitto:2.1.2-alpine}"
  local postgres_image="${POSTGRES_IMAGE:-automation-postgres:18.4-v14}"
  local master_image="${MASTER_IMAGE:-automation-master-node:0.26.0}"
  local proxy_image="${PROXY_IMAGE:-automation-proxy-gateway:0.26.0}"
  local mcp_image="${MCP_CONTROLLER_IMAGE:-automation-mcp-controller-server:0.1.0}"
  local python_runtime_image="${PYTHON_RUNTIME_IMAGE:-automation-python-free-threaded:3.14.6-v14}"
  echo "Staging Control Plane release images."
  docker pull "$broker_image"
  docker build --pull --no-cache --tag "$python_runtime_image" \
    "$root_dir/runtime/python314t"
  docker image inspect "$python_runtime_image" >/dev/null
  docker build --pull --no-cache --tag "$postgres_image" \
    "$root_dir/postgres_system"
  # The free-threaded runtime is release-local. --pull would incorrectly ask
  # Docker Hub for library/automation-python-free-threaded and must not be used.
  docker build --no-cache \
    --build-arg "PYTHON_RUNTIME_IMAGE=$python_runtime_image" \
    --tag "$master_image" \
    "$root_dir/master_node"
  docker build --pull --no-cache --tag "$proxy_image" \
    "$root_dir/proxy_gateway"
  docker build --pull --no-cache --tag "$mcp_image" \
    "$root_dir/mcp_controller_server"
  export CONTROL_PLANE_IMAGES_PREPARED=1
}

acquire_control_plane_lock() {
  if mkdir "$CONTROL_PLANE_LOCK_DIR" 2>/dev/null; then
    printf '%s\n' "$$" > "$CONTROL_PLANE_LOCK_DIR/pid"
    return 0
  fi
  local owner=""
  [[ -r "$CONTROL_PLANE_LOCK_DIR/pid" ]] && owner="$(<"$CONTROL_PLANE_LOCK_DIR/pid")"
  if [[ "$owner" =~ ^[0-9]+$ ]] && kill -0 "$owner" 2>/dev/null; then
    echo "Another control-plane rollout is active with PID $owner." >&2
    return 75
  fi
  rm -f "$CONTROL_PLANE_LOCK_DIR/pid"
  rmdir "$CONTROL_PLANE_LOCK_DIR" 2>/dev/null || {
    echo "Cannot recover stale rollout lock: $CONTROL_PLANE_LOCK_DIR" >&2
    return 75
  }
  mkdir "$CONTROL_PLANE_LOCK_DIR"
  printf '%s\n' "$$" > "$CONTROL_PLANE_LOCK_DIR/pid"
}

release_control_plane_lock() {
  rm -f "$CONTROL_PLANE_LOCK_DIR/pid"
  rmdir "$CONTROL_PLANE_LOCK_DIR" 2>/dev/null || true
}

stop_project_containers() {
  local project="$1"
  local timeout="${CONTROL_PLANE_STOP_TIMEOUT:-30}"
  local all_ids=()
  local running_ids=()
  local remaining_ids=()
  mapfile -t all_ids < <(docker ps -aq \
    --filter "label=com.docker.compose.project=$project")
  if (( ${#all_ids[@]} == 0 )); then
    echo "[$project] no existing containers found."
    return 0
  fi
  mapfile -t running_ids < <(docker ps -q \
    --filter "label=com.docker.compose.project=$project")
  if (( ${#running_ids[@]} > 0 )); then
    echo "[$project] stopping ${#running_ids[@]} running container(s)."
    docker stop --time "$timeout" "${running_ids[@]}" >/dev/null || true
  fi
  mapfile -t remaining_ids < <(docker ps -q \
    --filter "label=com.docker.compose.project=$project")
  if (( ${#remaining_ids[@]} > 0 )); then
    echo "[$project] grace period expired; killing remaining container(s)."
    docker kill "${remaining_ids[@]}" >/dev/null || true
  fi
  docker rm -f "${all_ids[@]}" >/dev/null
  mapfile -t remaining_ids < <(docker ps -aq \
    --filter "label=com.docker.compose.project=$project")
  if (( ${#remaining_ids[@]} > 0 )); then
    echo "[$project] container purge left ${#remaining_ids[@]} residual container(s)." >&2
    return 70
  fi
  echo "[$project] old containers removed."
}

purge_legacy_control_plane_containers() {
  local container_id
  local container_name
  local legacy_ids=()
  while read -r container_id container_name; do
    case "$container_name" in
      automation-mqtt-*|automation-postgres-*|automation-master-*|automation-proxy-*|automation-mcp-controller-*)
        legacy_ids+=("$container_id")
        ;;
    esac
  done < <(docker ps -a --format '{{.ID}} {{.Names}}')
  if (( ${#legacy_ids[@]} == 0 )); then
    echo "No legacy Control Plane containers found."
    return 0
  fi
  echo "Removing ${#legacy_ids[@]} legacy Control Plane container(s)."
  docker rm -f "${legacy_ids[@]}" >/dev/null
}

replace_existing_control_plane() {
  local project
  for project in "${CONTROL_PLANE_PROJECTS[@]}"; do
    stop_project_containers "$project"
  done
}

purge_project_volumes() {
  local project="$1"
  local volumes=()
  mapfile -t volumes < <(docker volume ls -q \
    --filter "label=com.docker.compose.project=$project")
  if (( ${#volumes[@]} == 0 )); then
    echo "[$project] no named volumes found."
    return 0
  fi
  echo "[$project] removing ${#volumes[@]} named volume(s)."
  docker volume rm "${volumes[@]}" >/dev/null
}

purge_known_control_plane_volumes() {
  local volume
  for volume in "${CONTROL_PLANE_NAMED_VOLUMES[@]}"; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then
      echo "Removing known Control Plane volume: $volume"
      docker volume rm "$volume" >/dev/null
    fi
  done
}

purge_control_plane_networks() {
  local project
  local network
  local project_networks=()
  for project in "${CONTROL_PLANE_PROJECTS[@]}"; do
    mapfile -t project_networks < <(docker network ls -q \
      --filter "label=com.docker.compose.project=$project")
    if (( ${#project_networks[@]} > 0 )); then
      echo "[$project] removing ${#project_networks[@]} Compose network(s)."
      docker network rm "${project_networks[@]}" >/dev/null
    fi
  done
  for network in automation_control automation_database; do
    if docker network inspect "$network" >/dev/null 2>&1; then
      echo "Removing control-plane network: $network"
      docker network rm "$network" >/dev/null
    fi
  done
}

purge_control_plane_images() {
  local repository
  local candidates=()
  local image_reference
  local unique=()
  for repository in "${CONTROL_PLANE_IMAGE_REPOSITORIES[@]}"; do
    while IFS= read -r image_reference; do
      [[ -n "$image_reference" && "$image_reference" != *':<none>' ]] \
        && candidates+=("$image_reference")
    done < <(docker image ls --format '{{.Repository}}:{{.Tag}}' \
      --filter "reference=$repository:*")
  done
  if (( ${#candidates[@]} == 0 )); then
    echo "No old Control Plane release images found."
  else
    mapfile -t unique < <(printf '%s\n' "${candidates[@]}" | LC_ALL=C sort -u)
    echo "Removing ${#unique[@]} old Control Plane release image reference(s)."
    docker image rm "${unique[@]}" >/dev/null
  fi
}

purge_control_plane_runtime_state() {
  local root_dir="$1"
  local canonical_root
  local relative
  local active_e2e_name
  local state_directories=(
    mqtt_broker/test-pki
    mqtt_broker/secrets
    mqtt_broker/runtime-config
    postgres_system/secrets
    postgres_system/tls
    postgres_system/test_ca_private
    master_node/secrets
    proxy_gateway/secrets
    proxy_gateway/test-pki
    mcp_controller_server/runtime-secrets
    integration_test/runtime
  )
  canonical_root="$(cd "$root_dir" && pwd -P)"
  [[ -f "$canonical_root/VERSION" \
      && -d "$canonical_root/mqtt_broker" \
      && -d "$canonical_root/postgres_system" ]] || {
    echo "Refusing to purge runtime state outside a release root: $canonical_root" >&2
    return 64
  }
  [[ -s "$canonical_root/integration_test/CURRENT_E2E" ]] || {
    echo "Active E2E selector is missing below release root: $canonical_root" >&2
    return 66
  }
  active_e2e_name="$(<"$canonical_root/integration_test/CURRENT_E2E")"
  [[ "$active_e2e_name" =~ ^e2e_v[0-9]{4,}$ \
      && -d "$canonical_root/integration_test/$active_e2e_name" ]] || {
    echo "Invalid active E2E selector: $active_e2e_name" >&2
    return 65
  }
  state_directories+=(
    "integration_test/$active_e2e_name/results"
    "integration_test/$active_e2e_name/runtime"
  )
  for relative in "${state_directories[@]}"; do
    [[ -d "$canonical_root/$relative" ]] || continue
    echo "Removing generated runtime state: $relative"
    find "$canonical_root/$relative" -mindepth 1 -depth -delete
    rmdir "$canonical_root/$relative"
  done
  find "$canonical_root" -type f \( -name '*.pyc' -o -name '*.pyo' \) -delete
  find "$canonical_root" -depth -type d -name '__pycache__' -empty -delete
}

verify_control_plane_purge() {
  local project
  local repository
  local volume
  local residual=""
  for project in "${CONTROL_PLANE_PROJECTS[@]}"; do
    residual="$(docker ps -aq --filter "label=com.docker.compose.project=$project")"
    [[ -z "$residual" ]] || {
      echo "Residual containers remain for Compose project $project." >&2
      return 70
    }
  done
  for volume in "${CONTROL_PLANE_NAMED_VOLUMES[@]}"; do
    if docker volume inspect "$volume" >/dev/null 2>&1; then
      echo "Residual Control Plane volume remains: $volume" >&2
      return 70
    fi
  done
  for repository in "${CONTROL_PLANE_IMAGE_REPOSITORIES[@]}"; do
    residual="$(docker image ls -q --filter "reference=$repository:*")"
    [[ -z "$residual" ]] || {
      echo "Residual Control Plane image remains: $repository" >&2
      return 70
    }
  done
  for residual in automation_control automation_database; do
    if docker network inspect "$residual" >/dev/null 2>&1; then
      echo "Residual Control Plane network remains: $residual" >&2
      return 70
    fi
  done
  echo "Verified: no previous Control Plane containers, volumes, networks or images remain."
}

verify_tcp_ports_available() {
  python3 - "$@" <<'PY'
import errno
import os
import socket
import sys
import time


def positive_integer(name, default):
    value = os.environ.get(name, default)
    try:
        parsed = int(value)
    except ValueError as error:
        raise RuntimeError("{} must be a positive integer".format(name)) from error
    if parsed < 1:
        raise RuntimeError("{} must be a positive integer".format(name))
    return parsed


def non_negative_float(name, default):
    value = os.environ.get(name, default)
    try:
        parsed = float(value)
    except ValueError as error:
        raise RuntimeError("{} must be non-negative".format(name)) from error
    if parsed < 0:
        raise RuntimeError("{} must be non-negative".format(name))
    return parsed


def check_endpoint(value, attempts, retry_seconds):
    address, port_text = value.rsplit(":", 1)
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    last_error = None
    for attempt in range(1, attempts + 1):
        probe = socket.socket(family, socket.SOCK_STREAM)
        try:
            probe.bind((address, int(port_text)))
            return
        except OSError as error:
            last_error = error
            if error.errno != errno.EADDRINUSE or attempt == attempts:
                break
        finally:
            probe.close()
        time.sleep(retry_seconds)
    raise RuntimeError(
        "{} is unavailable after {} attempt(s): {}".format(
            value, attempts, last_error)) from last_error


attempt_count = positive_integer("CONTROL_PLANE_PORT_PROBE_ATTEMPTS", "21")
retry_delay = non_negative_float(
    "CONTROL_PLANE_PORT_PROBE_RETRY_SECONDS", "0.25")
for endpoint in sys.argv[1:]:
    check_endpoint(endpoint, attempt_count, retry_delay)
print("Verified: all required lab TCP ports are available.")
PY
}

verify_lab_ports_available() {
  verify_tcp_ports_available \
    "${MQTT_TEST_HOST:-${MQTT_BIND_ADDRESS:-127.0.0.1}}:1883" \
    "${MQTT_BIND_ADDRESS:-127.0.0.1}:${MQTT_PORT:-8883}" \
    "${PROXY_BIND_ADDRESS:-127.0.0.1}:${PROXY_WSS_PORT:-8443}" \
    "${PROXY_BIND_ADDRESS:-127.0.0.1}:${PROXY_HTTPS_PORT:-8444}" \
    "${MASTER_METRICS_BIND:-127.0.0.1}:9090" \
    "${MCP_BIND_ADDRESS:-127.0.0.1}:${MCP_HTTP_PORT:-8080}" \
    "127.0.0.1:55432" \
    "127.0.0.1:55433"
}

purge_dedicated_docker_host() {
  [[ "${CONTROL_PLANE_CONFIRM_HOST_PURGE:-}" == "PURGE" ]] || {
    echo "--purge-host requires CONTROL_PLANE_CONFIRM_HOST_PURGE=PURGE." >&2
    return 64
  }
  local identifiers=()
  echo "Dedicated-host mode: removing every Docker container, volume, unused network, image and build cache."
  mapfile -t identifiers < <(docker ps -q)
  if (( ${#identifiers[@]} > 0 )); then
    docker stop --time "${CONTROL_PLANE_STOP_TIMEOUT:-30}" "${identifiers[@]}" >/dev/null || true
  fi
  mapfile -t identifiers < <(docker ps -aq)
  if (( ${#identifiers[@]} > 0 )); then
    docker rm -f "${identifiers[@]}" >/dev/null
  fi
  mapfile -t identifiers < <(docker volume ls -q)
  if (( ${#identifiers[@]} > 0 )); then
    docker volume rm "${identifiers[@]}" >/dev/null
  fi
  docker network prune --force >/dev/null
  docker image prune --all --force >/dev/null
  docker builder prune --all --force >/dev/null
  docker system prune --all --force --volumes >/dev/null
  [[ -z "$(docker ps -aq)" \
      && -z "$(docker volume ls -q)" \
      && -z "$(docker image ls -q)" ]] || {
    echo "Dedicated Docker host purge left containers, volumes or images behind." >&2
    return 70
  }
  echo "Verified: no Docker containers, volumes or images remain; unused networks and builder cache were pruned."
}

purge_control_plane_for_fresh_install() {
  local root_dir="$1"
  local project
  echo "Fresh mode: removing old containers, named volumes and generated state."
  replace_existing_control_plane
  purge_legacy_control_plane_containers
  for project in "${CONTROL_PLANE_PROJECTS[@]}"; do
    purge_project_volumes "$project"
  done
  purge_known_control_plane_volumes
  purge_control_plane_networks
  purge_control_plane_images
  purge_control_plane_runtime_state "$root_dir"
  verify_control_plane_purge
}

prepare_control_plane_deployment() {
  local mode="$1"
  local root_dir="$2"
  local state_source="${3:-}"
  case "$mode" in
    fresh)
      purge_control_plane_for_fresh_install "$root_dir"
      ;;
    upgrade)
      [[ -n "$state_source" && -d "$state_source" ]] || {
        echo "--upgrade requires CONTROL_PLANE_STATE_SOURCE to name the previous release directory." >&2
        return 64
      }
      "$root_dir/scripts/import_runtime_state.sh" "$state_source" "$root_dir"
      replace_existing_control_plane
      ;;
    *)
      echo "Unsupported deployment mode: $mode" >&2
      return 64
      ;;
  esac
}

bind_postgres_upgrade_tls_sources() {
  local state_source="$1"
  local source_dir=""
  local name=""
  [[ -n "$state_source" && -d "$state_source" ]] || {
    echo "PostgreSQL upgrade TLS source is unavailable." >&2
    return 64
  }
  state_source="$(cd "$state_source" && pwd -P)"
  source_dir="$state_source/postgres_system/tls"
  [[ -d "$source_dir" && ! -L "$source_dir" ]] || {
    echo "PostgreSQL upgrade TLS directory is unavailable or unsafe." >&2
    return 65
  }
  for name in ca.crt primary.crt primary.key standby.crt standby.key; do
    [[ -f "$source_dir/$name" && ! -L "$source_dir/$name" \
        && -s "$source_dir/$name" ]] || {
      echo "PostgreSQL upgrade TLS file is unavailable or unsafe: $name" >&2
      return 65
    }
  done
  export POSTGRES_CA_SOURCE="$source_dir/ca.crt"
  export POSTGRES_PRIMARY_CERT_SOURCE="$source_dir/primary.crt"
  export POSTGRES_PRIMARY_KEY_SOURCE="$source_dir/primary.key"
  export POSTGRES_STANDBY_CERT_SOURCE="$source_dir/standby.crt"
  export POSTGRES_STANDBY_KEY_SOURCE="$source_dir/standby.key"
  echo "PostgreSQL upgrade TLS sources are bound to the verified state source."
}

default_lab_bind_address() {
  if command -v ip >/dev/null 2>&1 \
      && ip -o -4 address show | grep -F '192.168.0.82/' >/dev/null; then
    echo '192.168.0.82'
    return 0
  fi
  echo '127.0.0.1'
}

print_control_plane_container_status() {
  local project
  for project in automation-mqtt automation-postgres automation-master automation-proxy automation-mcp-controller; do
    docker ps -a --filter "label=com.docker.compose.project=$project" \
      --format '{{.Names}}\t{{.Status}}\t{{.Image}}'
  done
}

run_rollout_stage() {
  local stage="$1"
  shift
  CONTROL_PLANE_CURRENT_STAGE="$stage"
  export CONTROL_PLANE_CURRENT_STAGE
  echo
  echo "==> $stage"
  "$@"
}

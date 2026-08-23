#!/usr/bin/env bash

# Remote Worker lifecycle used by rollout_automation.sh. Package installation
# is a separate, verified and recoverable step; this script owns processes,
# identity activation and immutable-package verification.

set -Eeuo pipefail
umask 077

action="${1:-}"
worker_path="${2:-python-skripte_22/__NEW_FILES__/automation_system_v0101}"
site_id="${3:-site-main}"
node_id="${4:-fn-01}"
profile="${5:-worker_simulation}"
pki_stage_path="${6:-.cache/automation-control-plane/pki/site-main/fn-01}"
worker_log_path="${7:-.local/state/automation-worker/site-main/fn-01/worker.log}"
reset_replica="${8:-no}"
expected_worker_version="${9:-automation_system_v0101}"
expected_manifest_sha256="${10:-699c29f9cf58b1bd6bd7407e879b3b5b3f41ad978343037a1dc33a75ec25026d}"
config_drift_policy="${11:-preserve}"

usage() {
  cat <<'EOF_USAGE'
Usage:
  worker_remote_lifecycle.sh ACTION WORKER_PATH SITE_ID NODE_ID PROFILE \
    PKI_STAGE LOG_PATH RESET EXPECTED_VERSION EXPECTED_MANIFEST_SHA256 \
    CONFIG_DRIFT_POLICY

Actions:
  verify | verify-active | stop-compatible | stop | restart | install-start |
  status | diagnose

RESET:
  yes | no

CONFIG_DRIFT_POLICY:
  preserve  Preserve and report commissioned _config/application and
            _config/config_data differences.
  strict    Require those deployment files to match the package baseline.
EOF_USAGE
}

resolve_home_path() {
  local value="$1"
  case "$value" in
    /*)
      printf '%s\n' "$value"
      ;;
    '~')
      printf '%s\n' "$HOME"
      ;;
    '~/'*)
      printf '%s/%s\n' "$HOME" "${value#~/}"
      ;;
    '$HOME')
      printf '%s\n' "$HOME"
      ;;
    '$HOME/'*)
      printf '%s/%s\n' "$HOME" "${value#\$HOME/}"
      ;;
    *)
      printf '%s/%s\n' "$HOME" "$value"
      ;;
  esac
}

validate_identifier() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
    echo "BLOCKED: invalid $label: $value" >&2
    exit 64
  }
}

validate_path() {
  local label="$1"
  local value="$2"
  [[ -n "$value" && "$value" != "/" \
      && "$value" != *$'\n'* && "$value" != *$'\r'* ]] || {
    echo "BLOCKED: unsafe $label" >&2
    exit 64
  }
}

validate_identifier site_id "$site_id"
validate_identifier node_id "$node_id"
[[ "$node_id" == "fn-01" ]] || {
  echo "BLOCKED: automation_system_v0101 installs fixed fn-01 certificate filenames" >&2
  exit 65
}
validate_identifier profile "$profile"
validate_identifier expected_worker_version "$expected_worker_version"
[[ "$expected_manifest_sha256" =~ ^[0-9a-f]{64}$ ]] || {
  echo "BLOCKED: EXPECTED_MANIFEST_SHA256 must be 64 lowercase hexadecimal characters" >&2
  exit 64
}
case "$reset_replica" in
  yes|no) ;;
  *) echo "BLOCKED: RESET must be yes or no" >&2; exit 64 ;;
esac
case "$config_drift_policy" in
  preserve|strict) ;;
  *) echo "BLOCKED: CONFIG_DRIFT_POLICY must be preserve or strict" >&2; exit 64 ;;
esac
case "$action" in
  verify|verify-active|stop-compatible|stop|restart|install-start|status|diagnose) ;;
  -h|--help|'')
    usage
    [[ -n "$action" ]] && exit 0 || exit 64
    ;;
  *)
    echo "BLOCKED: unknown Worker lifecycle action: $action" >&2
    usage >&2
    exit 64
    ;;
esac

home_dir="$(readlink -f -- "$HOME")"
worker_dir="$(readlink -m -- "$(resolve_home_path "$worker_path")")"
pki_stage="$(readlink -m -- "$(resolve_home_path "$pki_stage_path")")"
worker_log="$(readlink -m -- "$(resolve_home_path "$worker_log_path")")"
state_dir="$(dirname "$worker_log")"
pid_file="$state_dir/worker.pid"
lock_dir="$state_dir/.lifecycle.lock"
runtime_dir="$worker_dir/_runtime"
journal="$runtime_dir/distributed_operations_${node_id}.sqlite3"
verification_runtime=""

validate_path worker_dir "$worker_dir"
validate_path pki_stage "$pki_stage"
validate_path worker_log "$worker_log"
for scoped_path in "$worker_dir" "$pki_stage" "$worker_log"; do
  case "$scoped_path" in
    "$home_dir"/*) ;;
    *)
      echo "BLOCKED: Worker lifecycle paths must remain below HOME: $scoped_path" >&2
      exit 64
      ;;
  esac
done
case "$worker_dir/" in
  "$pki_stage/"*)
    echo "BLOCKED: PKI stage must not contain the installed Worker directory" >&2
    exit 64
    ;;
esac
case "$pki_stage" in
  "$worker_dir"|"$worker_dir"/*)
    echo "BLOCKED: PKI stage must be outside the immutable package tree" >&2
    exit 64
    ;;
esac
case "$worker_log" in
  "$worker_dir"|"$worker_dir"/*)
    echo "BLOCKED: Worker state/log path must be outside the immutable package tree" >&2
    exit 64
    ;;
esac

for command_name in chmod comm date dirname find grep install mkdir mktemp mv \
    nohup openssl pgrep ps readlink rm sed seq sha256sum sleep sort stat \
    tail tr; do
  command -v "$command_name" >/dev/null 2>&1 || {
    echo "BLOCKED: required Worker command is missing: $command_name" >&2
    exit 69
  }
done

if [[ "$action" != "stop-compatible" ]]; then
  test -d "$worker_dir" || {
    echo "BLOCKED: installed Worker directory is missing: $worker_dir" >&2
    exit 66
  }
  test -x "$worker_dir/run_worker.sh" || {
    echo "BLOCKED: installed Worker run_worker.sh is not executable" >&2
    exit 66
  }
  test -x "$worker_dir/install_mqtt_identity.sh" || {
    echo "BLOCKED: installed Worker install_mqtt_identity.sh is not executable" >&2
    exit 66
  }
fi

install -d -m 0700 "$state_dir"
if ! mkdir "$lock_dir" 2>/dev/null; then
  owner=""
  [[ -r "$lock_dir/pid" ]] && owner="$(<"$lock_dir/pid")"
  if [[ "$owner" =~ ^[1-9][0-9]*$ ]] && kill -0 "$owner" 2>/dev/null; then
    echo "BLOCKED: another Worker lifecycle operation is active: PID=$owner" >&2
    exit 75
  fi
  rm -rf -- "$lock_dir"
  mkdir "$lock_dir"
fi
printf '%s\n' "$$" >"$lock_dir/pid"

cleanup() {
  if [[ -n "$verification_runtime" ]]; then
    rm -rf -- "$verification_runtime"
    verification_runtime=""
  fi
  if [[ "$action" == "install-start" ]]; then
    rm -rf -- "$pki_stage"
  fi
  rm -rf -- "$lock_dir"
}
trap cleanup EXIT
trap 'exit 130' INT TERM

worker_command_line() {
  local pid="$1"
  tr '\0' ' ' <"/proc/$pid/cmdline" 2>/dev/null || true
}

is_worker_pid() {
  local pid="$1"
  local command_line=""
  local process_cwd=""
  [[ "$pid" =~ ^[1-9][0-9]*$ && -d "/proc/$pid" ]] || return 1
  command_line="$(worker_command_line "$pid")"
  [[ "$command_line" == *"src.sync_xserver_main"* ]] || return 1
  process_cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ "$process_cwd" == "$worker_dir" ]]
}

worker_pids() {
  local candidate=""
  declare -A seen=()
  if [[ -r "$pid_file" ]]; then
    candidate="$(<"$pid_file")"
    if is_worker_pid "$candidate"; then
      seen["$candidate"]=1
      printf '%s\n' "$candidate"
    fi
  fi
  while IFS= read -r candidate; do
    is_worker_pid "$candidate" || continue
    [[ -n "${seen[$candidate]:-}" ]] && continue
    seen["$candidate"]=1
    printf '%s\n' "$candidate"
  done < <(pgrep -f -- 'src\.sync_xserver_main' || true)
}

is_compatible_worker_pid() {
  local pid="$1"
  local command_line=""
  local process_cwd=""
  local process_uid=""
  [[ "$pid" =~ ^[1-9][0-9]*$ && -d "/proc/$pid" ]] || return 1
  process_uid="$(stat -c '%u' "/proc/$pid" 2>/dev/null || true)"
  [[ "$process_uid" == "$UID" ]] || return 1
  command_line="$(worker_command_line "$pid")"
  [[ "$command_line" == *"src.sync_xserver_main"* ]] || return 1
  process_cwd="$(readlink -f "/proc/$pid/cwd" 2>/dev/null || true)"
  [[ "${process_cwd%/*}" == "$home_dir/python-skripte_22/__NEW_FILES__" ]] || return 1
  [[ "${process_cwd##*/}" =~ ^automation_system_v[0-9]+$ ]]
}

compatible_worker_pids() {
  local candidate=""
  while IFS= read -r candidate; do
    is_compatible_worker_pid "$candidate" && printf '%s\n' "$candidate"
  done < <(pgrep -f -- 'src\.sync_xserver_main' || true)
}

stop_compatible_workers() {
  local pids=()
  local live=()
  local pid=""
  mapfile -t pids < <(compatible_worker_pids)
  if ((${#pids[@]} == 0)); then
    echo "Compatible Worker processes are already stopped."
    return 0
  fi
  printf 'Stopping compatible Worker PIDs: %s\n' "${pids[*]}"
  kill -TERM -- "${pids[@]}"
  for _ in $(seq 1 30); do
    live=()
    for pid in "${pids[@]}"; do
      is_compatible_worker_pid "$pid" && live+=("$pid")
    done
    ((${#live[@]} == 0)) && break
    sleep 1
  done
  if ((${#live[@]})); then
    echo "BLOCKED: compatible Worker did not stop after SIGTERM: ${live[*]}" >&2
    return 70
  fi
  rm -f -- "$pid_file"
  echo "Compatible Worker processes stopped cleanly."
}

stop_worker() {
  local pids=()
  local live=()
  local pid=""
  mapfile -t pids < <(worker_pids)
  if ((${#pids[@]} == 0)); then
    rm -rf -- "$pid_file"
    echo "Worker is already stopped."
    return 0
  fi
  printf 'Stopping Worker PIDs: %s\n' "${pids[*]}"
  kill -TERM -- "${pids[@]}"
  for _ in $(seq 1 30); do
    live=()
    for pid in "${pids[@]}"; do
      is_worker_pid "$pid" && live+=("$pid")
    done
    ((${#live[@]} == 0)) && break
    sleep 1
  done
  if ((${#live[@]})); then
    echo "BLOCKED: Worker did not stop after SIGTERM: ${live[*]}" >&2
    exit 70
  fi
  rm -rf -- "$pid_file"
  echo "Worker stopped cleanly."
}

is_mutable_deployment_file() {
  local relative="$1"
  case "$relative" in
    ./_config/application/*|./_config/config_data/*) return 0 ;;
    *) return 1 ;;
  esac
}

validate_manifest_relative_path() {
  local relative="$1"
  case "$relative" in
    ./*) ;;
    *) return 1 ;;
  esac
  case "$relative" in
    *'//'|*'/../'*|*'/..'|*'/./'*|*'/.'|*'\'*) return 1 ;;
  esac
  [[ "$relative" =~ ^\./[A-Za-z0-9._/-]+$ ]]
}

verify_worker_package() {
  local version_file="$worker_dir/VERSION"
  local manifest="$worker_dir/SHA256SUMS"
  local installed_version=""
  local actual_manifest_sha256=""
  local line=""
  local expected_hash=""
  local relative=""
  local target=""
  local actual_hash=""
  local line_number=0
  local immutable_count=0
  local mutable_count=0
  local mutable_drift_count=0
  local immutable_manifest=""
  local immutable_expected_files=""
  local immutable_actual_files=""
  local mutable_expected_files=""
  local mutable_actual_files=""
  local untracked_mutable_files=""
  local unsafe_mutable_files=""
  local symlink_files=""
  local checksum_report=""
  local missing_files=""
  local extra_files=""
  local untracked_mutable_count=0
  declare -A seen_paths=()

  test -f "$version_file" && [[ ! -L "$version_file" ]] || {
    echo "BLOCKED: installed Worker VERSION is missing or unsafe" >&2
    return 66
  }
  installed_version="$(<"$version_file")"
  [[ "$installed_version" == "$expected_worker_version" ]] || {
    echo "BLOCKED: installed Worker version does not match the rollout contract" >&2
    echo "Expected Worker version: $expected_worker_version" >&2
    echo "Actual Worker version:   ${installed_version:-<empty>}" >&2
    return 65
  }

  test -f "$manifest" && [[ ! -L "$manifest" ]] || {
    echo "BLOCKED: Worker SHA256SUMS is missing or unsafe" >&2
    return 66
  }
  read -r actual_manifest_sha256 _ < <(sha256sum -- "$manifest")
  [[ "$actual_manifest_sha256" == "$expected_manifest_sha256" ]] || {
    echo "BLOCKED: installed Worker manifest is not the canonical v0101 manifest" >&2
    echo "Expected SHA256SUMS hash: $expected_manifest_sha256" >&2
    echo "Actual SHA256SUMS hash:   $actual_manifest_sha256" >&2
    return 65
  }

  verification_runtime="$(mktemp -d "$state_dir/.package-verification.XXXXXX")"
  chmod 0700 "$verification_runtime"
  immutable_manifest="$verification_runtime/immutable.SHA256SUMS"
  immutable_expected_files="$verification_runtime/immutable.expected"
  immutable_actual_files="$verification_runtime/immutable.actual"
  mutable_expected_files="$verification_runtime/mutable.expected"
  mutable_actual_files="$verification_runtime/mutable.actual"
  untracked_mutable_files="$verification_runtime/mutable.untracked"
  unsafe_mutable_files="$verification_runtime/mutable.unsafe"
  symlink_files="$verification_runtime/symlinks"
  checksum_report="$verification_runtime/checksum.report"
  missing_files="$verification_runtime/missing.files"
  extra_files="$verification_runtime/extra.files"
  : >"$immutable_manifest"
  : >"$immutable_expected_files"
  : >"$mutable_expected_files"
  : >"$mutable_actual_files"
  : >"$unsafe_mutable_files"

  while IFS= read -r line || [[ -n "$line" ]]; do
    line_number=$((line_number + 1))
    if [[ ! "$line" =~ ^([0-9a-f]{64})\ \ (\./[A-Za-z0-9._/-]+)$ ]]; then
      echo "BLOCKED: malformed Worker manifest line $line_number" >&2
      return 65
    fi
    expected_hash="${BASH_REMATCH[1]}"
    relative="${BASH_REMATCH[2]}"
    validate_manifest_relative_path "$relative" || {
      echo "BLOCKED: unsafe Worker manifest path at line $line_number: $relative" >&2
      return 65
    }
    [[ -z "${seen_paths[$relative]:-}" ]] || {
      echo "BLOCKED: duplicate Worker manifest path: $relative" >&2
      return 65
    }
    seen_paths["$relative"]=1
    target="$worker_dir/${relative#./}"

    if is_mutable_deployment_file "$relative"; then
      mutable_count=$((mutable_count + 1))
      printf '%s\n' "$relative" >>"$mutable_expected_files"
      test -f "$target" && [[ ! -L "$target" ]] || {
        echo "BLOCKED: required Worker deployment configuration is missing or unsafe: $relative" >&2
        return 66
      }
      read -r actual_hash _ < <(sha256sum -- "$target")
      if [[ "$actual_hash" != "$expected_hash" ]]; then
        mutable_drift_count=$((mutable_drift_count + 1))
        printf 'NOTICE: preserved Worker deployment configuration drift: %s\n' "$relative"
        printf '        expected=%s\n' "$expected_hash"
        printf '        actual=%s\n' "$actual_hash"
      fi
      continue
    fi

    immutable_count=$((immutable_count + 1))
    printf '%s  %s\n' "$expected_hash" "$relative" >>"$immutable_manifest"
    printf '%s\n' "$relative" >>"$immutable_expected_files"
  done <"$manifest"

  (( line_number > 0 && immutable_count > 0 )) || {
    echo "BLOCKED: Worker manifest does not contain immutable package files" >&2
    return 65
  }

  cd "$worker_dir"

  assert_no_project_pycache || return $?

  find . -type l -print | LC_ALL=C sort -u >"$symlink_files"
  if [[ -s "$symlink_files" ]]; then
    echo "BLOCKED: symbolic links are forbidden in the installed Worker tree" >&2
    sed 's/^/  /' "$symlink_files" >&2
    return 65
  fi

  LC_ALL=C sort -u "$mutable_expected_files" -o "$mutable_expected_files"
  for config_root in ./_config/application ./_config/config_data; do
    if [[ -d "$config_root" ]]; then
      find "$config_root" -type f -print >>"$mutable_actual_files"
    fi
  done
  LC_ALL=C sort -u "$mutable_actual_files" -o "$mutable_actual_files"
  comm -13 "$mutable_expected_files" "$mutable_actual_files" >"$untracked_mutable_files"
  while IFS= read -r relative || [[ -n "$relative" ]]; do
    [[ -n "$relative" ]] || continue
    case "$relative" in
      *.json|*.yaml|*.yml|*.txt)
        untracked_mutable_count=$((untracked_mutable_count + 1))
        printf 'NOTICE: preserved untracked Worker deployment configuration: %s\n' \
          "$relative"
        ;;
      *)
        printf '%s\n' "$relative" >>"$unsafe_mutable_files"
        ;;
    esac
  done <"$untracked_mutable_files"
  if [[ -s "$unsafe_mutable_files" ]]; then
    echo "BLOCKED: unsafe untracked files exist in Worker deployment configuration" >&2
    sed 's/^/  /' "$unsafe_mutable_files" >&2
    return 65
  fi

  if [[ "$config_drift_policy" == "strict" ]] && \
      (( mutable_drift_count > 0 || untracked_mutable_count > 0 )); then
    echo "BLOCKED: Worker deployment configuration differs from the strict package baseline" >&2
    return 65
  fi

  if ! sha256sum --check --strict "$immutable_manifest" >"$checksum_report" 2>&1; then
    echo "BLOCKED: installed Worker immutable package integrity failed" >&2
    grep -Ev ': OK$' "$checksum_report" >&2 || cat "$checksum_report" >&2
    return 65
  fi

  LC_ALL=C sort -u "$immutable_expected_files" -o "$immutable_expected_files"
  find . \
    \( -path './_runtime' \
       -o -path './_config/application' \
       -o -path './_config/config_data' \
       -o -type d -name '__pycache__' \) -prune \
    -o -type f \
       ! -path './SHA256SUMS' \
       ! -path './_config/certs/mqtt_ca.crt' \
       ! -path "./_config/certs/worker_${node_id}.crt" \
       ! -path "./_config/certs/worker_${node_id}.key" \
       ! -name '*.pyc' ! -name '*.pyo' ! -name '*.log' ! -name '*.pid' \
       -print | LC_ALL=C sort -u >"$immutable_actual_files"

  comm -23 "$immutable_expected_files" "$immutable_actual_files" >"$missing_files"
  comm -13 "$immutable_expected_files" "$immutable_actual_files" >"$extra_files"
  if [[ -s "$missing_files" || -s "$extra_files" ]]; then
    echo "BLOCKED: installed Worker immutable file tree differs from its canonical manifest" >&2
    if [[ -s "$missing_files" ]]; then
      echo "Missing immutable files:" >&2
      sed 's/^/  /' "$missing_files" >&2
    fi
    if [[ -s "$extra_files" ]]; then
      echo "Unexpected immutable files:" >&2
      sed 's/^/  /' "$extra_files" >&2
    fi
    return 65
  fi

  printf 'Installed Worker version: %s\n' "$installed_version"
  printf 'Installed Worker canonical manifest anchor: PASS (%s)\n' \
    "$actual_manifest_sha256"
  printf 'Installed Worker immutable package: PASS (%s files)\n' \
    "$immutable_count"
  if (( mutable_drift_count == 0 && untracked_mutable_count == 0 )); then
    printf 'Worker deployment configuration baseline: PASS (%s files)\n' \
      "$mutable_count"
  else
    printf 'Worker deployment configuration: PRESERVED (%s/%s baseline files differ; %s safe untracked files)\n' \
      "$mutable_drift_count" "$mutable_count" "$untracked_mutable_count"
  fi

  rm -rf -- "$verification_runtime"
  verification_runtime=""
}

install_identity() {
  local identity=""
  for filename in ca.crt mqtt.crt mqtt.key; do
    test -s "$pki_stage/$filename" || {
      echo "BLOCKED: staged PKI file is missing: $pki_stage/$filename" >&2
      exit 66
    }
  done
  chmod 0644 "$pki_stage/ca.crt" "$pki_stage/mqtt.crt"
  chmod 0600 "$pki_stage/mqtt.key"
  cd "$worker_dir"
  ./install_mqtt_identity.sh \
    --source-dir "$pki_stage" \
    --site-id "$site_id" \
    --node-id "$node_id"
  openssl verify -purpose sslclient \
    -CAfile _config/certs/mqtt_ca.crt \
    "_config/certs/worker_${node_id}.crt" >/dev/null
  identity="urn:automation:${site_id}:worker:${node_id}"
  openssl x509 -in "_config/certs/worker_${node_id}.crt" \
    -noout -ext subjectAltName | tr ',' '\n' | sed 's/^[[:space:]]*//' | \
    grep -Fx "URI:$identity" >/dev/null
  echo "Worker MQTT identity installation: PASS"
  openssl x509 -in "_config/certs/worker_${node_id}.crt" \
    -noout -subject -issuer -dates -fingerprint -sha256
}

verify_installed_identity() {
  local ca="$worker_dir/_config/certs/mqtt_ca.crt"
  local cert="$worker_dir/_config/certs/worker_${node_id}.crt"
  local key="$worker_dir/_config/certs/worker_${node_id}.key"
  local identity="urn:automation:${site_id}:worker:${node_id}"
  local certificate_public_key=""
  local private_public_key=""

  for filename in "$ca" "$cert" "$key"; do
    test -s "$filename" && [[ ! -L "$filename" ]] || {
      echo "BLOCKED: installed Worker identity file is missing or unsafe: $filename" >&2
      return 66
    }
  done
  openssl verify -purpose sslclient -CAfile "$ca" "$cert" >/dev/null
  openssl x509 -in "$cert" -noout -checkend 0 >/dev/null
  openssl pkey -in "$key" -noout -check >/dev/null
  certificate_public_key="$(openssl x509 -in "$cert" -pubkey -noout | openssl sha256)"
  private_public_key="$(openssl pkey -in "$key" -pubout | openssl sha256)"
  [[ "$certificate_public_key" == "$private_public_key" ]] || {
    echo "BLOCKED: installed Worker certificate/private key mismatch" >&2
    return 65
  }
  openssl x509 -in "$cert" -noout -ext subjectAltName | \
    tr ',' '\n' | sed 's/^[[:space:]]*//' | grep -Fx "URI:$identity" >/dev/null
  echo "Installed Worker MQTT identity: PASS"
  openssl x509 -in "$cert" \
    -noout -subject -issuer -dates -fingerprint -sha256
}

require_single_active_worker() {
  local pids=()
  mapfile -t pids < <(worker_pids)
  ((${#pids[@]} == 1)) || {
    echo "BLOCKED: Worker reuse requires exactly one active process; found ${#pids[@]}" >&2
    return 70
  }
  printf 'Active Worker continuity: PASS; PID=%s\n' "${pids[0]}"
}

assert_no_project_pycache() {
  local unexpected=""
  unexpected="$(find "$worker_dir" -type d -name '__pycache__' -print -quit)"
  if [[ -n "$unexpected" ]]; then
    echo "BLOCKED: Python bytecode cache exists inside the Worker project: $unexpected" >&2
    return 65
  fi
  echo "Worker project pycache policy: PASS (cache-free project tree)"
}

reset_replica_journal() {
  local removed=0
  local candidate=""
  for candidate in "$journal" "$journal-wal" "$journal-shm"; do
    if [[ -e "$candidate" ]]; then
      rm -f -- "$candidate"
      removed=$((removed + 1))
    fi
  done
  if (( removed == 0 )); then
    echo "Worker replica journal is already clean."
  else
    printf 'Worker replica journal reset: removed %s local file(s).\n' "$removed"
  fi
}

reset_worker_log() {
  rm -f -- "$worker_log"
  echo "Worker log reset: $worker_log"
}

start_worker() {
  local worker_pid=""
  local temporary_pid_file=""
  cd "$worker_dir"
  AUTOMATION_PROFILE="$profile" ./run_worker.sh --preflight
  reset_worker_log
  nohup env AUTOMATION_PROFILE="$profile" \
    ./run_worker.sh >"$worker_log" 2>&1 </dev/null &
  worker_pid=$!
  temporary_pid_file="$pid_file.tmp.$$"
  printf '%s\n' "$worker_pid" >"$temporary_pid_file"
  chmod 0600 "$temporary_pid_file"
  # Idempotency boundary: a stale file, symlink or directory at the private
  # PID anchor must never block a valid Worker restart.
  rm -rf -- "$pid_file"
  mv -- "$temporary_pid_file" "$pid_file"
  sleep 5
  if ! is_worker_pid "$worker_pid"; then
    echo "BLOCKED: Worker process exited during startup: PID=$worker_pid" >&2
    tail -n 120 "$worker_log" >&2 || true
    exit 70
  fi
  assert_no_project_pycache
  echo "Worker started: PID=$worker_pid"
  echo "Worker log: $worker_log"
}

print_status() {
  local pids=()
  local cert=""
  mapfile -t pids < <(worker_pids)
  if ((${#pids[@]})); then
    printf 'Worker status: RUNNING; PIDs=%s\n' "${pids[*]}"
  else
    echo "Worker status: STOPPED"
  fi
  if [[ -s "$journal" ]]; then
    printf 'Replica journal: %s bytes; path=%s\n' \
      "$(stat -c '%s' "$journal")" "$journal"
  else
    echo "Replica journal: clean or not yet created"
  fi
  cert="$worker_dir/_config/certs/worker_${node_id}.crt"
  if [[ -s "$cert" ]]; then
    openssl x509 -in "$cert" -noout -subject -issuer -dates -fingerprint -sha256
  else
    echo "Worker certificate: missing"
  fi
}

case "$action" in
  verify)
    verify_worker_package
    print_status
    ;;
  verify-active)
    verify_worker_package
    verify_installed_identity
    require_single_active_worker
    print_status
    ;;
  stop-compatible)
    stop_compatible_workers
    ;;
  stop)
    stop_worker
    ;;
  restart)
    stop_worker
    verify_worker_package
    [[ "$reset_replica" == "yes" ]] && reset_replica_journal
    start_worker
    print_status
    ;;
  install-start)
    stop_worker
    verify_worker_package
    install_identity
    [[ "$reset_replica" == "yes" ]] && reset_replica_journal
    start_worker
    print_status
    ;;
  status)
    print_status
    ;;
  diagnose)
    echo "--- Worker package verification ---"
    if ! verify_worker_package; then
      echo "Worker package verification: FAILED (diagnostic mode continues)" >&2
    fi
    print_status
    echo "--- Worker process table ---"
    ps -ef | grep -E '[s]rc\.sync_xserver_main' || true
    echo "--- Worker log tail ---"
    tail -n 400 "$worker_log" 2>&1 || true
    echo "--- Replica runtime files ---"
    for candidate in "$journal" "$journal-wal" "$journal-shm"; do
      if [[ -e "$candidate" ]]; then
        stat --printf='%n %s bytes %y\n' "$candidate" || true
      fi
    done
    ;;
esac

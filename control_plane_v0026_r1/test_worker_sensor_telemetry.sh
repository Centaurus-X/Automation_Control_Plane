#!/usr/bin/env bash

# Verify that live Worker telemetry advances through Master persistence and
# reaches the Proxy replay cache before the WSS E2E is started.

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
site_id="${AUTOMATION_SITE_ID:-site-main}"
node_id="${1:-fn-01}"
timeout_s="${2:-45}"
progress_interval_s="${WORKER_TELEMETRY_PROGRESS_INTERVAL_S:-5}"
minimum_source_sequence="${WORKER_TELEMETRY_MIN_SOURCE_SEQUENCE:-}"
minimum_proxy_sequence="${WORKER_TELEMETRY_MIN_PROXY_SEQUENCE:-}"

for value in "$site_id" "$node_id"; do
  [[ "$value" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] || {
    echo "Usage: $0 [NODE_ID] [TIMEOUT_SECONDS]" >&2
    exit 64
  }
done
[[ "$timeout_s" =~ ^[1-9][0-9]*$ ]] || {
  echo "TIMEOUT_SECONDS must be a positive integer." >&2
  exit 64
}
[[ "$progress_interval_s" =~ ^[1-9][0-9]*$ ]] || {
  echo "WORKER_TELEMETRY_PROGRESS_INTERVAL_S must be a positive integer." >&2
  exit 64
}
command -v docker >/dev/null 2>&1 || {
  echo "docker is required" >&2
  exit 69
}

run_scalar_sql() {
  local sql="$1"
  (
    cd "$root_dir/postgres_system"
    docker compose exec -T -u postgres postgres-primary \
      psql -d automation_platform -At -v ON_ERROR_STOP=1 -c "$sql"
  )
}

read_state() {
  run_scalar_sql "
SELECT
  COALESCE((
    SELECT max(source_sequence)
      FROM master_control.sensor_latest
     WHERE site_id='$site_id' AND source_node_id='$node_id'
  ),0)::text || '|' ||
  COALESCE((
    SELECT max(event_sequence)
      FROM proxy_gateway.event_replay_cache
     WHERE site_id='$site_id' AND event_type='sensor.value'
       AND envelope #>> '{payload,source_node_id}'='$node_id'
  ),0)::text || '|' ||
  COALESCE((
    SELECT count(*)
      FROM master_control.sensor_latest
     WHERE site_id='$site_id' AND source_node_id='$node_id'
       AND source_sequence IS NULL
  ),0)::text;"
}

initial_state="$(read_state)"
IFS='|' read -r initial_source_sequence initial_proxy_sequence \
  initial_null_sequences <<< "$initial_state"
for value in "$initial_source_sequence" "$initial_proxy_sequence" \
    "$initial_null_sequences"; do
  [[ "$value" =~ ^[0-9]+$ ]] || {
    echo "BLOCKED: invalid telemetry database state: $initial_state" >&2
    exit 70
  }
done
for value in "$minimum_source_sequence" "$minimum_proxy_sequence"; do
  [[ -z "$value" || "$value" =~ ^[0-9]+$ ]] || {
    echo "BLOCKED: telemetry sequence floor is not an unsigned integer" >&2
    exit 64
  }
done
source_floor="$initial_source_sequence"
proxy_floor="$initial_proxy_sequence"
if [[ -n "$minimum_source_sequence" ]] \
    && (( minimum_source_sequence > source_floor )); then
  source_floor="$minimum_source_sequence"
fi
if [[ -n "$minimum_proxy_sequence" ]] \
    && (( minimum_proxy_sequence > proxy_floor )); then
  proxy_floor="$minimum_proxy_sequence"
fi

deadline=$((SECONDS + timeout_s))
next_progress=$SECONDS
last_state="$initial_state"
trap 'echo "Worker telemetry check interrupted." >&2; exit 130' INT TERM

while ((SECONDS < deadline)); do
  last_state="$(read_state)"
  IFS='|' read -r source_sequence proxy_sequence null_sequences <<< "$last_state"
  if [[ "$source_sequence" =~ ^[1-9][0-9]*$ \
      && "$proxy_sequence" =~ ^[1-9][0-9]*$ \
      && "$null_sequences" == "0" \
      && "$source_sequence" -gt "$source_floor" \
      && "$proxy_sequence" -gt "$proxy_floor" ]]; then
    echo "PASS Worker telemetry: source_sequence floor ${source_floor}->${source_sequence}; Proxy sensor.value event_sequence floor ${proxy_floor}->${proxy_sequence}."
    exit 0
  fi
  if ((SECONDS >= next_progress)); then
    remaining=$((deadline - SECONDS))
    echo "Waiting for live Worker telemetry (${remaining}s remaining): source_sequence=${source_sequence:-?}, proxy_event_sequence=${proxy_sequence:-?}, null_sequences=${null_sequences:-?}"
    next_progress=$((SECONDS + progress_interval_s))
  fi
  remaining=$((deadline - SECONDS))
  ((remaining > 0)) || break
  sleep_s=2
  ((remaining < sleep_s)) && sleep_s=$remaining
  sleep "$sleep_s"
done

echo "Worker telemetry did not advance through Master and Proxy within ${timeout_s}s." >&2
echo "Initial state: source_sequence|proxy_event_sequence|null_sequences = $initial_state" >&2
echo "Last state:    source_sequence|proxy_event_sequence|null_sequences = $last_state" >&2
echo "Check Worker SENSOR_VALUE_UPDATE publication, Master legacy MQTT statistics and Proxy event ingestion." >&2
exit 1

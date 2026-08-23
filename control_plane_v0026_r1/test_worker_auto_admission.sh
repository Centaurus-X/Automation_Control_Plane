#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
site_id="${AUTOMATION_SITE_ID:-site-main}"
node_id="${1:-fn-01}"
timeout_s="${2:-90}"
heartbeat_max_age_s="${WORKER_HEARTBEAT_MAX_AGE_S:-45}"
progress_interval_s="${WORKER_ADMISSION_PROGRESS_INTERVAL_S:-10}"

[[ "$site_id" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] || {
  echo "Invalid AUTOMATION_SITE_ID: $site_id" >&2
  exit 64
}
[[ "$node_id" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] || {
  echo "Usage: $0 [NODE_ID] [TIMEOUT_SECONDS]" >&2
  exit 64
}
[[ "$timeout_s" =~ ^[1-9][0-9]*$ ]] || {
  echo "TIMEOUT_SECONDS must be a positive integer." >&2
  exit 64
}
[[ "$heartbeat_max_age_s" =~ ^[1-9][0-9]*$ ]] || {
  echo "WORKER_HEARTBEAT_MAX_AGE_S must be a positive integer." >&2
  exit 64
}
[[ "$progress_interval_s" =~ ^[1-9][0-9]*$ ]] || {
  echo "WORKER_ADMISSION_PROGRESS_INTERVAL_S must be a positive integer." >&2
  exit 64
}
command -v docker >/dev/null 2>&1 || {
  echo "docker is required" >&2
  exit 69
}

read_authority_mode() {
  local config_file="$root_dir/master_node/master_node/_config/application/master_config.json"
  local configured=""
  if [[ -f "$config_file" ]]; then
    configured="$(awk -F '"' '/"authority_mode"[[:space:]]*:/ {print $4; exit}' \
      "$config_file" || true)"
  fi
  printf '%s' "${configured:-master_global}"
}

run_registry_query() {
  local sql="$1"
  (
    cd "$root_dir/postgres_system"
    docker compose exec -T -u postgres postgres-primary \
      psql -d automation_platform -At -v ON_ERROR_STOP=1 -c "$sql" 2>/dev/null
  )
}

read_node_local_state() {
  run_registry_query "
SELECT
    n.admission_state || '|' || n.desired_state || '|' ||
    n.observed_state || '|' ||
    (SELECT count(*)::text
       FROM master_control.node_heartbeats heartbeat
      WHERE heartbeat.site_id=n.site_id AND heartbeat.node_id=n.node_id) || '|' ||
    CASE WHEN authority.lease_until>clock_timestamp()
         THEN authority.authority_term ELSE 0 END::text || '|' ||
    COALESCE(n.node_replica_scope,'') || '|' ||
    n.node_replica_revision::text || '|' ||
    btrim(COALESCE(n.node_replica_fingerprint::text,'')) || '|' ||
    COALESCE(mirror.scope,'') || '|' ||
    COALESCE(mirror.source_node_id,'') || '|' ||
    COALESCE(mirror.owner_node_id,'') || '|' ||
    COALESCE(mirror.authority_epoch,0)::text || '|' ||
    COALESCE(mirror.scope_revision,0)::text || '|' ||
    COALESCE(mirror.execution_epoch,0)::text || '|' ||
    btrim(COALESCE(mirror.fingerprint::text,'')) || '|' ||
    (SELECT count(*)::text
       FROM master_control.worker_mirror_snapshots snapshot
      WHERE snapshot.scope=mirror.scope
        AND snapshot.authority_epoch=mirror.authority_epoch
        AND snapshot.scope_revision=mirror.scope_revision
        AND snapshot.execution_epoch=mirror.execution_epoch
        AND snapshot.source_node_id=mirror.source_node_id
        AND snapshot.fingerprint=mirror.fingerprint
        AND jsonb_typeof(snapshot.snapshot)='object') || '|' ||
    CASE WHEN n.last_seen_at>=clock_timestamp()-interval '$heartbeat_max_age_s seconds'
         THEN 'fresh' ELSE 'stale' END
FROM master_control.nodes n
LEFT JOIN master_control.worker_mirror_heads mirror
  ON mirror.scope='node:' || n.node_id AND mirror.scope_kind='node'
LEFT JOIN master_control.authority_state authority ON authority.site_id=n.site_id
WHERE n.site_id='$site_id' AND n.node_id='$node_id';"
}

read_master_global_state() {
  run_registry_query "
SELECT
    n.admission_state || '|' || n.desired_state || '|' ||
    n.observed_state || '|' ||
    (SELECT count(*)::text
       FROM master_control.node_heartbeats heartbeat
      WHERE heartbeat.site_id=n.site_id AND heartbeat.node_id=n.node_id) || '|' ||
    CASE WHEN authority.lease_until>clock_timestamp()
         THEN authority.authority_term ELSE 0 END::text || '|' ||
    n.observed_global_revision::text || '|' ||
    n.node_replica_revision::text || '|' ||
    COALESCE(head.scope_revision,0)::text || '|' ||
    btrim(COALESCE(n.node_replica_fingerprint::text,'')) || '|' ||
    btrim(COALESCE(head.fingerprint::text,'')) || '|' ||
    CASE WHEN n.last_seen_at>=clock_timestamp()-interval '$heartbeat_max_age_s seconds'
         THEN 'fresh' ELSE 'stale' END
FROM master_control.nodes n
LEFT JOIN master_control.scope_heads head ON head.scope='node:' || n.node_id
LEFT JOIN master_control.authority_state authority ON authority.site_id=n.site_id
WHERE n.site_id='$site_id' AND n.node_id='$node_id';"
}

authority_mode="${MASTER_CONFIG_AUTHORITY:-}"
[[ -n "$authority_mode" ]] || authority_mode="$(read_authority_mode)"
case "$authority_mode" in
  node_local|master_global) ;;
  *)
    echo "Unsupported Master authority mode: $authority_mode" >&2
    exit 78
    ;;
esac

deadline=$((SECONDS + timeout_s))
last_state="not registered"
next_progress=$SECONDS
trap 'echo "Worker convergence check interrupted." >&2; exit 130' INT TERM
while (( SECONDS < deadline )); do
  if [[ "$authority_mode" == "node_local" ]]; then
    last_state="$(read_node_local_state || true)"
    IFS='|' read -r admission desired observed heartbeat_count registry_term \
      node_scope node_revision node_fingerprint mirror_scope mirror_source \
      mirror_owner authority_epoch mirror_revision execution_epoch \
      mirror_fingerprint snapshot_count heartbeat_fresh <<< "$last_state"
    if [[ "$node_revision" =~ ^[0-9]+$ \
        && "$mirror_revision" =~ ^[0-9]+$ \
        && "$node_revision" == "$mirror_revision" \
        && -n "$node_fingerprint" && -n "$mirror_fingerprint" \
        && "$node_fingerprint" != "$mirror_fingerprint" ]]; then
      echo "BLOCKED immutable_mirror_conflict: Worker and Registry Mirror use revision ${node_revision}, but their fingerprints differ." >&2
      echo "The Registry must never overwrite the Worker authority automatically." >&2
      echo "Last registry state: $last_state" >&2
      exit 2
    fi
    if [[ "$admission" == "APPROVED" && "$desired" == "ACTIVE" \
        && "$observed" == "ACTIVE" && "$heartbeat_count" =~ ^[1-9][0-9]*$ \
        && "$registry_term" =~ ^[1-9][0-9]*$ \
        && "$node_scope" == "node:$node_id" \
        && "$mirror_scope" == "node:$node_id" \
        && "$mirror_source" == "$node_id" && "$mirror_owner" == "$node_id" \
        && "$authority_epoch" =~ ^[0-9]+$ \
        && "$node_revision" =~ ^[0-9]+$ \
        && "$mirror_revision" =~ ^[0-9]+$ \
        && "$execution_epoch" =~ ^[0-9]+$ \
        && "$node_revision" == "$mirror_revision" \
        && "$node_fingerprint" =~ ^[0-9a-f]{64}$ \
        && "$node_fingerprint" == "$mirror_fingerprint" \
        && "$snapshot_count" =~ ^[1-9][0-9]*$ \
        && "$heartbeat_fresh" == "fresh" ]]; then
      echo "Worker $site_id/$node_id was admitted and its authoritative Node snapshot is mirrored exactly."
      echo "Authority mode: $authority_mode"
      echo "Registry state: $last_state"
      exit 0
    fi
  else
    last_state="$(read_master_global_state || true)"
    IFS='|' read -r admission desired observed heartbeat_count master_authority_term \
      observed_global node_revision head_revision node_fingerprint \
      head_fingerprint heartbeat_fresh <<< "$last_state"
    if [[ "$heartbeat_fresh" == "fresh" \
        && "$node_revision" =~ ^[1-9][0-9]*$ \
        && "$head_revision" =~ ^[1-9][0-9]*$ \
        && -n "$node_fingerprint" && -n "$head_fingerprint" \
        && "$node_fingerprint" != "$head_fingerprint" \
        && "$node_revision" -ge "$head_revision" ]]; then
      echo "BLOCKED stale_replica: Worker Node replica ${node_revision} is not older than Master Node scope ${head_revision}, but the fingerprints differ." >&2
      echo "A --fresh Control Plane requires a clean Worker replica baseline before the Worker is restarted." >&2
      echo "Last registry state: $last_state" >&2
      exit 2
    fi
    if [[ "$admission" == "APPROVED" && "$desired" == "ACTIVE" \
        && "$observed" == "ACTIVE" && "$heartbeat_count" =~ ^[1-9][0-9]*$ \
        && "$master_authority_term" =~ ^[1-9][0-9]*$ \
        && "$observed_global" =~ ^[1-9][0-9]*$ \
        && "$node_revision" =~ ^[1-9][0-9]*$ \
        && "$node_revision" == "$head_revision" \
        && -n "$node_fingerprint" \
        && "$node_fingerprint" == "$head_fingerprint" \
        && "$heartbeat_fresh" == "fresh" ]]; then
      echo "Worker $site_id/$node_id was admitted and its Master-assigned Node shard converged."
      echo "Authority mode: $authority_mode"
      echo "Registry state: $last_state"
      exit 0
    fi
  fi
  if (( SECONDS >= next_progress )); then
    remaining=$((deadline - SECONDS))
    echo "Waiting for Worker convergence (${remaining}s remaining, authority=$authority_mode): $last_state"
    next_progress=$((SECONDS + progress_interval_s))
  fi
  remaining=$((deadline - SECONDS))
  (( remaining > 0 )) || break
  sleep_s=2
  (( remaining < sleep_s )) && sleep_s=$remaining
  sleep "$sleep_s"
done

echo "Worker auto-admission and convergence failed within ${timeout_s}s (authority=$authority_mode)." >&2
echo "Last registry state: ${last_state:-not registered}" >&2
if [[ "$authority_mode" == "node_local" ]]; then
  echo "Fields: admission|desired|observed|heartbeats|registry-term|node-scope|node-revision|node-fingerprint|mirror-scope|mirror-source|mirror-owner|authority-epoch|mirror-revision|execution-epoch|mirror-fingerprint|snapshots|heartbeat-age." >&2
else
  echo "Fields: admission|desired|observed|heartbeats|master-term|global|node-revision|head-revision|node-fingerprint|head-fingerprint|heartbeat-age." >&2
fi
echo "Run ./diagnose_worker_sync.sh and check Worker mTLS/MQTT logs." >&2
exit 1

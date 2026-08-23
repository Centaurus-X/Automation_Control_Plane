#!/usr/bin/env bash

# Read-only Worker synchronization diagnosis for both authority modes.

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
node_id="${1:-fn-01}"
site_id="${2:-${AUTOMATION_SITE_ID:-site-main}}"

for value in "$node_id" "$site_id"; do
  [[ "$value" =~ ^[a-z0-9][a-z0-9._-]{0,127}$ ]] || {
    echo "Usage: $0 [NODE_ID] [SITE_ID]" >&2
    exit 64
  }
done
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
  printf '%s' "${configured:-node_local}"
}

run_sql() {
  local sql="$1"
  (
    cd "$root_dir/postgres_system"
    docker compose exec -T -u postgres postgres-primary \
      psql -d automation_platform -v ON_ERROR_STOP=1 \
      --pset footer=off -c "$sql"
  )
}

diagnose_registry_state() {
  echo '=== Aktueller Worker-Registryzustand ==='
  run_sql "
SELECT admission_state,desired_state,observed_state,authority_term,
       node_replica_scope,node_replica_revision,
       left(btrim(coalesce(node_replica_fingerprint::text,'')),12)
         AS replica_fingerprint_12,
       left(coalesce(runtime_nonce,''),12) AS runtime_nonce_12,
       last_seen_at,
       round(extract(epoch FROM (clock_timestamp()-last_seen_at))::numeric,1)
         AS heartbeat_age_s
  FROM master_control.nodes
 WHERE site_id='$site_id' AND node_id='$node_id';"
}

diagnose_node_local() {
  echo '=== Autoritativer Worker-Mirror-Head ==='
  run_sql "
SELECT head.scope,head.scope_kind,head.owner_node_id,head.source_node_id,
       head.authority_epoch,head.scope_revision,head.execution_epoch,
       left(btrim(head.fingerprint::text),12) AS fingerprint_12,
       head.contracts_version,head.type_schema_version,
       head.updated_at,
       (SELECT count(*)
          FROM master_control.worker_mirror_snapshots snapshot
         WHERE snapshot.scope=head.scope
           AND snapshot.authority_epoch=head.authority_epoch
           AND snapshot.scope_revision=head.scope_revision
           AND snapshot.execution_epoch=head.execution_epoch
           AND snapshot.source_node_id=head.source_node_id
           AND snapshot.fingerprint=head.fingerprint
           AND jsonb_typeof(snapshot.snapshot)='object') AS exact_snapshot_count
  FROM master_control.worker_mirror_heads head
 WHERE head.scope='node:$node_id';"

  echo '=== Letzte unveränderliche Worker-Mirror-Snapshots ==='
  run_sql "
SELECT scope,authority_epoch,scope_revision,execution_epoch,source_node_id,
       left(btrim(fingerprint::text),12) AS fingerprint_12,
       octet_length(snapshot::text) AS snapshot_bytes,received_at
  FROM master_control.worker_mirror_snapshots
 WHERE scope='node:$node_id'
 ORDER BY received_at DESC LIMIT 10;"

  echo '=== Letzte Mirror-Inbox-Einträge ==='
  run_sql "
SELECT message_id,event_type,state,
       coalesce(result->>'code','') AS result_code,received_at,completed_at
  FROM master_control.inbox
 WHERE site_id='$site_id' AND source_service='worker'
   AND source_instance_id='$node_id'
   AND event_type IN ('distributed.mirror.announce',
                      'distributed.mirror.snapshot')
 ORDER BY received_at DESC LIMIT 20;"

  echo '=== Letzte Mirror-Requests an den Worker ==='
  run_sql "
SELECT message_id,event_type,state,attempt_count,last_error_code,
       created_at,updated_at
  FROM master_control.outbox
 WHERE site_id='$site_id' AND target_service='worker'
   AND target_instance_id='$node_id'
   AND event_type='distributed.mirror.request'
 ORDER BY updated_at DESC LIMIT 20;"

  echo '=== Zusammenfassende Node-Local-Diagnose ==='
  run_sql "
SELECT CASE
  WHEN node.node_id IS NULL THEN 'FEHLT: Worker ist nicht registriert'
  WHEN node.admission_state<>'APPROVED'
    THEN 'WARTET: Worker-Zulassung ist ' || node.admission_state
  WHEN node.desired_state<>'ACTIVE' OR node.observed_state<>'ACTIVE'
    THEN 'WARTET: Worker-Zustand ist desired=' || node.desired_state
         || ', observed=' || node.observed_state
  WHEN node.last_seen_at<clock_timestamp()-interval '45 seconds'
    THEN 'FEHLER: Worker-Heartbeat ist veraltet'
  WHEN authority.authority_term IS NULL
       OR authority.lease_until<=clock_timestamp()
    THEN 'FEHLER: keine aktive Registry-Lease'
  WHEN head.scope IS NULL
    THEN 'WARTET: noch kein Worker-Mirror-Head; Mirror-Request/Inbox prüfen'
  WHEN head.scope_kind<>'node' OR head.source_node_id<>'$node_id'
       OR head.owner_node_id<>'$node_id'
    THEN 'BLOCKER owner_mismatch: Mirror-Quelle oder Node-Eigentümer ist falsch'
  WHEN exact_snapshot.count=0
    THEN 'FEHLER: Mirror-Head besitzt kein exakt passendes, persistiertes Snapshot'
  WHEN node.node_replica_scope<>'node:$node_id'
    THEN 'WARTET: Worker meldet keinen eigenen Node-Scope'
  WHEN node.node_replica_revision=head.scope_revision
       AND btrim(node.node_replica_fingerprint::text)=btrim(head.fingerprint::text)
    THEN 'OK: Worker-Autorität und Registry-Mirror sind exakt konvergiert ('
         || node.observed_state || ')'
  WHEN node.node_replica_revision=head.scope_revision
       AND btrim(coalesce(node.node_replica_fingerprint::text,''))
           <>btrim(head.fingerprint::text)
    THEN 'BLOCKER immutable_mirror_conflict: gleiche Revision, andere Fingerprints; '
         || 'niemals automatisch überschreiben'
  WHEN node.node_replica_revision>head.scope_revision
    THEN 'BEFUND: Registry-Mirror liegt hinter der Worker-Autorität; '
         || 'Mirror-Request/Inbox prüfen'
  WHEN node.node_replica_revision<head.scope_revision
    THEN 'BLOCKER: Registry-Mirror ist neuer als die laufende Worker-Replica; '
         || 'expliziten Restore-/Authority-Transfer-Pfad verwenden'
  ELSE 'UNKLAR: Worker- und Mirror-Anker sind unvollständig'
END AS diagnose
FROM (SELECT 1) AS one
LEFT JOIN master_control.nodes node
  ON node.site_id='$site_id' AND node.node_id='$node_id'
LEFT JOIN master_control.authority_state authority
  ON authority.site_id=node.site_id
LEFT JOIN master_control.worker_mirror_heads head
  ON head.scope='node:' || node.node_id AND head.scope_kind='node'
LEFT JOIN LATERAL (
  SELECT count(*) AS count
    FROM master_control.worker_mirror_snapshots snapshot
   WHERE snapshot.scope=head.scope
     AND snapshot.authority_epoch=head.authority_epoch
     AND snapshot.scope_revision=head.scope_revision
     AND snapshot.execution_epoch=head.execution_epoch
     AND snapshot.source_node_id=head.source_node_id
     AND snapshot.fingerprint=head.fingerprint
     AND jsonb_typeof(snapshot.snapshot)='object') exact_snapshot ON TRUE;"
}

diagnose_master_global() {
  echo '=== Authority-Scope-Head ==='
  run_sql "
SELECT scope,scope_kind,scope_revision,global_revision,
       left(btrim(fingerprint::text),12) AS fingerprint_12,execution_epoch
  FROM master_control.scope_heads
 WHERE owner_node_id='$node_id' ORDER BY scope;"

  echo '=== Letzte Shard-Rollouts ==='
  run_sql "
SELECT rollout_id,scope,state,attempts,coordinator_term,transfer_id,next_attempt_at,
       detail->>'rearm_reason' AS rearm_reason,
       left(coalesce(detail->>'delivery_generation',''),12) AS delivery_generation_12,
       detail->>'previous_rollout_id' AS previous_rollout_id,
       detail->>'code' AS code,
       updated_at
  FROM master_control.shard_rollouts
 WHERE node_id='$node_id' ORDER BY updated_at DESC LIMIT 10;"

  echo '=== Letzte Worker-Outbox-Einträge ==='
  run_sql "
SELECT message_id,event_type,state,attempt_count,last_error_code,
       created_at,updated_at
  FROM master_control.outbox
 WHERE target_service='worker' AND target_instance_id='$node_id'
 ORDER BY updated_at DESC LIMIT 20;"

  echo '=== Zusammenfassende Master-Global-Diagnose ==='
  run_sql "
SELECT CASE
  WHEN node.node_id IS NULL THEN 'FEHLT: Worker ist nicht registriert'
  WHEN head.scope IS NULL THEN 'FEHLER: kein Node-Scope-Head'
  WHEN btrim(coalesce(node.node_replica_fingerprint::text,''))
       =btrim(head.fingerprint::text)
    THEN 'OK: Node-Shard exakt konvergiert (' || node.observed_state || ')'
  WHEN node.node_replica_revision>=head.scope_revision
       AND btrim(coalesce(node.node_replica_fingerprint::text,''))<>''
    THEN 'BLOCKER stale_replica: Worker-Revision ' || node.node_replica_revision
         || ' ist nicht kleiner als Master-Revision ' || head.scope_revision
         || ', Fingerprints weichen ab; nicht automatisch überschreiben'
  WHEN node.node_replica_revision<head.scope_revision
       AND rollout.state IN ('applied','converged','failed')
       AND coalesce(rollout.detail->>'rearm_reason','')
           <>'worker_runtime_replica_recovery'
    THEN 'FEHLER runtime_replay_missing: lokale Replica ist zurückgefallen, '
         || 'aber der terminale Rollout wurde nicht reaktiviert'
  WHEN node.node_replica_revision<head.scope_revision
       AND rollout.state='staged'
       AND coalesce(rollout.detail->>'rearm_reason','')
           ='worker_runtime_replica_recovery'
    THEN 'BEFUND: sicherer Runtime-Recovery-Rollout ist gestaged'
  WHEN node.node_replica_revision<head.scope_revision
       AND rollout.state IN ('sending','retry_wait')
       AND coalesce(rollout.detail->>'rearm_reason','')
           ='worker_runtime_replica_recovery'
    THEN 'BEFUND: sicherer Runtime-Recovery-Rollout ist aktiv ('
         || rollout.state || ')'
  WHEN rollout.rollout_id IS NULL
    THEN 'FEHLER: kein Rollout für den Node-Scope vorhanden'
  WHEN outbox.state='FAILED' AND outbox.last_error_code='stale_replica'
    THEN 'BLOCKER stale_replica: Worker lehnt die ältere Master-Replica korrekt ab'
  WHEN outbox.state='FAILED'
    THEN 'FEHLER: Outbox terminal FAILED ('
         || coalesce(outbox.last_error_code,'ohne Code') || ')'
  WHEN rollout.state IN ('retry_wait','failed')
    THEN 'FEHLER: Rollout ' || rollout.state || ', Detail oben auswerten'
  WHEN outbox.message_id IS NULL
    THEN 'FEHLER: Rollout ohne passende Outbox-Nachricht'
  WHEN outbox.state='SENT' AND outbox.attempt_count>=3
    THEN 'BEFUND: wiederholter Versand ohne Worker-ACK'
  WHEN outbox.state='ACKED'
    THEN 'BEFUND: ACK vorhanden, exakter Replica-Fingerprint fehlt noch'
  ELSE 'UNKLAR: outbox=' || coalesce(outbox.state,'-')
       || ', rollout=' || coalesce(rollout.state,'-')
END AS diagnose
FROM (SELECT 1) AS one
LEFT JOIN master_control.nodes node
  ON node.site_id='$site_id' AND node.node_id='$node_id'
LEFT JOIN master_control.scope_heads head
  ON head.owner_node_id=node.node_id AND head.scope_kind='node'
LEFT JOIN LATERAL (
  SELECT current_rollout.* FROM master_control.shard_rollouts current_rollout
   WHERE current_rollout.node_id=node.node_id
     AND current_rollout.scope=head.scope
   ORDER BY current_rollout.updated_at DESC LIMIT 1) rollout ON TRUE
LEFT JOIN LATERAL (
  SELECT current_outbox.* FROM master_control.outbox current_outbox
   WHERE current_outbox.target_service='worker'
     AND current_outbox.target_instance_id=node.node_id
     AND current_outbox.event_type IN ('distributed.config.apply',
                                       'distributed.config.patch',
                                       'distributed.config.chunk')
     AND (rollout.rollout_id IS NULL
          OR current_outbox.envelope->>'rollout_id'=rollout.rollout_id)
   ORDER BY current_outbox.updated_at DESC LIMIT 1) outbox ON TRUE;"
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

echo "Node-Sync diagnosis: site=$site_id node=$node_id authority=$authority_mode"
echo
diagnose_registry_state
if [[ "$authority_mode" == "node_local" ]]; then
  diagnose_node_local
else
  diagnose_master_global
fi

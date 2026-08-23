#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$root_dir"

for _attempt in $(seq 1 60); do
  count="$(docker compose exec -T -u postgres postgres-primary \
    psql -d automation_platform -Atqc \
    "SELECT count(*) FROM pg_stat_replication WHERE state='streaming';" 2>/dev/null || true)"
  if [[ "${count:-0}" -ge 1 ]]; then
    slot_active="$(docker compose exec -T -u postgres postgres-primary \
      psql -d automation_platform -Atqc \
      "SELECT active FROM pg_replication_slots WHERE slot_name='automation_standby';" \
      2>/dev/null || true)"
    recovery="$(docker compose exec -T -u postgres postgres-standby \
      psql -d automation_platform -Atqc "SELECT pg_is_in_recovery();" \
      2>/dev/null || true)"
    if [[ "$slot_active" == "t" && "$recovery" == "t" ]]; then
      echo "Streaming replication and physical slot are active."
      exit 0
    fi
  fi
  sleep 2
done

echo "Streaming replication did not become active." >&2
exit 1

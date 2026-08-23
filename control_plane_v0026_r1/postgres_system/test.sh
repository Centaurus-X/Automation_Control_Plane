#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"

bash -n setup.sh deploy.sh deploy_test.sh test.sh status.sh logs.sh stop.sh
bash -n scripts/*.sh init/*.sh
docker compose config --quiet
"$root_dir/deploy_test.sh"
"$root_dir/scripts/test_persistence.sh"
"$root_dir/scripts/verify_replication.sh"
"$root_dir/scripts/backup.sh"
"$root_dir/scripts/restore_test.sh"
echo "PostgreSQL deployment, persistence, replication and restore tests passed."

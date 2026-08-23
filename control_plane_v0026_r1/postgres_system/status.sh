#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"
docker compose ps
"$root_dir/scripts/verify_replication.sh"
"$root_dir/scripts/verify_role_credentials.sh"

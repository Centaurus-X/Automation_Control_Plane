#!/usr/bin/env bash

set -Eeuo pipefail
export ALLOW_TEST_POSTGRES_TLS=1
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export COMPOSE_FILE="$root_dir/compose.yaml:$root_dir/compose.test.yaml"
exec "$root_dir/setup.sh" "$@"

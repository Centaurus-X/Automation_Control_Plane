#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"
exec docker compose logs --tail="${LOG_TAIL:-200}" -f mqtt-broker

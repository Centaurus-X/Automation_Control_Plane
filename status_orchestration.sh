#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$root_dir/kubernetes_orchestration/scripts/status.sh" "$@"

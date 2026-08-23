#!/usr/bin/env bash
set -Eeuo pipefail

# Stable bundle-level lifecycle entry point.
component_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$component_root/scripts/logs.sh" "$@"

#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/automation_master_pycache_$(id -u)}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$root_dir/master_node:$root_dir/master_node/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$root_dir/master_node"
exec python3 -m master_main "$@"

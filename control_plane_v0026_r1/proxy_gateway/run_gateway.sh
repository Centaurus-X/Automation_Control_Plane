#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONPYCACHEPREFIX="${PYTHONPYCACHEPREFIX:-/tmp/automation_proxy_pycache_$(id -u)}"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONPATH="$root_dir/proxy_gateway:$root_dir/proxy_gateway/src${PYTHONPATH:+:$PYTHONPATH}"
cd "$root_dir/proxy_gateway"
exec python3 -m gateway_main "$@"

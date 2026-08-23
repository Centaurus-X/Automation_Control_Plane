#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export PYTHONPATH="$root_dir/src${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m automation_fleet.cli --root "$root_dir" "$@"

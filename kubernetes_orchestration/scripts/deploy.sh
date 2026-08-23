#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
"$root_dir/scripts/setup.sh"
"$root_dir/scripts/test.sh"
exec "$root_dir/scripts/start.sh"

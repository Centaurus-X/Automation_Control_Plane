#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
selector="$root_dir/integration_test/CURRENT_E2E"
test -s "$selector" || {
  echo "BLOCKED: active E2E selector is missing: $selector" >&2
  exit 66
}
active_e2e="$(<"$selector")"
[[ "$active_e2e" =~ ^e2e_v[0-9]{4,}$ ]] || {
  echo "BLOCKED: invalid active E2E selector: $active_e2e" >&2
  exit 65
}
exec "$root_dir/integration_test/$active_e2e/run_e2e.sh" "$@"

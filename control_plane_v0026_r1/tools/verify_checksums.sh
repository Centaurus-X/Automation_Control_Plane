#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd -P)"
selector="$root_dir/integration_test/CURRENT_E2E"
test -s "$selector" || {
  echo "ERROR: active E2E selector is missing: $selector" >&2
  exit 66
}
active_e2e="$(<"$selector")"
[[ "$active_e2e" =~ ^e2e_v[0-9]{4,}$ ]] || {
  echo "ERROR: invalid active E2E selector: $active_e2e" >&2
  exit 65
}
test -d "$root_dir/integration_test/$active_e2e" || {
  echo "ERROR: active E2E directory is missing: integration_test/$active_e2e" >&2
  exit 66
}

cd "$root_dir"
sha256sum --check --strict SHA256SUMS

manifest_files="$(mktemp)"
tree_files="$(mktemp)"
cleanup() {
  rm -f -- "$manifest_files" "$tree_files"
}
trap cleanup EXIT

sed -E 's/^[0-9a-f]{64}  //' SHA256SUMS | LC_ALL=C sort >"$manifest_files"
find . \
  \( -path './mqtt_broker/test-pki' \
     -o -path './mqtt_broker/secrets' \
     -o -path './mqtt_broker/runtime-config' \
     -o -path './mqtt_broker/runtime-pki-handoff' \
     -o -path './postgres_system/secrets' \
     -o -path './postgres_system/backup' \
     -o -path './postgres_system/runtime-tls-handoff' \
     -o -path './postgres_system/tls' \
     -o -path './postgres_system/test_ca_private' \
     -o -path './master_node/secrets' \
     -o -path './proxy_gateway/secrets' \
     -o -path './proxy_gateway/test-pki' \
     -o -path './mcp_controller_server/runtime-secrets' \
     -o -path './mcp_controller_server/.test-tmp' \
     -o -path './integration_test/runtime' \
     -o -path "./integration_test/$active_e2e/results" \
     -o -path "./integration_test/$active_e2e/runtime" \
     -o -type d -name '__pycache__' \) -prune \
  -o -type f ! -path './SHA256SUMS' \
     ! -name '*.pyc' ! -name '*.pyo' ! -name '*.log' -print \
  | LC_ALL=C sort >"$tree_files"
if ! diff -u "$manifest_files" "$tree_files"; then
  echo "ERROR: Root SHA256SUMS does not cover the release tree exactly." >&2
  exit 1
fi

for package in mqtt_broker postgres_system master_node proxy_gateway mcp_controller_server; do
  (cd "$root_dir/$package" && sha256sum --check --strict SHA256SUMS)
done
e2e_count=0
while IFS= read -r -d '' e2e_dir; do
  e2e_name="$(basename "$e2e_dir")"
  [[ "$e2e_name" =~ ^e2e_v[0-9]{4,}$ ]] || continue
  test -s "$e2e_dir/VERSION" || {
    echo "ERROR: incomplete E2E package: integration_test/$e2e_name" >&2
    exit 66
  }
  test -s "$e2e_dir/SHA256SUMS" || {
    echo "ERROR: E2E manifest is missing: integration_test/$e2e_name/SHA256SUMS" >&2
    exit 66
  }
  (cd "$e2e_dir" && sha256sum --check --strict SHA256SUMS)
  e2e_count=$((e2e_count + 1))
done < <(find "$root_dir/integration_test" -mindepth 1 -maxdepth 1 -type d \
  -name 'e2e_v[0-9][0-9][0-9][0-9]*' -print0 | LC_ALL=C sort -z)
(( e2e_count > 0 )) || {
  echo "ERROR: no versioned E2E packages were found." >&2
  exit 66
}
echo "Root, component and all $e2e_count E2E SHA-256 manifests are valid and complete."

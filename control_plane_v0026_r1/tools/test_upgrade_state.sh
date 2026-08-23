#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_dir="$(mktemp -d)"
cleanup() {
  find "$temporary_dir" -mindepth 1 -depth -delete 2>/dev/null || true
  rmdir "$temporary_dir" 2>/dev/null || true
}
trap cleanup EXIT

fail() {
  echo "FAIL test_upgrade_state: $*" >&2
  exit 1
}

expect_status() {
  local expected="$1"
  local actual="$2"
  local scenario="$3"
  [[ "$actual" == "$expected" ]] || \
    fail "$scenario returned status $actual; expected $expected"
}

expect_file_equal() {
  local source_file="$1"
  local target_file="$2"
  cmp "$source_file" "$target_file" || \
    fail "imported state differs: $target_file"
}

mkdir "$temporary_dir/bin"
printf '%s\n' \
  '#!/usr/bin/env bash' \
  'if [[ "$1" == "volume" && "$2" == "inspect" ]]; then' \
  '  [[ "${MOCK_DATABASE_VOLUME_PRESENT:-0}" == "1" ]]' \
  '  exit' \
  'fi' \
  'exit 64' \
  > "$temporary_dir/bin/docker"
chmod 0755 "$temporary_dir/bin/docker"

critical_state=(
  postgres_system/secrets/postgres_superuser_password
  postgres_system/secrets/replication_password
  postgres_system/secrets/master_migrator_password
  postgres_system/secrets/master_app_password
  postgres_system/secrets/proxy_migrator_password
  postgres_system/secrets/proxy_app_password
  proxy_gateway/secrets/password_pepper
  proxy_gateway/secrets/token_pepper
  proxy_gateway/secrets/binding_pepper
)

source_state="$temporary_dir/source"
mkdir "$source_state"
for relative in "${critical_state[@]}"; do
  install -d -m 0700 "$(dirname "$source_state/$relative")"
  printf 'state-%s\n' "$relative" > "$source_state/$relative"
  chmod 0600 "$source_state/$relative"
done
install -d -m 0700 "$source_state/mqtt_broker/test-pki/ca"
printf 'test-ca\n' > "$source_state/mqtt_broker/test-pki/ca/ca.crt"
install -d -m 0700 "$source_state/postgres_system/tls"
for name in ca.crt primary.crt primary.key standby.crt standby.key; do
  printf 'tls-state-%s\n' "$name" \
    >"$source_state/postgres_system/tls/$name"
done

fresh_target="$temporary_dir/fresh-target"
mkdir "$fresh_target"
echo "CHECK explicit fresh mode ignores an inherited state-source variable."
PATH="$temporary_dir/bin:$PATH" \
  CONTROL_PLANE_STATE_SOURCE="$source_state" \
  "$root_dir/scripts/import_runtime_state.sh" "" "$fresh_target" >/dev/null
[[ ! -e "$fresh_target/postgres_system/secrets" ]] || \
  fail "explicit empty source imported inherited runtime state"

volume_target="$temporary_dir/volume-target"
mkdir "$volume_target"
echo "CHECK existing database volume without credentials fails closed."
set +e
PATH="$temporary_dir/bin:$PATH" MOCK_DATABASE_VOLUME_PRESENT=1 \
  "$root_dir/scripts/import_runtime_state.sh" "" "$volume_target" \
  >"$temporary_dir/missing-state.log" 2>&1
missing_status="$?"
set -e
expect_status 66 "$missing_status" "missing credential guard"
grep -F 'matching runtime credentials are missing' \
  "$temporary_dir/missing-state.log" >/dev/null || \
  fail "missing credential guard did not explain the failure"

import_target="$temporary_dir/import-target"
mkdir "$import_target"
echo "CHECK upgrade state is copied and can be reused."
PATH="$temporary_dir/bin:$PATH" MOCK_DATABASE_VOLUME_PRESENT=1 \
  "$root_dir/scripts/import_runtime_state.sh" \
  "$source_state" "$import_target" >/dev/null
for relative in "${critical_state[@]}"; do
  expect_file_equal "$source_state/$relative" "$import_target/$relative"
done
expect_file_equal "$source_state/mqtt_broker/test-pki/ca/ca.crt" \
  "$import_target/mqtt_broker/test-pki/ca/ca.crt"
for name in ca.crt primary.crt primary.key standby.crt standby.key; do
  expect_file_equal "$source_state/postgres_system/tls/$name" \
    "$import_target/postgres_system/tls/$name"
done

source "$root_dir/scripts/control_plane_lifecycle.sh"
bind_postgres_upgrade_tls_sources "$source_state" >/dev/null
[[ "$POSTGRES_CA_SOURCE" == "$source_state/postgres_system/tls/ca.crt" ]]
[[ "$POSTGRES_PRIMARY_CERT_SOURCE" \
    == "$source_state/postgres_system/tls/primary.crt" ]]
[[ "$POSTGRES_PRIMARY_KEY_SOURCE" \
    == "$source_state/postgres_system/tls/primary.key" ]]
[[ "$POSTGRES_STANDBY_CERT_SOURCE" \
    == "$source_state/postgres_system/tls/standby.crt" ]]
[[ "$POSTGRES_STANDBY_KEY_SOURCE" \
    == "$source_state/postgres_system/tls/standby.key" ]]

unsafe_state="$temporary_dir/unsafe-state"
install -d -m 0700 "$unsafe_state/postgres_system/tls"
for name in ca.crt primary.crt primary.key standby.crt standby.key; do
  printf 'unsafe-%s\n' "$name" >"$unsafe_state/postgres_system/tls/$name"
done
rm "$unsafe_state/postgres_system/tls/ca.crt"
ln -s "$source_state/postgres_system/tls/ca.crt" \
  "$unsafe_state/postgres_system/tls/ca.crt"
set +e
bind_postgres_upgrade_tls_sources "$unsafe_state" >/dev/null 2>&1
unsafe_status="$?"
set -e
expect_status 65 "$unsafe_status" "unsafe PostgreSQL TLS source guard"

PATH="$temporary_dir/bin:$PATH" MOCK_DATABASE_VOLUME_PRESENT=1 \
  "$root_dir/scripts/import_runtime_state.sh" \
  "" "$import_target" >/dev/null

conflict_target="$temporary_dir/conflict-target"
install -d -m 0700 "$conflict_target/postgres_system/secrets"
printf 'conflict\n' \
  > "$conflict_target/postgres_system/secrets/postgres_superuser_password"
echo "CHECK conflicting target state fails closed."
set +e
PATH="$temporary_dir/bin:$PATH" MOCK_DATABASE_VOLUME_PRESENT=1 \
  "$root_dir/scripts/import_runtime_state.sh" \
  "$source_state" "$conflict_target" >/dev/null 2>&1
conflict_status="$?"
set -e
expect_status 78 "$conflict_status" "conflicting state guard"

echo "PASS fresh-install, state-import, reuse and conflict-fail-closed checks."

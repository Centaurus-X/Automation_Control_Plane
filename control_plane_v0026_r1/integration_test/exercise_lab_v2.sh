#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
runtime_dir="$root_dir/integration_test/runtime"
password_file="$runtime_dir/lab_v2_admin_password"
username_file="$runtime_dir/lab_v2_admin_username"
result_file="$runtime_dir/LAB_V2_PATCH_REQUEST.json"
proxy_host="${PROXY_TEST_HOST:-${PROXY_BIND_ADDRESS:-127.0.0.1}}"
[[ "$proxy_host" != "0.0.0.0" ]] || proxy_host=127.0.0.1

install -d -m 0700 "$runtime_dir"
if [[ ! -s "$password_file" || ! -s "$username_file" ]]; then
  umask 077
  openssl rand -base64 36 > "$password_file"
  printf 'labv2%s%s\n' "$(date +%s)" "$$" > "$username_file"
fi
chmod 0600 "$password_file" "$username_file"
username="$(<"$username_file")"

if [[ ! -e "$runtime_dir/.lab_v2_admin_created" ]]; then
  (cd "$root_dir/proxy_gateway" && \
    docker compose exec -T proxy-gateway \
      python -m admin_cli create-admin \
      --username "$username" --password-file /dev/stdin \
      < "$password_file")
  touch "$runtime_dir/.lab_v2_admin_created"
  chmod 0600 "$runtime_dir/.lab_v2_admin_created"
fi

PYTHONDONTWRITEBYTECODE=1 python3 \
  "$root_dir/integration_test/exercise_lab_v2_api.py" \
  --base-url "https://$proxy_host:${PROXY_HTTPS_PORT:-8444}" \
  --username "$username" \
  --password-file "$password_file" \
  --output "$result_file"

echo "Authenticated Control-Plane commit for all three v2 scopes was accepted."

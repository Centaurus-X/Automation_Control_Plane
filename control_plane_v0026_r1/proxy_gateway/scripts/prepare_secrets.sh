#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
secret_dir="$root_dir/secrets"
postgres_dir="${POSTGRES_SYSTEM_DIR:-$root_dir/../postgres_system}"
install -d -m 0700 "$secret_dir"

install_required() {
  local source="$1"
  local target="$2"
  [[ -s "$source" ]] || { echo "Required secret source is missing: $source" >&2; exit 66; }
  install -m 0600 "$source" "$secret_dir/$target"
}

generate_secret() {
  local target="$1"
  if [[ ! -s "$secret_dir/$target" ]]; then
    umask 077
    openssl rand -base64 48 > "$secret_dir/$target"
  fi
  chmod 0600 "$secret_dir/$target"
}

install_required "$postgres_dir/secrets/proxy_app_password" proxy_db_password
install_required "$postgres_dir/secrets/proxy_migrator_password" proxy_migrator_password
install_required "$postgres_dir/tls/ca.crt" postgres_ca.crt
generate_secret password_pepper
generate_secret token_pepper
generate_secret binding_pepper

if [[ -n "${PROXY_HTTPS_CERT_SOURCE:-}" && -n "${PROXY_HTTPS_KEY_SOURCE:-}" ]]; then
  install_required "$PROXY_HTTPS_CERT_SOURCE" proxy_https.crt
  install_required "$PROXY_HTTPS_KEY_SOURCE" proxy_https.key
  rm -f "$secret_dir/.proxy_https_test_certificate"
elif [[ "${ALLOW_TEST_HTTPS_CERT:-0}" == "1" ]]; then
  "$root_dir/scripts/generate_https_test_cert.sh"
elif [[ ! -s "$secret_dir/proxy_https.crt" || ! -s "$secret_dir/proxy_https.key" ]]; then
  echo "Production HTTPS certificate sources are required." >&2
  exit 66
elif [[ -e "$secret_dir/.proxy_https_test_certificate" \
        && "${ALLOW_TEST_HTTPS_CERT:-0}" != "1" ]]; then
  echo "Refusing a test HTTPS certificate in production mode." >&2
  exit 78
fi

if [[ "${ALLOW_INSECURE_MQTT_TEST:-0}" == "1" ]]; then
  for name in mqtt_ca.crt proxy_mqtt.crt proxy_mqtt.key; do
    if [[ ! -e "$secret_dir/$name" ]]; then
      umask 077
      printf 'test-only-placeholder\n' > "$secret_dir/$name"
    fi
  done
else
  install_required "${MQTT_CA_FILE:-}" mqtt_ca.crt
  install_required "${PROXY_MQTT_CERT_FILE:-}" proxy_mqtt.crt
  install_required "${PROXY_MQTT_KEY_FILE:-}" proxy_mqtt.key
fi
echo "Proxy secret files are ready."

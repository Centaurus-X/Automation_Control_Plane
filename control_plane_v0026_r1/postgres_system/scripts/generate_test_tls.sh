#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
tls_dir="$root_dir/tls"
private_dir="$root_dir/test_ca_private"
install -d -m 0700 "$tls_dir"
install -d -m 0700 "$private_dir"

if [[ ! -s "$private_dir/ca.key" || ! -s "$tls_dir/ca.crt" ]]; then
  openssl genrsa -out "$private_dir/ca.key" 4096
  chmod 0600 "$private_dir/ca.key"
  openssl req -x509 -new -sha256 -days 825 \
    -key "$private_dir/ca.key" \
    -subj "/CN=Automation Test Database CA" \
    -out "$tls_dir/ca.crt"
fi

generate_server() {
  local name="$1"
  local dns="$2"
  local config
  config="$(mktemp)"
  trap 'rm -f "$config"' RETURN
  printf '%s\n' \
    '[req]' \
    'distinguished_name = subject' \
    'req_extensions = extensions' \
    'prompt = no' \
    '[subject]' \
    "CN = $dns" \
    '[extensions]' \
    'subjectAltName = DNS:automation-postgres,DNS:postgres-primary,DNS:postgres-standby,DNS:localhost,IP:127.0.0.1' \
    'extendedKeyUsage = serverAuth' \
    > "$config"
  openssl genrsa -out "$tls_dir/$name.key" 3072
  openssl req -new -key "$tls_dir/$name.key" -config "$config" \
    -out "$tls_dir/$name.csr"
  openssl x509 -req -sha256 -days 397 \
    -in "$tls_dir/$name.csr" \
    -CA "$tls_dir/ca.crt" \
    -CAkey "$private_dir/ca.key" \
    -CAcreateserial \
    -extfile "$config" \
    -extensions extensions \
    -out "$tls_dir/$name.crt"
  chmod 0600 "$tls_dir/$name.key"
  chmod 0644 "$tls_dir/$name.crt"
  rm -f "$tls_dir/$name.csr"
  trap - RETURN
  rm -f "$config"
}

generate_server primary automation-postgres
generate_server standby postgres-standby
openssl verify -CAfile "$tls_dir/ca.crt" "$tls_dir/primary.crt" "$tls_dir/standby.crt"
printf '%s\n' 'test-only' > "$tls_dir/.test_tls_material"
echo "Test database TLS material is ready. Replace it for production."

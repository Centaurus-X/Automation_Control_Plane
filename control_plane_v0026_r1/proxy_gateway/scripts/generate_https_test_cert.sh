#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
secret_dir="$root_dir/secrets"
pki_dir="$root_dir/test-pki/wss"
bind_address="${PROXY_BIND_ADDRESS:-192.168.0.82}"

command -v openssl >/dev/null 2>&1 || {
  echo "OpenSSL is required for the lab Proxy PKI." >&2
  exit 69
}
if [[ ! "$bind_address" =~ ^[0-9A-Fa-f:.]+$ ]]; then
  echo "PROXY_BIND_ADDRESS must be an IP address for lab certificate generation." >&2
  exit 64
fi

install -d -m 0700 "$secret_dir" "$pki_dir"
material=(ca.key ca.crt proxy_wss.key proxy_wss.crt proxy_wss.fullchain.crt manifest.txt)
present=0
for name in "${material[@]}"; do
  [[ -e "$pki_dir/$name" ]] && present=$((present + 1))
done
if (( present > 0 && present < ${#material[@]} )); then
  echo "Partial Proxy lab PKI exists; recover it manually or use a fresh lab rollout." >&2
  exit 78
fi

reuse=0
if (( present == ${#material[@]} )); then
  if openssl verify -CAfile "$pki_dir/ca.crt" "$pki_dir/proxy_wss.crt" >/dev/null 2>&1 \
      && openssl x509 -checkend 86400 -noout -in "$pki_dir/proxy_wss.crt" >/dev/null 2>&1 \
      && openssl x509 -in "$pki_dir/proxy_wss.crt" -noout -ext subjectAltName \
         | grep -F "IP Address:$bind_address" >/dev/null; then
    reuse=1
  fi
fi

if (( reuse == 0 )); then
  stage="$(mktemp -d "$pki_dir/.stage.XXXXXX")"
  cleanup() {
    rm -rf -- "$stage"
  }
  trap cleanup EXIT
  config="$stage/openssl.cnf"
  cat >"$config" <<EOF
[req]
distinguished_name = subject
req_extensions = request_extensions
prompt = no
[subject]
CN = $bind_address
[request_extensions]
subjectAltName = IP:$bind_address,IP:127.0.0.1,DNS:localhost,DNS:proxy-gateway
[server_extensions]
basicConstraints = critical,CA:FALSE
keyUsage = critical,digitalSignature,keyEncipherment
extendedKeyUsage = serverAuth
subjectAltName = IP:$bind_address,IP:127.0.0.1,DNS:localhost,DNS:proxy-gateway
subjectKeyIdentifier = hash
authorityKeyIdentifier = keyid,issuer
EOF
  openssl req -x509 -newkey rsa:3072 -nodes -sha256 -days 365 \
    -subj "/CN=Automation Proxy Lab CA v0017" \
    -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
    -addext "keyUsage=critical,keyCertSign,cRLSign" \
    -keyout "$stage/ca.key" -out "$stage/ca.crt" >/dev/null 2>&1
  openssl req -new -newkey rsa:3072 -nodes -sha256 \
    -keyout "$stage/proxy_wss.key" -out "$stage/proxy_wss.csr" \
    -config "$config" >/dev/null 2>&1
  openssl x509 -req -sha256 -days 30 \
    -in "$stage/proxy_wss.csr" \
    -CA "$stage/ca.crt" -CAkey "$stage/ca.key" -CAcreateserial \
    -extfile "$config" -extensions server_extensions \
    -out "$stage/proxy_wss.crt" >/dev/null 2>&1
  openssl verify -CAfile "$stage/ca.crt" "$stage/proxy_wss.crt" >/dev/null
  openssl x509 -in "$stage/proxy_wss.crt" -noout -checkend 86400 >/dev/null
  cat "$stage/proxy_wss.crt" "$stage/ca.crt" >"$stage/proxy_wss.fullchain.crt"
  printf '%s\n' \
    'schema=automation.proxy.wss.lab-pki.v1' \
    "ip_address=$bind_address" \
    'test_only=true' >"$stage/manifest.txt"
  chmod 0600 "$stage/ca.key" "$stage/proxy_wss.key" "$stage/manifest.txt"
  chmod 0644 "$stage/ca.crt" "$stage/proxy_wss.crt" \
    "$stage/proxy_wss.fullchain.crt"
  for name in "${material[@]}"; do
    mv -f -- "$stage/$name" "$pki_dir/$name"
  done
fi

install -m 0600 "$pki_dir/proxy_wss.key" "$secret_dir/proxy_https.key"
install -m 0644 "$pki_dir/proxy_wss.fullchain.crt" "$secret_dir/proxy_https.crt"
printf '%s\n' 'test-only-ca-signed-v0017' > "$secret_dir/.proxy_https_test_certificate"
chmod 0600 "$secret_dir/.proxy_https_test_certificate"
echo "CA-signed short-lived Proxy lab certificate is ready at test-pki/wss/."

#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
work_dir="${MQTT_PKI_WORK_DIR:-$root_dir/test-pki}"
secret_dir="${MQTT_SECRET_DIR:-$root_dir/secrets}"
broker_ip="${MQTT_TEST_BROKER_IP:-192.168.0.82}"
marker="$work_dir/.test_mqtt_pki"
secret_marker="$secret_dir/.test_mqtt_pki"

create_client() {
  local directory="$1"
  local common_name="$2"
  local serial="$3"
  cat > "$directory/extensions.cnf" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature
extendedKeyUsage=clientAuth
subjectAltName=URI:$common_name
EOF
  openssl req -new -newkey rsa:3072 -sha256 -nodes \
    -subj "/O=Automation Test Only/CN=$common_name" \
    -keyout "$directory/mqtt.key" -out "$directory/mqtt.csr" >/dev/null 2>&1
  openssl x509 -req -sha256 -days 825 \
    -in "$directory/mqtt.csr" \
    -CA "$work_dir/ca/ca.crt" -CAkey "$work_dir/ca/ca.key" -set_serial "$serial" \
    -extfile "$directory/extensions.cnf" \
    -out "$directory/mqtt.crt" >/dev/null 2>&1
}

required_files=(
  "$work_dir/ca/ca.crt"
  "$work_dir/ca/ca.key"
  "$work_dir/server/mqtt_broker.crt"
  "$work_dir/server/mqtt_broker.key"
  "$work_dir/clients/master/mqtt.crt"
  "$work_dir/clients/master/mqtt.key"
  "$work_dir/clients/proxy/mqtt.crt"
  "$work_dir/clients/proxy/mqtt.key"
  "$work_dir/clients/mcp-controller/mqtt.crt"
  "$work_dir/clients/mcp-controller/mqtt.key"
  "$work_dir/clients/worker-fn-01/mqtt.crt"
  "$work_dir/clients/worker-fn-01/mqtt.key"
  "$work_dir/clients/worker-fn-02/mqtt.crt"
  "$work_dir/clients/worker-fn-02/mqtt.key"
  "$work_dir/clients/health/mqtt.crt"
  "$work_dir/clients/health/mqtt.key"
)

is_complete() {
  local path
  for path in "${required_files[@]}"; do
    [[ -s "$path" ]] || return 1
  done
  return 0
}

is_base_complete() {
  local path
  for path in "${required_files[@]}"; do
    case "$path" in
      */clients/mcp-controller/*) continue ;;
    esac
    [[ -s "$path" ]] || return 1
  done
  return 0
}

force_regenerate="${FORCE_TEST_MQTT_PKI:-${FORCE_REGENERATE_TEST_PKI:-0}}"
if [[ -e "$marker" && "$force_regenerate" != "1" ]] \
    && ! is_complete && is_base_complete; then
  install -d -m 0700 "$work_dir/clients/mcp-controller"
  create_client "$work_dir/clients/mcp-controller" \
    "urn:automation:site-main:proxy:mcp-controller-01" 4103
  find "$work_dir/clients/mcp-controller" -type f -name '*.key' -exec chmod 0600 {} +
  find "$work_dir/clients/mcp-controller" -type f -name '*.crt' -exec chmod 0644 {} +
  find "$work_dir/clients/mcp-controller" -type f \
    \( -name '*.csr' -o -name 'extensions.cnf' \) -delete
  MQTT_PKI_WORK_DIR="$work_dir" MQTT_SECRET_DIR="$secret_dir" \
    "$root_dir/scripts/verify_pki.sh"
  echo "Existing MQTT test PKI was extended with the isolated MCP identity."
  exit 0
fi
if [[ -e "$marker" && "$force_regenerate" != "1" ]] && is_complete; then
  if [[ -d "$secret_dir" && ! -e "$secret_marker" ]] \
      && find "$secret_dir" -mindepth 1 -print -quit | grep -q .; then
    echo "Refusing to replace unmarked MQTT runtime secrets: $secret_dir" >&2
    exit 78
  fi
  install -d -m 0700 "$secret_dir"
  install -m 0644 "$work_dir/ca/ca.crt" "$secret_dir/mqtt_ca.crt"
  install -m 0644 "$work_dir/server/mqtt_broker.crt" "$secret_dir/mqtt_broker.crt"
  install -m 0600 "$work_dir/server/mqtt_broker.key" "$secret_dir/mqtt_broker.key"
  install -m 0644 "$work_dir/clients/health/mqtt.crt" "$secret_dir/mqtt_health.crt"
  install -m 0600 "$work_dir/clients/health/mqtt.key" "$secret_dir/mqtt_health.key"
  touch "$secret_marker"
  MQTT_PKI_WORK_DIR="$work_dir" MQTT_SECRET_DIR="$secret_dir" \
    "$root_dir/scripts/verify_pki.sh"
  echo "Existing MQTT test PKI is complete and was reused."
  exit 0
fi

if [[ -d "$work_dir" && ! -e "$marker" ]] \
    && find "$work_dir" -mindepth 1 -print -quit | grep -q .; then
  echo "Refusing to replace an unmarked PKI directory: $work_dir" >&2
  exit 78
fi
if [[ -d "$secret_dir" && ! -e "$secret_marker" ]] \
    && find "$secret_dir" -mindepth 1 -print -quit | grep -q .; then
  echo "Refusing to replace unmarked MQTT runtime secrets: $secret_dir" >&2
  exit 78
fi

if [[ -e "$marker" ]]; then
  find "$work_dir" -mindepth 1 -depth -delete
fi
if [[ -e "$secret_marker" ]]; then
  find "$secret_dir" -mindepth 1 -depth -delete
fi

umask 077
install -d -m 0700 \
  "$work_dir/ca" "$work_dir/server" \
  "$work_dir/clients/master" "$work_dir/clients/proxy" \
  "$work_dir/clients/mcp-controller" \
  "$work_dir/clients/worker-fn-01" "$work_dir/clients/worker-fn-02" \
  "$work_dir/clients/health" \
  "$secret_dir"

openssl req -x509 -newkey rsa:3072 -sha256 -nodes -days 3650 \
  -subj "/O=Automation Test Only/CN=Automation MQTT Test CA" \
  -addext "basicConstraints=critical,CA:TRUE,pathlen:0" \
  -addext "keyUsage=critical,keyCertSign,cRLSign" \
  -keyout "$work_dir/ca/ca.key" -out "$work_dir/ca/ca.crt" >/dev/null 2>&1

cat > "$work_dir/server/extensions.cnf" <<EOF
basicConstraints=critical,CA:FALSE
keyUsage=critical,digitalSignature,keyEncipherment
extendedKeyUsage=serverAuth
subjectAltName=DNS:mqtt-broker,DNS:automation-mqtt,DNS:localhost,IP:127.0.0.1,IP:$broker_ip
EOF
openssl req -new -newkey rsa:3072 -sha256 -nodes \
  -subj "/O=Automation Test Only/CN=mqtt-broker" \
  -keyout "$work_dir/server/mqtt_broker.key" \
  -out "$work_dir/server/mqtt_broker.csr" >/dev/null 2>&1
openssl x509 -req -sha256 -days 825 \
  -in "$work_dir/server/mqtt_broker.csr" \
  -CA "$work_dir/ca/ca.crt" -CAkey "$work_dir/ca/ca.key" -set_serial 4097 \
  -extfile "$work_dir/server/extensions.cnf" \
  -out "$work_dir/server/mqtt_broker.crt" >/dev/null 2>&1

create_client "$work_dir/clients/master" \
  "urn:automation:site-main:master:master-01" 4098
create_client "$work_dir/clients/proxy" \
  "urn:automation:site-main:proxy:proxy-01" 4099
create_client "$work_dir/clients/mcp-controller" \
  "urn:automation:site-main:proxy:mcp-controller-01" 4103
create_client "$work_dir/clients/worker-fn-01" \
  "urn:automation:site-main:worker:fn-01" 4100
create_client "$work_dir/clients/worker-fn-02" \
  "urn:automation:site-main:worker:fn-02" 4102
create_client "$work_dir/clients/health" \
  "urn:automation:site-main:monitor:mqtt-health" 4101

find "$work_dir" -type f -name '*.key' -exec chmod 0600 {} +
find "$work_dir" -type f -name '*.crt' -exec chmod 0644 {} +
find "$work_dir" -type f \( -name '*.csr' -o -name 'extensions.cnf' \) -delete

install -m 0644 "$work_dir/ca/ca.crt" "$secret_dir/mqtt_ca.crt"
install -m 0644 "$work_dir/server/mqtt_broker.crt" "$secret_dir/mqtt_broker.crt"
install -m 0600 "$work_dir/server/mqtt_broker.key" "$secret_dir/mqtt_broker.key"
install -m 0644 "$work_dir/clients/health/mqtt.crt" "$secret_dir/mqtt_health.crt"
install -m 0600 "$work_dir/clients/health/mqtt.key" "$secret_dir/mqtt_health.key"
touch "$marker" "$secret_marker"
chmod 0600 "$marker" "$secret_marker"

MQTT_PKI_WORK_DIR="$work_dir" MQTT_SECRET_DIR="$secret_dir" \
  "$root_dir/scripts/verify_pki.sh"
echo "MQTT test PKI was generated. It is not production trust material."

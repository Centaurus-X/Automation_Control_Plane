#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_dir="$(mktemp -d)"
cleanup() {
  find "$temporary_dir" -mindepth 1 -depth -delete 2>/dev/null || true
  rmdir "$temporary_dir" 2>/dev/null || true
}
trap cleanup EXIT

cmp "$root_dir/mqtt_broker/scripts/prepare_compose_secrets.sh" \
  "$root_dir/postgres_system/scripts/prepare_compose_secrets.sh"
cmp "$root_dir/mqtt_broker/scripts/prepare_compose_secrets.sh" \
  "$root_dir/master_node/scripts/prepare_compose_secrets.sh"
cmp "$root_dir/mqtt_broker/scripts/prepare_compose_secrets.sh" \
  "$root_dir/proxy_gateway/scripts/prepare_compose_secrets.sh"

mkdir "$temporary_dir/secrets" "$temporary_dir/bin"
printf 'test-secret\n' > "$temporary_dir/secrets/example"
chmod 0640 "$temporary_dir/secrets/example"

printf '%s\n' \
  '#!/usr/bin/env bash' \
  'set -Eeuo pipefail' \
  'if [[ "$1" == "image" && "$2" == "inspect" ]]; then exit 0; fi' \
  'if [[ "$1" == "run" ]]; then exit 0; fi' \
  'if [[ "$1" == "compose" && "$2" == "version" ]]; then exit 0; fi' \
  'if [[ "$1" == "info" ]]; then exit 0; fi' \
  'if [[ "$1" == "compose" && "$2" == "ps" ]]; then exit 0; fi' \
  'exit 0' \
  > "$temporary_dir/bin/docker"
chmod 0755 "$temporary_dir/bin/docker"

PATH="$temporary_dir/bin:$PATH" \
  "$root_dir/mqtt_broker/scripts/prepare_compose_secrets.sh" \
  "$temporary_dir/secrets" "$(id -g)" test-helper:local >/dev/null

start_seconds="$SECONDS"
set +e
PATH="$temporary_dir/bin:$PATH" CONTROL_PLANE_HEALTH_ATTEMPTS=45 \
  "$root_dir/test_running_all.sh" >"$temporary_dir/test-running.log" 2>&1
running_status="$?"
set -e
elapsed_seconds="$((SECONDS - start_seconds))"

[[ "$running_status" == "69" ]]
(( elapsed_seconds < 5 ))
grep -F "mqtt_broker/mqtt-broker is missing" \
  "$temporary_dir/test-running.log" >/dev/null

grep -F 'verify_publish_denied worker proxy master' \
  "$root_dir/mqtt_broker/test.sh" >/dev/null
grep -F 'verify_publish_denied proxy worker master' \
  "$root_dir/mqtt_broker/test.sh" >/dev/null
grep -F 'verify_subscription_denied proxy worker' \
  "$root_dir/mqtt_broker/test.sh" >/dev/null
grep -F 'events/security-$$/master-01' \
  "$root_dir/mqtt_broker/test.sh" >/dev/null
if grep -F 'events/security-$$/fn-02' \
    "$root_dir/mqtt_broker/test.sh" >/dev/null; then
  echo "MQTT ACL probe still contradicts the Proxy Master-only event ACL." >&2
  exit 1
fi
if grep -F 'expect_denied run_client proxy mosquitto_pub' \
    "$root_dir/mqtt_broker/test.sh" >/dev/null; then
  echo "MQTT publish denials must not depend on mosquitto_pub exit status." >&2
  exit 1
fi

# Exercise the ACL oracle without Docker. The mock deliberately returns status
# 0 for the denied publisher, matching the target-host failure that motivated
# the patch. One run suppresses the payload; the second delivers it and must be
# detected as an ACL violation.
acl_mock_dir="$temporary_dir/acl-mock"
mkdir "$acl_mock_dir"
run_client() {
  local identity="$1"
  local program="$2"
  shift 2
  local payload=""
  local null_payload=0
  local topic=""
  while (( $# > 0 )); do
    case "$1" in
      -m) payload="$2"; shift 2 ;;
      -n) null_payload=1; shift ;;
      -t) topic="$2"; shift 2 ;;
      *) shift ;;
    esac
  done

  if [[ "$program" == "mosquitto_sub" ]]; then
    if [[ "$identity" == "proxy" && "$topic" == */fn-01 ]]; then
      if [[ "${MQTT_ACL_MOCK_DELIVER_DENIED_SUBSCRIPTION:-0}" != "1" ]]; then
        return 1
      fi
      [[ -s "$acl_mock_dir/retained" ]] || return 1
      sed -n '1p' "$acl_mock_dir/retained"
      return 0
    fi
    for _attempt in $(seq 1 100); do
      [[ -s "$acl_mock_dir/ready" ]] && break
      sleep 0.02
    done
    [[ -s "$acl_mock_dir/ready" ]] || return 1
    sed -n '1p' "$acl_mock_dir/ready"
    for _attempt in $(seq 1 100); do
      [[ -s "$acl_mock_dir/messages" ]] && break
      sleep 0.02
    done
    [[ -s "$acl_mock_dir/messages" ]] || return 1
    sed -n '1p' "$acl_mock_dir/messages"
    return 0
  fi

  if (( null_payload == 1 )); then
    find "$acl_mock_dir" -type f -delete
  elif [[ "$payload" == acl-ready-* ]]; then
    printf '%s\n' "$payload" > "$acl_mock_dir/ready"
  elif [[ "$payload" == acl-read-denied-* ]]; then
    printf '%s\n' "$payload" > "$acl_mock_dir/retained"
  elif [[ "$identity" == "master" \
      || "${MQTT_ACL_MOCK_DELIVER_DENIED:-0}" == "1" ]]; then
    printf '%s\n' "$payload" >> "$acl_mock_dir/messages"
  fi
  return 0
}
source "$root_dir/mqtt_broker/scripts/mqtt_acl_probe.sh"

verify_publish_denied worker proxy master \
  automation/v1/site-main/nodes/fn-01/commands/test/mock >/dev/null
verify_publish_denied proxy worker master \
  automation/v1/site-main/events/security/mock/master-01 >/dev/null
if MQTT_ACL_MOCK_DELIVER_DENIED=1 \
    verify_publish_denied worker proxy master \
      automation/v1/site-main/nodes/fn-01/commands/test/mock \
      >"$temporary_dir/acl-violation.log" 2>&1; then
  echo "MQTT ACL oracle accepted a delivered unauthorized payload." >&2
  exit 1
fi
grep -F 'MQTT ACL violation' "$temporary_dir/acl-violation.log" >/dev/null

verify_subscription_denied proxy worker \
  automation/v1/site-main/events/sensor-value/fn-01 >/dev/null
if MQTT_ACL_MOCK_DELIVER_DENIED_SUBSCRIPTION=1 \
    verify_subscription_denied proxy worker \
      automation/v1/site-main/events/sensor-value/fn-01 \
      >"$temporary_dir/acl-read-violation.log" 2>&1; then
  echo "MQTT ACL read oracle accepted an unauthorized delivered payload." >&2
  exit 1
fi
grep -F 'MQTT ACL violation: unauthorized subscriber received' \
  "$temporary_dir/acl-read-violation.log" >/dev/null

echo "PASS secret-helper, MQTT ACL oracle and missing-container fail-fast checks."

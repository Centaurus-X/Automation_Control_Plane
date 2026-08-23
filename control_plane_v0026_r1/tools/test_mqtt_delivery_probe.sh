#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
source "$root_dir/mqtt_broker/scripts/mqtt_delivery_probe.sh"

fail() {
  echo "MQTT delivery-probe regression failed: $1" >&2
  exit 1
}

scratch="$(mktemp -d)"
cleanup() {
  if [[ -d "$scratch" ]]; then
    find "$scratch" -depth -delete
  fi
}
trap cleanup EXIT

payload_file="$scratch/payload"
publish_count_file="$scratch/publish-count"
fake_mode="success"

reset_fake_state() {
  find "$payload_file" "$publish_count_file" -maxdepth 0 -delete 2>/dev/null \
    || true
}

run_client() {
  local _identity="$1"
  local program="$2"
  local argument=""
  local previous_argument=""
  local subscriber_timeout_s=""
  local publish_count=0
  shift 2

  if [[ "$program" == "mosquitto_sub" ]]; then
    for argument in "$@"; do
      if [[ "$previous_argument" == "-W" ]]; then
        subscriber_timeout_s="$argument"
        break
      fi
      previous_argument="$argument"
    done
    case "$fake_mode" in
      success)
        for _attempt in $(seq 1 300); do
          if [[ -s "$payload_file" ]]; then
            echo "Client fake received SUBACK"
            sed -n '1p' "$payload_file"
            return 0
          fi
          sleep 0.01
        done
        return 1
        ;;
      delayed_readiness)
        for _attempt in $(seq 1 600); do
          publish_count=0
          if [[ -s "$publish_count_file" ]]; then
            read -r publish_count <"$publish_count_file"
          fi
          if (( publish_count >= 2 )) && [[ -s "$payload_file" ]]; then
            echo "Client fake received SUBACK"
            sed -n '1p' "$payload_file"
            return 0
          fi
          sleep 0.01
        done
        return 1
        ;;
      delayed_publisher_startup)
        [[ "$subscriber_timeout_s" =~ ^[0-9]+$ ]] \
          && (( subscriber_timeout_s >= 90 )) || return 1
        for _attempt in $(seq 1 500); do
          if [[ -s "$payload_file" ]]; then
            echo "Client fake received SUBACK"
            sed -n '1p' "$payload_file"
            return 0
          fi
          sleep 0.01
        done
        return 1
        ;;
      no_readiness)
        for _attempt in $(seq 1 300); do
          if [[ -s "$payload_file" ]]; then
            sed -n '1p' "$payload_file"
            return 0
          fi
          sleep 0.01
        done
        return 1
        ;;
      no_delivery)
        echo "Client fake received SUBACK"
        echo '{"event_type":"live-without-probe"}'
        return 0
        ;;
      *)
        return 64
        ;;
    esac
  fi

  if [[ "$program" == "mosquitto_pub" ]]; then
    if [[ -s "$publish_count_file" ]]; then
      read -r publish_count <"$publish_count_file"
    fi
    publish_count=$((publish_count + 1))
    printf '%s\n' "$publish_count" >"$publish_count_file"
    if [[ "$fake_mode" == "delayed_publisher_startup" ]]; then
      sleep 1.2
    fi
    if [[ "$fake_mode" == "success" || "$fake_mode" == "delayed_readiness" \
        || "$fake_mode" == "delayed_publisher_startup" \
        || "$fake_mode" == "no_readiness" ]]; then
      printf '%s\n' "${@: -1}" >"$payload_file"
    fi
    return 0
  fi
  return 64
}

verify_exact_mqtt_delivery worker master \
  automation/v1/site-main/events/sensor_value_update/master-01 \
  exact-regression-payload || \
  fail "pipe-buffered SUBACK hid the exact probe payload"
[[ "$(<"$publish_count_file")" == "1" ]] \
  || fail "the immediate path used more than one challenge"

reset_fake_state
fake_mode="delayed_readiness"
verify_exact_mqtt_delivery worker master \
  automation/v1/site-main/events/sensor_value_update/master-01 \
  delayed-readiness-regression-payload || \
  fail "bounded SUBACK challenge convergence did not retry"
[[ "$(<"$publish_count_file")" == "2" ]] \
  || fail "delayed readiness did not converge on the second challenge"

reset_fake_state
fake_mode="delayed_publisher_startup"
verify_exact_mqtt_delivery worker master \
  automation/v1/site-main/events/sensor_value_update/master-01 \
  delayed-publisher-regression-payload || \
  fail "subscriber lifetime did not cover bounded publisher-container startup"
[[ "$(<"$publish_count_file")" == "1" ]] \
  || fail "delayed publisher startup caused an unnecessary retry"

reset_fake_state
fake_mode="no_readiness"
if verify_exact_mqtt_delivery worker master \
    automation/v1/site-main/events/sensor_value_update/master-01 \
    rejected-without-readiness; then
  fail "an exact payload without SUBACK evidence was accepted"
fi

reset_fake_state
fake_mode="no_delivery"
if verify_exact_mqtt_delivery worker master \
    automation/v1/site-main/events/sensor_value_update/master-01 \
    rejected-without-delivery; then
  fail "unrelated live traffic was accepted as the probe payload"
fi

echo "MQTT delivery-probe regression passed."

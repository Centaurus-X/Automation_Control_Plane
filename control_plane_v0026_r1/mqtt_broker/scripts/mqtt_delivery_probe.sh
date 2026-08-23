#!/usr/bin/env bash

# This file is sourced after test.sh defines run_client.
# A fixed delay is not a subscription-readiness proof, and this topic also
# carries live production traffic. mosquitto_sub diagnostics are pipe-buffered
# while the Worker is intentionally stopped during rollout, so SUBACK text alone
# is not an observable readiness boundary. A bounded unique challenge publish
# makes the client flush its SUBACK and payload together. Exact reception proves
# both subscription readiness and end-to-end delivery without accepting
# unrelated production traffic.

verify_exact_mqtt_delivery() (
  set +e
  local subscriber_identity="$1"
  local publisher_identity="$2"
  local topic="$3"
  local payload="$4"
  local temporary_directory=""
  local subscriber_output=""
  local subscriber_error=""
  local subscriber_pid=""
  local delivered=""
  local convergence_attempts=3
  local observation_attempts=20
  local subscriber_timeout_s=120

  if ! declare -F run_client >/dev/null 2>&1; then
    echo "MQTT delivery probe requires run_client." >&2
    exit 1
  fi
  temporary_directory="$(mktemp -d)" || exit 1
  subscriber_output="$temporary_directory/subscriber.out"
  subscriber_error="$temporary_directory/subscriber.err"

  cleanup_delivery_probe() {
    if [[ -n "$subscriber_pid" ]] \
      && kill -0 "$subscriber_pid" 2>/dev/null; then
      kill "$subscriber_pid" 2>/dev/null || true
      wait "$subscriber_pid" 2>/dev/null || true
    fi
    find "$temporary_directory" -mindepth 1 -delete 2>/dev/null || true
    rmdir "$temporary_directory" 2>/dev/null || true
  }
  trap cleanup_delivery_probe EXIT

  run_client "$subscriber_identity" mosquitto_sub -d -q 1 \
    -t "$topic" -W "$subscriber_timeout_s" </dev/null \
    >"$subscriber_output" 2>"$subscriber_error" &
  subscriber_pid="$!"

  for _publish_attempt in $(seq 1 "$convergence_attempts"); do
    if ! run_client "$publisher_identity" mosquitto_pub -q 1 \
        -t "$topic" -m "$payload" >/dev/null; then
      echo "MQTT delivery publisher failed." >&2
      exit 1
    fi
    for _attempt in $(seq 1 "$observation_attempts"); do
      if grep -E 'received SUBACK|^Subscribed \(mid:' \
          "$subscriber_output" >/dev/null 2>&1 \
        && grep -Fx "$payload" "$subscriber_output" >/dev/null 2>&1; then
        delivered=1
        break 2
      fi
      kill -0 "$subscriber_pid" 2>/dev/null || break 2
      sleep 0.1
    done
  done
  if [[ "$delivered" != "1" ]]; then
    echo "MQTT delivery probe did not converge after bounded SUBACK challenges." >&2
    sed -n '1,20p' "$subscriber_output" >&2
    sed -n '1,20p' "$subscriber_error" >&2
    exit 1
  fi
  if ! grep -E 'received SUBACK|^Subscribed \(mid:' \
      "$subscriber_output" >/dev/null 2>&1; then
    echo "MQTT delivery probe received data without SUBACK evidence." >&2
    exit 1
  fi
  if ! grep -Fx "$payload" "$subscriber_output" >/dev/null 2>&1; then
    echo "MQTT delivery probe did not receive its unique payload." >&2
    exit 1
  fi
  echo "Verified exact MQTT delivery through bounded SUBACK convergence."
)

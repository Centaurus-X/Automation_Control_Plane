#!/usr/bin/env bash

# A denied MQTT v5 QoS-1 PUBLISH is broker-side policy. Depending on the
# mosquitto_pub version, the client may still exit with status 0 after receiving
# the negative PUBACK. Verify the security property at the subscriber instead:
# an authenticated reader must receive the retained readiness marker followed
# by the authorized control message, never the unauthorized payload.
verify_publish_denied() (
  set +e
  local subscriber_identity="$1"
  local denied_publisher_identity="$2"
  local allowed_publisher_identity="$3"
  local topic="$4"
  local run_id="$$-$(date +%s%N)"
  local ready_payload="acl-ready-$run_id"
  local denied_payload="acl-denied-$run_id"
  local allowed_payload="acl-allowed-$run_id"
  local temporary_directory=""
  local subscriber_output=""
  local subscriber_error=""
  local subscriber_pid=""
  local ready=""
  local subscriber_status=0
  local result=0
  local -a received=()

  temporary_directory="$(mktemp -d)" || exit 1
  subscriber_output="$temporary_directory/subscriber.out"
  subscriber_error="$temporary_directory/subscriber.err"

  cleanup_acl_probe() {
    if [[ -n "$subscriber_pid" ]] && kill -0 "$subscriber_pid" 2>/dev/null; then
      kill "$subscriber_pid" 2>/dev/null || true
      wait "$subscriber_pid" 2>/dev/null || true
    fi
    run_client "$allowed_publisher_identity" mosquitto_pub -q 1 -r -n \
      -t "$topic" >/dev/null 2>&1 || true
    find "$temporary_directory" -mindepth 1 -delete 2>/dev/null || true
    rmdir "$temporary_directory" 2>/dev/null || true
  }
  trap cleanup_acl_probe EXIT

  if ! run_client "$allowed_publisher_identity" mosquitto_pub -q 1 -r \
      -t "$topic" -m "$ready_payload" >/dev/null; then
    echo "Could not publish retained ACL readiness marker on $topic." >&2
    exit 1
  fi

  run_client "$subscriber_identity" mosquitto_sub -q 1 -t "$topic" \
    -C 2 -W 12 >"$subscriber_output" 2>"$subscriber_error" &
  subscriber_pid="$!"

  for _attempt in $(seq 1 50); do
    if grep -Fx "$ready_payload" "$subscriber_output" >/dev/null 2>&1; then
      ready=1
      break
    fi
    kill -0 "$subscriber_pid" 2>/dev/null || break
    sleep 0.1
  done
  if [[ "$ready" != "1" ]]; then
    echo "Authorized MQTT subscriber did not receive its ACL readiness marker." >&2
    sed -n '1,20p' "$subscriber_error" >&2
    exit 1
  fi

  # The publisher exit status is deliberately not used as the deny oracle.
  run_client "$denied_publisher_identity" mosquitto_pub -q 1 \
    -t "$topic" -m "$denied_payload" >/dev/null 2>&1
  run_client "$allowed_publisher_identity" mosquitto_pub -q 1 \
    -t "$topic" -m "$allowed_payload" >/dev/null 2>&1

  wait "$subscriber_pid"
  subscriber_status="$?"
  subscriber_pid=""
  mapfile -t received <"$subscriber_output"

  if (( subscriber_status != 0 )); then
    echo "Authorized MQTT subscriber failed during the ACL deny probe." >&2
    sed -n '1,20p' "$subscriber_error" >&2
    result=1
  elif (( ${#received[@]} != 2 )); then
    echo "ACL deny probe received ${#received[@]} messages instead of 2." >&2
    result=1
  elif [[ "${received[0]}" != "$ready_payload" ]]; then
    echo "ACL deny probe did not receive the expected readiness marker first." >&2
    result=1
  elif [[ "${received[1]}" == "$denied_payload" ]]; then
    echo "MQTT ACL violation: unauthorized payload was delivered on $topic." >&2
    result=1
  elif [[ "${received[1]}" != "$allowed_payload" ]]; then
    echo "ACL deny probe received an unexpected control payload on $topic." >&2
    result=1
  fi

  (( result == 0 )) && \
    echo "Verified broker-side MQTT publish denial for $denied_publisher_identity."
  exit "$result"
)

# Prove that an authenticated identity cannot read a retained payload outside
# its ACL namespace. MQTT v5 brokers/clients may report a denied subscription
# either through a negative SUBACK or by withholding matching publications, so
# delivery of the unique marker is the security oracle, not one particular
# mosquitto_sub exit code.
verify_subscription_denied() (
  set +e
  local denied_subscriber_identity="$1"
  local allowed_publisher_identity="$2"
  local topic="$3"
  local run_id="$$-$(date +%s%N)"
  local marker="acl-read-denied-$run_id"
  local temporary_directory=""
  local subscriber_output=""
  local subscriber_error=""
  local subscriber_status=0
  local result=0

  temporary_directory="$(mktemp -d)" || exit 1
  subscriber_output="$temporary_directory/subscriber.out"
  subscriber_error="$temporary_directory/subscriber.err"

  cleanup_subscription_probe() {
    run_client "$allowed_publisher_identity" mosquitto_pub -q 1 -r -n \
      -t "$topic" >/dev/null 2>&1 || true
    find "$temporary_directory" -mindepth 1 -delete 2>/dev/null || true
    rmdir "$temporary_directory" 2>/dev/null || true
  }
  trap cleanup_subscription_probe EXIT

  if ! run_client "$allowed_publisher_identity" mosquitto_pub -q 1 -r \
      -t "$topic" -m "$marker" >/dev/null; then
    echo "Could not publish retained ACL read-denial marker on $topic." >&2
    exit 1
  fi

  run_client "$denied_subscriber_identity" mosquitto_sub -q 1 -t "$topic" \
    -C 1 -W 3 >"$subscriber_output" 2>"$subscriber_error"
  subscriber_status="$?"

  if grep -Fx "$marker" "$subscriber_output" >/dev/null 2>&1; then
    echo "MQTT ACL violation: unauthorized subscriber received $topic." >&2
    result=1
  elif (( subscriber_status == 0 )); then
    echo "MQTT ACL read-denial probe ended successfully without its marker." >&2
    sed -n '1,20p' "$subscriber_output" >&2
    sed -n '1,20p' "$subscriber_error" >&2
    result=1
  fi

  (( result == 0 )) && \
    echo "Verified MQTT subscription denial for $denied_subscriber_identity."
  exit "$result"
)

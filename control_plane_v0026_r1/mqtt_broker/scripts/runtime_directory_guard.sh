#!/usr/bin/env bash

# This file is sourced by handoff_live_pki.sh. Keep the checks fail-closed:
# only the already verified active broker directory may be converged in place.

mqtt_same_physical_directory() {
  local left="$1"
  local right="$2"
  local left_real
  local right_real

  [[ -d "$left" && ! -L "$left" && -d "$right" && ! -L "$right" ]] || \
    return 1
  left_real="$(cd -- "$left" 2>/dev/null && pwd -P)" || return 1
  right_real="$(cd -- "$right" 2>/dev/null && pwd -P)" || return 1
  [[ "$left_real" == "$right_real" ]]
}

mqtt_allow_verified_active_secret_destination() {
  local destination="$1"
  local active_secret_dir="$2"
  local expected_destination="$3"
  local profile="$4"
  local required
  local entry
  local name

  [[ "$profile" == "test" || "$profile" == "production" ]] || return 1
  [[ "$destination" == "$expected_destination" ]] || return 1
  mqtt_same_physical_directory "$destination" "$active_secret_dir" || return 1

  for required in \
      mqtt_ca.crt \
      mqtt_broker.crt \
      mqtt_broker.key \
      mqtt_health.crt \
      mqtt_health.key; do
    [[ -f "$destination/$required" \
      && ! -L "$destination/$required" \
      && -s "$destination/$required" ]] || return 1
  done

  while IFS= read -r -d '' entry; do
    [[ -f "$entry" && ! -L "$entry" ]] || return 1
    name="$(basename -- "$entry")"
    case "$name" in
      mqtt_ca.crt|mqtt_broker.crt|mqtt_broker.key|mqtt_health.crt|mqtt_health.key)
        ;;
      .test_mqtt_pki|.live_mqtt_pki_handoff)
        ;;
      *)
        return 1
        ;;
    esac
  done < <(find "$destination" -mindepth 1 -maxdepth 1 -print0)

  if [[ "$profile" == "test" ]]; then
    [[ -f "$destination/.test_mqtt_pki" \
      && ! -L "$destination/.test_mqtt_pki" ]] || return 1
  else
    [[ ! -e "$destination/.test_mqtt_pki" ]] || return 1
  fi
}

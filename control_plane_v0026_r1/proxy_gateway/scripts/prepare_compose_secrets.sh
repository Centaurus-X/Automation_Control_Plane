#!/usr/bin/env bash

set -Eeuo pipefail

secret_dir="${1:?Usage: prepare_compose_secrets.sh SECRET_DIRECTORY RUNTIME_GID HELPER_IMAGE}"
runtime_gid="${2:?Usage: prepare_compose_secrets.sh SECRET_DIRECTORY RUNTIME_GID HELPER_IMAGE}"
helper_image="${3:?Usage: prepare_compose_secrets.sh SECRET_DIRECTORY RUNTIME_GID HELPER_IMAGE}"

[[ -d "$secret_dir" ]] || {
  echo "Secret directory does not exist: $secret_dir" >&2
  exit 66
}
[[ "$runtime_gid" =~ ^[0-9]+$ ]] || {
  echo "Runtime GID must be numeric: $runtime_gid" >&2
  exit 64
}
command -v docker >/dev/null 2>&1 || { echo "docker is required" >&2; exit 69; }
docker image inspect "$helper_image" >/dev/null 2>&1 || {
  echo "Secret-permission helper image is unavailable: $helper_image" >&2
  exit 69
}

secret_dir="$(cd "$secret_dir" && pwd)"
owner_uid="${COMPOSE_SECRET_OWNER_UID:-$(id -u)}"

docker run --rm \
  --network none \
  --read-only \
  --user 0:0 \
  --cap-drop ALL \
  --cap-add CHOWN \
  --cap-add FOWNER \
  --cap-add DAC_OVERRIDE \
  --entrypoint /bin/sh \
  --volume "$secret_dir:/secrets" \
  "$helper_image" \
  -eu -c '
    owner_uid="$1"
    runtime_gid="$2"
    found=0
    for secret_file in /secrets/*; do
      [ -f "$secret_file" ] || continue
      found=1
      chown "${owner_uid}:${runtime_gid}" "$secret_file"
      chmod 0640 "$secret_file"
    done
    [ "$found" -eq 1 ]
  ' compose-secret-permissions "$owner_uid" "$runtime_gid"

while IFS= read -r -d '' secret_file; do
  [[ "$(stat -c '%u' "$secret_file")" == "$owner_uid" ]]
  [[ "$(stat -c '%g' "$secret_file")" == "$runtime_gid" ]]
  [[ "$(stat -c '%a' "$secret_file")" == "640" ]]
done < <(find "$secret_dir" -maxdepth 1 -type f ! -name '.*' -print0)

echo "Compose secrets are readable only by host UID $owner_uid and runtime GID $runtime_gid."

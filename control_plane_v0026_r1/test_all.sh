#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$root_dir/test_static_all.sh"
if [[ "${RUN_DEPLOYMENT_TEST:-0}" == "1" ]]; then
  exec "$root_dir/deploy_test_lab.sh" --fresh --profile lab_v2
fi
if command -v docker >/dev/null 2>&1; then
  "$root_dir/master_node/test_container.sh"
  "$root_dir/proxy_gateway/test_container.sh"
  (cd "$root_dir/postgres_system" && docker compose build --pull)
  (cd "$root_dir/mcp_controller_server" && docker compose build --pull)
  docker pull "${MOSQUITTO_IMAGE:-eclipse-mosquitto:2.1.2-alpine}"
  echo "All static, functional and release-image build tests passed."
else
  echo "Static and functional tests passed; Docker image/live tests require a Docker host."
fi

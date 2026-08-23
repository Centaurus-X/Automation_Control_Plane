#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$root_dir/scripts/control_plane_lifecycle.sh"
require_control_plane_tools
print_control_plane_container_status
echo
echo "MQTT Broker"
(cd "$root_dir/mqtt_broker" && docker compose ps) || echo "not initialized"
echo
echo "PostgreSQL"
(cd "$root_dir/postgres_system" && docker compose ps) || echo "not initialized"
echo
echo "Master"
(cd "$root_dir/master_node" && docker compose ps) || echo "not initialized"
echo
echo "Proxy"
(cd "$root_dir/proxy_gateway" && docker compose ps) || echo "not initialized"

echo
echo "MCP Controller Server"
(cd "$root_dir/mcp_controller_server" && docker compose ps) || echo "not initialized"

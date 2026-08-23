#!/usr/bin/env bash
set -Eeuo pipefail

# Deploy with externally supplied production trust and authentication material.
component_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export MCP_MQTT_SOURCE_INSTANCE_ID="${MCP_MQTT_SOURCE_INSTANCE_ID:-mcp-controller-01}"
exec "$component_root/scripts/deploy.sh" "$@"

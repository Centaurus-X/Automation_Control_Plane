#!/usr/bin/env bash
set -Eeuo pipefail

# Bind the integrated MCP component to the generated laboratory trust domain.
component_root="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
bundle_root="$(cd "$component_root/.." && pwd)"
pki_root="${LAB_PKI_DIR:-$bundle_root/mqtt_broker/test-pki}"
export AUTOMATION_SITE_ID="${AUTOMATION_SITE_ID:-site-main}"
export MASTER_INSTANCE_ID="${MASTER_INSTANCE_ID:-master-01}"
export MCP_MQTT_SOURCE_INSTANCE_ID="${MCP_MQTT_SOURCE_INSTANCE_ID:-mcp-controller-01}"
export MCP_MQTT_CA_SOURCE="${MCP_MQTT_CA_SOURCE:-$pki_root/ca/ca.crt}"
export MCP_MQTT_CERT_SOURCE="${MCP_MQTT_CERT_SOURCE:-$pki_root/clients/mcp-controller/mqtt.crt}"
export MCP_MQTT_KEY_SOURCE="${MCP_MQTT_KEY_SOURCE:-$pki_root/clients/mcp-controller/mqtt.key}"
export MCP_AUTH_SOURCE="${MCP_AUTH_SOURCE:-$HOME/.config/automation-control-plane/mcp_auth.json}"
export MCP_BIND_ADDRESS="${MCP_BIND_ADDRESS:-127.0.0.1}"
export MCP_HTTP_PORT="${MCP_HTTP_PORT:-8080}"
exec "$component_root/scripts/deploy.sh" "$@"

#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
"$root_dir/test_running_all.sh"
"$root_dir/postgres_system/scripts/test_persistence.sh"
"$root_dir/postgres_system/scripts/verify_replication.sh"
"$root_dir/postgres_system/scripts/backup.sh"
"$root_dir/postgres_system/scripts/restore_test.sh"
"$root_dir/mqtt_broker/scripts/backup.sh"
"$root_dir/mcp_controller_server/backup.sh"
"$root_dir/test_running_all.sh"
echo "Extended persistence, PostgreSQL/MQTT/MCP backup and isolated restore tests passed."

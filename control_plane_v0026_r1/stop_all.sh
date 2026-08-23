#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
source "$root_dir/scripts/control_plane_lifecycle.sh"
require_control_plane_tools
acquire_control_plane_lock
trap release_control_plane_lock EXIT
replace_existing_control_plane
echo "All control-plane containers stopped; named volumes and runtime PKI were preserved."

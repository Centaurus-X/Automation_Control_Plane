#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$root_dir"
docker build --pull --tag automation-proxy-gateway:test .
echo "Proxy release-image tests passed."

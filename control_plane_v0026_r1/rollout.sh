#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
unset COMPOSE_FILE COMPOSE_PROJECT_NAME COMPOSE_PROFILES COMPOSE_ENV_FILES
action="${1:-test}"
shift || true
case "$action" in
  --fresh|--upgrade)
    # Direct lab shortcut kept for fast-rollout compatibility. Production
    # remains explicit and therefore cannot be purged through this alias.
    exec "$root_dir/deploy_test_lab.sh" "$action" "$@"
    ;;
  test|lab)
    exec "$root_dir/deploy_test_lab.sh" "$@"
    ;;
  production|prod)
    exec "$root_dir/deploy_production.sh" "$@"
    ;;
  status)
    exec "$root_dir/status_all.sh" "$@"
    ;;
  stop)
    exec "$root_dir/stop_all.sh" "$@"
    ;;
  test-static)
    exec "$root_dir/test_static_all.sh" "$@"
    ;;
  test-running)
    exec "$root_dir/test_running_all.sh" "$@"
    ;;
  test-e2e|e2e)
    exec "$root_dir/test_e2e.sh" "$@"
    ;;
  *)
    echo "Usage: $0 {[--fresh|--upgrade] [--purge-host] [--profile lab_v2]|test [--fresh|--upgrade] [--purge-host] [--profile lab_v2]|production {--fresh|--upgrade}|status|stop|test-static|test-running|test-e2e}" >&2
    exit 64
    ;;
esac

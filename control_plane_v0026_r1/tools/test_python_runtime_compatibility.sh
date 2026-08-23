#!/usr/bin/env bash

set -Eeuo pipefail
root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
temporary_dir="$(mktemp -d)"
cleanup() {
  find "$temporary_dir" -mindepth 1 -depth -delete 2>/dev/null || true
  rmdir "$temporary_dir" 2>/dev/null || true
}
trap cleanup EXIT

[[ "$(<"$root_dir/master_node/requirements.txt")" \
    == $'psycopg==3.3.4\npsycopg-pool==3.3.1' ]]
grep -Fx 'psycopg[binary]==3.3.4' \
  "$root_dir/proxy_gateway/requirements.txt" >/dev/null
grep -F 'libpq-dev' "$root_dir/runtime/python314t/Dockerfile" >/dev/null
grep -F 'PSYCOPG_IMPL=python' "$root_dir/master_node/Dockerfile" >/dev/null
grep -F 'PSYCOPG_IMPL=binary' "$root_dir/proxy_gateway/Dockerfile" >/dev/null
cmp "$root_dir/master_node/tools/check_runtime_dependencies.py" \
  "$root_dir/proxy_gateway/tools/check_runtime_dependencies.py"

fake_modules="$temporary_dir/modules"
mkdir -p "$fake_modules/psycopg"
printf '%s\n' 'from . import pq' > "$fake_modules/psycopg/__init__.py"
printf '%s\n' \
  '__impl__ = "python"' \
  'def version():' \
  '    return 150014' \
  > "$fake_modules/psycopg/pq.py"
for module in psycopg_pool anyio argon2 starlette uvicorn websockets; do
  printf '%s\n' '"""Runtime dependency test double."""' \
    > "$fake_modules/$module.py"
done

PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$fake_modules" \
  python3 "$root_dir/master_node/tools/check_runtime_dependencies.py" master \
  | grep -F 'psycopg_impl=python, libpq=150014' >/dev/null

sed -i 's/__impl__ = "python"/__impl__ = "binary"/' \
  "$fake_modules/psycopg/pq.py"
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$fake_modules" \
  python3 "$root_dir/proxy_gateway/tools/check_runtime_dependencies.py" proxy \
  | grep -F 'psycopg_impl=binary, libpq=150014' >/dev/null

sed -i 's/__impl__ = "binary"/__impl__ = "c"/' \
  "$fake_modules/psycopg/pq.py"
set +e
PYTHONDONTWRITEBYTECODE=1 PYTHONPATH="$fake_modules" \
  python3 "$root_dir/master_node/tools/check_runtime_dependencies.py" master \
  >"$temporary_dir/wrong-implementation.log" 2>&1
status="$?"
set -e
[[ "$status" == "65" ]]
grep -F 'psycopg implementation mismatch' \
  "$temporary_dir/wrong-implementation.log" >/dev/null

echo "PASS cp314t Master pure-psycopg/libpq and cp314 Proxy binary boundaries."

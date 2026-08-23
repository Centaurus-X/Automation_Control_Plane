#!/usr/bin/env bash

set -Eeuo pipefail

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
cd "$root_dir"

username="admin"
password_stdin=0
allow_existing=0

usage() {
  cat <<'EOF'
Usage:
  ./create_admin.sh [USERNAME]
  ./create_admin.sh --username USERNAME [--password-stdin] [--allow-existing]

Without --password-stdin, the administration CLI prompts interactively.
With --password-stdin, exactly one password is read from standard input.
EOF
}

if (($# > 0)) && [[ "$1" != -* ]]; then
  username="$1"
  shift
fi
while (($# > 0)); do
  case "$1" in
    --username)
      (($# >= 2)) || { echo "--username requires a value" >&2; exit 64; }
      username="$2"
      shift 2
      ;;
    --password-stdin)
      password_stdin=1
      shift
      ;;
    --allow-existing)
      allow_existing=1
      shift
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      echo "Unknown option: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
done

[[ "$username" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
  echo "Invalid administrator username: $username" >&2
  exit 64
}

command=(
  docker compose exec
)
if ((password_stdin == 1)); then
  command+=(-T)
fi
command+=(
  proxy-gateway python -m admin_cli create-admin
  --username "$username"
)
if ((password_stdin == 1)); then
  command+=(--password-file /dev/stdin)
fi
if ((allow_existing == 1)); then
  command+=(--ensure-existing-password)
fi

exec "${command[@]}"

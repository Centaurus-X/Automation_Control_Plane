#!/usr/bin/env bash

set -Eeuo pipefail
umask 077

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
release_root="$(cd "$root_dir/.." && pwd)"
export PYTHONDONTWRITEBYTECODE=1
export PYTHONNOUSERSITE=1
export PYTHONPATH="$root_dir/src:$root_dir/tests${PYTHONPATH:+:$PYTHONPATH}"

python3 "$root_dir/tools/vendor_helm_dependencies.py" --root "$root_dir" >/dev/null
python3 "$root_dir/tools/verify_release.py" --root "$release_root"

if python3 -c 'import yaml' >/dev/null 2>&1; then
  ROOT_DIR="$root_dir" python3 - <<'PY'
import os
import pathlib
import yaml

root = pathlib.Path(os.environ["ROOT_DIR"])
files = []
for path in root.rglob("*"):
    if path.suffix not in {".yaml", ".yml"} or "/templates/" in path.as_posix():
        continue
    with path.open(encoding="utf-8") as handle:
        list(yaml.safe_load_all(handle))
    files.append(path)
print("PASS YAML syntax: " + str(len(files)))
PY
else
  echo "SKIP YAML parser validation: PyYAML is not installed"
fi

for test_file in "$root_dir"/tests/test_*.py; do
  python3 "$test_file"
done
"$root_dir/tests/test_lifecycle.sh"

if command -v node >/dev/null 2>&1; then
  node --check "$root_dir/web/app.js"
  echo "PASS JavaScript syntax"
else
  echo "SKIP JavaScript syntax: node is not installed"
fi

if command -v helm >/dev/null 2>&1; then
  image_digest="sha256:1111111111111111111111111111111111111111111111111111111111111111"
  for chart in "$root_dir"/deploy/helm/automation-*; do
    [[ "$(basename "$chart")" == "automation-control-plane" ]] && continue
    if [[ "$(basename "$chart")" == "automation-postgresql-integration" ]]; then
      helm lint --strict "$chart"
      helm template test "$chart" >/dev/null
    else
      helm lint --strict "$chart" \
        --set image.repository=registry.example.invalid/automation/test \
        --set image.digest="$image_digest"
      helm template test "$chart" \
        --set image.repository=registry.example.invalid/automation/test \
        --set image.digest="$image_digest" >/dev/null
    fi
  done
  helm lint --strict "$root_dir/deploy/helm/automation-control-plane"
  helm template test "$root_dir/deploy/helm/automation-control-plane" >/dev/null
  echo "PASS Helm lint and template"
else
  echo "SKIP Helm binary validation: helm is not installed"
fi

if [[ -n "${AUTOMATION_RUNTIME_ARCHIVE:-}" ]]; then
  python3 "$root_dir/tools/validate_runtime_archive.py" \
    "$AUTOMATION_RUNTIME_ARCHIVE" \
    --expected-digest c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467
  if python3 "$root_dir/tools/validate_runtime_archive.py" \
      "$AUTOMATION_RUNTIME_ARCHIVE" \
      --expected-digest 2222222222222222222222222222222222222222222222222222222222222222 \
      >/dev/null 2>&1; then
    echo "FAIL runtime release binding accepted an alternate expected digest" >&2
    exit 1
  fi
  echo "PASS runtime release binding rejects alternate expected digests"
else
  echo "SKIP external runtime archive validation: AUTOMATION_RUNTIME_ARCHIVE is unset"
fi

if [[ -n "${AUTOMATION_RUNTIME_TREE:-}" ]]; then
  (cd "$AUTOMATION_RUNTIME_TREE" && ./test_all.sh)
else
  echo "SKIP external runtime tests: AUTOMATION_RUNTIME_TREE is unset"
fi

if [[ "${RUN_PRESERVED_CONTROL_TESTS:-0}" == "1" ]]; then
  (cd "$release_root/control_plane_v0026_r1" && ./test_static_all.sh)
else
  echo "SKIP preserved Docker-dependent control tests: RUN_PRESERVED_CONTROL_TESTS is not 1"
fi

echo "PASS automation_control_plane_release_v0028_r1 release test suite"

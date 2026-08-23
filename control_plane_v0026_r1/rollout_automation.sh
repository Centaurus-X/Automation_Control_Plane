#!/bin/sh
#
# AUTONOMY CONTRACT — AUTOMATION CONTROL PLANE ROLLOUT
# =====================================================
# This script is contractually required to work idempotently and to remain
# release-independent:
#
# 1. Every normal invocation discovers all adjacent Control Plane release ZIPs
#    and extracted release directories by reading VERSION and RELEASE_REVISION.
# 2. Unless --release or --archive pins a specific candidate, the numerically
#    newest revision is selected automatically. Therefore v0026-r1 wins over
#    v0019-r22, v0019-r21, v0019-r20, v0019-r19, v0019-r13, v0019-r12, v0019-r11, v0019-r8, v0019-r7, v0019-r6, v0019-r5, v0019-r3 and v0017-r13, and
#    v0017-r13 wins over
#    v0017-r12/r11/r10/r9/r8/r7/r6/r5/r4/r3/r2/r1, regardless of names or
#    directory enumeration order.
# 3. A selected release is integrity-checked, installed into its revision-
#    specific directory and re-executed from there. Repeated invocations reuse
#    an already valid installation and do not require manual extraction,
#    copying, editing or backup preparation.
# 4. Existing rollout configuration and protected credentials remain outside
#    the immutable release. Missing standard configuration is initialized from
#    the selected release; credentials are read from credentials.json without
#    exposing passwords in arguments or logs.
# 5. Fresh and Upgrade verify, stream and recoverably install the exact anchored
#    Worker release. Reuse verifies and preserves the active Worker process,
#    identity and sequence continuity. Both paths execute convergence,
#    telemetry and E2E gates.
# 6. No permanent Control-Plane release, replica-journal or log backup is a
#    prerequisite for rollout. Displaced Worker version directories are moved
#    recoverably below the remote __OLD__ directory.
# 7. Integrity, credential, path, certificate and E2E failures remain fail-
#    closed. "Autonomous" never means silently accepting corrupted code.
#
# This contract must be retained by later releases so that an older compliant
# launcher can discover, install and delegate to a newer compliant release.

# The first statements are POSIX-sh compatible so `sh rollout_automation.sh`
# reliably upgrades itself to Bash before any Bash-specific syntax is parsed.
if [ -z "${BASH_VERSION:-}" ]; then
  if ! command -v bash >/dev/null 2>&1; then
    echo "ERROR: bash is required for rollout automation." >&2
    exit 69
  fi
  exec bash "$0" "$@"
fi

set -Eeuo pipefail
umask 077

bootstrap_script_path="$(readlink -f -- "${BASH_SOURCE[0]}")"
bootstrap_script_dir="$(dirname -- "$bootstrap_script_path")"

bootstrap_usage() {
  cat <<'EOF_USAGE'
Autonomous Automation Control Plane installer, rollout and test controller

Minimal usage:
  sh rollout_automation.sh
  sh rollout_automation.sh --fresh
  sh rollout_automation.sh --fresh --purge-host

Automatic release selection:
  --release SELECTOR     Pin a revision (for example v0026-r1), version or path
  --archive FILE         Pin one release ZIP
  --search-dir DIR       Search only this directory; may be specified repeatedly
  --install-root DIR     Installation parent; default: $HOME
  --list-releases        List discovered candidates and exit
  --install-only         Install/select the release without rollout

Rollout actions and modes:
  run                    Install/select and deploy (default action)
  test                   Run acceptance, Worker and Core-E2E gates
  status                 Show Control Plane and Worker status
  diagnose               Collect Control Plane and Worker diagnosis
  --reuse                Reuse an already running compatible Control Plane
  --fresh                Rebuild the Control Plane from a clean scoped baseline
  --upgrade              Use the stateful upgrade path
  --purge-host           With Fresh, purge the dedicated Docker host completely
  --worker-archive FILE  Exact anchored automation_system_v0101.zip

The selected release provides all additional topology, credential, Worker and
E2E options. Fresh/Upgrade require the separately supplied, anchored Worker
ZIP and recoverably install it. Previous Worker directories are moved below
__OLD__; Reuse verifies and preserves the already active canonical Worker.
Without an explicit mode a newly installed/pinned release uses Fresh; an
unchanged latest release uses Reuse. --purge-host always implies Fresh and is
destructive for every Docker project on the host.
EOF_USAGE
}

bootstrap_die() {
  local message="$1"
  local code="${2:-65}"
  echo "ERROR: $message" >&2
  exit "$code"
}

bootstrap_canonical_path() {
  local value="$1"
  case "$value" in
    '~') value="$HOME" ;;
    '~/'*) value="$HOME/${value#~/}" ;;
    '$HOME') value="$HOME" ;;
    '$HOME/'*) value="$HOME/${value#\$HOME/}" ;;
  esac
  case "$value" in
    /*) ;;
    *) value="$(pwd -P)/$value" ;;
  esac
  readlink -m -- "$value"
}

bootstrap_secure_local_file() {
  local label="$1"
  local path="$2"
  [[ -f "$path" && ! -L "$path" ]] || \
    bootstrap_die "$label must be a regular non-symlink file: $path" 65
  local expected_uid actual_uid
  expected_uid="$(id -u)"
  actual_uid="$(stat -c '%u' "$path")"
  [[ "$actual_uid" == "$expected_uid" ]] || \
    bootstrap_die \
      "$label must be owned by UID $expected_uid (got UID $actual_uid): $path" 65
  chmod 0600 "$path"
}

bootstrap_discover_release() {
  local output_file="$1"
  local selector="$2"
  local archive_override="$3"
  local list_only="$4"
  shift 4
  python3 - "$selector" "$archive_override" "$list_only" "$@" \
      >"$output_file" <<'PY_DISCOVER'
import hashlib
import os
import re
import stat
import sys
import zipfile
from functools import partial
from pathlib import Path, PurePosixPath

REVISION_RE = re.compile(r"v([0-9]{4,})-r([1-9][0-9]*)")
ARCHIVE_NAME_RE = re.compile(
    r"automation_control_plane_release_v([0-9]{4,})_r([1-9][0-9]*)[.]zip"
)
PREFIX = "automation_control_plane_release_v"


def digest(path):
    value = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(partial(stream.read, 1024 * 1024), b""):
            value.update(block)
    return value.hexdigest()


def parse_metadata(version, revision, source):
    version = version.strip()
    revision = revision.strip()
    match = REVISION_RE.fullmatch(revision)
    if match is None:
        raise ValueError(f"invalid RELEASE_REVISION in {source}: {revision!r}")
    if not re.fullmatch(r"[0-9]+[.][0-9]+[.][0-9]+", version):
        raise ValueError(f"invalid VERSION in {source}: {version!r}")
    serial = int(match.group(1))
    release_revision = int(match.group(2))
    install_dir = "automation_control_plane_release_" + revision.replace("-", "_")
    return version, revision, serial, release_revision, install_dir


def validate_archive_member(info, seen):
    name = info.filename
    if not name or "\\" in name or "\x00" in name:
        raise ValueError(f"unsafe ZIP member: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise ValueError(f"unsafe ZIP member: {name!r}")
    normalized = path.as_posix()
    if normalized in seen:
        raise ValueError(f"duplicate ZIP member: {normalized}")
    seen.add(normalized)
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise ValueError(f"symbolic link in ZIP: {normalized}")
    if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise ValueError(f"unsupported ZIP member type: {normalized}")
    return path


def archive_sidecar_status(path):
    sidecar = Path(str(path) + ".sha256")
    if not sidecar.is_file() or sidecar.is_symlink():
        raise ValueError(f"required SHA-256 sidecar is missing or unsafe: {sidecar}")
    line = next((item.strip() for item in sidecar.read_text(
        encoding="utf-8", errors="strict").splitlines() if item.strip()), "")
    fields = line.split()
    if not fields or re.fullmatch(r"[0-9a-fA-F]{64}", fields[0]) is None:
        raise ValueError(f"invalid SHA-256 sidecar: {sidecar}")
    expected = fields[0].lower()
    actual = digest(path)
    if actual != expected:
        raise ValueError(
            f"SHA-256 mismatch for {path}: expected={expected} actual={actual}"
        )
    return "sha256-sidecar-verified"


def parse_archive(path):
    if not path.is_file() or path.is_symlink():
        raise ValueError(f"release archive is missing or unsafe: {path}")
    with zipfile.ZipFile(path, "r") as archive:
        seen = set()
        members = {}
        top_levels = set()
        for info in archive.infolist():
            member = validate_archive_member(info, seen)
            top_levels.add(member.parts[0])
            members[member.as_posix()] = info
        if len(top_levels) != 1:
            raise ValueError(f"release ZIP must have exactly one top-level directory: {path}")
        top = next(iter(top_levels))
        required = (
            f"{top}/VERSION",
            f"{top}/RELEASE_REVISION",
            f"{top}/SHA256SUMS",
            f"{top}/rollout_automation.sh",
            f"{top}/tools/verify_checksums.sh",
        )
        for name in required:
            info = members.get(name)
            if info is None or info.is_dir():
                raise ValueError(f"release ZIP is incomplete; missing {name}: {path}")
            if info.file_size > 1024 * 1024:
                raise ValueError(f"unexpectedly large release metadata file: {name}")
        version = archive.read(required[0]).decode("utf-8", errors="strict")
        revision = archive.read(required[1]).decode("utf-8", errors="strict")
    version, revision, serial, release_revision, install_dir = parse_metadata(
        version, revision, path
    )
    filename_match = ARCHIVE_NAME_RE.fullmatch(path.name)
    if filename_match is not None:
        filename_key = (int(filename_match.group(1)), int(filename_match.group(2)))
        if filename_key != (serial, release_revision):
            raise ValueError(
                f"archive filename and RELEASE_REVISION disagree: {path.name} / {revision}"
            )
    return {
        "kind": "zip",
        "path": str(path.resolve()),
        "version": version,
        "revision": revision,
        "serial": serial,
        "release_revision": release_revision,
        "install_dir": install_dir,
        "top": top,
        "verification": archive_sidecar_status(path),
        "rank": 30,
    }


def parse_directory(path):
    if not path.is_dir() or path.is_symlink():
        raise ValueError(f"release directory is missing or unsafe: {path}")
    required = (
        path / "VERSION",
        path / "RELEASE_REVISION",
        path / "SHA256SUMS",
        path / "rollout_automation.sh",
        path / "tools" / "verify_checksums.sh",
    )
    for item in required:
        if not item.is_file() or item.is_symlink():
            raise ValueError(f"release directory is incomplete; missing/unsafe {item}")
    version, revision, serial, release_revision, install_dir = parse_metadata(
        required[0].read_text(encoding="utf-8"),
        required[1].read_text(encoding="utf-8"),
        path,
    )
    rank = 20 if path.name == install_dir else 10
    return {
        "kind": "dir",
        "path": str(path.resolve()),
        "version": version,
        "revision": revision,
        "serial": serial,
        "release_revision": release_revision,
        "install_dir": install_dir,
        "top": path.name,
        "verification": "internal-manifest",
        "rank": rank,
    }


def revision_hint(path):
    if path.is_file():
        match = ARCHIVE_NAME_RE.fullmatch(path.name)
    else:
        match = re.fullmatch(
            r"automation_control_plane_release_v([0-9]{4,})_r([1-9][0-9]*)",
            path.name,
        )
    if match is None:
        return None
    return int(match.group(1)), int(match.group(2))


def selector_matches(candidate, selector):
    if not selector or selector == "latest":
        return True
    normalized = selector.replace("_r", "-r")
    if normalized == candidate["revision"] or selector == candidate["version"]:
        return True
    if selector in (Path(candidate["path"]).name, Path(candidate["path"]).stem):
        return True
    try:
        return Path(selector).expanduser().resolve() == Path(candidate["path"])
    except OSError:
        return False


selector = sys.argv[1]
archive_override = sys.argv[2]
list_only = sys.argv[3] == "1"
search_dirs = [Path(value).expanduser().resolve() for value in sys.argv[4:]]

paths = []
if archive_override:
    paths = [Path(archive_override).expanduser().resolve()]
else:
    for directory in search_dirs:
        if directory.is_dir() and (directory / "VERSION").is_file() \
                and (directory / "RELEASE_REVISION").is_file():
            paths.append(directory)
        if not directory.is_dir():
            continue
        for child in directory.iterdir():
            if child.name.startswith(PREFIX) and (
                child.is_dir() or (child.is_file() and child.suffix == ".zip")
            ):
                paths.append(child)

candidates = []
errors = []
seen_sources = set()
for path in paths:
    resolved = str(path.resolve())
    if resolved in seen_sources:
        continue
    seen_sources.add(resolved)
    try:
        candidate = parse_archive(path) if path.is_file() else parse_directory(path)
        candidates.append(candidate)
    except (OSError, UnicodeError, ValueError, zipfile.BadZipFile) as error:
        errors.append((path, revision_hint(path), str(error)))

matching = [item for item in candidates if selector_matches(item, selector)]
if not matching:
    for path, _, message in errors:
        if selector and selector_matches({
            "revision": "", "version": "", "path": str(path)
        }, selector):
            raise SystemExit("ERROR: " + message)
    details = "\n".join("  " + str(path) + ": " + message for path, _, message in errors)
    raise SystemExit(
        "ERROR: no usable Automation Control Plane release was found"
        + (" for selector " + repr(selector) if selector else "")
        + ("\nRejected candidates:\n" + details if details else "")
    )

def candidate_sort_key(item):
    return (
        item["serial"], item["release_revision"], item["rank"], item["path"]
    )


matching.sort(key=candidate_sort_key, reverse=True)
selected = matching[0]
selected_key = (selected["serial"], selected["release_revision"])
for path, hint, message in errors:
    if not selector and hint is not None and hint > selected_key:
        raise SystemExit(
            f"ERROR: a newer-looking release candidate is invalid: {path}\n{message}"
        )

if list_only:
    all_candidates = sorted(
        candidates, key=candidate_sort_key, reverse=True)
    for item in all_candidates:
        marker = "*" if item["path"] == selected["path"] else " "
        print(
            f"{marker} {item['revision']}  {item['version']}  "
            f"{item['kind']}  {item['verification']}  {item['path']}"
        )
    for path, _, message in errors:
        print(f"! rejected  {path}: {message}", file=sys.stderr)
    raise SystemExit(0)

fields = (
    selected["kind"], selected["path"], selected["version"],
    selected["revision"], selected["install_dir"], selected["top"],
    selected["verification"],
)
sys.stdout.buffer.write(b"\0".join(value.encode("utf-8") for value in fields) + b"\0")
PY_DISCOVER
}

bootstrap_stage_release() {
  local kind="$1"
  local source="$2"
  local stage_parent="$3"
  local install_dir="$4"
  local expected_revision="$5"
  python3 - "$kind" "$source" "$stage_parent" "$install_dir" \
      "$expected_revision" <<'PY_STAGE'
import os
import re
import shutil
import stat
import sys
import zipfile
from pathlib import Path, PurePosixPath

kind, source_value, stage_value, install_dir, expected_revision = sys.argv[1:]
source = Path(source_value)
stage_parent = Path(stage_value)
destination = stage_parent / install_dir
REVISION_RE = re.compile(r"v[0-9]{4,}-r[1-9][0-9]*")


def safe_member(info, seen):
    name = info.filename
    if not name or "\\" in name or "\x00" in name:
        raise RuntimeError(f"unsafe ZIP member: {name!r}")
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise RuntimeError(f"unsafe ZIP member: {name!r}")
    normalized = path.as_posix()
    if normalized in seen:
        raise RuntimeError(f"duplicate ZIP member: {normalized}")
    seen.add(normalized)
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise RuntimeError(f"symbolic link in ZIP: {normalized}")
    if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise RuntimeError(f"unsupported ZIP member type: {normalized}")
    return path, mode


def safe_mode(mode, executable=False):
    permissions = stat.S_IMODE(mode) if mode else (0o755 if executable else 0o644)
    return permissions & (0o755 if executable else 0o644)


if destination.exists():
    raise RuntimeError(f"staging destination already exists: {destination}")

if kind == "zip":
    with zipfile.ZipFile(source, "r") as archive:
        seen = set()
        entries = []
        top_levels = set()
        for info in archive.infolist():
            path, mode = safe_member(info, seen)
            top_levels.add(path.parts[0])
            entries.append((info, path, mode))
        if len(top_levels) != 1:
            raise RuntimeError("release ZIP must contain exactly one top-level directory")
        top = next(iter(top_levels))
        destination.mkdir(mode=0o755)
        for info, path, mode in entries:
            relative_parts = path.parts[1:]
            if not relative_parts:
                continue
            target = destination.joinpath(*relative_parts)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.open(info, "r") as reader, target.open("wb") as writer:
                shutil.copyfileobj(reader, writer, 1024 * 1024)
            target.chmod(safe_mode(mode, executable=bool(mode & 0o111)))
elif kind == "dir":
    manifest = source / "SHA256SUMS"
    if not manifest.is_file() or manifest.is_symlink():
        raise RuntimeError("source release SHA256SUMS is missing or unsafe")
    destination.mkdir(mode=0o755)
    shutil.copy2(manifest, destination / "SHA256SUMS", follow_symlinks=False)
    (destination / "SHA256SUMS").chmod(0o644)
    seen = set()
    for line_number, line in enumerate(
        manifest.read_text(encoding="utf-8").splitlines(), start=1
    ):
        match = re.fullmatch(r"([0-9a-f]{64})  [.]\/([A-Za-z0-9._\/-]+)", line)
        if match is None:
            raise RuntimeError(f"malformed source manifest line {line_number}")
        relative = PurePosixPath(match.group(2))
        if any(part in ("", ".", "..") for part in relative.parts):
            raise RuntimeError(f"unsafe source manifest path: {relative}")
        normalized = relative.as_posix()
        if normalized in seen:
            raise RuntimeError(f"duplicate source manifest path: {normalized}")
        seen.add(normalized)
        item = source.joinpath(*relative.parts)
        if not item.is_file() or item.is_symlink():
            raise RuntimeError(f"source release file is missing or unsafe: {item}")
        target = destination.joinpath(*relative.parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(item, target, follow_symlinks=False)
else:
    raise RuntimeError(f"unsupported release source kind: {kind}")

revision_file = destination / "RELEASE_REVISION"
if not revision_file.is_file():
    raise RuntimeError("staged release has no RELEASE_REVISION")
revision = revision_file.read_text(encoding="utf-8").strip()
if REVISION_RE.fullmatch(revision) is None or revision != expected_revision:
    raise RuntimeError(
        f"staged release revision mismatch: expected={expected_revision} actual={revision}"
    )
PY_STAGE
}

bootstrap_target_is_valid() {
  local target="$1"
  local expected_revision="$2"
  [[ -d "$target" && ! -L "$target" ]] || return 1
  [[ -s "$target/RELEASE_REVISION" ]] || return 1
  [[ "$(<"$target/RELEASE_REVISION")" == "$expected_revision" ]] || return 1
  [[ -x "$target/rollout_automation.sh" ]] || return 1
  [[ -x "$target/tools/verify_checksums.sh" ]] || return 1
  VERIFY_CHECKSUMS_QUIET=1 "$target/tools/verify_checksums.sh" >/dev/null 2>&1
}

bootstrap_target_matches_source() {
  local kind="$1"
  local source="$2"
  local target="$3"
  python3 - "$kind" "$source" "$target" <<'PY_MATCH'
import sys
import zipfile
from pathlib import Path, PurePosixPath

kind, source_value, target_value = sys.argv[1:]
source = Path(source_value)
target_manifest = Path(target_value) / "SHA256SUMS"
if not target_manifest.is_file() or target_manifest.is_symlink():
    raise SystemExit(1)
target_bytes = target_manifest.read_bytes()

if kind == "dir":
    source_manifest = source / "SHA256SUMS"
    if not source_manifest.is_file() or source_manifest.is_symlink():
        raise SystemExit(1)
    source_bytes = source_manifest.read_bytes()
elif kind == "zip":
    with zipfile.ZipFile(source, "r") as archive:
        candidates = []
        for info in archive.infolist():
            path = PurePosixPath(info.filename)
            if len(path.parts) == 2 and path.parts[1] == "SHA256SUMS" and not info.is_dir():
                candidates.append(info)
        if len(candidates) != 1:
            raise SystemExit(1)
        source_bytes = archive.read(candidates[0])
else:
    raise SystemExit(1)

raise SystemExit(0 if source_bytes == target_bytes else 1)
PY_MATCH
}

bootstrap_install_release() {
  local kind="$1"
  local source="$2"
  local revision="$3"
  local install_dir="$4"
  local install_root="$5"
  local target="$install_root/$install_dir"
  local stage_parent=""
  local staged=""
  local displaced=""

  if [[ ! -d "$install_root" ]]; then
    install -d -m 0755 "$install_root"
  fi
  [[ -d "$install_root" && ! -L "$install_root" ]] || {
    echo "ERROR: installation root is missing or unsafe: $install_root" >&2
    return 65
  }
  if bootstrap_target_is_valid "$target" "$revision"; then
    if bootstrap_target_matches_source "$kind" "$source" "$target"; then
      printf '%s\0%s\0' "$target" "reused"
      return 0
    fi
    echo "ERROR: immutable release revision collision: $revision" >&2
    echo "The selected source differs from the installed release with the same revision; publish a new revision." >&2
    return 65
  fi

  stage_parent="$(mktemp -d "$install_root/.automation-release-stage.XXXXXX")"
  chmod 0700 "$stage_parent"
  staged="$stage_parent/$install_dir"
  displaced="$install_root/.automation-release-displaced.${install_dir}.$$"
  rm -rf -- "$displaced"

  if ! bootstrap_stage_release "$kind" "$source" "$stage_parent" \
      "$install_dir" "$revision"; then
    rm -rf -- "$stage_parent"
    return 65
  fi
  if ! VERIFY_CHECKSUMS_QUIET=1 "$staged/tools/verify_checksums.sh" >/dev/null; then
    echo "ERROR: selected release failed its internal manifests: $source" >&2
    VERIFY_CHECKSUMS_QUIET=0 "$staged/tools/verify_checksums.sh" >&2 || true
    rm -rf -- "$stage_parent"
    return 65
  fi

  if [[ -e "$target" ]]; then
    mv -- "$target" "$displaced"
  fi
  if ! mv -- "$staged" "$target"; then
    [[ -e "$displaced" ]] && mv -- "$displaced" "$target"
    rm -rf -- "$stage_parent"
    return 74
  fi
  rm -rf -- "$stage_parent"

  if ! bootstrap_target_is_valid "$target" "$revision"; then
    rm -rf -- "$target"
    [[ -e "$displaced" ]] && mv -- "$displaced" "$target"
    echo "ERROR: installed release failed post-install verification: $target" >&2
    return 65
  fi
  rm -rf -- "$displaced"
  printf '%s\0%s\0' "$target" "installed"
}

bootstrap_main() {
  local selector=""
  local archive_override=""
  local install_root="${AUTOMATION_RELEASE_INSTALL_ROOT:-$HOME}"
  local list_only=0
  local install_only=0
  local explicit_mode=0
  local purge_host=0
  local explicit_selection=0
  local action="run"
  local action_seen=0
  local config_argument=""
  local credentials_argument=""
  local discovery_output=""
  local install_output=""
  local selected_kind=""
  local selected_source=""
  local selected_version=""
  local selected_revision=""
  local selected_install_dir=""
  local selected_top=""
  local selected_verification=""
  local target_dir=""
  local install_action=""
  local standard_config=""
  local standard_credentials=""
  local candidate=""
  local canonical=""
  local search_dirs=()
  local forwarded=()
  local raw=("$@")
  local index=0

  command -v python3 >/dev/null 2>&1 || \
    bootstrap_die "python3 is required for secure release discovery/extraction" 69
  command -v readlink >/dev/null 2>&1 || bootstrap_die "readlink is required" 69
  command -v sha256sum >/dev/null 2>&1 || bootstrap_die "sha256sum is required" 69
  command -v install >/dev/null 2>&1 || bootstrap_die "install is required" 69

  while (( index < ${#raw[@]} )); do
    candidate="${raw[$index]}"
    case "$candidate" in
      -h|--help)
        bootstrap_usage
        exit 0
        ;;
      --release)
        (( index + 1 < ${#raw[@]} )) || bootstrap_die "--release requires a value" 64
        selector="${raw[$((index + 1))]}"
        explicit_selection=1
        index=$((index + 2))
        ;;
      --archive)
        (( index + 1 < ${#raw[@]} )) || bootstrap_die "--archive requires a value" 64
        archive_override="$(bootstrap_canonical_path "${raw[$((index + 1))]}")"
        explicit_selection=1
        index=$((index + 2))
        ;;
      --search-dir)
        (( index + 1 < ${#raw[@]} )) || bootstrap_die "--search-dir requires a value" 64
        search_dirs+=("$(bootstrap_canonical_path "${raw[$((index + 1))]}")")
        index=$((index + 2))
        ;;
      --install-root)
        (( index + 1 < ${#raw[@]} )) || bootstrap_die "--install-root requires a value" 64
        install_root="$(bootstrap_canonical_path "${raw[$((index + 1))]}")"
        index=$((index + 2))
        ;;
      --list-releases)
        list_only=1
        index=$((index + 1))
        ;;
      --install-only)
        install_only=1
        index=$((index + 1))
        ;;
      --reuse|--fresh|--upgrade)
        explicit_mode=1
        forwarded+=("$candidate")
        index=$((index + 1))
        ;;
      --purge-host)
        purge_host=1
        forwarded+=("$candidate")
        index=$((index + 1))
        ;;
      --config)
        (( index + 1 < ${#raw[@]} )) || bootstrap_die "--config requires a value" 64
        config_argument="$(bootstrap_canonical_path "${raw[$((index + 1))]}")"
        forwarded+=("--config" "$config_argument")
        index=$((index + 2))
        ;;
      --credentials)
        (( index + 1 < ${#raw[@]} )) || bootstrap_die "--credentials requires a value" 64
        credentials_argument="$(bootstrap_canonical_path "${raw[$((index + 1))]}")"
        forwarded+=("--credentials" "$credentials_argument")
        index=$((index + 2))
        ;;
      run|test|status|diagnose)
        (( action_seen == 0 )) || bootstrap_die "only one action may be specified" 64
        action="$candidate"
        action_seen=1
        index=$((index + 1))
        ;;
      *)
        forwarded+=("$candidate")
        index=$((index + 1))
        ;;
    esac
  done

  install_root="$(bootstrap_canonical_path "$install_root")"
  [[ "$install_root" != "/" ]] || bootstrap_die "installation root must not be /" 64
  if (( action_seen == 1 )); then
    forwarded=("$action" "${forwarded[@]}")
  fi

  if ((${#search_dirs[@]} == 0)); then
    for candidate in \
        "$bootstrap_script_dir" \
        "$(dirname -- "$bootstrap_script_dir")" \
        "$(pwd -P)" \
        "$HOME"; do
      canonical="$(bootstrap_canonical_path "$candidate")"
      local already=0
      local existing=""
      for existing in "${search_dirs[@]}"; do
        [[ "$existing" == "$canonical" ]] && already=1
      done
      (( already == 1 )) || search_dirs+=("$canonical")
    done
  fi

  discovery_output="$(mktemp "${TMPDIR:-/tmp}/automation-release-discovery.XXXXXX")"
  if bootstrap_discover_release "$discovery_output" "$selector" \
      "$archive_override" "$list_only" "${search_dirs[@]}"; then
    :
  else
    local result=$?
    rm -f -- "$discovery_output"
    exit "$result"
  fi
  if (( list_only == 1 )); then
    cat "$discovery_output"
    rm -f -- "$discovery_output"
    exit 0
  fi

  mapfile -d '' -t selected_fields <"$discovery_output"
  rm -f -- "$discovery_output"
  discovery_output=""
  ((${#selected_fields[@]} == 7)) || \
    bootstrap_die "invalid release discovery result" 65
  selected_kind="${selected_fields[0]}"
  selected_source="${selected_fields[1]}"
  selected_version="${selected_fields[2]}"
  selected_revision="${selected_fields[3]}"
  selected_install_dir="${selected_fields[4]}"
  selected_top="${selected_fields[5]}"
  selected_verification="${selected_fields[6]}"

  printf 'Selected release: %s / %s\n' "$selected_version" "$selected_revision"
  printf 'Selected source:  %s (%s, %s)\n' \
    "$selected_source" "$selected_kind" "$selected_verification"

  install_output="$(mktemp "${TMPDIR:-/tmp}/automation-release-install.XXXXXX")"
  if bootstrap_install_release "$selected_kind" "$selected_source" \
      "$selected_revision" "$selected_install_dir" "$install_root" \
      >"$install_output"; then
    :
  else
    local result=$?
    rm -f -- "$install_output"
    exit "$result"
  fi
  mapfile -d '' -t install_fields <"$install_output"
  rm -f -- "$install_output"
  install_output=""
  ((${#install_fields[@]} == 2)) || bootstrap_die "invalid release installation result" 65
  target_dir="${install_fields[0]}"
  install_action="${install_fields[1]}"
  printf 'Release installation: %s (%s)\n' "$install_action" "$target_dir"

  standard_config="$HOME/.config/automation-control-plane/rollout_automation.json"
  standard_credentials="$HOME/.config/automation-control-plane/credentials.json"
  install -d -m 0700 "$HOME/.config/automation-control-plane"

  if [[ -z "$config_argument" ]]; then
    if [[ ! -e "$standard_config" ]]; then
      install -m 0600 "$target_dir/rollout_automation.json.example" "$standard_config"
      echo "Initialized rollout configuration: $standard_config"
    fi
    bootstrap_secure_local_file "rollout configuration" "$standard_config"
    forwarded+=("--config" "$standard_config")
  else
    bootstrap_secure_local_file "rollout configuration" "$config_argument"
  fi

  if [[ -z "$credentials_argument" ]]; then
    if [[ ! -e "$standard_credentials" ]]; then
      if [[ -f "$HOME/credentials.json" && ! -L "$HOME/credentials.json" ]]; then
        install -m 0600 "$HOME/credentials.json" "$standard_credentials"
        echo "Installed protected credentials: $standard_credentials"
      else
        bootstrap_die \
          "credentials are missing; provide $HOME/credentials.json or --credentials FILE" 66
      fi
    fi
    bootstrap_secure_local_file "credentials" "$standard_credentials"
    forwarded+=("--credentials" "$standard_credentials")
  else
    bootstrap_secure_local_file "credentials" "$credentials_argument"
  fi

  if (( install_only == 1 )); then
    printf 'PASS: release %s is installed and verified at %s\n' \
      "$selected_revision" "$target_dir"
    return 0
  fi

  if (( explicit_mode == 0 )) && [[ "$action" == "run" ]]; then
    if (( purge_host == 1 )) || (( explicit_selection == 1 )) \
        || [[ "$install_action" == "installed" ]]; then
      forwarded+=("--fresh")
    else
      forwarded+=("--reuse")
    fi
  fi

  [[ -x "$target_dir/rollout_automation.sh" ]] || \
    bootstrap_die "installed rollout_automation.sh is not executable" 66

  export AUTOMATION_ROLLOUT_INTERNAL=1
  export AUTOMATION_ROLLOUT_SELECTED_SOURCE="$selected_source"
  export AUTOMATION_ROLLOUT_SELECTED_REVISION="$selected_revision"
  export AUTOMATION_ROLLOUT_INSTALL_ACTION="$install_action"
  exec bash "$target_dir/rollout_automation.sh" "${forwarded[@]}"
}

if [[ "${AUTOMATION_ROLLOUT_INTERNAL:-0}" != "1" ]]; then
  bootstrap_main "$@"
  exit $?
fi

root_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)"
version="$(<"$root_dir/VERSION")"
revision="$(<"$root_dir/RELEASE_REVISION")"
current_e2e_file="$root_dir/integration_test/CURRENT_E2E"
test -s "$current_e2e_file" || {
  echo "BLOCKED: active E2E selector is missing: $current_e2e_file" >&2
  exit 66
}
e2e_name="$(<"$current_e2e_file")"
[[ "$e2e_name" =~ ^e2e_v[0-9]{4,}$ ]] || {
  echo "BLOCKED: invalid active E2E selector: $e2e_name" >&2
  exit 65
}
e2e_dir="$root_dir/integration_test/$e2e_name"
e2e_version_file="$e2e_dir/VERSION"
e2e_serial="${e2e_name#e2e_}"
e2e_result_dir="${E2E_RESULT_DIR:-$e2e_dir/results}"
e2e_result="$e2e_result_dir/e2e-${e2e_serial}-result.json"

# Reference-lab defaults. Values which depend on another topology field stay
# empty until configuration and command-line overrides have been processed.
# This prevents a changed bind address or node ID from inheriting a stale
# derived default from the script's initial state.
CONTROL_PLANE_BIND_ADDRESS="${CONTROL_PLANE_BIND_ADDRESS:-192.168.0.82}"
MQTT_BIND_ADDRESS="${MQTT_BIND_ADDRESS:-}"
MQTT_TEST_HOST="${MQTT_TEST_HOST:-}"
MQTT_TEST_BROKER_IP="${MQTT_TEST_BROKER_IP:-}"
PROXY_BIND_ADDRESS="${PROXY_BIND_ADDRESS:-}"
PROXY_WSS_PORT="${PROXY_WSS_PORT:-8443}"
PROXY_HTTPS_PORT="${PROXY_HTTPS_PORT:-8444}"
PROXY_ORIGIN_ALLOWLIST="${PROXY_ORIGIN_ALLOWLIST:-}"
MCP_BIND_ADDRESS="${MCP_BIND_ADDRESS:-127.0.0.1}"
MCP_HTTP_PORT="${MCP_HTTP_PORT:-8080}"
MCP_MQTT_SOURCE_INSTANCE_ID="${MCP_MQTT_SOURCE_INSTANCE_ID:-mcp-controller-01}"
MCP_AUTH_FILE="${MCP_AUTH_FILE:-$HOME/.config/automation-control-plane/mcp_auth.json}"
MCP_AUTH_CREDENTIAL_KEY="${MCP_AUTH_CREDENTIAL_KEY:-mcp_controller.local_agent.token}"
MCP_CLIENT_ID="${MCP_CLIENT_ID:-automation-orchestrator}"
MCP_SUBJECT="${MCP_SUBJECT:-mcp:automation-orchestrator}"
E2E_ORIGIN="${E2E_ORIGIN:-}"
CONTROL_PLANE_PROFILE="${CONTROL_PLANE_PROFILE:-lab_v2}"
CONTROL_PLANE_STATE_SOURCE="${CONTROL_PLANE_STATE_SOURCE:-}"
SITE_ID="${AUTOMATION_SITE_ID:-${SITE_ID:-site-main}}"
NODE_ID="${AUTOMATION_NODE_ID:-${NODE_ID:-fn-01}}"
WORKER_SSH="${WORKER_SSH:-philo@192.168.0.80}"
WORKER_PATH="${WORKER_PATH:-python-skripte_22/__NEW_FILES__/automation_system_v0101}"
WORKER_PROFILE="${WORKER_PROFILE:-worker_simulation}"
WORKER_PKI_STAGE="${WORKER_PKI_STAGE:-}"
WORKER_LOG_PATH="${WORKER_LOG_PATH:-}"
WORKER_ARCHIVE="${WORKER_ARCHIVE:-}"
WORKER_EXPECTED_VERSION="${WORKER_EXPECTED_VERSION:-automation_system_v0101}"
WORKER_ARCHIVE_SHA256="${WORKER_ARCHIVE_SHA256:-c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467}"
WORKER_MANIFEST_SHA256="${WORKER_MANIFEST_SHA256:-699c29f9cf58b1bd6bd7407e879b3b5b3f41ad978343037a1dc33a75ec25026d}"
WORKER_CONFIG_DRIFT_POLICY="${WORKER_CONFIG_DRIFT_POLICY:-preserve}"
REUSE_COMPATIBLE_REVISIONS="${REUSE_COMPATIBLE_REVISIONS:-v0019-r5}"
if [[ -n "${AUTOMATION_CREDENTIALS_FILE:-}" ]]; then
  CREDENTIALS_FILE="$AUTOMATION_CREDENTIALS_FILE"
elif [[ -s "$HOME/.config/automation-control-plane/credentials.json" ]]; then
  CREDENTIALS_FILE="$HOME/.config/automation-control-plane/credentials.json"
else
  CREDENTIALS_FILE="$HOME/credentials.json"
fi
SSH_CREDENTIAL_KEY="${SSH_CREDENTIAL_KEY:-}"
ADMIN_CREDENTIAL_KEY="${ADMIN_CREDENTIAL_KEY:-admin}"
ADMIN_USERNAME="${ADMIN_USERNAME:-admin}"
CONVERGENCE_TIMEOUT_S="${CONVERGENCE_TIMEOUT_S:-180}"
TELEMETRY_TIMEOUT_S="${TELEMETRY_TIMEOUT_S:-60}"
LOG_ROOT="${AUTOMATION_ROLLOUT_LOG_ROOT:-$HOME/.local/state/automation-control-plane/rollouts}"
ACTUATOR_HOST="${ACTUATOR_HOST:-192.168.0.3}"
ACTUATOR_PORT="${ACTUATOR_PORT:-5020}"

usage() {
  cat <<EOF
Automation Control Plane $version/$revision rollout automation

Usage:
  $0 [run] [--reuse|--fresh|--upgrade] [OPTIONS]
  $0 test [OPTIONS]
  $0 status [OPTIONS]
  $0 diagnose [OPTIONS]

Core options:
  --reuse                    keep Control Plane and active Worker without restart
  --fresh                    execute the complete fresh Control Plane rollout
  --upgrade                  execute the Control Plane upgrade path
  --purge-host               with --fresh, purge the dedicated Docker host
  --reset-worker-replica     delete/reset only the Worker replica journal
  --keep-worker-replica      preserve the Worker replica journal
  --skip-pki                 do not transfer/install the Worker MQTT identity
  --skip-worker-restart      do not stop/restart the existing Worker
  --skip-telemetry           omit the live telemetry gate
  --skip-e2e                 omit the Core-E2E
  --skip-admin-bootstrap     omit idempotent administrator bootstrap
  --config FILE              JSON topology/configuration file
  --credentials FILE         protected credentials JSON (0600)
  --mcp-auth-file FILE       external MCP Bearer auth document (0600)
  --worker-ssh USER@HOST     Worker SSH target
  --worker-archive FILE      exact automation_system_v0101.zip
  --site-id ID               site identifier
  --node-id ID               Worker node identifier
  --log-root DIRECTORY       rollout log root
  -h, --help                 show this help

Fresh and upgrade modes verify, transfer and recoverably install the exact
Worker v0101 archive. Previous Worker directories are moved below __OLD__.
Reuse mode keeps the already verified active Worker untouched.
EOF
}

expand_local_path() {
  local value="$1"
  case "$value" in
    '~') printf '%s\n' "$HOME" ;;
    '~/'*) printf '%s/%s\n' "$HOME" "${value#~/}" ;;
    '$HOME') printf '%s\n' "$HOME" ;;
    '$HOME/'*) printf '%s/%s\n' "$HOME" "${value#\$HOME/}" ;;
    *) printf '%s\n' "$value" ;;
  esac
}

canonical_local_path() {
  local value=""
  value="$(expand_local_path "$1")"
  case "$value" in
    /*) ;;
    *) value="$(pwd -P)/$value" ;;
  esac
  readlink -m -- "$value"
}

reject_release_local_state() {
  local label="$1"
  local value="$2"
  case "$value" in
    "$root_dir"|"$root_dir"/*)
      echo "BLOCKED: $label must remain outside the immutable release tree" >&2
      exit 64
      ;;
  esac
}


validate_identifier() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$ ]] || {
    echo "BLOCKED: invalid $label: $value" >&2
    exit 64
  }
}

validate_positive_integer() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[1-9][0-9]*$ ]] || {
    echo "BLOCKED: $label must be a positive integer" >&2
    exit 64
  }
}

validate_sha256() {
  local label="$1"
  local value="$2"
  [[ "$value" =~ ^[0-9a-f]{64}$ ]] || {
    echo "BLOCKED: $label must be 64 lowercase hexadecimal characters" >&2
    exit 64
  }
}

validate_compatible_revisions() {
  local value="$1"
  local candidate=""
  local revisions=()
  IFS=',' read -r -a revisions <<<"$value"
  ((${#revisions[@]} > 0)) || {
    echo "BLOCKED: REUSE_COMPATIBLE_REVISIONS must not be empty" >&2
    exit 64
  }
  for candidate in "${revisions[@]}"; do
    [[ "$candidate" =~ ^v[0-9]{4,}-r[1-9][0-9]*$ ]] || {
      echo "BLOCKED: invalid compatible runtime revision: $candidate" >&2
      exit 64
    }
  done
  # The list is a backward-compatible allow-list only. The current release is
  # accepted dynamically and therefore an older external configuration never
  # needs manual editing merely because a newer r-revision was installed.
}

# Help must remain available even if a local configuration file is malformed.
for early_argument in "$@"; do
  case "$early_argument" in
    -h|--help) usage; exit 0 ;;
  esac
done

# Locate --config before applying the remaining command-line overrides.
raw_arguments=("$@")
config_file="${AUTOMATION_ROLLOUT_CONFIG:-}"
for ((argument_index=0; argument_index<${#raw_arguments[@]}; argument_index++)); do
  if [[ "${raw_arguments[$argument_index]}" == "--config" ]]; then
    (( argument_index + 1 < ${#raw_arguments[@]} )) || {
      echo "--config requires a value" >&2
      exit 64
    }
    config_file="${raw_arguments[$((argument_index + 1))]}"
  fi
done
standard_config_file="$HOME/.config/automation-control-plane/rollout_automation.json"
if [[ -z "$config_file" && -s "$standard_config_file" ]]; then
  config_file="$standard_config_file"
fi
if [[ -e "$root_dir/rollout_automation.json" ]]; then
  echo "BLOCKED: move rollout_automation.json to $standard_config_file" >&2
  echo "The release tree must remain byte-identical to its SHA-256 manifest." >&2
  exit 64
fi
if [[ -n "$config_file" ]]; then
  config_file="$(canonical_local_path "$config_file")"
  reject_release_local_state "rollout configuration" "$config_file"
  test -s "$config_file" || {
    echo "BLOCKED: rollout configuration is missing: $config_file" >&2
    exit 66
  }
  config_stream="$(mktemp "${TMPDIR:-/tmp}/automation-rollout-config.XXXXXX")"
  if ! "$root_dir/tools/read_rollout_config.py" "$config_file" >"$config_stream"; then
    rm -f -- "$config_stream"
    exit 65
  fi
  mapfile -d '' -t config_items <"$config_stream"
  rm -f -- "$config_stream"
  (( ${#config_items[@]} % 2 == 0 )) || {
    echo "BLOCKED: invalid rollout configuration stream" >&2
    exit 65
  }
  for ((config_index=0; config_index<${#config_items[@]}; config_index+=2)); do
    printf -v "${config_items[$config_index]}" '%s' \
      "${config_items[$((config_index + 1))]}"
  done
fi

# Derived defaults are resolved after all topology sources, including CLI
# overrides, have been applied (see below).

ACTION=run
DEPLOYMENT_MODE=reuse
PURGE_HOST=0
RESET_POLICY=auto
SKIP_PKI=0
SKIP_WORKER_RESTART=0
SKIP_TELEMETRY=0
SKIP_E2E=0
SKIP_ADMIN_BOOTSTRAP=0
REUSE_PRESERVE_WORKER=0

if (( $# > 0 )) && [[ "$1" != -* ]]; then
  case "$1" in
    run|test|status|diagnose)
      ACTION="$1"
      shift
      ;;
    *)
      echo "Unknown action: $1" >&2
      usage >&2
      exit 64
      ;;
  esac
fi
while (( $# > 0 )); do
  case "$1" in
    --reuse) DEPLOYMENT_MODE=reuse; shift ;;
    --fresh) DEPLOYMENT_MODE=fresh; shift ;;
    --upgrade) DEPLOYMENT_MODE=upgrade; shift ;;
    --purge-host) PURGE_HOST=1; shift ;;
    --reset-worker-replica) RESET_POLICY=yes; shift ;;
    --keep-worker-replica) RESET_POLICY=no; shift ;;
    --skip-pki) SKIP_PKI=1; shift ;;
    --skip-worker-restart) SKIP_WORKER_RESTART=1; shift ;;
    --skip-telemetry) SKIP_TELEMETRY=1; shift ;;
    --skip-e2e) SKIP_E2E=1; shift ;;
    --skip-admin-bootstrap) SKIP_ADMIN_BOOTSTRAP=1; shift ;;
    --config) shift 2 ;;
    --credentials)
      (( $# >= 2 )) || { echo "--credentials requires a value" >&2; exit 64; }
      CREDENTIALS_FILE="$2"; shift 2 ;;
    --mcp-auth-file)
      (( $# >= 2 )) || { echo "--mcp-auth-file requires a value" >&2; exit 64; }
      MCP_AUTH_FILE="$2"; shift 2 ;;
    --worker-ssh)
      (( $# >= 2 )) || { echo "--worker-ssh requires a value" >&2; exit 64; }
      WORKER_SSH="$2"; shift 2 ;;
    --worker-archive)
      (( $# >= 2 )) || { echo "--worker-archive requires a value" >&2; exit 64; }
      WORKER_ARCHIVE="$2"; shift 2 ;;
    --site-id)
      (( $# >= 2 )) || { echo "--site-id requires a value" >&2; exit 64; }
      SITE_ID="$2"; shift 2 ;;
    --node-id)
      (( $# >= 2 )) || { echo "--node-id requires a value" >&2; exit 64; }
      NODE_ID="$2"; shift 2 ;;
    --log-root)
      (( $# >= 2 )) || { echo "--log-root requires a value" >&2; exit 64; }
      LOG_ROOT="$2"; shift 2 ;;
    -h|--help) usage; exit 0 ;;
    *) echo "Unknown option: $1" >&2; usage >&2; exit 64 ;;
  esac
done

# The external rollout profile intentionally survives release upgrades and may
# therefore still name the previous version directory.  The verified installer
# always installs the anchored release below the configured parent; keep that
# parent but bind every subsequent lifecycle gate to the exact new version.
if [[ "${WORKER_PATH##*/}" != "$WORKER_EXPECTED_VERSION" ]]; then
  previous_worker_path="$WORKER_PATH"
  case "$WORKER_PATH" in
    */*) WORKER_PATH="${WORKER_PATH%/*}/$WORKER_EXPECTED_VERSION" ;;
    *) WORKER_PATH="$WORKER_EXPECTED_VERSION" ;;
  esac
  echo "Adapted Worker release path: $previous_worker_path -> $WORKER_PATH"
fi

MQTT_BIND_ADDRESS="${MQTT_BIND_ADDRESS:-$CONTROL_PLANE_BIND_ADDRESS}"
MQTT_TEST_HOST="${MQTT_TEST_HOST:-$MQTT_BIND_ADDRESS}"
MQTT_TEST_BROKER_IP="${MQTT_TEST_BROKER_IP:-$MQTT_BIND_ADDRESS}"
PROXY_BIND_ADDRESS="${PROXY_BIND_ADDRESS:-$CONTROL_PLANE_BIND_ADDRESS}"
PROXY_ORIGIN_ALLOWLIST="${PROXY_ORIGIN_ALLOWLIST:-https://$PROXY_BIND_ADDRESS:$PROXY_HTTPS_PORT}"
E2E_ORIGIN="${E2E_ORIGIN:-${PROXY_ORIGIN_ALLOWLIST%%,*}}"
WORKER_PKI_STAGE="${WORKER_PKI_STAGE:-.cache/automation-control-plane/pki/$SITE_ID/$NODE_ID}"
WORKER_LOG_PATH="${WORKER_LOG_PATH:-.local/state/automation-worker/$SITE_ID/$NODE_ID/worker.log}"
if [[ -z "$WORKER_ARCHIVE" ]]; then
  worker_archive_name="${WORKER_EXPECTED_VERSION}.zip"
  selected_source="${AUTOMATION_ROLLOUT_SELECTED_SOURCE:-}"
  worker_archive_candidates=(
    "$HOME/$worker_archive_name"
    "$(dirname -- "$root_dir")/$worker_archive_name"
  )
  if [[ -n "$selected_source" ]]; then
    worker_archive_candidates+=("$(dirname -- "$selected_source")/$worker_archive_name")
  fi
  for worker_archive_candidate in "${worker_archive_candidates[@]}"; do
    if [[ -f "$worker_archive_candidate" && ! -L "$worker_archive_candidate" ]]; then
      WORKER_ARCHIVE="$worker_archive_candidate"
      break
    fi
  done
fi
if [[ -n "$WORKER_ARCHIVE" ]]; then
  WORKER_ARCHIVE="$(canonical_local_path "$WORKER_ARCHIVE")"
  reject_release_local_state "Worker release archive" "$WORKER_ARCHIVE"
fi
if [[ -z "$SSH_CREDENTIAL_KEY" ]]; then
  if [[ "$WORKER_SSH" == *@* ]]; then
    SSH_CREDENTIAL_KEY="${WORKER_SSH%@*}"
  else
    SSH_CREDENTIAL_KEY="$(id -un)"
  fi
fi

CREDENTIALS_FILE="$(canonical_local_path "$CREDENTIALS_FILE")"
MCP_AUTH_FILE="$(canonical_local_path "$MCP_AUTH_FILE")"
LOG_ROOT="$(canonical_local_path "$LOG_ROOT")"
reject_release_local_state "credentials file" "$CREDENTIALS_FILE"
reject_release_local_state "MCP authentication file" "$MCP_AUTH_FILE"
reject_release_local_state "rollout log root" "$LOG_ROOT"
if [[ -n "$CONTROL_PLANE_STATE_SOURCE" ]]; then
  CONTROL_PLANE_STATE_SOURCE="$(canonical_local_path "$CONTROL_PLANE_STATE_SOURCE")"
  reject_release_local_state "upgrade state source" "$CONTROL_PLANE_STATE_SOURCE"
fi
validate_identifier SITE_ID "$SITE_ID"
validate_identifier NODE_ID "$NODE_ID"
[[ "$NODE_ID" == "fn-01" ]] || {
  echo "BLOCKED: automation_system_v0101 installs fixed fn-01 certificate filenames" >&2
  echo "Use NODE_ID=fn-01 or release a compatible Worker package first" >&2
  exit 65
}
validate_identifier WORKER_PROFILE "$WORKER_PROFILE"
validate_identifier WORKER_EXPECTED_VERSION "$WORKER_EXPECTED_VERSION"
validate_sha256 WORKER_ARCHIVE_SHA256 "$WORKER_ARCHIVE_SHA256"
validate_sha256 WORKER_MANIFEST_SHA256 "$WORKER_MANIFEST_SHA256"
case "$WORKER_CONFIG_DRIFT_POLICY" in
  preserve|strict) ;;
  *)
    echo "BLOCKED: WORKER_CONFIG_DRIFT_POLICY must be preserve or strict" >&2
    exit 64
    ;;
esac
validate_compatible_revisions "$REUSE_COMPATIBLE_REVISIONS"
validate_identifier CONTROL_PLANE_PROFILE "$CONTROL_PLANE_PROFILE"
validate_identifier ADMIN_USERNAME "$ADMIN_USERNAME"
validate_identifier MCP_MQTT_SOURCE_INSTANCE_ID "$MCP_MQTT_SOURCE_INSTANCE_ID"
[[ "$MCP_AUTH_CREDENTIAL_KEY" =~ ^[A-Za-z0-9][A-Za-z0-9._-]{0,255}$ ]] || {
  echo "BLOCKED: invalid MCP_AUTH_CREDENTIAL_KEY" >&2
  exit 64
}
[[ "$MCP_CLIENT_ID" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$ ]] || {
  echo "BLOCKED: invalid MCP_CLIENT_ID" >&2
  exit 64
}
[[ "$MCP_SUBJECT" =~ ^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$ ]] || {
  echo "BLOCKED: invalid MCP_SUBJECT" >&2
  exit 64
}
validate_positive_integer CONVERGENCE_TIMEOUT_S "$CONVERGENCE_TIMEOUT_S"
validate_positive_integer TELEMETRY_TIMEOUT_S "$TELEMETRY_TIMEOUT_S"
validate_positive_integer PROXY_WSS_PORT "$PROXY_WSS_PORT"
validate_positive_integer PROXY_HTTPS_PORT "$PROXY_HTTPS_PORT"
validate_positive_integer MCP_HTTP_PORT "$MCP_HTTP_PORT"
validate_positive_integer ACTUATOR_PORT "$ACTUATOR_PORT"
[[ "$WORKER_SSH" =~ ^([^@[:space:]]+@)?[^@[:space:]]+$ ]] || {
  echo "BLOCKED: invalid WORKER_SSH target" >&2
  exit 64
}
[[ -s "$e2e_version_file" ]] || {
  echo "BLOCKED: active E2E package is incomplete: $e2e_dir" >&2
  exit 66
}
if (( PURGE_HOST == 1 )) && [[ "$DEPLOYMENT_MODE" != fresh ]]; then
  echo "BLOCKED: --purge-host is valid only with --fresh" >&2
  exit 64
fi
if [[ "$DEPLOYMENT_MODE" == upgrade && -z "$CONTROL_PLANE_STATE_SOURCE" ]]; then
  echo "BLOCKED: --upgrade requires CONTROL_PLANE_STATE_SOURCE in environment/config" >&2
  exit 64
fi
if [[ "$ACTION" == run && "$DEPLOYMENT_MODE" == reuse ]]; then
  REUSE_PRESERVE_WORKER=1
  [[ "$RESET_POLICY" != yes ]] || {
    echo "BLOCKED: --reuse preserves the active Worker and cannot reset its replica journal" >&2
    echo "Use --fresh or --upgrade for a controlled Worker restart." >&2
    exit 64
  }
fi
if [[ "$ACTION" == run && "$SKIP_WORKER_RESTART" == 1 && "$SKIP_PKI" == 0 ]]; then
  echo "BLOCKED: --skip-worker-restart requires --skip-pki because a transferred identity would not become active" >&2
  exit 64
fi
if [[ "$ACTION" == run && "$DEPLOYMENT_MODE" != reuse ]] \
    && (( SKIP_WORKER_RESTART == 1 || SKIP_PKI == 1 )); then
  echo "BLOCKED: v0026 fresh/upgrade installs Worker v0101 and therefore requires identity installation plus a controlled restart" >&2
  exit 64
fi
if [[ "$ACTION" == run && "$SKIP_WORKER_RESTART" == 1 && "$RESET_POLICY" == yes ]]; then
  echo "BLOCKED: --reset-worker-replica requires a controlled Worker restart" >&2
  exit 64
fi
if [[ "$DEPLOYMENT_MODE" == fresh ]]; then
  [[ "$RESET_POLICY" != no ]] || {
    echo "BLOCKED: a fresh Control Plane cannot preserve a divergent Worker replica" >&2
    exit 64
  }
  (( SKIP_PKI == 0 )) || {
    echo "BLOCKED: a fresh Control Plane requires the newly generated MQTT identity" >&2
    exit 64
  }
  (( SKIP_WORKER_RESTART == 0 )) || {
    echo "BLOCKED: a fresh Control Plane requires a controlled Worker restart" >&2
    exit 64
  }
fi
if [[ "$RESET_POLICY" == auto ]]; then
  [[ "$DEPLOYMENT_MODE" == fresh ]] && RESET_WORKER_REPLICA=yes \
    || RESET_WORKER_REPLICA=no
else
  RESET_WORKER_REPLICA="$RESET_POLICY"
fi

run_id="$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 0700 "$LOG_ROOT"
log_dir="$LOG_ROOT/$run_id"
install -d -m 0700 "$log_dir"
node_local_proposal_proof_file="$log_dir/node-local-proposal-proof.json"
main_log="$log_dir/rollout.log"
touch "$main_log"
chmod 0600 "$main_log"
exec > >(tee -a "$main_log") 2>&1

current_stage=initialization
runtime_revision=""
ssh_runtime=""
ssh_socket=""
ssh_master_active=0
remote_worker_archive_relative=".cache/automation-control-plane/releases/${WORKER_EXPECTED_VERSION}/${WORKER_EXPECTED_VERSION}.zip"
automation_lock="${TMPDIR:-/tmp}/automation-control-plane-automation-${UID}.lock"
automation_lock_owned=0

acquire_automation_lock() {
  if mkdir "$automation_lock" 2>/dev/null; then
    printf '%s\n' "$$" >"$automation_lock/pid"
    automation_lock_owned=1
    return 0
  fi
  owner=""
  [[ -r "$automation_lock/pid" ]] && owner="$(<"$automation_lock/pid")"
  if [[ "$owner" =~ ^[1-9][0-9]*$ ]] && kill -0 "$owner" 2>/dev/null; then
    echo "BLOCKED: another rollout automation process is active: PID=$owner" >&2
    return 75
  fi
  rm -rf -- "$automation_lock"
  mkdir "$automation_lock"
  printf '%s\n' "$$" >"$automation_lock/pid"
  automation_lock_owned=1
}

release_automation_lock() {
  (( automation_lock_owned == 1 )) || return 0
  if [[ -r "$automation_lock/pid" && "$(<"$automation_lock/pid")" == "$$" ]]; then
    rm -rf -- "$automation_lock"
  fi
  automation_lock_owned=0
}

ssh_master_is_alive() {
  [[ -n "$ssh_socket" && -S "$ssh_socket" ]] || return 1
  ssh -S "$ssh_socket" \
    -O check \
    -o BatchMode=yes \
    -o ConnectTimeout=5 \
    "$WORKER_SSH" </dev/null >/dev/null 2>&1
}

initialize_ssh_runtime() {
  if [[ -z "$ssh_runtime" ]]; then
    ssh_runtime="$(mktemp -d "${TMPDIR:-/tmp}/automation-rollout-ssh.XXXXXX")"
    chmod 0700 "$ssh_runtime"
    ssh_socket="$ssh_runtime/control"
  fi
}

close_ssh_master() {
  if ssh_master_is_alive; then
    ssh -S "$ssh_socket" -O exit -o BatchMode=yes \
      "$WORKER_SSH" </dev/null >/dev/null 2>&1 || true
  fi
  ssh_master_active=0
  if [[ -n "$ssh_runtime" ]]; then
    rm -rf -- "$ssh_runtime"
    ssh_runtime=""
    ssh_socket=""
  fi
}

start_ssh_master() {
  local ssh_user="${WORKER_SSH%@*}"
  local key_auth_log=""
  local options=()

  if ssh_master_is_alive; then
    ssh_master_active=1
    return 0
  fi
  if [[ "$ssh_user" == "$WORKER_SSH" ]]; then
    ssh_user="$(id -un)"
  fi
  [[ -n "$SSH_CREDENTIAL_KEY" ]] || SSH_CREDENTIAL_KEY="$ssh_user"
  initialize_ssh_runtime
  rm -f -- "$ssh_socket"
  key_auth_log="$ssh_runtime/key-authentication.log"
  options=(
    -S "$ssh_socket"
    -o ControlPersist=yes
    -o ConnectTimeout=10
    -o ConnectionAttempts=1
    -o ServerAliveInterval=15
    -o ServerAliveCountMax=3
    -o StrictHostKeyChecking=accept-new
    -o NumberOfPasswordPrompts=1
  )

  if ssh "${options[@]}" \
      -o BatchMode=yes \
      -o PasswordAuthentication=no \
      -M -N -f "$WORKER_SSH" </dev/null \
      >"$key_auth_log" 2>&1; then
    echo "Worker SSH authentication: public key / agent"
  else
    rm -f -- "$ssh_socket"
    test -s "$CREDENTIALS_FILE" || {
      echo "BLOCKED: SSH key authentication failed and credentials file is missing" >&2
      return 67
    }
    "$root_dir/tools/credential_reader.py" \
      --file "$CREDENTIALS_FILE" \
      --key "$SSH_CREDENTIAL_KEY" \
      --key "ssh.$SSH_CREDENTIAL_KEY" \
      --check-only
    command -v setsid >/dev/null 2>&1 || {
      echo "BLOCKED: setsid is required for non-interactive SSH password authentication" >&2
      return 69
    }
    if ! setsid -w env \
      DISPLAY=automation-rollout:0 \
      PYTHONDONTWRITEBYTECODE=1 \
      SSH_ASKPASS_REQUIRE=force \
      SSH_ASKPASS="$root_dir/tools/ssh_askpass.py" \
      AUTOMATION_CREDENTIALS_FILE="$CREDENTIALS_FILE" \
      AUTOMATION_SSH_CREDENTIAL_KEY="$SSH_CREDENTIAL_KEY" \
      ssh "${options[@]}" -o BatchMode=no -M -N -f \
        "$WORKER_SSH" </dev/null; then
      echo "BLOCKED: Worker SSH authentication failed" >&2
      [[ -s "$key_auth_log" ]] && sed 's/^/  key-auth: /' "$key_auth_log" >&2
      return 67
    fi
    echo "Worker SSH authentication: protected credentials JSON"
  fi

  if ! ssh_master_is_alive; then
    echo "BLOCKED: Worker SSH ControlMaster did not become ready" >&2
    return 67
  fi
  ssh_master_active=1
}

ensure_ssh_master() {
  if ssh_master_is_alive; then
    ssh_master_active=1
    return 0
  fi
  if (( ssh_master_active == 1 )); then
    echo "Worker SSH ControlMaster was lost; rebuilding it non-interactively."
  fi
  ssh_master_active=0
  [[ -n "$ssh_socket" ]] && rm -f -- "$ssh_socket"
  start_ssh_master
}

remote_ssh() {
  ensure_ssh_master
  ssh -S "$ssh_socket" \
    -o ControlMaster=no \
    -o BatchMode=yes \
    -o NumberOfPasswordPrompts=0 \
    -o ConnectTimeout=10 \
    "$WORKER_SSH" "$@"
}

remote_lifecycle() {
  local action="$1"
  local reset_value="${2:-no}"
  local remote_command=""
  printf -v remote_command \
    'bash -s -- %q %q %q %q %q %q %q %q %q %q %q' \
    "$action" "$WORKER_PATH" "$SITE_ID" "$NODE_ID" "$WORKER_PROFILE" \
    "$WORKER_PKI_STAGE" "$WORKER_LOG_PATH" "$reset_value" \
    "$WORKER_EXPECTED_VERSION" "$WORKER_MANIFEST_SHA256" \
    "$WORKER_CONFIG_DRIFT_POLICY"
  remote_ssh "$remote_command" <"$root_dir/tools/worker_remote_lifecycle.sh"
}

run_logged() {
  local filename="$1"
  shift
  "$@" 2>&1 | tee "$filename"
  return "${PIPESTATUS[0]}"
}

run_step() {
  local label="$1"
  shift
  current_stage="$label"
  printf '\n==> %s\n' "$label"
  "$@"
  printf 'PASS: %s\n' "$label"
}

prepare_mcp_auth_document() {
  "$root_dir/tools/prepare_mcp_auth.py" \
    --credentials-file "$CREDENTIALS_FILE" \
    --credential-key "$MCP_AUTH_CREDENTIAL_KEY" \
    --credential-key "mcp_controller.local_agent.token" \
    --output "$MCP_AUTH_FILE" \
    --client-id "$MCP_CLIENT_ID" \
    --subject "$MCP_SUBJECT" \
    --site-id "$SITE_ID"
}

verify_local_prerequisites() {
  local command_name=""
  for command_name in docker openssl python3 readlink sha256sum ssh tar; do
    command -v "$command_name" >/dev/null 2>&1 || {
      echo "BLOCKED: required command is missing: $command_name" >&2
      return 69
    }
  done
  docker compose version >/dev/null
  docker info >/dev/null
  VERIFY_CHECKSUMS_QUIET=1 "$root_dir/tools/verify_checksums.sh"
  if [[ "$ACTION" == "run" ]]; then
    prepare_mcp_auth_document
  fi
  echo "Release package integrity: $version/$revision PASS"
}

verify_worker_release_archive() {
  local actual_sha256=""
  local expected_sidecar=""
  local sidecar="${WORKER_ARCHIVE}.sha256"
  [[ -n "$WORKER_ARCHIVE" ]] || {
    echo "BLOCKED: Worker archive ${WORKER_EXPECTED_VERSION}.zip was not found" >&2
    echo "Place it beside rollout_automation.sh or pass --worker-archive FILE." >&2
    return 66
  }
  test -f "$WORKER_ARCHIVE" && [[ ! -L "$WORKER_ARCHIVE" ]] || {
    echo "BLOCKED: Worker release archive is missing or unsafe: $WORKER_ARCHIVE" >&2
    return 66
  }
  test -f "$sidecar" && [[ ! -L "$sidecar" ]] || {
    echo "BLOCKED: Worker archive SHA-256 sidecar is missing or unsafe: $sidecar" >&2
    return 66
  }
  read -r actual_sha256 _ < <(sha256sum -- "$WORKER_ARCHIVE")
  [[ "$actual_sha256" == "$WORKER_ARCHIVE_SHA256" ]] || {
    echo "BLOCKED: Worker archive differs from the $revision release anchor" >&2
    echo "Expected: $WORKER_ARCHIVE_SHA256" >&2
    echo "Actual:   $actual_sha256" >&2
    return 65
  }
  expected_sidecar="$WORKER_ARCHIVE_SHA256  $(basename -- "$WORKER_ARCHIVE")"
  [[ "$(<"$sidecar")" == "$expected_sidecar" ]] || {
    echo "BLOCKED: Worker archive sidecar does not match the anchored package" >&2
    return 65
  }
  printf 'Worker release archive: PASS (%s)\n' "$actual_sha256"
}

transfer_worker_release_archive() {
  local inner=""
  local outer=""
  printf -v inner '%s' \
    'set -Eeuo pipefail; umask 077; relative="$1"; target="$HOME/$relative"; parent="${target%/*}"; case "$target" in "$HOME"/*) ;; *) echo "BLOCKED: Worker archive stage escapes HOME" >&2; exit 64;; esac; install -d -m 0700 "$parent"; temporary="$target.tmp.$$"; trap '\''rm -f -- "$temporary"'\'' EXIT; cat >"$temporary"; chmod 0600 "$temporary"; mv -- "$temporary" "$target"; trap - EXIT'
  printf -v outer 'bash -c %q -- %q' "$inner" "$remote_worker_archive_relative"
  remote_ssh "$outer" <"$WORKER_ARCHIVE"
  echo "Worker release archive transferred to protected remote staging."
}

run_remote_worker_installer() {
  local verify_only="${1:-no}"
  local inner=""
  local outer=""
  local optional=()
  [[ "$verify_only" == yes ]] && optional=(--verify-only)
  printf -v inner '%s' \
    'set -Eeuo pipefail; worker_value="$1"; archive_relative="$2"; expected_version="$3"; expected_archive="$4"; expected_manifest="$5"; config_drift_policy="$6"; shift 6; case "$worker_value" in /*) worker="$worker_value";; "~/"*) worker="$HOME/${worker_value#~/}";; "\$HOME/"*) worker="$HOME/${worker_value#\$HOME/}";; *) worker="$HOME/$worker_value";; esac; worker="$(readlink -m -- "$worker")"; case "$worker" in "$HOME"/*) ;; *) echo "BLOCKED: Worker install target escapes HOME" >&2; exit 64;; esac; install_root="${worker%/*}"; exec python3 - --archive "$HOME/$archive_relative" --home "$HOME" --install-root "$install_root" --old-root "$HOME/__OLD__" --expected-version "$expected_version" --expected-sha256 "$expected_archive" --expected-manifest-sha256 "$expected_manifest" --config-drift-policy "$config_drift_policy" "$@"'
  printf -v outer 'bash -c %q -- %q %q %q %q %q %q' \
    "$inner" "$WORKER_PATH" "$remote_worker_archive_relative" \
    "$WORKER_EXPECTED_VERSION" "$WORKER_ARCHIVE_SHA256" \
    "$WORKER_MANIFEST_SHA256" "$WORKER_CONFIG_DRIFT_POLICY"
  if ((${#optional[@]})); then
    printf -v outer '%s %q' "$outer" "${optional[0]}"
  fi
  remote_ssh "$outer" <"$root_dir/tools/install_worker_release.py"
}

prepare_worker_release() {
  verify_worker_release_archive
  transfer_worker_release_archive
  run_remote_worker_installer yes
}

install_worker_release() {
  run_remote_worker_installer no
}

verify_worker_baseline() {
  remote_lifecycle verify no
}

verify_active_worker_identity() {
  remote_lifecycle verify-active no
}

stop_worker_for_rollout() {
  remote_lifecycle stop-compatible no
}

control_plane_rollout() {
  export CONTROL_PLANE_BIND_ADDRESS MQTT_BIND_ADDRESS MQTT_TEST_HOST
  export MQTT_TEST_BROKER_IP PROXY_BIND_ADDRESS PROXY_WSS_PORT
  export PROXY_HTTPS_PORT PROXY_ORIGIN_ALLOWLIST
  export MCP_BIND_ADDRESS MCP_HTTP_PORT MCP_MQTT_SOURCE_INSTANCE_ID
  export MCP_AUTH_SOURCE="$MCP_AUTH_FILE" AUTOMATION_SITE_ID="$SITE_ID"
  case "$DEPLOYMENT_MODE" in
    reuse)
      echo "Reusing the deployed Control Plane; no images or volumes are rebuilt."
      ;;
    fresh)
      local arguments=(--fresh --profile "$CONTROL_PLANE_PROFILE")
      if (( PURGE_HOST == 1 )); then
        export CONTROL_PLANE_CONFIRM_HOST_PURGE=PURGE
        arguments+=(--purge-host)
      fi
      run_logged "$log_dir/control-plane-rollout.log" \
        "$root_dir/rollout.sh" "${arguments[@]}"
      ;;
    upgrade)
      export CONTROL_PLANE_STATE_SOURCE
      run_logged "$log_dir/control-plane-rollout.log" \
        "$root_dir/rollout.sh" --upgrade --profile "$CONTROL_PLANE_PROFILE"
      ;;
  esac
}

revision_is_compatible_for_reuse() {
  local requested="$1"
  local candidate=""
  local requested_serial=""
  local requested_r=""
  local current_serial=""
  local current_r=""
  local revisions=()

  if [[ "$requested" =~ ^v([0-9]{4,})-r([1-9][0-9]*)$ ]]; then
    requested_serial="${BASH_REMATCH[1]}"
    requested_r="${BASH_REMATCH[2]}"
  fi
  if [[ "$revision" =~ ^v([0-9]{4,})-r([1-9][0-9]*)$ ]]; then
    current_serial="${BASH_REMATCH[1]}"
    current_r="${BASH_REMATCH[2]}"
  fi
  if [[ -n "$requested_serial" && "$requested_serial" == "$current_serial" ]] \
      && (( 10#$requested_r <= 10#$current_r )); then
    return 0
  fi

  IFS=',' read -r -a revisions <<<"$REUSE_COMPATIBLE_REVISIONS"
  for candidate in "${revisions[@]}"; do
    [[ "$candidate" == "$requested" ]] && return 0
  done
  return 1
}

read_container_label() {
  local package="$1"
  local service="$2"
  local label="$3"
  local container_id=""
  container_id="$(cd "$root_dir/$package" && docker compose ps -q "$service")"
  [[ -n "$container_id" ]] || return 69
  docker inspect --format \
    "{{with .Config.Labels}}{{index . \"$label\"}}{{end}}" \
    "$container_id"
}

resolve_runtime_revision() {
  local master_version=""
  local proxy_version=""
  local master_revision=""
  local proxy_revision=""

  if [[ "$DEPLOYMENT_MODE" != "reuse" ]]; then
    runtime_revision="$revision"
    export CONTROL_PLANE_EXPECTED_REVISION="$runtime_revision"
    return 0
  fi

  master_version="$(read_container_label master_node master-node \
    org.opencontainers.image.version)" || {
      echo "BLOCKED: running Master container is missing" >&2
      return 69
    }
  proxy_version="$(read_container_label proxy_gateway proxy-gateway \
    org.opencontainers.image.version)" || {
      echo "BLOCKED: running Proxy container is missing" >&2
      return 69
    }
  master_revision="$(read_container_label master_node master-node \
    org.opencontainers.image.revision)" || return 69
  proxy_revision="$(read_container_label proxy_gateway proxy-gateway \
    org.opencontainers.image.revision)" || return 69

  [[ "$master_version" == "$version" && "$proxy_version" == "$version" ]] || {
    echo "BLOCKED: --reuse requires running Master and Proxy version $version" >&2
    echo "Master version: ${master_version:-missing}" >&2
    echo "Proxy version:  ${proxy_version:-missing}" >&2
    return 70
  }
  [[ -n "$master_revision" && "$master_revision" == "$proxy_revision" ]] || {
    echo "BLOCKED: running Master and Proxy revisions differ" >&2
    echo "Master revision: ${master_revision:-missing}" >&2
    echo "Proxy revision:  ${proxy_revision:-missing}" >&2
    return 70
  }
  revision_is_compatible_for_reuse "$master_revision" || {
    echo "BLOCKED: runtime revision is not compatible with $revision reuse mode" >&2
    echo "Runtime revision: $master_revision" >&2
    echo "Compatible runtime revisions: same release series up to $revision; optional allow-list=$REUSE_COMPATIBLE_REVISIONS" >&2
    return 70
  }

  runtime_revision="$master_revision"
  export CONTROL_PLANE_EXPECTED_REVISION="$runtime_revision"
  if [[ "$runtime_revision" != "$revision" ]]; then
    echo "Compatible runtime reuse: package $revision operates existing $version/$runtime_revision images."
    echo "A later fresh/upgrade rollout can rebuild OCI labels as $revision."
  else
    echo "Running Control Plane revision: $runtime_revision"
  fi
}

verify_control_plane() {
  "$root_dir/rollout.sh" status
  "$root_dir/rollout.sh" test-running
  "$root_dir/master_node/test_runtime_state.sh"
}

read_admin_password() {
  "$root_dir/tools/credential_reader.py" \
    --file "$CREDENTIALS_FILE" \
    --key "$ADMIN_CREDENTIAL_KEY" \
    --key "control_plane.$ADMIN_CREDENTIAL_KEY" \
    --key "control_plane.admin"
}

bootstrap_administrator() {
  local password=""
  password="$(read_admin_password)"
  printf '%s\n' "$password" | \
    "$root_dir/proxy_gateway/create_admin.sh" \
      --username "$ADMIN_USERNAME" --password-stdin --allow-existing
  unset password
}

verify_and_transfer_pki() {
  local ca="$root_dir/mqtt_broker/test-pki/ca/ca.crt"
  local cert="$root_dir/mqtt_broker/test-pki/clients/worker-$NODE_ID/mqtt.crt"
  local key="$root_dir/mqtt_broker/test-pki/clients/worker-$NODE_ID/mqtt.key"
  local identity="urn:automation:${SITE_ID}:worker:${NODE_ID}"
  local certificate_public_key=""
  local private_public_key=""
  local inner=""
  local outer=""
  for filename in "$ca" "$cert" "$key"; do
    test -s "$filename" || {
      echo "BLOCKED: generated Worker PKI file is missing: $filename" >&2
      return 66
    }
  done
  openssl verify -purpose sslclient -CAfile "$ca" "$cert"
  openssl x509 -in "$cert" -noout -checkend 0 >/dev/null
  openssl pkey -in "$key" -noout -check >/dev/null
  certificate_public_key="$(openssl x509 -in "$cert" -pubkey -noout | openssl sha256)"
  private_public_key="$(openssl pkey -in "$key" -pubout | openssl sha256)"
  [[ "$certificate_public_key" == "$private_public_key" ]] || {
    echo "BLOCKED: generated Worker certificate/private key mismatch" >&2
    return 65
  }
  openssl x509 -in "$cert" -noout -ext subjectAltName | \
    tr ',' '\n' | sed 's/^[[:space:]]*//' | grep -Fx "URI:$identity" >/dev/null
  openssl x509 -in "$cert" \
    -noout -subject -issuer -dates -fingerprint -sha256 | \
    tee "$log_dir/worker-certificate.txt"

  printf -v inner '%s' \
    'set -Eeuo pipefail; umask 077; stage_value="$1"; worker_value="$2"; case "$stage_value" in /*) stage="$stage_value";; "~/"*) stage="$HOME/${stage_value#~/}";; "\$HOME/"*) stage="$HOME/${stage_value#\$HOME/}";; *) stage="$HOME/$stage_value";; esac; case "$worker_value" in /*) worker="$worker_value";; "~/"*) worker="$HOME/${worker_value#~/}";; "\$HOME/"*) worker="$HOME/${worker_value#\$HOME/}";; *) worker="$HOME/$worker_value";; esac; stage="$(readlink -m -- "$stage")"; worker="$(readlink -m -- "$worker")"; home="$(readlink -f -- "$HOME")"; for path in "$stage" "$worker"; do case "$path" in "$home"/*) ;; *) echo "BLOCKED: remote Worker paths must be below HOME" >&2; exit 64;; esac; done; case "$stage" in "$worker"|"$worker"/*) echo "BLOCKED: remote PKI stage must be outside the Worker package" >&2; exit 64;; esac; case "$worker/" in "$stage/"*) echo "BLOCKED: remote PKI stage must not contain the Worker package" >&2; exit 64;; esac; rm -rf -- "$stage"; install -d -m 0700 "$stage"; tar --no-same-owner --no-same-permissions -C "$stage" -xf -; chmod 0644 "$stage/ca.crt" "$stage/mqtt.crt"; chmod 0600 "$stage/mqtt.key"'
  printf -v outer 'bash -c %q -- %q %q' \
    "$inner" "$WORKER_PKI_STAGE" "$WORKER_PATH"
  tar --create --file=- \
    --directory="$(dirname "$ca")" "$(basename "$ca")" \
    --directory="$(dirname "$cert")" "$(basename "$cert")" "$(basename "$key")" | \
    remote_ssh "$outer"
  echo "Worker MQTT identity transferred without a persistent local private-key copy."
}

install_and_start_worker() {
  if (( SKIP_PKI == 0 )); then
    remote_lifecycle install-start "$RESET_WORKER_REPLICA"
  else
    remote_lifecycle restart "$RESET_WORKER_REPLICA"
  fi
}

worker_convergence_test() {
  AUTOMATION_SITE_ID="$SITE_ID" \
    "$root_dir/test_worker_auto_admission.sh" "$NODE_ID" "$CONVERGENCE_TIMEOUT_S"
}

worker_telemetry_test() {
  AUTOMATION_SITE_ID="$SITE_ID" \
    "$root_dir/test_worker_sensor_telemetry.sh" "$NODE_ID" "$TELEMETRY_TIMEOUT_S"
}

read_worker_telemetry_anchor() {
  (
    cd "$root_dir/postgres_system"
    docker compose exec -T -u postgres postgres-primary \
      psql -d automation_platform -At -v ON_ERROR_STOP=1 -c "
SELECT
  COALESCE((
    SELECT max(source_sequence)
      FROM master_control.sensor_latest
     WHERE site_id='$SITE_ID' AND source_node_id='$NODE_ID'
  ),0)::text || '|' ||
  COALESCE((
    SELECT max(event_sequence)
      FROM proxy_gateway.event_replay_cache
     WHERE site_id='$SITE_ID' AND event_type='sensor.value'
       AND envelope #>> '{payload,source_node_id}'='$NODE_ID'
  ),0)::text || '|' ||
  COALESCE((
    SELECT runtime_nonce
      FROM master_control.nodes
     WHERE site_id='$SITE_ID' AND node_id='$NODE_ID'
  ),'');"
  )
}

worker_restart_sequence_recovery_test() {
  local before=""
  local after=""
  local before_source=""
  local before_proxy=""
  local before_nonce=""
  local after_source=""
  local after_proxy=""
  local after_nonce=""
  before="$(read_worker_telemetry_anchor)"
  IFS='|' read -r before_source before_proxy before_nonce <<<"$before"
  [[ "$before_source" =~ ^[1-9][0-9]*$ \
      && "$before_proxy" =~ ^[1-9][0-9]*$ \
      && -n "$before_nonce" ]] || {
    echo "BLOCKED: Worker restart baseline is incomplete: $before" >&2
    return 70
  }
  remote_lifecycle restart no
  worker_convergence_test
  WORKER_TELEMETRY_MIN_SOURCE_SEQUENCE="$before_source" \
    WORKER_TELEMETRY_MIN_PROXY_SEQUENCE="$before_proxy" \
    worker_telemetry_test
  after="$(read_worker_telemetry_anchor)"
  IFS='|' read -r after_source after_proxy after_nonce <<<"$after"
  [[ "$after_source" =~ ^[1-9][0-9]*$ \
      && "$after_proxy" =~ ^[1-9][0-9]*$ \
      && -n "$after_nonce" ]] || {
    echo "FAILED: Worker restart result is incomplete: $after" >&2
    return 70
  }
  (( after_source > before_source && after_proxy > before_proxy )) || {
    echo "FAILED: telemetry did not cross the pre-restart high-water marks" >&2
    echo "Before: $before" >&2
    echo "After:  $after" >&2
    return 70
  }
  [[ "$after_nonce" != "$before_nonce" ]] || {
    echo "FAILED: Worker runtime nonce did not change across restart" >&2
    return 70
  }
  printf 'PASS Worker restart recovery: runtime_nonce changed; source_sequence %s->%s; proxy_event_sequence %s->%s.\n' \
    "$before_source" "$after_source" "$before_proxy" "$after_proxy"
}

worker_fieldbus_prerequisite() {
  local outer=""
  local program=""
  program='import socket,sys; host=sys.argv[1]; port=int(sys.argv[2]); connection=socket.create_connection((host,port),5.0); connection.close(); print("PASS Worker fieldbus prerequisite: {}:{} reachable".format(host,port))'
  printf -v outer 'python3 -c %q %q %q' \
    "$program" "$ACTUATOR_HOST" "$ACTUATOR_PORT"
  remote_ssh "$outer"
}

worker_config_file_e2e_test() {
  local inner=""
  local outer=""
  local result_file="$log_dir/worker-config-file-e2e-result.json"
  local trace_file="$log_dir/worker-config-file-e2e-trace.log"
  local proof_fields=()
  mapfile -t proof_fields < <(
    PYTHONDONTWRITEBYTECODE=1 python3 \
      "$root_dir/tools/read_node_local_proposal_proof.py" \
      --proof "$node_local_proposal_proof_file"
  )
  (( ${#proof_fields[@]} == 10 )) || {
    echo "FAILED: current Core-E2E node-local proposal proof is incomplete" >&2
    return 65
  }
  printf -v inner '%s' \
    'set -Eeuo pipefail; worker_value="$1"; node="$2"; timeout="$3"; log="$4"; case "$worker_value" in /*) worker="$worker_value";; "~/"*) worker="$HOME/${worker_value#~/}";; "\$HOME/"*) worker="$HOME/${worker_value#\$HOME/}";; *) worker="$HOME/$worker_value";; esac; worker="$(readlink -m -- "$worker")"; case "$worker" in "$HOME"/*) ;; *) echo "BLOCKED: Worker E2E path escapes HOME" >&2; exit 64;; esac; export PYTHONDONTWRITEBYTECODE=1; export PYTHONPYCACHEPREFIX="/tmp/automation_system_pycache_$(id -u)"; exec python3 - --worker-root "$worker" --worker-log "$log" --node-id "$node" --timeout "$timeout" --core-operation-id "$5" --core-relay-message-id "$6" --core-authority-epoch "$7" --core-base-scope-revision "$8" --core-target-scope-revision "$9" --core-target-scope-fingerprint "${10}" --core-node-replica-revision "${11}" --core-node-replica-fingerprint "${12}" --core-operation-format "${13}" --core-operation-count "${14}" --approved-actuator-host "${15}" --approved-actuator-port "${16}"'
  printf -v outer '%q ' bash -c "$inner" -- \
    "$WORKER_PATH" "$NODE_ID" "$CONVERGENCE_TIMEOUT_S" \
    "$WORKER_LOG_PATH" "${proof_fields[0]}" "${proof_fields[1]}" \
    "${proof_fields[2]}" "${proof_fields[3]}" "${proof_fields[4]}" \
    "${proof_fields[5]}" "${proof_fields[6]}" "${proof_fields[7]}" \
    "${proof_fields[8]}" "${proof_fields[9]}" \
    "$ACTUATOR_HOST" "$ACTUATOR_PORT"
  outer="${outer% }"
  set +e
  remote_ssh "$outer" \
    <"$root_dir/integration_test/e2e_v0025/worker_config_file_e2e.py" \
    >"$result_file" 2>"$trace_file"
  local remote_result=$?
  set -e
  [[ ! -s "$trace_file" ]] || cat "$trace_file"
  [[ ! -s "$result_file" ]] || cat "$result_file"
  (( remote_result == 0 )) || return "$remote_result"
  PYTHONDONTWRITEBYTECODE=1 python3 \
    "$root_dir/tools/verify_worker_config_file_e2e.py" --result "$result_file"
}

core_e2e_test() {
  local password=""
  password="$(read_admin_password)"
  export AUTOMATION_ADMIN_PASSWORD="$password"
  export AUTOMATION_SITE_ID="$SITE_ID"
  export E2E_NODE_ID="$NODE_ID"
  export E2E_WORKER_TIMEOUT_S="$CONVERGENCE_TIMEOUT_S"
  export E2E_HTTPS_URL="https://$PROXY_BIND_ADDRESS:$PROXY_HTTPS_PORT"
  export E2E_WSS_URL="wss://$PROXY_BIND_ADDRESS:$PROXY_WSS_PORT"
  export E2E_ORIGIN="$E2E_ORIGIN"
  export E2E_APPROVED_ACTUATOR_HOST="$ACTUATOR_HOST"
  export E2E_APPROVED_ACTUATOR_PORT="$ACTUATOR_PORT"
  set +e
  run_logged "$log_dir/core-e2e.log" "$root_dir/run_core_e2e_lab.sh"
  local result=$?
  set -e
  unset AUTOMATION_ADMIN_PASSWORD AUTOMATION_SITE_ID password
  (( result == 0 )) || return "$result"
  test -s "$e2e_result"
  "$root_dir/tools/verify_core_e2e_result.py" \
    --result "$e2e_result" --version-file "$e2e_version_file" \
    --node-local-proposal-proof-output "$node_local_proposal_proof_file"
}

collect_local_diagnostics() {
  {
    echo "Control Plane package: $version/$revision"
    echo "Control Plane runtime revision: ${runtime_revision:-unresolved}"
    echo "Action/mode: $ACTION/$DEPLOYMENT_MODE"
    date -u '+UTC: %Y-%m-%dT%H:%M:%SZ'
    docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}' || true
    "$root_dir/rollout.sh" status || true
    "$root_dir/master_node/test_runtime_state.sh" || true
    AUTOMATION_SITE_ID="$SITE_ID" \
      "$root_dir/diagnose_worker_sync.sh" "$NODE_ID" "$SITE_ID" || true
  } >"$log_dir/control-plane-diagnosis.log" 2>&1
  LOG_TAIL=500 "$root_dir/logs_all.sh" \
    >"$log_dir/control-plane-services.log" 2>&1 || true
  if [[ -d "$e2e_result_dir" ]]; then
    install -d -m 0700 "$log_dir/e2e-results"
    cp -a "$e2e_result_dir/." "$log_dir/e2e-results/" 2>/dev/null || true
    find "$log_dir/e2e-results" -type f -exec chmod 0600 {} + 2>/dev/null || true
  fi
}

collect_remote_diagnostics() {
  if (( ssh_master_active == 1 )) || [[ -n "$ssh_runtime" ]]; then
    if ensure_ssh_master; then
      remote_lifecycle diagnose no >"$log_dir/worker-diagnosis.log" 2>&1 || true
    fi
  fi
}

finish() {
  local result=$?
  trap - EXIT INT TERM
  set +e
  unset AUTOMATION_ADMIN_PASSWORD
  current_stage="${current_stage:-unknown}"
  collect_local_diagnostics
  collect_remote_diagnostics
  close_ssh_master
  release_automation_lock
  local status=PASSED
  (( result == 0 )) || status=FAILED
  "$root_dir/tools/write_rollout_summary.py" \
    --output "$log_dir/summary.json" \
    --run-id "$run_id" \
    --status "$status" \
    --exit-code "$result" \
    --stage "$current_stage" \
    --version "$version" \
    --revision "$revision" \
    --runtime-revision "${runtime_revision:-unresolved}" \
    --mode "$DEPLOYMENT_MODE" \
    --site-id "$SITE_ID" \
    --node-id "$NODE_ID" \
    --worker-target "$WORKER_SSH" \
    --log-directory "$log_dir"
  ln -sfnT "$run_id" "$LOG_ROOT/latest"
  printf '\nRollout automation result: %s (exit=%s)\n' "$status" "$result"
  echo "Log directory: $log_dir"
  echo "Summary: $log_dir/summary.json"
  exit "$result"
}
trap finish EXIT
trap 'exit 130' INT TERM

printf 'Automation Control Plane rollout: %s/%s\n' "$version" "$revision"
printf 'Selected release source: %s\n' "${AUTOMATION_ROLLOUT_SELECTED_SOURCE:-$root_dir}"
printf 'Release installation: %s\n' "${AUTOMATION_ROLLOUT_INSTALL_ACTION:-direct}"
printf 'Action=%s mode=%s site=%s node=%s worker=%s\n' \
  "$ACTION" "$DEPLOYMENT_MODE" "$SITE_ID" "$NODE_ID" "$WORKER_SSH"
printf 'MCP integration: source=%s bind=%s:%s auth=%s\n' \
  "$MCP_MQTT_SOURCE_INSTANCE_ID" "$MCP_BIND_ADDRESS" "$MCP_HTTP_PORT" "$MCP_AUTH_FILE"
printf 'Worker package policy: verified install for fresh/upgrade; preserve for reuse\n'
printf 'Worker integrity anchors: %s / archive=%s / manifest=%s\n' \
  "$WORKER_EXPECTED_VERSION" "$WORKER_ARCHIVE_SHA256" "$WORKER_MANIFEST_SHA256"
printf 'Worker configuration drift policy: %s\n' "$WORKER_CONFIG_DRIFT_POLICY"
printf 'Worker replica reset: %s\n' "$RESET_WORKER_REPLICA"
if (( REUSE_PRESERVE_WORKER == 1 )); then
  printf 'Worker reuse policy: preserve one active process, identity and telemetry sequence\n'
fi
printf 'Run ID: %s\n' "$run_id"

acquire_automation_lock
run_step "local release and host preflight" verify_local_prerequisites
run_step "Worker SSH session" start_ssh_master
if (( SKIP_E2E == 0 )) && [[ "$ACTION" == run || "$ACTION" == test ]]; then
  run_step "Worker fieldbus prerequisite" worker_fieldbus_prerequisite
fi

case "$ACTION" in
  status)
    run_step "resolve running Control Plane revision" resolve_runtime_revision
    run_step "Control Plane status" "$root_dir/rollout.sh" status
    run_step "Worker status" remote_lifecycle status no
    ;;
  diagnose)
    run_step "Worker package baseline" verify_worker_baseline
    run_step "Control Plane Worker-sync diagnosis" \
      "$root_dir/diagnose_worker_sync.sh" "$NODE_ID" "$SITE_ID"
    run_step "Worker diagnosis" remote_lifecycle diagnose no
    ;;
  test)
    run_step "Worker package baseline" verify_worker_baseline
    run_step "resolve running Control Plane revision" resolve_runtime_revision
    run_step "Control Plane running acceptance" verify_control_plane
    run_step "Worker Node-shard convergence" worker_convergence_test
    if (( SKIP_TELEMETRY == 0 )); then
      run_step "Worker live telemetry" worker_telemetry_test
    fi
    if (( SKIP_E2E == 0 )); then
      run_step "Core-E2E" core_e2e_test
      run_step "Worker JSON config-file E2E" worker_config_file_e2e_test
      run_step "post-file-E2E Worker convergence" worker_convergence_test
    fi
    ;;
  run)
    if (( REUSE_PRESERVE_WORKER == 0 )); then
      run_step "verify and stage Worker v0101 release" prepare_worker_release
      run_step "stop existing Worker before rollout" stop_worker_for_rollout
      run_step "recoverably install Worker v0101 release" install_worker_release
    fi
    run_step "Worker package baseline" verify_worker_baseline
    run_step "Control Plane deployment" control_plane_rollout
    run_step "resolve running Control Plane revision" resolve_runtime_revision
    run_step "Control Plane running acceptance" verify_control_plane
    if (( SKIP_ADMIN_BOOTSTRAP == 0 )); then
      run_step "idempotent administrator bootstrap" bootstrap_administrator
    fi
    if (( REUSE_PRESERVE_WORKER == 1 )); then
      run_step "verify active Worker identity without restart" \
        verify_active_worker_identity
    elif (( SKIP_PKI == 0 )); then
      run_step "verify and transfer Worker MQTT identity" verify_and_transfer_pki
    fi
    if (( SKIP_WORKER_RESTART == 0 && REUSE_PRESERVE_WORKER == 0 )); then
      run_step "install identity and restart existing Worker" install_and_start_worker
    fi
    run_step "Worker Node-shard convergence" worker_convergence_test
    if (( SKIP_TELEMETRY == 0 )); then
      run_step "Worker live telemetry" worker_telemetry_test
    fi
    if (( SKIP_TELEMETRY == 0 && REUSE_PRESERVE_WORKER == 0 )); then
      run_step "Worker restart and telemetry sequence recovery" \
        worker_restart_sequence_recovery_test
    fi
    if (( SKIP_E2E == 0 )); then
      run_step "Core-E2E" core_e2e_test
      run_step "Worker JSON config-file E2E" worker_config_file_e2e_test
      run_step "post-file-E2E Worker convergence" worker_convergence_test
    fi
    run_step "final Worker-sync diagnosis" \
      "$root_dir/diagnose_worker_sync.sh" "$NODE_ID" "$SITE_ID"
    ;;
esac

current_stage=complete

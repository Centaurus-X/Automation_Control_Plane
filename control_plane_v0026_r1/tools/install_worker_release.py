#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Fail-closed, recoverable installer for an Automation Worker release ZIP."""

import argparse
import datetime
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import uuid
import zipfile

from functools import partial
from pathlib import Path, PurePosixPath


VERSION_PATTERN = re.compile(r"automation_system_v[0-9]{4,}")
MANIFEST_LINE = re.compile(r"([0-9a-f]{64})  [.]\/([A-Za-z0-9._\/-]+)")
MAX_ARCHIVE_BYTES = 256 * 1024 * 1024
MAX_EXPANDED_BYTES = 512 * 1024 * 1024
MAX_MEMBER_COUNT = 20000
MAX_PRESERVED_CONFIG_BYTES = 16 * 1024 * 1024
PRESERVED_CONFIG_RELATIVE = PurePosixPath(
    "_config/config_data/device_state_data_v9_node_fn-01.json")
PRESERVED_RUNTIME_NAMES = (
    re.compile(r"sensor_sequence[.]json"),
    re.compile(r"config_file_sync_[A-Za-z0-9._-]+[.]json"),
    re.compile(r"distributed_operations_[A-Za-z0-9._-]+[.]sqlite3(?:-wal|-shm)?"),
    re.compile(r"cross_snapshot_[A-Za-z0-9._-]+[.]json"),
)


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(partial(stream.read, 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _inside(path, root):
    candidate = path.resolve()
    boundary = root.resolve()
    return candidate == boundary or boundary in candidate.parents


def _require_safe_path(path, home, label, allow_missing=False):
    candidate = path.expanduser().resolve(strict=False)
    if candidate == home or not _inside(candidate, home):
        raise RuntimeError(label + " must be a strict descendant of HOME")
    if candidate.exists() and candidate.is_symlink():
        raise RuntimeError(label + " must not be a symbolic link")
    if not allow_missing and not candidate.exists():
        raise RuntimeError(label + " does not exist: " + str(candidate))
    return candidate


def _safe_member(info, seen, expected_version):
    name = info.filename
    if not name or "\\" in name or "\x00" in name:
        raise RuntimeError("unsafe Worker ZIP member: " + repr(name))
    path = PurePosixPath(name)
    if path.is_absolute() or any(part in ("", ".", "..") for part in path.parts):
        raise RuntimeError("unsafe Worker ZIP member: " + repr(name))
    if path.parts[0] != expected_version:
        raise RuntimeError("Worker ZIP top-level directory differs from VERSION")
    normalized = path.as_posix()
    if normalized in seen:
        raise RuntimeError("duplicate Worker ZIP member: " + normalized)
    seen.add(normalized)
    mode = info.external_attr >> 16
    if stat.S_ISLNK(mode):
        raise RuntimeError("symbolic link in Worker ZIP: " + normalized)
    if mode and not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
        raise RuntimeError("unsupported Worker ZIP member type: " + normalized)
    return path


def validate_archive(archive_path, expected_version, expected_sha256):
    if archive_path.is_symlink() or not archive_path.is_file():
        raise RuntimeError("Worker archive is missing or unsafe")
    if archive_path.stat().st_size > MAX_ARCHIVE_BYTES:
        raise RuntimeError("Worker archive exceeds the size limit")
    actual_sha256 = sha256(archive_path)
    if actual_sha256 != expected_sha256:
        raise RuntimeError(
            "Worker archive SHA-256 mismatch: expected={} actual={}".format(
                expected_sha256, actual_sha256
            )
        )
    seen = set()
    expanded = 0
    with zipfile.ZipFile(archive_path) as archive:
        if len(archive.infolist()) > MAX_MEMBER_COUNT:
            raise RuntimeError("Worker ZIP contains too many members")
        if archive.testzip() is not None:
            raise RuntimeError("Worker ZIP CRC validation failed")
        for info in archive.infolist():
            _safe_member(info, seen, expected_version)
            expanded += int(info.file_size)
            if expanded > MAX_EXPANDED_BYTES:
                raise RuntimeError("Worker ZIP expanded size exceeds the limit")
    return actual_sha256


def extract_archive(archive_path, stage_parent, expected_version):
    with zipfile.ZipFile(archive_path) as archive:
        seen = set()
        for info in archive.infolist():
            relative = _safe_member(info, seen, expected_version)
            target = stage_parent.joinpath(*relative.parts)
            if info.is_dir():
                target.mkdir(mode=0o755, parents=True, exist_ok=True)
                continue
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            descriptor = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            try:
                with os.fdopen(descriptor, "wb") as writer, archive.open(info) as reader:
                    shutil.copyfileobj(reader, writer, 1024 * 1024)
            except Exception:
                try:
                    os.close(descriptor)
                except OSError:
                    pass
                raise
            executable = info.filename.endswith(".sh") or info.filename.endswith(".py")
            target.chmod(0o755 if executable else 0o644)
    extracted = stage_parent / expected_version
    if not extracted.is_dir() or extracted.is_symlink():
        raise RuntimeError("Worker ZIP did not produce the expected release root")
    return extracted


def verify_release_tree(
        root, expected_version, expected_manifest_sha256,
        allow_preserved_runtime=False, preserved_config_sha256=None):
    version_file = root / "VERSION"
    manifest = root / "SHA256SUMS"
    if version_file.read_text(encoding="utf-8").strip() != expected_version:
        raise RuntimeError("Worker VERSION mismatch")
    if sha256(manifest) != expected_manifest_sha256:
        raise RuntimeError("Worker SHA256SUMS anchor mismatch")
    seen = set()
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        match = MANIFEST_LINE.fullmatch(line)
        if match is None:
            raise RuntimeError("malformed Worker manifest line {}".format(number))
        relative = PurePosixPath(match.group(2))
        if any(part in ("", ".", "..") for part in relative.parts):
            raise RuntimeError("unsafe Worker manifest path")
        name = relative.as_posix()
        if name in seen:
            raise RuntimeError("duplicate Worker manifest path: " + name)
        seen.add(name)
        path = root.joinpath(*relative.parts)
        if path.is_symlink() or not path.is_file():
            raise RuntimeError("Worker manifest file is absent or unsafe: " + name)
        actual_sha256 = sha256(path)
        if (
                name == PRESERVED_CONFIG_RELATIVE.as_posix()
                and preserved_config_sha256 is not None):
            if actual_sha256 != preserved_config_sha256:
                raise RuntimeError("preserved Worker V9 config hash mismatch")
        elif actual_sha256 != match.group(1):
            raise RuntimeError("Worker manifest hash mismatch: " + name)
    actual = set()
    for path in root.rglob("*"):
        if path.is_symlink():
            raise RuntimeError("symbolic link in extracted Worker release")
        if path.is_file() and path.name != "SHA256SUMS":
            relative = path.relative_to(root)
            if (
                    allow_preserved_runtime
                    and len(relative.parts) == 2
                    and relative.parts[0] == "_runtime"
                    and _runtime_file_allowed(path.name)):
                continue
            actual.add(relative.as_posix())
    if actual != seen:
        raise RuntimeError("Worker manifest does not cover the release exactly")
    for name in actual:
        relative = PurePosixPath(name)
        suffix = relative.suffix.lower()
        if suffix in (".key", ".pem", ".p12", ".pfx", ".pyc", ".pyo", ".log"):
            raise RuntimeError("forbidden runtime or sensitive Worker file: " + name)
        if relative.parts and relative.parts[0] in ("logs", "_runtime"):
            raise RuntimeError("Worker release contains mutable runtime data: " + name)
        if relative.parts[:2] == ("_config", "certs") and relative.name != "README.md":
            raise RuntimeError("Worker release contains identity material: " + name)
    return len(actual)


def _runtime_file_allowed(name):
    return any(pattern.fullmatch(name) is not None for pattern in PRESERVED_RUNTIME_NAMES)


def capture_runtime(target):
    captured = {}
    runtime = target / "_runtime"
    if not runtime.is_dir() or runtime.is_symlink():
        return captured
    for path in runtime.iterdir():
        if not _runtime_file_allowed(path.name):
            continue
        if path.is_symlink() or not path.is_file() or path.stat().st_size > 32 * 1024 * 1024:
            raise RuntimeError("preserved Worker runtime file is unsafe: " + path.name)
        captured[path.name] = path.read_bytes()
    return captured


def restore_runtime(target, captured):
    runtime = target / "_runtime"
    runtime.mkdir(mode=0o700, exist_ok=True)
    runtime.chmod(0o700)
    for name, payload in captured.items():
        if not _runtime_file_allowed(name):
            raise RuntimeError("internal preserved runtime name is unsafe")
        path = runtime / name
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
    (target / "logs").mkdir(mode=0o700, exist_ok=True)


def _version_number(version):
    match = VERSION_PATTERN.fullmatch(version)
    if match is None:
        raise RuntimeError("invalid Worker release version")
    return int(version.rsplit("v", 1)[1])


def _release_sort_key(item):
    return _version_number(item.name), item.name


def _active_release(install_root, expected_version):
    """Return the newest installed same/older release, never an OLD archive."""
    maximum = _version_number(expected_version)
    candidates = []
    for item in install_root.iterdir():
        if (
                item.is_dir()
                and not item.is_symlink()
                and VERSION_PATTERN.fullmatch(item.name) is not None
                and _version_number(item.name) <= maximum):
            candidates.append(item)
    if not candidates:
        return None
    return max(candidates, key=_release_sort_key)


def _baseline_hash(root, relative):
    manifest = root / "SHA256SUMS"
    if manifest.is_symlink() or not manifest.is_file():
        raise RuntimeError("active Worker SHA256SUMS is missing or unsafe")
    found = None
    seen = set()
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        match = MANIFEST_LINE.fullmatch(line)
        if match is None:
            raise RuntimeError(
                "malformed active Worker manifest line {}".format(number))
        name = match.group(2)
        if name in seen:
            raise RuntimeError("duplicate active Worker manifest path: " + name)
        seen.add(name)
        if name == relative.as_posix():
            found = match.group(1)
    if found is None:
        raise RuntimeError("active Worker manifest has no canonical V9 node config")
    return found


def _json_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON object key: " + str(key))
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("non-finite JSON value: " + value)


def _contains_rows(value):
    if isinstance(value, dict):
        return "rows" in value or any(_contains_rows(item) for item in value.values())
    if isinstance(value, list):
        return any(_contains_rows(item) for item in value)
    return False


def _strict_flat_v9_node_config(payload):
    try:
        document = json.loads(
            payload.decode("utf-8"),
            object_pairs_hook=_json_object,
            parse_constant=_reject_json_constant)
    except (UnicodeDecodeError, ValueError, json.JSONDecodeError) as error:
        raise RuntimeError("canonical V9 node config is invalid JSON") from error
    if not isinstance(document, dict):
        raise RuntimeError("canonical V9 node config root must be an object")
    meta = document.get("meta")
    if not isinstance(meta, list) or len(meta) != 1 or not isinstance(meta[0], dict):
        raise RuntimeError("canonical V9 node config meta must contain one object")
    identity = meta[0]
    if (
            type(identity.get("version")) is not int
            or identity.get("version") != 9
            or identity.get("schema") != "device_state_norm"
            or identity.get("scope") != "node_local"
            or identity.get("node_id") != "fn-01"):
        raise RuntimeError("config is not canonical V9/node_local/device_state_norm fn-01")
    required_tables = (
        "controllers", "sensors", "actuators", "processes",
        "process_states", "rules",
    )
    for table in required_tables:
        rows = document.get(table)
        if not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
            raise RuntimeError("canonical V9 node config table is invalid: " + table)
    if _contains_rows(document):
        raise RuntimeError("grouped rows are forbidden in canonical flat V9 node config")
    for row in document["sensors"]:
        if "sensor_id" not in row or "controller_uuid" not in row:
            raise RuntimeError("canonical flat V9 sensor identity is incomplete")
    for row in document["actuators"]:
        if "actuator_id" not in row or "controller_uuid" not in row:
            raise RuntimeError("canonical flat V9 actuator identity is incomplete")


def capture_committed_config(install_root, expected_version, policy):
    if policy not in ("preserve", "strict"):
        raise RuntimeError("Worker config drift policy must be preserve or strict")
    result = {
        "status": "strict" if policy == "strict" else "no_active_release",
        "source_version": None,
        "baseline_sha256": None,
        "preserved_sha256": None,
        "payload": None,
    }
    if policy == "strict":
        return result
    active = _active_release(install_root, expected_version)
    if active is None:
        return result
    result["source_version"] = active.name
    version_file = active / "VERSION"
    if (
            version_file.is_symlink()
            or not version_file.is_file()
            or version_file.read_text(encoding="utf-8").strip() != active.name):
        result["status"] = "invalid_release_identity"
        return result
    try:
        baseline_sha256 = _baseline_hash(active, PRESERVED_CONFIG_RELATIVE)
    except (OSError, RuntimeError, UnicodeError):
        result["status"] = "invalid_release_manifest"
        return result
    result["baseline_sha256"] = baseline_sha256
    path = active.joinpath(*PRESERVED_CONFIG_RELATIVE.parts)
    if path.is_symlink() or not path.is_file():
        result["status"] = "missing_or_unsafe_config"
        return result
    if path.stat().st_size > MAX_PRESERVED_CONFIG_BYTES:
        result["status"] = "oversize_config"
        return result
    payload = path.read_bytes()
    if len(payload) > MAX_PRESERVED_CONFIG_BYTES:
        result["status"] = "oversize_config"
        return result
    actual_sha256 = hashlib.sha256(payload).hexdigest()
    if actual_sha256 == baseline_sha256:
        result["status"] = "baseline"
        return result
    try:
        _strict_flat_v9_node_config(payload)
    except RuntimeError:
        result["status"] = "invalid_noncanonical_config"
        return result
    result.update({
        "status": "eligible",
        "preserved_sha256": actual_sha256,
        "payload": payload,
    })
    return result


def validate_committed_config_for_release(captured, release_root):
    """Require the incoming Worker's complete V9 storage contract."""
    if captured["status"] != "eligible":
        return
    library_root = release_root / "src" / "library"
    validator = library_root / "_generic_type_library.py"
    if library_root.is_symlink() or not library_root.is_dir() \
            or validator.is_symlink() or not validator.is_file():
        raise RuntimeError("incoming Worker V9 contract validator is missing or unsafe")
    program = (
        "import json,sys; "
        "sys.path.insert(0,sys.argv[1]); "
        "from _generic_type_library import verify_storage_config_data; "
        "document=json.loads(sys.stdin.buffer.read().decode('utf-8')); "
        "report=verify_storage_config_data(document); "
        "raise SystemExit(0 if report.get('status') == 'OK' else 2)"
    )
    try:
        completed = subprocess.run(
            [sys.executable, "-I", "-S", "-B", "-c", program, str(library_root)],
            input=captured["payload"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.PIPE,
            check=False,
            timeout=30,
        )
    except (OSError, subprocess.SubprocessError) as error:
        raise RuntimeError("incoming Worker V9 contract validator failed") from error
    if completed.returncode == 0:
        return
    if completed.returncode != 2:
        raise RuntimeError("incoming Worker V9 contract validator failed")
    captured.update({
        "status": "invalid_worker_contract_config",
        "preserved_sha256": None,
        "payload": None,
    })


def _fsync_directory(path):
    if not hasattr(os, "O_DIRECTORY"):
        return
    descriptor = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def restore_committed_config(target, captured):
    if captured["status"] != "eligible":
        return
    destination = target.joinpath(*PRESERVED_CONFIG_RELATIVE.parts)
    if destination.is_symlink() or not destination.is_file():
        raise RuntimeError("new Worker canonical V9 node config is missing or unsafe")
    parent = destination.parent
    temporary = parent / ("." + destination.name + ".preserve." + uuid.uuid4().hex)
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb") as stream:
            stream.write(captured["payload"])
            stream.flush()
            os.fsync(stream.fileno())
        temporary.chmod(stat.S_IMODE(destination.stat().st_mode))
        os.replace(temporary, destination)
        _fsync_directory(parent)
    finally:
        if temporary.exists():
            temporary.unlink()
    captured["status"] = "preserved"


def _archive_name(version):
    timestamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    return "{}_pre_{}_{}".format(version, timestamp, uuid.uuid4().hex[:8])


def _path_name(item):
    return item.name


def install_release(
        extracted, install_root, old_root, expected_version,
        config_drift_policy="strict"):
    target = install_root / expected_version
    captured = capture_runtime(target) if target.is_dir() and not target.is_symlink() else {}
    captured_config = capture_committed_config(
        install_root, expected_version, config_drift_policy)
    validate_committed_config_for_release(captured_config, extracted)
    old_root.mkdir(mode=0o700, parents=True, exist_ok=True)
    displaced = []
    candidates = []
    for item in install_root.iterdir():
        if item.is_dir() and not item.is_symlink() and VERSION_PATTERN.fullmatch(item.name):
            candidates.append(item)
        elif item.name == expected_version:
            raise RuntimeError("unsafe existing Worker release target")
    try:
        for candidate in sorted(candidates, key=_path_name):
            destination = old_root / _archive_name(candidate.name)
            os.replace(candidate, destination)
            displaced.append((candidate, destination))
        os.replace(extracted, target)
        restore_runtime(target, captured)
        restore_committed_config(target, captured_config)
    except Exception:
        if target.exists() and target.is_dir() and not target.is_symlink():
            failed = old_root / _archive_name(expected_version + "_failed")
            os.replace(target, failed)
        for original, destination in reversed(displaced):
            if destination.exists() and not original.exists():
                os.replace(destination, original)
        raise
    public_config = dict(captured_config)
    public_config.pop("payload", None)
    return (
        target,
        [str(destination) for _original, destination in displaced],
        sorted(captured),
        public_config,
    )


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--archive", required=True, type=Path)
    parser.add_argument("--home", required=True, type=Path)
    parser.add_argument("--install-root", required=True, type=Path)
    parser.add_argument("--old-root", required=True, type=Path)
    parser.add_argument("--expected-version", required=True)
    parser.add_argument("--expected-sha256", required=True)
    parser.add_argument("--expected-manifest-sha256", required=True)
    parser.add_argument(
        "--config-drift-policy", required=True, choices=("preserve", "strict"))
    parser.add_argument("--verify-only", action="store_true")
    arguments = parser.parse_args()
    if VERSION_PATTERN.fullmatch(arguments.expected_version) is None:
        raise RuntimeError("invalid expected Worker version")
    if re.fullmatch(r"[0-9a-f]{64}", arguments.expected_sha256) is None:
        raise RuntimeError("invalid expected Worker archive SHA-256")
    if re.fullmatch(r"[0-9a-f]{64}", arguments.expected_manifest_sha256) is None:
        raise RuntimeError("invalid expected Worker manifest SHA-256")
    home = arguments.home.resolve()
    if not home.is_dir() or home.is_symlink():
        raise RuntimeError("HOME is missing or unsafe")
    archive_path = _require_safe_path(arguments.archive, home, "archive")
    install_root = _require_safe_path(
        arguments.install_root, home, "install root", allow_missing=True)
    old_root = _require_safe_path(arguments.old_root, home, "OLD root", allow_missing=True)
    install_root.mkdir(mode=0o755, parents=True, exist_ok=True)
    actual_sha256 = validate_archive(
        archive_path, arguments.expected_version, arguments.expected_sha256)
    stage_parent = Path(tempfile.mkdtemp(prefix=".automation-worker-stage-", dir=install_root))
    target = None
    archived = []
    preserved = []
    preserved_config = {
        "status": "verify_only",
        "source_version": None,
        "baseline_sha256": None,
        "preserved_sha256": None,
    }
    try:
        extracted = extract_archive(
            archive_path, stage_parent, arguments.expected_version)
        file_count = verify_release_tree(
            extracted, arguments.expected_version,
            arguments.expected_manifest_sha256)
        if not arguments.verify_only:
            target, archived, preserved, preserved_config = install_release(
                extracted, install_root, old_root, arguments.expected_version,
                config_drift_policy=arguments.config_drift_policy)
            verify_release_tree(
                target, arguments.expected_version,
                arguments.expected_manifest_sha256,
                allow_preserved_runtime=True,
                preserved_config_sha256=preserved_config["preserved_sha256"])
    finally:
        shutil.rmtree(stage_parent, ignore_errors=True)
    print(json.dumps({
        "ok": True,
        "version": arguments.expected_version,
        "archive_sha256": actual_sha256,
        "file_count": file_count,
        "installed_path": str(target) if target is not None else None,
        "archived_paths": archived,
        "preserved_runtime_files": preserved,
        "config_drift_policy": arguments.config_drift_policy,
        "config_preservation_status": preserved_config["status"],
        "preserved_config_path": (
            PRESERVED_CONFIG_RELATIVE.as_posix()
            if preserved_config["status"] == "preserved" else None),
        "preserved_config_sha256": preserved_config["preserved_sha256"],
        "preserved_config_source_version": preserved_config["source_version"],
        "preserved_config_baseline_sha256": preserved_config["baseline_sha256"],
        "verify_only": bool(arguments.verify_only),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, zipfile.BadZipFile) as error:
        print("BLOCKED: " + str(error), file=__import__("sys").stderr)
        raise SystemExit(1) from error

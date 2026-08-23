#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Build and independently verify a reproducible Control Plane release ZIP."""

import argparse
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

from functools import partial
from pathlib import Path


COMPONENTS = (
    "mqtt_broker", "postgres_system", "master_node", "proxy_gateway",
    "mcp_controller_server",
)
TRANSIENT_DIR_NAMES = frozenset((
    "__pycache__",
    ".git",
    ".pytest_cache",
    ".test-tmp",
    ".venv",
    "venv",
    "generated",
    "results",
    "runtime-config",
    "runtime-pki-handoff",
    "runtime-tls-handoff",
    "runtime-secrets",
    "test-pki",
    "secrets",
    "tls",
    "test_ca_private",
    "backup",
    "backups",
))
TRANSIENT_SUFFIXES = (".pyc", ".pyo", ".log")
SENSITIVE_FILE_NAMES = frozenset((
    "credentials.json",
    "rollout_automation.json",
    "rollout_automation.env",
))
SENSITIVE_SUFFIXES = (".key", ".pem", ".p12", ".pfx")
ZIP_TIMESTAMP = (2026, 8, 15, 0, 0, 0)
E2E_PATTERN = re.compile(r"e2e_v[0-9]{4,}")
REVISION_PATTERN = re.compile(r"v[0-9]{4,}-r[1-9][0-9]*")


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(partial(stream.read, 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def read_required_text(path):
    value = path.read_text(encoding="utf-8").strip()
    if not value:
        raise RuntimeError("required metadata file is empty: " + str(path))
    return value


def read_active_e2e(root):
    name = read_required_text(root / "integration_test" / "CURRENT_E2E")
    if E2E_PATTERN.fullmatch(name) is None:
        raise RuntimeError("invalid integration_test/CURRENT_E2E selector: " + name)
    directory = root / "integration_test" / name
    if not directory.is_dir() or not (directory / "VERSION").is_file():
        raise RuntimeError("active E2E package is incomplete: " + str(directory))
    return name


def e2e_package_sort_key(value):
    return value.encode("utf-8")


def read_e2e_packages(root):
    """Return every versioned E2E package in deterministic order."""
    integration_root = root / "integration_test"
    packages = []
    for path in integration_root.iterdir():
        if path.is_dir() and E2E_PATTERN.fullmatch(path.name):
            if not (path / "VERSION").is_file():
                raise RuntimeError("E2E package is incomplete: " + str(path))
            packages.append(path.name)
    return tuple(sorted(packages, key=e2e_package_sort_key))


def expected_release_directory(revision):
    if REVISION_PATTERN.fullmatch(revision) is None:
        raise RuntimeError("invalid RELEASE_REVISION: " + revision)
    return "automation_control_plane_release_" + revision.replace("-", "_")


def relative_sort_key(item):
    return item.as_posix().encode("utf-8")


def directory_sort_key(root, item):
    return item.relative_to(root).as_posix().encode("utf-8")


def transient_relative(relative):
    value = relative.as_posix()
    safe_secret_documentation = frozenset((
        "secrets",
        "secrets/README.md",
        "mcp_controller_server/secrets",
        "mcp_controller_server/secrets/README.md",
    ))
    if value in safe_secret_documentation:
        return False
    if any(part in TRANSIENT_DIR_NAMES for part in relative.parts):
        return True
    if (
        len(relative.parts) >= 2
        and relative.parts[0] == "integration_test"
        and "runtime" in relative.parts[1:]
    ):
        return True
    fixed_paths = (
        "integration_test/runtime",
        "integration_test/generated",
    )
    return any(value == path or value.startswith(path + "/") for path in fixed_paths)


def relative_files(root, **kwargs):
    omit_root_manifest = bool(kwargs.get("omit_root_manifest", False))
    files = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if transient_relative(relative):
            continue
        if path.is_symlink():
            raise RuntimeError(
                "symbolic links are not allowed in the release: " + relative.as_posix()
            )
        if not path.is_file():
            continue
        if (
            path.suffix in TRANSIENT_SUFFIXES
            or path.name == "pyvenv.cfg"
            or path.name in SENSITIVE_FILE_NAMES
            or path.suffix.lower() in SENSITIVE_SUFFIXES
        ):
            continue
        if omit_root_manifest and relative == Path("SHA256SUMS"):
            continue
        files.append(relative)
    return sorted(files, key=relative_sort_key)


def reject_transient_content(root):
    failures = []
    for path in root.rglob("*"):
        relative = path.relative_to(root)
        if transient_relative(relative):
            failures.append(relative.as_posix())
        elif path.is_symlink():
            failures.append(relative.as_posix() + " (symbolic link)")
        elif path.is_file() and (
            path.suffix in TRANSIENT_SUFFIXES
            or path.name == "pyvenv.cfg"
            or path.name in SENSITIVE_FILE_NAMES
            or path.suffix.lower() in SENSITIVE_SUFFIXES
        ):
            failures.append(relative.as_posix())
        elif path.is_file() and path.suffix.lower() == ".sh" \
                and b"\r" in path.read_bytes():
            failures.append(
                relative.as_posix() + " (carriage return in shell script)")
    if failures:
        preview = "\n  ".join(sorted(failures)[:30])
        raise RuntimeError(
            "transient, linked, local-state or sensitive content must be removed before packaging:\n  "
            + preview
        )


def write_manifest(root, manifest):
    lines = []
    for relative in relative_files(root, omit_root_manifest=True):
        lines.append("{}  ./{}".format(sha256(root / relative), relative.as_posix()))
    temporary = manifest.with_name(manifest.name + ".tmp")
    temporary.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    temporary.chmod(0o644)
    os.replace(temporary, manifest)


def zip_info(name, mode, **kwargs):
    directory = bool(kwargs.get("directory", False))
    info = zipfile.ZipInfo(name, ZIP_TIMESTAMP)
    info.create_system = 3
    info.flag_bits |= 0x800
    file_type = stat.S_IFDIR if directory else stat.S_IFREG
    info.external_attr = (file_type | mode) << 16
    if directory:
        info.external_attr |= 0x10
    info.compress_type = zipfile.ZIP_DEFLATED
    return info


def build_zip(root, destination):
    top = root.name
    directories = []
    for path in root.rglob("*"):
        if path.is_dir() and not transient_relative(path.relative_to(root)):
            directories.append(path)
    files = relative_files(root)
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    temporary.unlink(missing_ok=True)
    try:
        with zipfile.ZipFile(
            temporary,
            "w",
            compression=zipfile.ZIP_DEFLATED,
            compresslevel=9,
            strict_timestamps=True,
        ) as archive:
            archive.writestr(zip_info(top + "/", 0o755, directory=True), b"")
            for directory in sorted(
                directories, key=partial(directory_sort_key, root)
            ):
                relative = directory.relative_to(root).as_posix()
                archive.writestr(
                    zip_info(top + "/" + relative + "/", 0o755, directory=True),
                    b"",
                )
            for relative in files:
                source = root / relative
                mode = stat.S_IMODE(source.stat().st_mode)
                archive.writestr(
                    zip_info(top + "/" + relative.as_posix(), mode),
                    source.read_bytes(),
                )
        os.replace(temporary, destination)
    finally:
        temporary.unlink(missing_ok=True)


def verify_zip_tree(root, destination):
    expected = set()
    for path in relative_files(root):
        expected.add(root.name + "/" + path.as_posix())
    with zipfile.ZipFile(destination) as archive:
        corrupt = archive.testzip()
        if corrupt:
            raise RuntimeError("ZIP CRC validation failed for " + corrupt)
        names = set()
        for info in archive.infolist():
            name = info.filename
            parts = Path(name).parts
            if name.startswith("/") or ".." in parts or not parts:
                raise RuntimeError("unsafe ZIP member name: " + name)
            mode = (info.external_attr >> 16) & 0o170000
            if not info.is_dir() and mode not in (0, stat.S_IFREG):
                raise RuntimeError("non-regular ZIP member is forbidden: " + name)
            if not info.is_dir():
                names.add(name)
        if names != expected:
            missing = sorted(expected - names)
            extra = sorted(names - expected)
            raise RuntimeError(
                "ZIP tree mismatch; missing={!r}, extra={!r}".format(missing, extra)
            )


def extract_verified_zip(destination, extraction_root):
    extraction_root = extraction_root.resolve()
    with zipfile.ZipFile(destination) as archive:
        for info in archive.infolist():
            parts = Path(info.filename).parts
            if info.filename.startswith("/") or ".." in parts or not parts:
                raise RuntimeError("unsafe ZIP member name: " + info.filename)
            target = extraction_root.joinpath(*parts)
            resolved_parent = target.parent.resolve()
            if resolved_parent != extraction_root and extraction_root not in resolved_parent.parents:
                raise RuntimeError("ZIP member escapes extraction root: " + info.filename)
            if info.is_dir():
                target.mkdir(parents=True, exist_ok=True)
                target.chmod(0o755)
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            mode = (info.external_attr >> 16) & 0o7777
            mode = mode or 0o644
            with target.open("xb") as stream:
                stream.write(archive.read(info))
            target.chmod(mode)


def run_checked(command, **kwargs):
    subprocess.run(
        command,
        cwd=kwargs["cwd"],
        env=kwargs.get("env"),
        check=True,
    )


def validate_release_tree(root):
    environment = os.environ.copy()
    environment["VALIDATE_ARCHIVE_TREE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    environment["PYTHONNOUSERSITE"] = "1"
    run_checked(["bash", "tools/validate_release.sh"], cwd=root, env=environment)
    run_checked(["bash", "tools/verify_checksums.sh"], cwd=root, env=environment)
    run_checked(["bash", "-n", "rollout_automation.sh"], cwd=root, env=environment)
    run_checked(
        ["python3", "tools/test_rollout_bootstrap.py"],
        cwd=root,
        env=environment,
    )
    run_checked(
        ["python3", "tools/test_rollout_automation.py"],
        cwd=root,
        env=environment,
    )
    run_checked(
        ["python3", "tools/test_worker_remote_lifecycle.py"],
        cwd=root,
        env=environment,
    )
    run_checked(
        ["python3", "tools/test_rollout_ssh_reconnect.py"],
        cwd=root,
        env=environment,
    )
    run_checked(["bash", "tools/test_lifecycle.sh"], cwd=root, env=environment)


def validate_extracted_archive(destination, release_directory_name):
    temporary_root = Path(tempfile.mkdtemp(prefix="automation-release-archive-check-"))
    try:
        extract_verified_zip(destination, temporary_root)
        extracted = temporary_root / release_directory_name
        if not extracted.is_dir():
            raise RuntimeError("expected release root is missing after extraction")
        environment = os.environ.copy()
        if environment.get("CONTROL_PLANE_VALIDATION_ACTIVE") != "/release":
            environment.pop("CONTROL_PLANE_VALIDATION_ACTIVE", None)
        environment["VALIDATE_ARCHIVE_TREE"] = "1"
        environment["VERIFY_RELEASE_CHECKSUMS"] = "1"
        environment["PYTHONDONTWRITEBYTECODE"] = "1"
        environment["PYTHONNOUSERSITE"] = "1"
        run_checked(
            ["bash", "test_static_all.sh"],
            cwd=extracted,
            env=environment,
        )
    finally:
        shutil.rmtree(temporary_root)


def validate_pinned_worker_contract(root):
    """Execute the cross-artifact V9/WAL gate against the pinned Worker ZIP."""
    manifest_path = root / "administration" / "release_manifest.json"
    with manifest_path.open("r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    artifacts = manifest.get("artifacts") if isinstance(manifest, dict) else None
    worker = next((
        item for item in artifacts or ()
        if isinstance(item, dict)
        and item.get("artifact_id") == "automation-system-v0101"
    ), None)
    if worker is None:
        raise RuntimeError("pinned Worker artifact is absent from release manifest")
    expected_hash = str(worker.get("sha256") or "").lower()
    if re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
        raise RuntimeError("pinned Worker SHA-256 is invalid")
    archive = (root / str(worker.get("path") or "")).resolve()
    expected_archive = (root.parent / "automation_system_v0101.zip").resolve()
    if archive != expected_archive or archive.is_symlink() or not archive.is_file():
        raise RuntimeError("exact sibling automation_system_v0101.zip is required")
    if sha256(archive) != expected_hash:
        raise RuntimeError("pinned Worker archive SHA-256 differs")
    sidecar = archive.with_name(archive.name + ".sha256")
    if sidecar.is_symlink() or not sidecar.is_file():
        raise RuntimeError("pinned Worker SHA-256 sidecar is required")
    sidecar_text = sidecar.read_text(encoding="utf-8").strip()
    if sidecar_text != expected_hash + "  " + archive.name:
        raise RuntimeError("pinned Worker SHA-256 sidecar differs")
    expected_root = "automation_system_v0101"
    with zipfile.ZipFile(archive) as worker_zip:
        if worker_zip.testzip() is not None:
            raise RuntimeError("pinned Worker ZIP CRC validation failed")
        for info in worker_zip.infolist():
            parts = Path(info.filename).parts
            if not parts or parts[0] != expected_root \
                    or info.filename.startswith("/") or ".." in parts:
                raise RuntimeError("unsafe or foreign pinned Worker ZIP member")
            file_type = (info.external_attr >> 16) & 0o170000
            if not info.is_dir() and file_type not in (0, stat.S_IFREG):
                raise RuntimeError("non-regular pinned Worker ZIP member")
    temporary_root = Path(tempfile.mkdtemp(prefix="automation-worker-contract-check-"))
    try:
        extract_verified_zip(archive, temporary_root)
        worker_root = temporary_root / expected_root
        if not worker_root.is_dir() or worker_root.is_symlink():
            raise RuntimeError("pinned Worker release root is missing")
        contract_roots = (
            root / "master_node" / "master_node" / "src" / "library"
            / "_generic_system_contract_library",
            root / "proxy_gateway" / "proxy_gateway" / "src" / "library"
            / "_generic_system_contract_library",
            worker_root / "src" / "library"
            / "_generic_system_contract_library",
        )
        type_paths = (
            root / "master_node" / "master_node" / "src" / "library"
            / "_generic_type_library.py",
            root / "proxy_gateway" / "proxy_gateway" / "src" / "library"
            / "_generic_type_library.py",
            worker_root / "src" / "library" / "_generic_type_library.py",
        )
        if not all(path.is_dir() for path in contract_roots):
            raise RuntimeError(
                "canonical Contract Authority is absent from a release component")
        if not all(path.is_file() for path in type_paths):
            raise RuntimeError(
                "canonical Type Authority is absent from a release component")
        component_files = []
        for contract_root in contract_roots:
            component_files.append({
                path.relative_to(contract_root).as_posix(): path
                for path in contract_root.rglob("*") if path.is_file()
            })
        reference_names = set(component_files[0])
        if any(set(files) != reference_names for files in component_files[1:]):
            raise RuntimeError(
                "release components contain different canonical contract files")
        mismatches = [
            name for name in sorted(reference_names)
            if len({
                files[name].read_bytes() for files in component_files
            }) != 1
        ]
        if mismatches:
            raise RuntimeError(
                "release component Contract Authorities differ: "
                + ",".join(mismatches))
        if len({path.read_bytes() for path in type_paths}) != 1:
            raise RuntimeError("release component Type Authorities differ")
        contract_environment = os.environ.copy()
        contract_environment["PYTHONDONTWRITEBYTECODE"] = "1"
        contract_environment["PYTHONNOUSERSITE"] = "1"
        run_checked(
            ["sha256sum", "-c", "SHA256SUMS"],
            cwd=worker_root,
        )
        run_checked(
            [
                "python3",
                str(root / "tools" / "test_worker_config_file_e2e_wal_projection.py"),
                "--worker-root",
                str(worker_root),
            ],
            cwd=root,
            env=contract_environment,
        )
        run_checked(
            [
                "python3",
                str(root / "integration_test" / "e2e_v0025"
                    / "config_commit_path_equivalence_e2e.py"),
                "--worker-root",
                str(worker_root),
            ],
            cwd=root,
            env=contract_environment,
        )
    finally:
        shutil.rmtree(temporary_root)


def external_worker_contract_required(root, skip_external_worker_contract):
    """Resolve the explicit cross-artifact validation policy."""
    if not skip_external_worker_contract:
        return True
    archive = (root.parent / "automation_system_v0101.zip").resolve()
    sidecar = archive.with_name(archive.name + ".sha256")
    present = tuple(path for path in (archive, sidecar) if path.exists())
    if present:
        raise RuntimeError(
            "--skip-external-worker-contract is only allowed when the pinned "
            "Worker ZIP and sidecar are both absent; validate the available "
            "artifact instead: " + ", ".join(str(path) for path in present)
        )
    return False


def write_sha256_sidecar(destination):
    sidecar = destination.with_name(destination.name + ".sha256")
    temporary = sidecar.with_name(sidecar.name + ".tmp")
    temporary.write_text(
        sha256(destination) + "  " + destination.name + "\n",
        encoding="utf-8",
        newline="\n",
    )
    temporary.chmod(0o644)
    os.replace(temporary, sidecar)
    return sidecar


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output",
        type=Path,
        help="ZIP destination (default: next to the release directory)",
    )
    parser.add_argument(
        "--skip-external-worker-contract",
        action="store_true",
        help=(
            "allow packaging only when the pinned sibling Worker ZIP and its "
            "sidecar are both unavailable; the default remains fail-closed"
        ),
    )
    arguments = parser.parse_args()

    root = Path(__file__).resolve().parent.parent
    revision = read_required_text(root / "RELEASE_REVISION")
    expected_name = expected_release_directory(revision)
    if root.name != expected_name:
        raise RuntimeError(
            "release directory must be named {!r}, got {!r}".format(
                expected_name, root.name
            )
        )
    active_e2e = read_active_e2e(root)
    reject_transient_content(root)

    e2e_packages = read_e2e_packages(root)
    if active_e2e not in e2e_packages:
        raise RuntimeError("active E2E package is absent from the package set")
    manifest_roots = COMPONENTS + tuple(
        "integration_test/" + name for name in e2e_packages
    )
    for manifest_root in manifest_roots:
        nested_root = root / manifest_root
        write_manifest(nested_root, nested_root / "SHA256SUMS")
    write_manifest(root, root / "SHA256SUMS")
    validate_release_tree(root)
    preflight_environment = os.environ.copy()
    preflight_environment["PYTHONDONTWRITEBYTECODE"] = "1"
    preflight_environment["PYTHONNOUSERSITE"] = "1"
    require_worker_contract = external_worker_contract_required(
        root, arguments.skip_external_worker_contract)
    administration_command = [
        "python3", "tools/test_administration_release_set.py",
    ]
    if require_worker_contract:
        administration_command.append("--require-worker-artifact")
    run_checked(
        administration_command,
        cwd=root,
        env=preflight_environment,
    )
    if require_worker_contract:
        validate_pinned_worker_contract(root)
    else:
        print(
            "SKIP pinned Worker cross-artifact contract: explicit packaging "
            "override accepted because automation_system_v0101.zip and its "
            "sidecar are both absent"
        )

    archive_revision = revision.removeprefix("v").replace("-", "_")
    default_destination = root.parent / (
        "automation_control_plane_release_v" + archive_revision + ".zip"
    )
    destination = (arguments.output or default_destination).expanduser().resolve()
    if destination == root or root in destination.parents:
        raise RuntimeError("ZIP destination must be outside the release directory")

    build_zip(root, destination)
    verify_zip_tree(root, destination)
    validate_extracted_archive(destination, expected_name)
    sidecar = write_sha256_sidecar(destination)

    print("PASS release ZIP: " + str(destination))
    print("PASS release SHA-256: " + str(sidecar))
    print("SHA-256: " + sha256(destination))
    print("Size: {} bytes".format(destination.stat().st_size))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, subprocess.CalledProcessError, zipfile.BadZipFile) as error:
        print("ERROR: " + str(error), file=sys.stderr)
        raise SystemExit(1) from error

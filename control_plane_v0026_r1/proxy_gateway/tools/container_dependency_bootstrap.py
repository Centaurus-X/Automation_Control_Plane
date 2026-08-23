#!/usr/bin/env python3
"""Verify or repair Proxy dependencies from the image-local wheelhouse."""

try:
    import fcntl
except ImportError:
    fcntl = None
import hashlib
import os
import shutil
import subprocess
import sys
import tempfile

from functools import partial
from pathlib import Path


REQUIREMENTS = Path("/app/requirements.txt")
CHECKER = Path("/app/tools/check_python_dependencies.py")
WHEELHOUSE = Path("/opt/automation-proxy-wheelhouse")
RUNTIME_ROOT = Path("/app/proxy_gateway/_runtime")
DEPENDENCY_TARGET = RUNTIME_ROOT / "python-dependencies-v0017"
MANIFEST_NAME = "SHA256SUMS"
COMPLETE_NAME = ".complete"
BOOTSTRAP_FORMAT = "proxy-offline-dependency-bootstrap-v1"


def sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(partial(stream.read, 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def wheel_sort_key(path):
    return path.name.encode("utf-8")


def wheel_files(wheelhouse):
    return sorted(
        (path for path in wheelhouse.iterdir() if path.is_file() and path.suffix == ".whl"),
        key=wheel_sort_key,
    )


def write_manifest(wheelhouse):
    wheels = wheel_files(wheelhouse)
    if not wheels:
        raise RuntimeError("wheelhouse contains no wheels")
    lines = ["{}  {}".format(sha256(path), path.name) for path in wheels]
    manifest = wheelhouse / MANIFEST_NAME
    manifest.write_text("\n".join(lines) + "\n", encoding="utf-8", newline="\n")
    manifest.chmod(0o444)


def verify_manifest(wheelhouse):
    manifest = wheelhouse / MANIFEST_NAME
    if not manifest.is_file() or manifest.is_symlink():
        raise RuntimeError("wheelhouse checksum manifest is missing or unsafe")
    expected = {}
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        parts = line.split("  ", 1)
        valid_digest = len(parts) == 2 and len(parts[0]) == 64 and all(
            character in "0123456789abcdef" for character in parts[0]
        )
        if not valid_digest or "/" in parts[1] or "\\" in parts[1]:
            raise RuntimeError("invalid wheelhouse manifest line {}".format(number))
        if parts[1] in expected:
            raise RuntimeError("duplicate wheelhouse manifest entry: " + parts[1])
        expected[parts[1]] = parts[0]
    actual_paths = wheel_files(wheelhouse)
    if any(path.is_symlink() for path in actual_paths):
        raise RuntimeError("wheelhouse must not contain symbolic links")
    actual = {path.name for path in actual_paths}
    if actual != set(expected):
        raise RuntimeError("wheelhouse file set differs from its checksum manifest")
    for path in actual_paths:
        if sha256(path) != expected[path.name]:
            raise RuntimeError("wheelhouse checksum mismatch: " + path.name)
    if not actual_paths:
        raise RuntimeError("wheelhouse contains no wheels")
    return sha256(manifest)


def clean_environment(extra_path=None):
    environment = os.environ.copy()
    environment.pop("PYTHONHOME", None)
    environment["PYTHONNOUSERSITE"] = "1"
    environment["PYTHONDONTWRITEBYTECODE"] = "1"
    base_path = "/app/proxy_gateway:/app/proxy_gateway/src"
    environment["PYTHONPATH"] = (
        str(extra_path) + ":" + base_path if extra_path is not None else base_path
    )
    return environment


def check_dependencies(extra_path=None, quiet=False):
    commands = (
        [
            sys.executable,
            str(CHECKER),
            str(REQUIREMENTS),
            "--expect",
            "psycopg-binary==3.3.4",
        ],
        [sys.executable, "-m", "pip", "check"],
    )
    for command in commands:
        result = subprocess.run(
            command,
            env=clean_environment(extra_path),
            text=True,
            capture_output=True,
            check=False,
        )
        if not quiet and result.stdout:
            print(result.stdout.rstrip(), file=sys.stderr)
        if result.returncode != 0:
            if not quiet and result.stderr:
                print(result.stderr.rstrip(), file=sys.stderr)
            return False
    return True


def repair_command(temporary):
    return [
        sys.executable,
        "-m",
        "pip",
        "install",
        "--disable-pip-version-check",
        "--no-input",
        "--no-index",
        "--find-links",
        str(WHEELHOUSE),
        "--only-binary=:all:",
        "--target",
        str(temporary),
        "-r",
        str(REQUIREMENTS),
    ]


def target_valid(target, fingerprint):
    marker = target / COMPLETE_NAME
    if not target.is_dir() or target.is_symlink() or marker.is_symlink():
        return False
    if not marker.is_file() or marker.read_text(encoding="utf-8").strip() != fingerprint:
        return False
    return check_dependencies(target, quiet=True)


def ensure_runtime_root():
    if RUNTIME_ROOT.is_symlink():
        raise RuntimeError("runtime dependency root must not be a symlink")
    RUNTIME_ROOT.mkdir(mode=0o700, parents=True, exist_ok=True)
    if not os.access(RUNTIME_ROOT, os.W_OK):
        raise RuntimeError("runtime dependency root is not writable by the service UID")


def repair_dependencies(fingerprint):
    if fcntl is None:
        raise RuntimeError("POSIX file locking is unavailable")
    ensure_runtime_root()
    lock_path = RUNTIME_ROOT / ".dependency-bootstrap.lock"
    with lock_path.open("a+", encoding="utf-8") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX)
        for stale in RUNTIME_ROOT.glob(".dependencies-*"):
            if stale.parent != RUNTIME_ROOT:
                raise RuntimeError("unsafe stale dependency path")
            if stale.is_symlink():
                stale.unlink()
            elif stale.is_dir():
                shutil.rmtree(stale)
            else:
                raise RuntimeError("unexpected stale dependency entry")
        if target_valid(DEPENDENCY_TARGET, fingerprint):
            return DEPENDENCY_TARGET
        temporary = Path(tempfile.mkdtemp(prefix=".dependencies-", dir=RUNTIME_ROOT))
        try:
            print(
                "Base dependency check failed; repairing from the verified local wheelhouse.",
                file=sys.stderr,
            )
            result = subprocess.run(
                repair_command(temporary),
                env=clean_environment(),
                stdout=sys.stderr,
                stderr=sys.stderr,
                check=False,
            )
            if result.returncode != 0 or not check_dependencies(temporary):
                raise RuntimeError("offline dependency repair failed validation")
            marker = temporary / COMPLETE_NAME
            marker.write_text(fingerprint + "\n", encoding="utf-8")
            marker.chmod(0o600)
            if DEPENDENCY_TARGET.exists():
                if DEPENDENCY_TARGET.is_symlink() or DEPENDENCY_TARGET.parent != RUNTIME_ROOT:
                    raise RuntimeError("unsafe runtime dependency target")
                shutil.rmtree(DEPENDENCY_TARGET)
            os.replace(temporary, DEPENDENCY_TARGET)
        finally:
            if os.path.lexists(str(temporary)):
                shutil.rmtree(temporary)
    return DEPENDENCY_TARGET


def dependency_path():
    manifest_digest = verify_manifest(WHEELHOUSE)
    fingerprint = hashlib.sha256(
        (BOOTSTRAP_FORMAT + "\0" + manifest_digest + "\0").encode("utf-8")
        + REQUIREMENTS.read_bytes()
    ).hexdigest()
    if check_dependencies():
        return None
    return repair_dependencies(fingerprint)


def main():
    arguments = sys.argv[1:]
    if len(arguments) == 2 and arguments[0] == "--write-wheelhouse-manifest":
        write_manifest(Path(arguments[1]))
        return 0
    if arguments[:1] == ["--"]:
        arguments = arguments[1:]
    if not arguments:
        print("container dependency bootstrap requires a command", file=sys.stderr)
        return 64
    try:
        target = dependency_path()
    except (OSError, RuntimeError) as error:
        print("BLOCKED Proxy dependency bootstrap: {}".format(error), file=sys.stderr)
        return 70
    environment = clean_environment(target)
    os.execvpe(arguments[0], arguments, environment)
    return 70


if __name__ == "__main__":
    raise SystemExit(main())

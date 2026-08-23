#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Build and verify a deterministic standalone release ZIP."""

import hashlib
import os
import stat
import sys
import tempfile
import zipfile

from functools import partial


EXCLUDED_NAMES = {
    "__pycache__", ".git", ".venv", ".test-tmp", ".env", "runtime-secrets",
    "build", "dist",
}
EXCLUDED_SUFFIXES = (".pyc", ".pyo", ".zip")
ZIP_TIMESTAMP = (2026, 8, 12, 0, 0, 0)


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(partial(handle.read, 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _release_files(root):
    result = []
    for directory, names, files in os.walk(root):
        names[:] = sorted(
            name for name in names
            if name not in EXCLUDED_NAMES and not name.endswith(".egg-info")
        )
        for name in sorted(files):
            if name in EXCLUDED_NAMES or name.endswith(EXCLUDED_SUFFIXES):
                continue
            path = os.path.join(directory, name)
            if os.path.islink(path):
                raise RuntimeError("symbolic links are forbidden: " + path)
            result.append(path)
    return sorted(result)


def _write_manifest(root, files):
    path = os.path.join(root, "SHA256SUMS")
    lines = []
    for source in files:
        relative = os.path.relpath(source, root).replace(os.sep, "/")
        lines.append(_sha256(source) + "  " + relative)
    with open(path, "w", encoding="utf-8", newline="\n") as handle:
        handle.write("\n".join(lines) + "\n")
        handle.flush()
        os.fsync(handle.fileno())
    return path


def _zip_info(archive_name, mode):
    info = zipfile.ZipInfo(archive_name, ZIP_TIMESTAMP)
    info.compress_type = zipfile.ZIP_DEFLATED
    info.create_system = 3
    info.external_attr = (stat.S_IFREG | mode) << 16
    return info


def _write_zip(root, destination):
    folder = os.path.basename(root)
    files = _release_files(root)
    with zipfile.ZipFile(
            destination, "w", compression=zipfile.ZIP_DEFLATED,
            compresslevel=9, strict_timestamps=True) as archive:
        for source in files:
            relative = os.path.relpath(source, root).replace(os.sep, "/")
            archive_name = folder + "/" + relative
            mode = stat.S_IMODE(os.stat(source).st_mode)
            with open(source, "rb") as handle:
                archive.writestr(_zip_info(archive_name, mode), handle.read())


def _verify_zip(path, expected_prefix):
    with zipfile.ZipFile(path, "r") as archive:
        names = archive.namelist()
        if not names or any(
                name.startswith("/") or ".." in name.split("/")
                or not name.startswith(expected_prefix + "/") for name in names):
            raise RuntimeError("release ZIP contains an unsafe path")
        bad = archive.testzip()
        if bad is not None:
            raise RuntimeError("release ZIP CRC failed: " + bad)
        for info in archive.infolist():
            mode = (info.external_attr >> 16) & 0o170000
            if mode == stat.S_IFLNK:
                raise RuntimeError("release ZIP contains a symbolic link")


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    destination = os.path.join(os.path.dirname(root), os.path.basename(root) + ".zip")
    files = [
        path for path in _release_files(root)
        if os.path.basename(path) != "SHA256SUMS"
    ]
    manifest = _write_manifest(root, files)
    try:
        _write_zip(root, destination)
        _verify_zip(destination, os.path.basename(root))
        descriptor, second = tempfile.mkstemp(prefix=".mcp-release-", suffix=".zip")
        os.close(descriptor)
        try:
            _write_zip(root, second)
            if _sha256(destination) != _sha256(second):
                raise RuntimeError("release ZIP is not reproducible")
        finally:
            os.unlink(second)
    except Exception:
        try:
            os.unlink(destination)
        except OSError:
            pass
        raise
    print(destination)
    print(_sha256(destination))
    print(os.path.getsize(destination))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

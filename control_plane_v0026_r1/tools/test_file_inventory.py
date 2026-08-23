#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Verify file-inventory coverage, modes, sizes and hashes."""

import csv
import hashlib
import stat

from functools import partial
from pathlib import Path

from build_release_archive import relative_files
from build_file_inventory import INVENTORY_RELATIVE, SPECIAL_STATUS


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(partial(stream.read, 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main():
    root = Path(__file__).resolve().parent.parent
    inventory = root / INVENTORY_RELATIVE
    with inventory.open("r", encoding="utf-8", newline="") as stream:
        rows = tuple(csv.DictReader(stream))
    paths = tuple(row.get("path", "") for row in rows)
    if len(paths) != len(set(paths)):
        raise ValueError("file inventory contains duplicate paths")
    expected = set(relative_files(root))
    expected.add(Path("SHA256SUMS"))
    expected.add(INVENTORY_RELATIVE)
    actual = {Path(value) for value in paths}
    if actual != expected:
        missing = sorted((item.as_posix() for item in expected - actual))
        extra = sorted((item.as_posix() for item in actual - expected))
        raise ValueError("file inventory coverage differs: missing={!r}, extra={!r}".format(missing, extra))
    for row in rows:
        relative = Path(row["path"])
        path = root / relative
        if not path.is_file() or path.is_symlink():
            raise ValueError("inventory path is not a regular file: " + relative.as_posix())
        expected_mode = format(stat.S_IMODE(path.stat().st_mode), "04o")
        if row.get("mode_octal") != expected_mode:
            raise ValueError("inventory mode differs: " + relative.as_posix())
        special = SPECIAL_STATUS.get(relative)
        if special:
            if row.get("syntax_status") != special:
                raise ValueError("inventory special status differs: " + relative.as_posix())
            continue
        if row.get("size_bytes") != str(path.stat().st_size):
            raise ValueError("inventory size differs: " + relative.as_posix())
        if row.get("sha256") != _sha256(path):
            raise ValueError("inventory SHA-256 differs: " + relative.as_posix())
    print("PASS v0026 file inventory coverage and hashes: {} paths".format(len(rows)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print("file inventory validation failed: " + str(exc), file=__import__("sys").stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read one protected credential from an allowlisted JSON path.

The module is intentionally importable by ``ssh_askpass.py``.  It never emits
an entire credentials document and never includes secret values in errors.
"""

import argparse
import errno
import json
import os
import stat
import sys

from pathlib import Path


MAX_FILE_BYTES = 64 * 1024
MAX_CREDENTIAL_CHARS = 4096


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + str(key))
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("unsupported JSON numeric constant: " + value)


def _secure_read(path):
    path = Path(path).expanduser()
    try:
        link_metadata = os.lstat(path)
    except FileNotFoundError as error:
        raise ValueError("credentials file not found: " + str(path)) from error
    if stat.S_ISLNK(link_metadata.st_mode):
        raise ValueError("credentials file must not be a symbolic link")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError("credentials file must not be a symbolic link") from error
        raise ValueError("credentials file cannot be opened: " + str(path)) from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("credentials path must name a regular file")
        if metadata.st_uid != os.geteuid():
            raise ValueError("credentials file must be owned by the current user")
        mode = stat.S_IMODE(metadata.st_mode)
        if mode & ~0o600:
            raise ValueError(
                "credentials file permissions must be 0600 or stricter: " + oct(mode)
            )
        if not mode & stat.S_IRUSR:
            raise ValueError("credentials file must be readable by its owner")
        if metadata.st_size > MAX_FILE_BYTES:
            raise ValueError("credentials file exceeds 64 KiB")

        chunks = []
        remaining = MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
        if len(raw) > MAX_FILE_BYTES:
            raise ValueError("credentials file exceeds 64 KiB")
        return raw
    finally:
        os.close(descriptor)


def load_document(path):
    raw = _secure_read(path)
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("credentials file is not valid UTF-8") from error
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError(
            "credentials file is not valid JSON at line {}, column {}".format(
                error.lineno, error.colno
            )
        ) from error
    if not isinstance(document, dict):
        raise ValueError("credentials root must be a JSON object")
    return document


def _lookup(document, dotted_key):
    current = document
    for segment in dotted_key.split("."):
        if not segment or not isinstance(current, dict) or segment not in current:
            return None
        current = current[segment]
    if isinstance(current, dict):
        current = current.get("password")
    return current


def _validate_credential(value):
    if not isinstance(value, str) or not value:
        return None
    if len(value) > MAX_CREDENTIAL_CHARS:
        raise ValueError("credential value exceeds 4096 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError("credential value contains control characters")
    return value


def load_credential(path, keys):
    document = load_document(path)
    for dotted_key in keys:
        value = _validate_credential(_lookup(document, dotted_key))
        if value is not None:
            return value
    raise ValueError("required credential key was not found: " + ", ".join(keys))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--file", type=Path, required=True)
    parser.add_argument("--key", action="append", required=True)
    parser.add_argument("--check-only", action="store_true")
    arguments = parser.parse_args()

    try:
        value = load_credential(arguments.file, tuple(arguments.key))
    except ValueError as error:
        print("credentials error: " + str(error), file=sys.stderr)
        return 65
    except OSError as error:
        print("credentials error: " + str(error), file=sys.stderr)
        return 65

    if not arguments.check_only:
        sys.stdout.write(value)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

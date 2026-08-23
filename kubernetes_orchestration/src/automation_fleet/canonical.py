"""Canonical serialization, digest and atomic file helpers."""

import hashlib
import json
import os
import pathlib
import tempfile


def canonical_text(value):
    return json.dumps(
        value,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )


def canonical_bytes(value):
    return canonical_text(value).encode("utf-8")


def digest_bytes(data):
    return "sha256:" + hashlib.sha256(data).hexdigest()


def digest_value(value):
    return digest_bytes(canonical_bytes(value))


def digest_file(path):
    state = hashlib.sha256()
    handle = open(path, "rb")
    try:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            state.update(block)
    finally:
        handle.close()
    return "sha256:" + state.hexdigest()


def read_json(path):
    handle = open(path, "r", encoding="utf-8")
    try:
        return json.load(handle)
    finally:
        handle.close()


def atomic_write(path, data, mode=0o600):
    target = pathlib.Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(
        prefix="." + target.name + ".",
        dir=str(target.parent),
    )
    try:
        os.fchmod(descriptor, mode)
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory_descriptor = os.open(
            str(target.parent), os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory_descriptor)
        finally:
            os.close(directory_descriptor)
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def atomic_json(path, value, mode=0o600):
    data = json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True)
    atomic_write(path, (data + "\n").encode("utf-8"), mode=mode)


def digest_tree(root):
    base = pathlib.Path(root).resolve()
    entries = []
    for path in sorted(base.rglob("*")):
        if path.is_symlink():
            raise ValueError("tree contains a symbolic link: " + str(path))
        if not path.is_file():
            continue
        relative = path.relative_to(base).as_posix()
        entries.append({
            "path": relative,
            "sha256": digest_file(path),
            "size": path.stat().st_size,
        })
    return digest_value(entries)

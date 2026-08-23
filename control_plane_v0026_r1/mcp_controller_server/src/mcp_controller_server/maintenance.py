# -*- coding: utf-8 -*-

"""Online state backup utilities for lifecycle automation."""

import argparse
import hashlib
import json
import os
import shutil
import sqlite3
import tempfile

from functools import partial

from mcp_controller_server.canonical import canonical_text, format_timestamp, utc_now
from mcp_controller_server.config import load_config


def _sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for block in iter(partial(handle.read, 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _copy_complete_lines(source, target):
    if not os.path.isfile(source):
        with open(target, "wb"):
            pass
        return
    with open(source, "rb") as input_handle:
        data = input_handle.read(64 * 1024 * 1024 + 1)
    if len(data) > 64 * 1024 * 1024:
        raise ValueError("audit log exceeds online backup limit")
    final_newline = data.rfind(b"\n")
    complete = data[:final_newline + 1] if final_newline >= 0 else b""
    with open(target, "wb") as output_handle:
        output_handle.write(complete)
        output_handle.flush()
        os.fsync(output_handle.fileno())
    os.chmod(target, 0o600)


def _sqlite_backup(source, target):
    source_connection = sqlite3.connect("file:" + source + "?mode=ro", uri=True)
    target_connection = sqlite3.connect(target)
    try:
        source_connection.backup(target_connection)
        checked = target_connection.execute("PRAGMA integrity_check").fetchone()
        if not checked or checked[0] != "ok":
            raise ValueError("SQLite backup integrity check failed")
    finally:
        target_connection.close()
        source_connection.close()
    os.chmod(target, 0o600)


def backup_state(config, directory):
    destination = os.path.abspath(directory)
    if os.path.lexists(destination):
        raise ValueError("backup destination already exists")
    parent = os.path.dirname(destination)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    temporary = tempfile.mkdtemp(prefix=".mcp-backup-", dir=parent)
    try:
        database_target = os.path.join(temporary, "controller.sqlite3")
        audit_target = os.path.join(temporary, "audit.jsonl")
        _sqlite_backup(config["policy"]["state_db"], database_target)
        _copy_complete_lines(config["policy"]["audit_log"], audit_target)
        manifest = {
            "schema_version": 1,
            "component": "mcp_controller_server",
            "created_at": format_timestamp(utc_now()),
            "files": {
                "controller.sqlite3": _sha256_file(database_target),
                "audit.jsonl": _sha256_file(audit_target),
            },
        }
        manifest_path = os.path.join(temporary, "manifest.json")
        with open(manifest_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_text(manifest) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, destination)
        return manifest
    except Exception:
        shutil.rmtree(temporary, ignore_errors=True)
        raise


def _parser():
    parser = argparse.ArgumentParser(description="MCP controller maintenance")
    parser.add_argument("--config", default=os.environ.get("MCP_CONTROLLER_CONFIG"))
    subparsers = parser.add_subparsers(dest="command", required=True)
    backup = subparsers.add_parser("backup")
    backup.add_argument("--directory", required=True)
    return parser


def main(argv=None):
    arguments = _parser().parse_args(argv)
    config = load_config(arguments.config, check_files=True)
    if arguments.command == "backup":
        manifest = backup_state(config, arguments.directory)
        print(json.dumps({"ok": True, "manifest": manifest}, sort_keys=True))
        return 0
    return 1


if __name__ == "__main__":
    raise SystemExit(main())

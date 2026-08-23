# -*- coding: utf-8 -*-

"""Audit sanitization, symlink defense and online state backup tests."""

import json
import os
import tempfile

from mcp_controller_server.approval_store import create_approval_store, prepare_plan
from mcp_controller_server.audit import append_audit, create_audit_writer
from mcp_controller_server.maintenance import backup_state
from tests.support import make_test_config


def test_audit_is_payload_free_and_rejects_symlink():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "audit.jsonl")
        writer = create_audit_writer(path)
        append_audit(writer, {
            "request_id": "1",
            "subject": "mcp:test",
            "tool": "automation_nodes_list",
            "decision": "ALLOW",
            "secret": "must-not-appear",
            "payload": {"must": "not-appear"},
        })
        with open(path, "r", encoding="utf-8") as handle:
            record = json.loads(handle.readline())
        assert record["subject"] == "mcp:test"
        assert "secret" not in record and "payload" not in record
        target = os.path.join(directory, "target.log")
        link = os.path.join(directory, "linked.log")
        with open(target, "w", encoding="utf-8"):
            pass
        os.symlink(target, link)
        try:
            create_audit_writer(link)
        except ValueError:
            pass
        else:
            raise AssertionError("symbolic audit path was accepted")


def test_online_backup_has_integrity_manifest_and_private_modes():
    with tempfile.TemporaryDirectory() as directory:
        config = make_test_config(directory)
        store = create_approval_store(config["policy"]["state_db"], 300)
        prepare_plan(store, "mcp:test", "node.drain", {"node_id": "fn-01"})
        writer = create_audit_writer(config["policy"]["audit_log"])
        append_audit(writer, {
            "request_id": "2", "tool": "automation_change_prepare",
            "decision": "ALLOW", "subject": "mcp:test",
        })
        destination = os.path.join(directory, "backups", "backup-1")
        manifest = backup_state(config, destination)
        assert set(manifest["files"]) == {"controller.sqlite3", "audit.jsonl"}
        for name in manifest["files"]:
            path = os.path.join(destination, name)
            assert os.path.isfile(path)
            assert os.stat(path).st_mode & 0o077 == 0
        with open(os.path.join(destination, "manifest.json"), "r", encoding="utf-8") as handle:
            stored = json.load(handle)
        assert stored == manifest

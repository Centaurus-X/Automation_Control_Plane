# -*- coding: utf-8 -*-

"""Cross-check the embedded subset against the supplied Control Plane source."""

import datetime
import hashlib
import importlib
import os
import sys
import uuid

from mcp_controller_server import automation_contract as candidate


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                return digest.hexdigest()
            digest.update(block)


def test_supplied_control_plane_contract_vectors():
    release_root = os.environ.get("CONTROL_PLANE_REFERENCE_ROOT")
    if not release_root:
        print("SKIP supplied Control Plane reference was not selected")
        return
    master_root = os.path.join(release_root, "master_node", "master_node")
    sys.path.insert(0, master_root)
    try:
        reference = importlib.import_module(
            "src.library._generic_system_contract_library.protocol_contracts")
        message_id = str(uuid.UUID("11111111-1111-4111-8111-111111111111"))
        assert candidate.master_command_topic(
            "site-main", "master-01", "config.get") == reference.master_command_topic(
                "site-main", "master-01", "config.get")
        assert candidate.proxy_ack_topic(
            "site-main", "proxy-01", "master-01", message_id) == reference.proxy_ack_topic(
                "site-main", "proxy-01", "master-01", message_id)
        assert candidate.control_event_filter(
            "site-main", "master-01") == reference.control_event_filter(
                "site-main", "master-01")
        now = datetime.datetime(2026, 8, 12, tzinfo=datetime.timezone.utc)
        values = {
            "site_id": "site-main",
            "message_id": message_id,
            "correlation_id": message_id,
            "now": now,
            "ttl_s": 30.0,
            "principal": {
                "subject": "mcp:test", "permissions": ["node.read"],
                "site_scopes": ["site-main"], "resource_scopes": [],
            },
            "payload": {},
        }
        source = {"service": "proxy", "instance_id": "proxy-01"}
        target = {"service": "master", "instance_id": "master-01"}
        assert candidate.make_envelope(
            "runtime.status", source, target, **values) == reference.make_envelope(
                "runtime.status", source, target, **values)
        payloads = (
            ("runtime.status", {}),
            ("nodes.list", {}),
            ("node.drain", {"node_id": "fn-01", "deadline_s": 10}),
            ("config.history", {"limit": 50}),
        )
        for event_type, payload in payloads:
            assert candidate.validate_payload(event_type, payload) == (
                reference.validate_payload(event_type, payload))
        copied = os.path.join(
            os.path.dirname(candidate.__file__), "_lightweight_mqtt_client.py")
        source_client = os.path.join(
            master_root, "src", "library", "_lightweight_mqtt_client.py")
        assert _sha256(copied) == _sha256(source_client)
    finally:
        sys.path.remove(master_root)

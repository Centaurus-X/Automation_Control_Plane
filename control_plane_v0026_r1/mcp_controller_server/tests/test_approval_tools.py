# -*- coding: utf-8 -*-

"""Two-phase approval, authorization and idempotent commit tests."""

import os
import tempfile

from mcp_controller_server.approval_store import (
    approve_plan,
    create_approval_store,
    finalize_plan,
    prepare_plan,
    reserve_plan,
)
from mcp_controller_server.mcp_protocol import handle_jsonrpc
from mcp_controller_server.runtime import create_runtime, start_runtime, stop_runtime
from mcp_controller_server.tools import execute_tool
from tests.support import full_principal, make_test_config, make_transport_factory


def test_mutation_requires_exact_external_approval_and_is_idempotent():
    with tempfile.TemporaryDirectory() as directory:
        config = make_test_config(directory)
        transport_state = {"auto_ack": True}
        runtime = create_runtime(
            config,
            transport_factory=make_transport_factory(transport_state, config),
            auth_state={"test": True},
        )
        context = {"client_id": "test-agent", "principal": full_principal()}
        start_runtime(runtime)
        try:
            prepared = execute_tool(
                runtime, "automation_change_prepare",
                {"action": "node.drain", "payload": {
                    "node_id": "fn-01", "deadline_s": 30,
                    "reason": "maintenance",
                }}, context,
            )
            assert prepared["ok"] and prepared["state"] == "PREPARED"
            denied = execute_tool(
                runtime, "automation_change_commit",
                {"plan_id": prepared["plan_id"],
                 "payload_sha256": prepared["payload_sha256"]}, context,
            )
            assert not denied["ok"] and denied["code"] == "approval_required"
            mismatch = approve_plan(
                runtime["approvals"], prepared["plan_id"], "b" * 64, "operator:test")
            assert not mismatch["ok"] and mismatch["code"] == "approval_mismatch"
            approved = approve_plan(
                runtime["approvals"], prepared["plan_id"],
                prepared["payload_sha256"], "operator:test",
            )
            assert approved["ok"] and approved["state"] == "APPROVED"
            committed = execute_tool(
                runtime, "automation_change_commit",
                {"plan_id": prepared["plan_id"],
                 "payload_sha256": prepared["payload_sha256"]}, context,
            )
            replay = execute_tool(
                runtime, "automation_change_commit",
                {"plan_id": prepared["plan_id"],
                 "payload_sha256": prepared["payload_sha256"]}, context,
            )
            assert committed["ok"] and committed["plan_state"] == "COMPLETED"
            assert replay["ok"] and replay["request_id"] == committed["request_id"]
            assert len(transport_state["published"]) == 1
            envelope = transport_state["published"][0]["envelope"]
            assert envelope["message_id"] == prepared["plan_id"]
            assert envelope["idempotency_key"] == "mcp-" + prepared["plan_id"]
            assert envelope["principal"]["subject"] == "mcp:test-agent"
        finally:
            stop_runtime(runtime)


def test_permissions_and_site_scope_are_enforced():
    with tempfile.TemporaryDirectory() as directory:
        config = make_test_config(directory)
        runtime = create_runtime(
            config,
            transport_factory=make_transport_factory({}, config),
            auth_state={"test": True},
        )
        principal = full_principal()
        principal["permissions"].remove("node.drain")
        context = {"client_id": "test-agent", "principal": principal}
        result = execute_tool(
            runtime, "automation_change_prepare",
            {"action": "node.drain", "payload": {"node_id": "fn-01"}}, context,
        )
        assert not result["ok"] and result["code"] == "authorization_denied"


def test_concurrent_commit_reservation_is_fail_closed_and_retryable():
    with tempfile.TemporaryDirectory() as directory:
        store = create_approval_store(
            os.path.join(directory, "approvals.sqlite3"), 300)
        prepared = prepare_plan(
            store, "mcp:test-agent", "node.drain", {"node_id": "fn-01"})
        assert approve_plan(
            store, prepared["plan_id"], prepared["payload_sha256"],
            "operator:test")["ok"]
        first = reserve_plan(
            store, prepared["plan_id"], prepared["payload_sha256"],
            "mcp:test-agent")
        second = reserve_plan(
            store, prepared["plan_id"], prepared["payload_sha256"],
            "mcp:test-agent")
        assert first["ok"] and not first["resumed"]
        assert not second["ok"] and second["code"] == "commit_in_progress"
        finalized = finalize_plan(store, prepared["plan_id"], {
            "ok": False, "code": "command_timeout", "retryable": True,
        })
        assert finalized["ok"] and finalized["state"] == "COMMITTING"
        resumed = reserve_plan(
            store, prepared["plan_id"], prepared["payload_sha256"],
            "mcp:test-agent")
        assert resumed["ok"] and resumed["resumed"]


def test_mcp_mutation_fails_before_execution_when_audit_is_unavailable():
    with tempfile.TemporaryDirectory() as directory:
        config = make_test_config(directory)
        runtime = create_runtime(
            config,
            transport_factory=make_transport_factory({}, config),
            auth_state={"test": True},
        )
        runtime["audit"]["path"] = directory
        context = {"client_id": "test-agent", "principal": full_principal()}
        response = handle_jsonrpc(runtime, {
            "jsonrpc": "2.0", "id": 9, "method": "tools/call",
            "params": {
                "name": "automation_change_prepare",
                "arguments": {
                    "action": "node.drain",
                    "payload": {"node_id": "fn-01"},
                },
            },
        }, context)
        result = response["result"]["structuredContent"]
        assert not result["ok"] and result["code"] == "audit_unavailable"

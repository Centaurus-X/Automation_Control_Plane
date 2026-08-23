# -*- coding: utf-8 -*-

"""Regression test for JSONB-safe durable control-command results."""

import datetime
import json

from types import ModuleType

from src.library import authority_store_postgresql


def _test_datetime_results_are_normalized_before_jsonb_persistence():
    captured = {}

    def execute(_statement, parameters):
        captured["parameters"] = parameters

    def connection():
        return None

    connection.execute = execute

    def transaction(operation, **_kwargs):
        return operation(connection)

    def capture_jsonb(value):
        captured["jsonb"] = value
        return value

    database = {"transaction": transaction}
    message = {
        "site_id": "site_main",
        "source_service": "proxy",
        "source_instance_id": "proxy-01",
        "message_id": "00000000-0000-0000-0000-000000000001",
    }
    result = {
        "ok": True,
        "nodes": [{
            "node_id": "fn-01",
            "last_seen_at": datetime.datetime(
                2026, 7, 16, 20, 0, tzinfo=datetime.timezone.utc),
        }],
    }
    original_jsonb = authority_store_postgresql.jsonb
    try:
        authority_store_postgresql.jsonb = capture_jsonb
        returned = authority_store_postgresql.complete_inbox(
            database, message, result)
    finally:
        authority_store_postgresql.jsonb = original_jsonb
    assert returned == result
    assert captured["parameters"][1] == captured["jsonb"]
    assert captured["jsonb"]["nodes"][0]["last_seen_at"].endswith("+00:00")
    json.dumps(captured["jsonb"], allow_nan=False)


def _test_duplicate_outbox_reports_its_terminal_state():
    existing = {
        "message_id": "rollout-01",
        "site_id": "site_main",
        "target_service": "worker",
        "target_instance_id": "fn-01",
        "event_type": "distributed.config.apply",
        "envelope": {"rollout_id": "rollout-01"},
        "state": "FAILED",
        "last_error_code": "stale_replica",
    }
    result = ModuleType("duplicate_outbox_result")

    def fetchone():
        return dict(existing)

    result.fetchone = fetchone

    def execute(statement, _parameters):
        assert statement.startswith("SELECT * FROM master_control.outbox")
        return result

    def connection():
        return None

    connection.execute = execute

    def transaction(operation, **_kwargs):
        return operation(connection)

    duplicate = authority_store_postgresql.enqueue_outbox(
        {"transaction": transaction}, dict(existing))
    assert duplicate["ok"] and duplicate["duplicate"]
    assert duplicate["state"] == "FAILED"
    assert duplicate["last_error_code"] == "stale_replica"


def main():
    _test_datetime_results_are_normalized_before_jsonb_persistence()
    _test_duplicate_outbox_reports_its_terminal_state()
    print("PASS test_inbox_json_persistence (2 scenarios)")


if __name__ == "__main__":
    main()

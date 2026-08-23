# -*- coding: utf-8 -*-

"""PostgreSQL latest-state writes must reject reordered telemetry."""

import copy

from types import ModuleType

from src.library import node_store_postgresql
from src.library.node_store_postgresql import record_heartbeat, upsert_sensor


def _row_result(row):
    result = ModuleType("row_result")

    def fetchone():
        return copy.deepcopy(row)

    result.fetchone = fetchone
    return result


def _identity(value):
    return value


def _heartbeat_database(initial):
    state = copy.deepcopy(initial)
    history = set()
    statements = []
    connection = ModuleType("heartbeat_connection")

    def execute(statement, parameters=None):
        statements.append((statement, parameters))
        if statement.startswith("SELECT * FROM master_control.nodes"):
            return _row_result(state)
        if statement.startswith("INSERT INTO master_control.node_heartbeats"):
            message_id = parameters[2]
            if message_id in history:
                return _row_result(None)
            history.add(message_id)
            return _row_result({"heartbeat_id": len(history)})
        if statement.startswith("UPDATE master_control.nodes SET observed_state"):
            keys = (
                "observed_state", "authority_term", "config_revision",
                "config_fingerprint", "capabilities", "runtime_nonce",
                "protocol_version", "protocol_versions", "contracts_version",
                "observed_global_revision",
                "node_replica_scope", "node_replica_revision", "node_replica_fingerprint",
                "cross_replica_scope", "cross_replica_revision",
                "cross_replica_fingerprint", "live_config_revision",
                "pending_proposal_count", "max_packet_size",
            )
            for key, value in zip(keys, parameters[:19]):
                if value is not None:
                    state[key] = value
            return _row_result(state)
        raise AssertionError("unexpected heartbeat statement: " + statement)

    def transaction(operation, **_kwargs):
        return operation(connection)

    connection.execute = execute
    return {"transaction": transaction}, state, history, statements


def _node():
    return {
        "site_id": "site_main", "node_id": "fn-01",
        "admission_state": "APPROVED", "observed_state": "ACTIVE",
        "authority_term": 5, "config_revision": 10,
        "config_fingerprint": "a" * 64,
        "observed_global_revision": 10, "live_config_revision": 10,
        "node_replica_scope": "node:fn-01", "node_replica_revision": 8,
        "node_replica_fingerprint": "b" * 64,
        "cross_replica_scope": "cross:fn-01.default", "cross_replica_revision": 7,
        "cross_replica_fingerprint": "c" * 64,
        "capabilities": {}, "pending_proposal_count": 0,
        "max_packet_size": 1048576, "runtime_nonce": "runtime-01",
    }


def _heartbeat(message_id, **kwargs):
    return {
        "site_id": "site_main", "node_id": "fn-01", "message_id": message_id,
        "authority_term": kwargs.get("authority_term", 5),
        "config_revision": kwargs.get("config_revision", 10),
        "config_fingerprint": kwargs.get("config_fingerprint", "a" * 64),
        "observed_state": kwargs.get("observed_state", "ACTIVE"), "health": {},
        "observed_global_revision": kwargs.get("observed_global_revision", 10),
        "live_config_revision": kwargs.get("live_config_revision", 10),
        "node_replica": kwargs.get("node_replica", {
            "scope": "node:fn-01", "revision": 8, "fingerprint": "b" * 64,
        }),
        "cross_replica": kwargs.get("cross_replica", {
            "scope": "cross:fn-01.default", "revision": 7,
            "fingerprint": "c" * 64,
        }),
        "runtime_nonce": kwargs.get("runtime_nonce", "runtime-01"),
    }


def _test_heartbeat_term_and_replica_anchors_are_monotone():
    database, state, history, statements = _heartbeat_database(_node())
    stale = record_heartbeat(database, _heartbeat(
        "heartbeat-old-term", authority_term=4,
        config_revision=99, node_replica={
            "scope": "node:fn-01", "revision": 99, "fingerprint": "x" * 64,
        }))
    assert stale["ok"] and stale["stale"] and not stale["updated"]
    assert state["authority_term"] == 5 and state["config_revision"] == 10
    assert state["node_replica_revision"] == 8
    assert "heartbeat-old-term" in history
    assert not any(item[0].startswith(
        "UPDATE master_control.nodes SET observed_state") for item in statements)

    updates_before = len([item for item in statements if item[0].startswith(
        "UPDATE master_control.nodes SET observed_state")])
    stale_replica = record_heartbeat(database, _heartbeat(
        "heartbeat-old-replica", config_revision=10,
        node_replica={
            "scope": "node:fn-01", "revision": 7, "fingerprint": "e" * 64,
        },
        cross_replica={
            "scope": "cross:fn-01.default", "revision": 8,
            "fingerprint": "f" * 64,
        }))
    assert stale_replica["ok"] and stale_replica["stale"]
    assert not stale_replica["updated"]
    assert state["node_replica_revision"] == 8
    assert state["cross_replica_revision"] == 7
    updates_after = len([item for item in statements if item[0].startswith(
        "UPDATE master_control.nodes SET observed_state")])
    assert updates_after == updates_before

    advanced_replica = record_heartbeat(database, _heartbeat(
        "heartbeat-new-cross-replica", observed_global_revision=9,
        live_config_revision=9, cross_replica={
            "scope": "cross:fn-01.default", "revision": 8,
            "fingerprint": "f" * 64,
        }))
    assert advanced_replica["ok"] and advanced_replica["updated"]
    assert not advanced_replica["stale"]
    assert state["observed_global_revision"] == 10
    assert state["live_config_revision"] == 10
    assert state["node_replica_revision"] == 8
    assert state["node_replica_fingerprint"] == "b" * 64
    assert state["cross_replica_revision"] == 8
    assert state["cross_replica_fingerprint"] == "f" * 64

    advanced = record_heartbeat(database, _heartbeat(
        "heartbeat-new-term", authority_term=6, config_revision=11,
        config_fingerprint="1" * 64, observed_global_revision=11,
        live_config_revision=11, node_replica={
            "scope": "node:fn-01", "revision": 9, "fingerprint": "2" * 64,
        }, cross_replica={
            "scope": "cross:fn-01.default", "revision": 8,
            "fingerprint": "f" * 64,
        }))
    assert advanced["ok"] and advanced["updated"]
    assert state["authority_term"] == 6 and state["config_revision"] == 11
    assert state["node_replica_revision"] == 9

    reset = record_heartbeat(database, _heartbeat(
        "heartbeat-new-runtime", runtime_nonce="runtime-02",
        authority_term=0, config_revision=0, config_fingerprint=None,
        observed_global_revision=0, live_config_revision=0,
        node_replica={
            "scope": "node:fn-01", "revision": 0, "fingerprint": "",
        },
        cross_replica={
            "scope": "cross:fn-01.default", "revision": 0, "fingerprint": "",
        }))
    assert reset["ok"] and reset["updated"] and not reset["stale"]
    assert state["runtime_nonce"] == "runtime-02"
    assert state["authority_term"] == 0 and state["config_revision"] == 0
    assert state["node_replica_revision"] == 0
    assert state["node_replica_fingerprint"] == ""


def _sensor_database(initial_sequence):
    state = {"source_sequence": initial_sequence, "updates": 0}
    connection = ModuleType("sensor_connection")

    def execute(statement, parameters=None):
        if statement.startswith("SELECT 1 AS owned"):
            return _row_result({"owned": 1})
        if statement.startswith("INSERT INTO master_control.sensor_latest"):
            assert "EXCLUDED.source_sequence IS NOT NULL" in statement
            assert "EXCLUDED.source_sequence>sensor_latest.source_sequence" in statement
            assert "EXCLUDED.source_sequence>=" not in statement
            supplied = parameters[6]
            current = state["source_sequence"]
            advances = supplied is not None and (current is None or supplied > current)
            if advances:
                state["source_sequence"] = supplied
                state["updates"] += 1
                return _row_result({"sensor_id": parameters[2]})
            return _row_result(None)
        raise AssertionError("unexpected sensor statement: " + statement)

    def transaction(operation, **_kwargs):
        return operation(connection)

    connection.execute = execute
    return {"transaction": transaction}, state


def _sensor(sequence):
    return {
        "site_id": "site_main", "controller_uuid": "00000000-0000-0000-0000-000000000001",
        "sensor_id": "temperature", "source_node_id": "fn-01",
        "authority_term": 6, "config_revision": 11, "value": sequence,
        "quality": "good", "source_sequence": sequence,
        "measured_at": "2026-07-18T12:00:00Z",
    }


def _test_sensor_latest_requires_a_strict_non_null_sequence_advance():
    database, state = _sensor_database(10)
    for sequence in (10, None, 9):
        result = upsert_sensor(database, _sensor(sequence))
        assert result["ok"] and result["stale"] and not result["updated"]
        assert state["source_sequence"] == 10 and state["updates"] == 0
    result = upsert_sensor(database, _sensor(11))
    assert result["ok"] and result["updated"] and not result["stale"]
    assert state["source_sequence"] == 11 and state["updates"] == 1


def main():
    node_store_postgresql.jsonb = _identity
    _test_heartbeat_term_and_replica_anchors_are_monotone()
    _test_sensor_latest_requires_a_strict_non_null_sequence_advance()
    print("PASS monotone PostgreSQL heartbeat and sensor latest-state writes")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-

"""Master returns the durable Worker telemetry high-water on every ACK."""

from types import ModuleType

from src.core.node_registry import _registration_result
from src.library.node_store_postgresql import get_sensor_sequence_floor


def _row(value):
    result = ModuleType("sensor_sequence_floor_result")

    def fetchone():
        return value

    result.fetchone = fetchone
    return result


def main():
    captured = {}
    connection = ModuleType("sensor_sequence_floor_connection")

    def execute(statement, parameters=None):
        captured["statement"] = statement
        captured["parameters"] = parameters
        return _row({"sequence_floor": 3492})

    connection.execute = execute
    def transaction(operation, **_kwargs):
        return operation(connection)

    database = {"transaction": transaction}
    assert get_sensor_sequence_floor(database, "site_main", "fn-01") == 3492
    assert "MAX(source_sequence)" in captured["statement"]
    assert captured["parameters"] == ("site_main", "fn-01")

    def authority_reader():
        return {
            "authority_term": 7,
            "config_revision": 42,
            "config_fingerprint": "a" * 64,
        }

    def sensor_sequence_floor(site_id, node_id):
        assert (site_id, node_id) == ("site_main", "fn-01")
        return 3492

    context = {
        "site_id": "site_main",
        "coordinator_node_id": "master-01",
        "authority_reader": authority_reader,
        "store": {"sensor_sequence_floor": sensor_sequence_floor},
    }
    ack = _registration_result(context, {
        "node_id": "fn-01", "desired_state": "ACTIVE",
        "observed_state": "ACTIVE",
    })
    assert ack["ok"] and ack["sensor_sequence_floor"] == 3492
    assert ack["coordinator_term"] == 7
    assert ack["authority_mode"] == "node_local"
    assert ack["config_authority"] == "worker"
    assert ack["authority_revision"] == 0
    assert ack["global_revision"] == 0
    assert ack["mirror_head"] == {
        "scope": None,
        "authority_epoch": 0,
        "scope_revision": 0,
        "fingerprint": None,
    }
    print(
        "PASS node-local registration ACK separates telemetry floor, "
        "Registry fencing and Worker config authority")


if __name__ == "__main__":
    main()

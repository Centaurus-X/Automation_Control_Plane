# -*- coding: utf-8 -*-

"""Prometheus snapshot coverage for the pure-websockets transport."""

from src.interfaces.https_interface import _metrics_lines


def _metric_values(lines):
    values = {}
    for line in lines:
        if not line.startswith("#"):
            name, value = line.split(" ", 1)
            assert name not in values, name
            values[name] = value
    return values


def main():
    snapshot = {
        "ready": True,
        "mqtt": {"connected": False},
        "wss": {
            "clients": 7,
            "event_clients": 4,
            "control_clients": 3,
            "response_queue_depth": 11,
            "task_count": 9,
            "statistics": {
                "commands_accepted": 13,
                "commands_rejected": 5,
                "connections_accepted": 17,
                "connections_closed": 10,
                "connections_closed_cleanly": 8,
                "handshake_rejections": {"authentication": 2, "origin": 3},
                "command_results": {
                    "nodes.list|completed": 4,
                    "other|invalid_payload": 1,
                },
                "command_latency_seconds": {
                    "nodes.list|completed": 1.25,
                    "other|invalid_payload": 0.5,
                },
                "replay_sessions": 6,
                "replay_records": 21,
                "replay_lag_events_sum": 31,
                "replay_lag_events_max": 14,
                "event_fanout_deliveries": 12,
                "event_fanout_latency_seconds_sum": 0.125,
            },
        },
        "stream": {
            "clients": 4,
            "queue_depth": 17,
            "coalesced": 19,
            "critical_disconnects": 2,
            "telemetry_overflow_disconnects": 3,
        },
        "last_error": "secret-canary",
    }
    lines = _metrics_lines(snapshot)
    values = _metric_values(lines)

    assert values == {
        "automation_proxy_ready": "1",
        "automation_proxy_mqtt_connected": "0",
        "automation_proxy_websocket_clients": "4",
        "automation_proxy_websocket_queue_depth": "17",
        "automation_proxy_wss_event_clients": "4",
        "automation_proxy_wss_control_clients": "3",
        "automation_proxy_wss_response_queue_depth": "11",
        "automation_proxy_event_queue_depth": "17",
        "automation_proxy_event_coalesced_depth": "19",
        "automation_proxy_event_critical_disconnects_total": "2",
        "automation_proxy_event_telemetry_overflow_disconnects_total": "3",
        "automation_proxy_wss_commands_accepted_total": "13",
        "automation_proxy_wss_commands_rejected_total": "5",
        "automation_proxy_wss_handshakes_accepted_total": "17",
        "automation_proxy_wss_connections_closed_total": "10",
        "automation_proxy_wss_connections_closed_cleanly_total": "8",
        "automation_proxy_wss_tasks": "9",
        "automation_proxy_wss_replay_sessions_total": "6",
        "automation_proxy_wss_replay_records_total": "21",
        "automation_proxy_wss_replay_lag_events_sum": "31",
        "automation_proxy_wss_replay_lag_events_max": "14",
        "automation_proxy_wss_event_fanout_latency_seconds_sum": "0.125000000",
        "automation_proxy_wss_event_fanout_latency_seconds_count": "12",
        'automation_proxy_wss_handshake_rejections_total{reason="authentication"}': "2",
        'automation_proxy_wss_handshake_rejections_total{reason="origin"}': "3",
        'automation_proxy_wss_command_results_total{action="nodes.list",code="completed"}': "4",
        'automation_proxy_wss_command_results_total{action="other",code="invalid_payload"}': "1",
        'automation_proxy_wss_command_latency_seconds_sum{action="nodes.list",code="completed"}':
            "1.250000000",
        'automation_proxy_wss_command_latency_seconds_count{action="nodes.list",code="completed"}':
            "4",
        'automation_proxy_wss_command_latency_seconds_sum{action="other",code="invalid_payload"}':
            "0.500000000",
        'automation_proxy_wss_command_latency_seconds_count{action="other",code="invalid_payload"}':
            "1",
    }
    rendered = "\n".join(lines)
    assert "secret-canary" not in rendered
    type_names = [line.split()[2] for line in lines if line.startswith("# TYPE ")]
    assert len(type_names) == len(set(type_names))
    assert "# TYPE automation_proxy_event_critical_disconnects_total counter" in rendered
    assert "# TYPE automation_proxy_event_telemetry_overflow_disconnects_total counter" \
        in rendered

    legacy = _metric_values(_metrics_lines({
        "ready": False,
        "mqtt": {},
        "stream": {},
    }))
    assert legacy["automation_proxy_wss_event_clients"] == "0"
    assert legacy["automation_proxy_wss_control_clients"] == "0"
    assert legacy["automation_proxy_wss_response_queue_depth"] == "0"
    assert legacy["automation_proxy_event_coalesced_depth"] == "0"
    assert legacy["automation_proxy_wss_commands_accepted_total"] == "0"
    assert legacy["automation_proxy_wss_commands_rejected_total"] == "0"
    print("PASS WSS and event-stream Prometheus metrics")


if __name__ == "__main__":
    main()

# -*- coding: utf-8 -*-

"""Functional contract tests; deliberately no application-owned test classes."""

import datetime

from src.library._generic_system_contract_library.protocol_contracts import make_envelope, validate_envelope
from src.library._generic_system_contract_library.protocol_contracts import certificate_uri, identity_matches_certificate
from src.library._generic_system_contract_library.protocol_contracts import (
    control_event_filter,
    master_command_topic,
    proxy_ack_topic,
)


def _envelope(**kwargs):
    now = kwargs.get("now") or datetime.datetime.now(datetime.timezone.utc)
    return make_envelope(
        "runtime.status",
        {"service": "proxy", "instance_id": "proxy-01"},
        {"service": "master", "instance_id": "master-01"},
        site_id="site_main",
        principal={"subject": "00000000-0000-0000-0000-000000000001"},
        now=now,
        ttl_s=kwargs.get("ttl_s", 30.0),
        payload={},
    )


def _test_round_trip_and_strict_unknown_field():
    envelope = _envelope()
    assert validate_envelope(envelope, expected_site_id="site_main")["ok"]
    envelope["critical_future_field"] = True
    result = validate_envelope(envelope, expected_site_id="site_main")
    assert not result["ok"]
    assert result["code"] == "invalid_envelope"


def _test_expired_message_is_rejected():
    old = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(minutes=5)
    envelope = _envelope(now=old, ttl_s=1.0)
    result = validate_envelope(
        envelope,
        expected_site_id="site_main",
        now=datetime.datetime.now(datetime.timezone.utc),
    )
    assert not result["ok"]
    assert result["code"] == "expired"


def _test_certificate_identity_binding():
    identity = {"service": "worker", "instance_id": "fn-01"}
    uri = certificate_uri("site_main", "worker", "fn-01")
    assert identity_matches_certificate(identity, uri, site_id="site_main")["ok"]
    assert not identity_matches_certificate(
        identity, uri + "-other", site_id="site_main")["ok"]


def _test_topics_are_closed_and_deterministic():
    assert master_command_topic(
        "site_main", "master-01", "config.propose",
    ) == "automation/v1/site_main/master/master-01/commands/config/propose"
    assert proxy_ack_topic(
        "site_main", "proxy-01", "master-01", "abc-123",
    ) == "automation/v1/site_main/proxy/proxy-01/acks/master-01/abc-123"
    assert control_event_filter(
        "site_main", "master-01",
    ) == "automation/v1/site_main/events/+/master-01"
    try:
        master_command_topic("site_main", "master-01", "config/#")
    except ValueError:
        return
    raise AssertionError("unsafe MQTT command token was accepted")


def main():
    tests = (
        _test_round_trip_and_strict_unknown_field,
        _test_expired_message_is_rejected,
        _test_certificate_identity_binding,
        _test_topics_are_closed_and_deterministic,
    )
    for test in tests:
        test()
    print("PASS test_contracts ({} checks)".format(len(tests)))


if __name__ == "__main__":
    main()

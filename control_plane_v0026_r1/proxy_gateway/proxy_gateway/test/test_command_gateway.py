# -*- coding: utf-8 -*-

"""Functional tests for fixed command mapping and idempotency requirements."""

from src.core.command_gateway import create_command_gateway


def _test_fixed_domain_commands():
    sent = []

    def send_command(event_type, payload, **kwargs):
        sent.append({
            "event_type": event_type,
            "payload": payload,
            "kwargs": kwargs,
        })
        return {"ok": True, "accepted": True}

    gateway = create_command_gateway(
        site_id="site-main",
        authority_mode="node_local",
        mqtt={"send_command": send_command},
        wait_timeout_s=1.0,
    )
    principal = {
        "subject": "00000000-0000-0000-0000-000000000001",
        "permissions": ["config.propose", "config.rollback", "config.read"],
        "site_scopes": ["site-main"],
        "resource_scopes": [],
    }
    operation = {
        "op": "replace_field",
        "section": "rules",
        "entity_key": "rule:17",
        "json_pointer": "/enabled",
        "expected_value_hash": "a" * 64,
        "value": True,
    }
    proposal = {
        "target_node_id": "fn-01",
        "operation": "scope_operations",
        "operation_format": "config_scope_operations_v1",
        "scope": "node:fn-01",
        "base_authority_epoch": 7,
        "base_scope_revision": 184,
        "base_scope_fingerprint": "b" * 64,
        "reason": "validated relay test",
        "operations": [operation],
    }
    missing = gateway["propose_config"](principal, proposal)
    assert missing["code"] == "invalid_payload"
    proposal["idempotency_key"] = "proposal-01"
    proposed = gateway["propose_config"](principal, proposal)
    assert proposed["ok"] and sent[-1]["event_type"] == "config.propose"
    relayed = sent[-1]["payload"]
    assert relayed["operations"] == [operation]
    assert "base_revision" not in relayed and "changes" not in relayed
    scoped = gateway["get_config"](principal, scope="node:fn-01")
    assert scoped["ok"] and sent[-1]["payload"] == {"scope": "node:fn-01"}
    rejected = gateway["rollback_config"](principal, {
        "base_revision": 2,
        "target_revision": 1,
        "idempotency_key": "rollback-01",
    })
    assert rejected["code"] == "unsupported_operation_format"

    legacy = create_command_gateway(
        site_id="site-main",
        authority_mode="master_global",
        mqtt={"send_command": send_command},
        wait_timeout_s=1.0,
    )
    rolled = legacy["rollback_config"](principal, {
        "base_revision": 2,
        "target_revision": 1,
        "idempotency_key": "rollback-legacy-01",
    })
    assert rolled["ok"] and sent[-1]["event_type"] == "config.rollback"
    assert all("topic" not in item["payload"] for item in sent)


def main():
    _test_fixed_domain_commands()
    print("PASS test_command_gateway (1 scenario)")


if __name__ == "__main__":
    main()

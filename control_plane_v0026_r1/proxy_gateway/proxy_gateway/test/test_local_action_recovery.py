# -*- coding: utf-8 -*-

"""Fail-closed recovery for an abandoned local-mutation idempotency claim."""

from unittest import mock

from src.library.gateway_store_postgresql import claim_local_action


def _identity(value):
    return value


def main():
    connection = mock.MagicMock()
    pending = {
        "request_id": "request-old",
        "payload_fingerprint": "fingerprint",
        "state": "PENDING",
        "result": None,
    }
    recovered = dict(pending)
    recovered.update({
        "state": "FAILED",
        "result": {
            "ok": False,
            "code": "conflict",
            "recovery_required": True,
        },
    })
    connection.execute.return_value.fetchone.side_effect = (
        None,
        pending,
        recovered,
    )

    def transaction(operation, **_kwargs):
        return operation(connection)

    with mock.patch(
            "src.library.gateway_store_postgresql.jsonb", side_effect=_identity):
        result = claim_local_action({"transaction": transaction}, {
            "request_id": "request-new",
            "user_id": "00000000-0000-0000-0000-000000000001",
            "site_id": "site-main",
            "action": "user.create",
            "idempotency_key": "stable-key",
            "payload_fingerprint": "fingerprint",
        })
    assert result["ok"] and result["duplicate"]
    assert result["action_record"]["state"] == "FAILED"
    recovery_statement = connection.execute.call_args_list[2].args[0]
    assert "state='FAILED'" in recovery_statement
    assert "interval '15 minutes'" in recovery_statement
    print("PASS abandoned local mutation becomes fail-closed recovery-required")


if __name__ == "__main__":
    main()

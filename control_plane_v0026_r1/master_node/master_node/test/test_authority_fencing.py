# -*- coding: utf-8 -*-

"""Fresh-deployment authority fencing and stable rollout identity tests."""

from unittest import mock

from src.library.authority_store_postgresql import acquire_or_renew
from src.library.master_entity_store_postgresql import _rollout_identity


def _transaction_runner(connection):
    def transaction(operation, **_kwargs):
        return operation(connection)

    return transaction


def _test_fresh_database_uses_deployment_term_floor():
    initial_result = mock.MagicMock()
    initial_result.fetchone.return_value = {
        "authority_term": 0,
        "leader_instance_id": None,
        "lease_token": None,
        "lease_active": False,
    }
    updated_result = mock.MagicMock()
    updated_result.fetchone.return_value = {
        "authority_term": 1_750_000_000_000_000_001,
        "leader_instance_id": "master-01",
        "lease_token": "new-token",
    }
    connection = mock.MagicMock()
    connection.execute.side_effect = [None, initial_result, updated_result]
    database = {"transaction": _transaction_runner(connection)}

    result = acquire_or_renew(
        database,
        "site_main",
        "master-01",
        eligible=True,
        term_floor=1_750_000_000_000_000_000,
    )

    assert result["ok"] and result["role"] == "leader"
    assert result["authority"]["authority_term"] == 1_750_000_000_000_000_001
    update_parameters = connection.execute.call_args_list[2].args[1]
    assert update_parameters[0] == 1_750_000_000_000_000_001


def _test_rollout_identity_is_retry_stable_and_payload_unique():
    first = {
        "scope": "node:fn-01",
        "scope_revision": 1,
        "fingerprint": "a" * 64,
    }
    changed = dict(first, fingerprint="b" * 64)
    identity = _rollout_identity(7, first)
    assert identity == _rollout_identity(7, first)
    assert identity != _rollout_identity(7, changed)
    assert identity.endswith("a" * 64)


def main():
    _test_fresh_database_uses_deployment_term_floor()
    _test_rollout_identity_is_retry_stable_and_payload_unique()
    print("PASS authority fencing and payload-unique rollout identities")


if __name__ == "__main__":
    main()

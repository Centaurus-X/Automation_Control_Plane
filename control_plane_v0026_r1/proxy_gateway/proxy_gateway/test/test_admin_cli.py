# -*- coding: utf-8 -*-

"""Functional tests for idempotent administrator credential convergence."""

import copy

from unittest import mock

from admin_cli import ensure_existing_admin
from src.library.identity_store_postgresql import replace_password_for_role
from src.library.token_security import hash_password, verify_password


PASSWORD = "correct horse battery staple"
OTHER_PASSWORD = "different battery staple value"
PEPPER = "bootstrap-test-pepper"
USER_ID = "00000000-0000-0000-0000-000000000001"


def _credentials(password, **kwargs):
    return {
        "user_id": USER_ID,
        "username": "admin",
        "enabled": bool(kwargs.get("enabled", True)),
        "locked_until": kwargs.get("locked_until"),
        "must_change": bool(kwargs.get("must_change", False)),
        "password_hash": hash_password(
            password, PEPPER, minimum_length=14,
            time_cost=2, memory_cost_kib=32768, parallelism=1,
        ),
    }


def _identity(credentials, **kwargs):
    state = {"updates": []}
    role_allowed = bool(kwargs.get("role_allowed", True))

    def get_credentials(unused_username):
        return copy.deepcopy(credentials)

    def has_role(request):
        state["role_request"] = copy.deepcopy(request)
        return role_allowed

    def replace_password(request):
        state["updates"].append(copy.deepcopy(request))
        return {"ok": True, "user_id": request["user_id"]}

    capabilities = {
        "get_credentials": get_credentials,
        "has_role": has_role,
        "replace_password_for_role": replace_password,
    }
    return capabilities, state


def _ensure(identity, password=PASSWORD):
    return ensure_existing_admin(
        identity, "admin", password, PEPPER, "site-main", 14)


def _test_matching_credential_is_noop():
    identity, state = _identity(_credentials(PASSWORD))
    result = _ensure(identity)
    assert result == {"ok": True, "changed": False}
    assert state["updates"] == []
    assert state["role_request"] == {
        "user_id": USER_ID,
        "role_name": "platform_admin",
        "site_id": "site-main",
    }


def _test_mismatch_is_rotated_without_exposing_password():
    identity, state = _identity(_credentials(OTHER_PASSWORD))
    result = _ensure(identity)
    assert result == {"ok": True, "changed": True}
    assert len(state["updates"]) == 1
    update = state["updates"][0]
    assert PASSWORD not in update["password_hash"]
    assert verify_password(PASSWORD, update["password_hash"], PEPPER)
    assert update["role_name"] == "platform_admin"
    assert update["site_id"] == "site-main"


def _test_wrong_role_fails_closed():
    identity, state = _identity(
        _credentials(PASSWORD), role_allowed=False)
    result = _ensure(identity)
    assert not result["ok"]
    assert result["code"] == "authorization_denied"
    assert state["updates"] == []


def _test_disabled_existing_user_fails_closed():
    identity, state = _identity(_credentials(PASSWORD, enabled=False))
    result = _ensure(identity)
    assert not result["ok"]
    assert result["code"] == "conflict"
    assert state["updates"] == []


def _test_security_event_parameters_have_explicit_sql_types():
    connection = mock.MagicMock()
    connection.execute.return_value.fetchone.return_value = {
        "user_id": USER_ID,
    }

    def transaction(operation, **_kwargs):
        return operation(connection)

    result = replace_password_for_role({"transaction": transaction}, {
        "user_id": USER_ID,
        "role_name": "platform_admin",
        "site_id": "site-main",
        "password_hash": "argon2id-encoded-test-value",
    })
    assert result["ok"]
    statement, parameters = connection.execute.call_args.args
    assert "'site_id',%s::text" in statement
    assert "'role_name',%s::text" in statement
    assert parameters[2:] == ("site-main", "platform_admin")


def main():
    tests = (
        _test_matching_credential_is_noop,
        _test_mismatch_is_rotated_without_exposing_password,
        _test_wrong_role_fails_closed,
        _test_disabled_existing_user_fails_closed,
        _test_security_event_parameters_have_explicit_sql_types,
    )
    for test in tests:
        test()
    print("PASS test_admin_cli ({} checks)".format(len(tests)))


if __name__ == "__main__":
    main()

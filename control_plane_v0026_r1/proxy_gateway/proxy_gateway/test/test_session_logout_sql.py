# -*- coding: utf-8 -*-

"""Session SQL regression checks for PostgreSQL/Psycopg."""

import datetime

from unittest import mock

from src.library.identity_store_postgresql import revoke_session, rotate_session


def _test_token_and_session_parameters_have_explicit_sql_types():
    connection = mock.MagicMock()
    connection.execute.return_value.fetchone.return_value = {"session_id": "session-01"}

    def transaction(operation, **_kwargs):
        return operation(connection)

    database = {"transaction": transaction}
    assert revoke_session(database, {
        "session_id": None,
        "token_hash": "a" * 64,
        "reason": "logout",
    }) is True
    statement, parameters = connection.execute.call_args.args
    assert "%s::uuid IS NOT NULL" in statement
    assert "session_id=%s::uuid" in statement
    assert "%s::text IS NOT NULL" in statement
    assert "token_hash=%s::text" in statement
    assert parameters == ("logout", None, None, "a" * 64, "a" * 64)


def _test_rotation_caps_sliding_expiry_at_the_absolute_deadline():
    connection = mock.MagicMock()
    now = datetime.datetime.now(datetime.timezone.utc)
    absolute = now + datetime.timedelta(minutes=2)
    connection.execute.return_value.fetchone.side_effect = (
        {
            "session_id": "00000000-0000-0000-0000-000000000001",
            "user_id": "00000000-0000-0000-0000-000000000002",
            "absolute_expires_at": absolute,
        },
        {
            "session_id": "00000000-0000-0000-0000-000000000003",
            "expires_at": absolute,
        },
        None,
    )

    def transaction(operation, **_kwargs):
        return operation(connection)

    result = rotate_session({"transaction": transaction}, {
        "old_token_hash": "old",
        "token_hash": "new",
        "csrf_hash": "csrf",
        "client_binding_hash": "binding",
        "expires_at": now + datetime.timedelta(minutes=30),
    })
    assert result["ok"]
    insert_parameters = connection.execute.call_args_list[1].args[1]
    assert insert_parameters[5] == absolute
    assert insert_parameters[6] == absolute


def main():
    _test_token_and_session_parameters_have_explicit_sql_types()
    _test_rotation_caps_sliding_expiry_at_the_absolute_deadline()
    print("PASS test_session_logout_sql (logout typing, rotation deadline)")


if __name__ == "__main__":
    main()

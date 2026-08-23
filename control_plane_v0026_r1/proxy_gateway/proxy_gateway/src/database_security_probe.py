# -*- coding: utf-8 -*-

"""Real PostgreSQL probe for role delegation and current API-token scopes."""

import uuid

from functools import partial

from src.core.access_policy import authorize, authorize_role_grant
from src.core.auth_management import authenticate_api_token
from src.library.gateway_data_store import create_gateway_data_store
from src.library.postgresql import env_database_options
from src.library.token_security import token_hash


PROBE_USER_ID = "ffffffff-ffff-4fff-8fff-000000000003"
PROBE_USERNAME = "__v0003_security_probe__"
PROBE_PEPPER = "v0003-database-security-probe"


def _cleanup(connection):
    connection.execute(
        "DELETE FROM proxy_gateway.resource_scopes "
        "WHERE subject_type='USER' AND subject_id=%s",
        (PROBE_USER_ID,),
    )
    connection.execute(
        "DELETE FROM proxy_gateway.users WHERE user_id=%s OR username_normalized=%s",
        (PROBE_USER_ID, PROBE_USERNAME),
    )
    return True


def _insert_fixture(connection, scope_id):
    _cleanup(connection)
    connection.execute(
        "INSERT INTO proxy_gateway.users("
        "user_id,username,username_normalized,display_name,enabled) "
        "VALUES (%s,%s,%s,'v0003 security probe',true)",
        (PROBE_USER_ID, PROBE_USERNAME, PROBE_USERNAME),
    )
    role = connection.execute(
        "SELECT role_id FROM proxy_gateway.roles WHERE role_name='operator'"
    ).fetchone()
    if not role:
        raise RuntimeError("operator role is missing")
    connection.execute(
        "INSERT INTO proxy_gateway.user_roles(user_id,role_id,site_id,granted_by) "
        "VALUES (%s,%s,'site-main',%s)",
        (PROBE_USER_ID, role["role_id"], PROBE_USER_ID),
    )
    connection.execute(
        "INSERT INTO proxy_gateway.resource_scopes("
        "scope_id,subject_type,subject_id,site_id,resource_type,resource_pattern) "
        "VALUES (%s,'USER',%s,'site-main','controller','probe-allowed-*')",
        (scope_id, PROBE_USER_ID),
    )
    return True


def _revoke_roles(connection):
    connection.execute(
        "DELETE FROM proxy_gateway.user_roles WHERE user_id=%s",
        (PROBE_USER_ID,),
    )
    return True


def _role(roles, name):
    return next(
        (item for item in roles if str(item.get("role_name") or "") == name),
        None,
    )


def _check_role_catalog(identity):
    roles = identity["list_roles"]()
    site_admin = _role(roles, "site_admin")
    platform_admin = _role(roles, "platform_admin")
    operator = _role(roles, "operator")
    if not all((site_admin, platform_admin, operator)):
        raise RuntimeError("required built-in roles are missing")
    site_principal = {
        "subject": PROBE_USER_ID,
        "permissions": site_admin["permissions"],
        "site_scopes": ["site-main"],
        "resource_scopes": [],
    }
    denied = authorize_role_grant(
        site_principal, "site-main", platform_admin,
        managed_site_id="site-main",
    )
    if denied.get("ok"):
        raise RuntimeError("site_admin could grant platform_admin")
    platform_principal = {
        "subject": PROBE_USER_ID,
        "permissions": platform_admin["permissions"],
        "site_scopes": ["site-main"],
        "resource_scopes": [],
    }
    if not authorize_role_grant(
            platform_principal, "site-main", operator,
            managed_site_id="site-main").get("ok"):
        raise RuntimeError("platform_admin could not grant operator")


def _check_api_token(store, raw_token):
    context = {
        "identity": store["identity"],
        "token_pepper": PROBE_PEPPER,
    }
    authenticated = authenticate_api_token(context, raw_token)
    if not authenticated.get("ok"):
        raise RuntimeError("probe API token could not be authenticated")
    principal = authenticated["principal"]
    if principal["site_scopes"] != ["site-main"]:
        raise RuntimeError("token site scopes were not intersected")
    if "node.drain" in principal["permissions"]:
        raise RuntimeError("token retained a permission absent from the current role")
    allowed = authorize(
        principal, "actuator.request", site_id="site-main",
        resource_type="controller", resource_id="probe-allowed-1",
    )
    denied = authorize(
        principal, "actuator.request", site_id="site-main",
        resource_type="controller", resource_id="probe-denied-1",
    )
    if not allowed.get("ok") or denied.get("ok"):
        raise RuntimeError("token resource scope was not preserved")


def main():
    store = create_gateway_data_store(env_database_options())
    scope_id = str(uuid.uuid4())
    raw_token = "v0003-probe-" + uuid.uuid4().hex
    try:
        store["database"]["transaction"](
            partial(_insert_fixture, scope_id=scope_id))
        created = store["identity"]["create_api_token"]({
            "user_id": PROBE_USER_ID,
            "token_prefix": "v0003-probe",
            "token_hash": token_hash(raw_token, PROBE_PEPPER),
            "name": "v0003 database security probe",
            "permissions": ["actuator.request", "node.read", "node.drain"],
            "site_scopes": ["site-main", "site_other"],
        })
        if not created.get("ok"):
            raise RuntimeError("probe API token could not be stored")
        _check_role_catalog(store["identity"])
        _check_api_token(store, raw_token)
        store["database"]["transaction"](_revoke_roles)
        principal = authenticate_api_token({
            "identity": store["identity"], "token_pepper": PROBE_PEPPER,
        }, raw_token)["principal"]
        if principal["permissions"] or principal["site_scopes"]:
            raise RuntimeError("role or site revocation did not constrain the token")
    finally:
        try:
            store["database"]["transaction"](_cleanup)
        finally:
            store["close"]()
    print("Proxy PostgreSQL role and API-token security probe passed.")


if __name__ == "__main__":
    raise SystemExit(main())

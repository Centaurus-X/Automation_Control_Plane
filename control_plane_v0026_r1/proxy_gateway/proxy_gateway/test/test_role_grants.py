# -*- coding: utf-8 -*-

"""Negative and positive tests for bounded role delegation."""

from src.core.access_policy import authorize_role_grant


ALL_PERMISSIONS = {
    "actuator.request", "audit.read", "config.propose", "config.read",
    "config.rollback", "node.admit", "node.drain", "node.read",
    "operation.read", "role.manage", "terminal.execute", "user.manage",
}


def _principal(permissions, sites=None):
    return {
        "subject": "00000000-0000-0000-0000-000000000001",
        "permissions": sorted(permissions),
        "site_scopes": list(sites or ["site-main"]),
        "resource_scopes": [],
    }


def _role(name, permissions):
    return {"role_name": name, "permissions": sorted(permissions)}


def _test_site_admin_cannot_grant_platform_admin():
    principal = _principal({
        "node.read", "node.drain", "node.admit", "config.read",
        "operation.read", "user.manage", "role.manage", "terminal.execute",
    })
    result = authorize_role_grant(
        principal, "site-main", _role("platform_admin", ALL_PERMISSIONS),
        managed_site_id="site-main",
    )
    assert result["code"] == "authorization_denied"


def _test_security_admin_cannot_grant_actuator_role():
    principal = _principal({
        "node.read", "operation.read", "user.manage", "role.manage", "audit.read",
    })
    result = authorize_role_grant(
        principal, "site-main",
        _role("operator", {"node.read", "actuator.request"}),
        managed_site_id="site-main",
    )
    assert result["code"] == "authorization_denied"


def _test_platform_admin_can_grant_known_roles():
    principal = _principal(ALL_PERMISSIONS, sites=["*"])
    for role in (
            _role("viewer", {"node.read", "config.read", "operation.read"}),
            _role("platform_admin", ALL_PERMISSIONS)):
        result = authorize_role_grant(
            principal, "site-main", role, managed_site_id="site-main")
        assert result["ok"]


def _test_unknown_role_is_denied():
    result = authorize_role_grant(
        _principal(ALL_PERMISSIONS), "site-main", None,
        managed_site_id="site-main",
    )
    assert result["code"] == "not_found"


def _test_foreign_site_is_denied_even_with_wildcard_scope():
    result = authorize_role_grant(
        _principal(ALL_PERMISSIONS, sites=["*"]), "site_other",
        _role("viewer", {"node.read"}), managed_site_id="site-main",
    )
    assert result["code"] == "authorization_denied"


def _test_exact_permission_set_is_allowed():
    permissions = {"user.manage", "node.read"}
    result = authorize_role_grant(
        _principal(permissions), "site-main", _role("bounded_admin", permissions),
        managed_site_id="site-main",
    )
    assert result["ok"]


def main():
    tests = (
        _test_site_admin_cannot_grant_platform_admin,
        _test_security_admin_cannot_grant_actuator_role,
        _test_platform_admin_can_grant_known_roles,
        _test_unknown_role_is_denied,
        _test_foreign_site_is_denied_even_with_wildcard_scope,
        _test_exact_permission_set_is_allowed,
    )
    for test in tests:
        test()
    print("PASS test_role_grants ({} checks)".format(len(tests)))


if __name__ == "__main__":
    main()

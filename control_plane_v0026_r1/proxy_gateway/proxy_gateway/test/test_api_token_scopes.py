# -*- coding: utf-8 -*-

"""Regression tests for API-token permission, site and resource ceilings."""

import copy

from src.core.access_policy import authorize
from src.core.auth_management import authenticate_api_token


USER_ID = "00000000-0000-0000-0000-000000000001"


def _context(record, current):
    state = {"record": copy.deepcopy(record), "current": copy.deepcopy(current)}

    def get_api_token(_digest):
        return copy.deepcopy(state["record"])

    def effective_access(_user_id):
        return copy.deepcopy(state["current"])

    return {
        "identity": {
            "get_api_token": get_api_token,
            "effective_access": effective_access,
        },
        "token_pepper": "test-token-pepper",
    }, state


def _record(**kwargs):
    value = {
        "user_id": USER_ID,
        "username": "bounded-user",
        "permissions": ["actuator.request", "node.read", "node.drain"],
        "site_scopes": ["site-main", "site_other"],
    }
    value.update(kwargs)
    return value


def _access(**kwargs):
    value = {
        "permissions": ["actuator.request", "node.read"],
        "site_scopes": ["site-main"],
        "resource_scopes": [{
            "site_id": "site-main",
            "resource_type": "controller",
            "resource_pattern": "6267*",
        }],
    }
    value.update(kwargs)
    return value


def _test_resource_scope_is_preserved():
    context, _state = _context(_record(), _access())
    result = authenticate_api_token(context, "raw-token")
    principal = result["principal"]
    assert principal["resource_scopes"] == _access()["resource_scopes"]
    assert authorize(
        principal, "actuator.request", site_id="site-main",
        resource_type="controller", resource_id="62670001",
    )["ok"]
    assert not authorize(
        principal, "actuator.request", site_id="site-main",
        resource_type="controller", resource_id="other-controller",
    )["ok"]


def _test_token_is_intersected_with_current_access():
    context, _state = _context(_record(), _access())
    principal = authenticate_api_token(context, "raw-token")["principal"]
    assert principal["permissions"] == ["actuator.request", "node.read"]
    assert principal["site_scopes"] == ["site-main"]


def _test_role_and_site_revocation_apply_immediately():
    context, state = _context(_record(), _access())
    assert authenticate_api_token(context, "raw-token")["principal"]["permissions"]
    state["current"]["permissions"] = ["node.read"]
    state["current"]["site_scopes"] = []
    principal = authenticate_api_token(context, "raw-token")["principal"]
    assert principal["permissions"] == ["node.read"]
    assert principal["site_scopes"] == []


def _test_token_record_without_resource_scopes_is_not_unrestricted():
    record = _record()
    record.pop("resource_scopes", None)
    context, _state = _context(record, _access())
    principal = authenticate_api_token(context, "raw-token")["principal"]
    denied = authorize(
        principal, "actuator.request", site_id="site-main",
        resource_type="controller", resource_id="outside-scope",
    )
    assert denied["code"] == "authorization_denied"


def _test_site_wildcards_intersect_as_sets():
    context, state = _context(
        _record(site_scopes=["*"]), _access(site_scopes=["site-main", "site_two"]))
    principal = authenticate_api_token(context, "raw-token")["principal"]
    assert principal["site_scopes"] == ["site-main", "site_two"]
    state["record"]["site_scopes"] = ["site-main"]
    state["current"]["site_scopes"] = ["*"]
    principal = authenticate_api_token(context, "raw-token")["principal"]
    assert principal["site_scopes"] == ["site-main"]


def main():
    tests = (
        _test_resource_scope_is_preserved,
        _test_token_is_intersected_with_current_access,
        _test_role_and_site_revocation_apply_immediately,
        _test_token_record_without_resource_scopes_is_not_unrestricted,
        _test_site_wildcards_intersect_as_sets,
    )
    for test in tests:
        test()
    print("PASS test_api_token_scopes ({} checks)".format(len(tests)))


if __name__ == "__main__":
    main()

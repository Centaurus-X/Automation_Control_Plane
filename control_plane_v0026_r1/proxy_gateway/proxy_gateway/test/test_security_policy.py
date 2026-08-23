# -*- coding: utf-8 -*-

"""Functional tests for tokens, RBAC, site and resource isolation."""

from src.core.access_policy import (
    CONFIG_ALLOWED_SECTIONS,
    authorize,
    authorize_config_operations,
)
from src.library.token_security import generate_token, token_hash, validate_password


def _principal():
    return {
        "subject": "00000000-0000-0000-0000-000000000001",
        "permissions": ["config.propose", "actuator.request", "node.read"],
        "site_scopes": ["site-main"],
        "resource_scopes": [{
            "site_id": "site-main",
            "resource_type": "controller",
            "resource_pattern": "6267*",
        }],
    }


def _test_token_hashing_and_password_policy():
    token = generate_token()
    assert token not in token_hash(token, "pepper")
    assert token_hash(token, "pepper") == token_hash(token, "pepper")
    assert token_hash(token, "pepper") != token_hash(token, "other-pepper")
    assert not validate_password("short", minimum_length=14)["ok"]
    assert validate_password("correct horse battery staple", minimum_length=14)["ok"]


def _test_default_deny_and_scopes():
    principal = _principal()
    assert authorize(principal, "node.read", site_id="site-main")["ok"]
    assert not authorize(principal, "node.drain", site_id="site-main")["ok"]
    assert not authorize(principal, "node.read", site_id="site_other")["ok"]
    assert authorize(
        principal, "actuator.request", site_id="site-main",
        resource_type="controller", resource_id="62671112-test",
    )["ok"]
    assert not authorize(
        principal, "actuator.request", site_id="site-main",
        resource_type="controller", resource_id="different",
    )["ok"]


def _operation(section):
    return {
        "op": "replace_field",
        "section": section,
        "entity_key": "entity:1",
        "json_pointer": "/enabled",
        "expected_value_hash": "a" * 64,
        "value": True,
    }


def _test_config_scope_policy():
    principal = _principal()
    for section in CONFIG_ALLOWED_SECTIONS:
        result = authorize_config_operations(
            principal, "site-main", [_operation(section)])
        assert result["ok"], (section, result)
    denied = authorize_config_operations(
        principal, "site-main", [_operation("secrets")])
    assert denied["code"] == "authorization_denied"
    invalid = _operation("rules")
    invalid["path"] = "rules.0.enabled"
    rejected = authorize_config_operations(
        principal, "site-main", [invalid])
    assert rejected["code"] == "unsupported_operation_format"


def main():
    tests = (
        _test_token_hashing_and_password_policy,
        _test_default_deny_and_scopes,
        _test_config_scope_policy,
    )
    for test in tests:
        test()
    print("PASS test_security_policy ({} checks)".format(len(tests)))


if __name__ == "__main__":
    main()

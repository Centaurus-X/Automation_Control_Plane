# -*- coding: utf-8 -*-

"""Tests for deployable, exact and fail-closed browser origins."""

import inspect
import os

from gateway_main import _serve_proxy
from src.system.bootstrap_system_interface import (
    _origin_allowlist,
    _validate_listener_config,
)


def _with_environment(origin_value, test_mode, callback):
    previous_origin = os.environ.get("PROXY_ORIGIN_ALLOWLIST")
    previous_mode = os.environ.get("CONTROL_PLANE_TEST_MODE")
    try:
        if origin_value is None:
            os.environ.pop("PROXY_ORIGIN_ALLOWLIST", None)
        else:
            os.environ["PROXY_ORIGIN_ALLOWLIST"] = origin_value
        if test_mode is None:
            os.environ.pop("CONTROL_PLANE_TEST_MODE", None)
        else:
            os.environ["CONTROL_PLANE_TEST_MODE"] = test_mode
        return callback()
    finally:
        if previous_origin is None:
            os.environ.pop("PROXY_ORIGIN_ALLOWLIST", None)
        else:
            os.environ["PROXY_ORIGIN_ALLOWLIST"] = previous_origin
        if previous_mode is None:
            os.environ.pop("CONTROL_PLANE_TEST_MODE", None)
        else:
            os.environ["CONTROL_PLANE_TEST_MODE"] = previous_mode


def _expect_value(origin_value, test_mode, expected):
    def evaluate():
        assert _origin_allowlist(["https://base.invalid"]) == expected
    _with_environment(origin_value, test_mode, evaluate)


def _expect_rejected(origin_value, test_mode):
    def evaluate():
        try:
            _origin_allowlist(["https://base.invalid"])
        except ValueError:
            return
        raise AssertionError("invalid origin allowlist was accepted")
    _with_environment(origin_value, test_mode, evaluate)


def _test_base_config_is_used_without_environment_override():
    _expect_value(None, None, ["https://base.invalid"])


def _test_csv_is_trimmed_normalized_and_deduplicated():
    _expect_value(
        " https://CONTROL.EXAMPLE:443,https://control.example ", None,
        ["https://control.example"],
    )


def _test_empty_and_malformed_values_are_rejected():
    for value in ("", "https://valid.example,,https://other.example", "https://host/path"):
        _expect_rejected(value, None)


def _test_http_origin_is_always_rejected_for_wss():
    _expect_rejected("http://127.0.0.1:8443", None)
    _expect_rejected("http://127.0.0.1:8443", "1")


def _listener_config(wss_port=8443, https_port=8444):
    origins = ["https://control.example"]
    return {
        "wss": {
            "enabled": True,
            "host": "0.0.0.0",
            "port": wss_port,
            "origin_allowlist": origins,
            "tls": {"certfile": "/cert", "keyfile": "/key"},
        },
        "https": {
            "enabled": True,
            "host": "0.0.0.0",
            "port": https_port,
            "websocket_enabled": False,
            "tls_enabled": True,
            "cert_file": "/cert",
            "key_file": "/key",
            "origin_allowlist": origins,
        },
    }


def _test_listener_ports_and_http_only_contract():
    assert _validate_listener_config(_listener_config())
    for config in (
            _listener_config(8443, 8443),
            _listener_config(0, 8444)):
        try:
            _validate_listener_config(config)
        except ValueError:
            continue
        raise AssertionError("invalid listener configuration was accepted")
    config = _listener_config()
    config["https"]["websocket_enabled"] = True
    try:
        _validate_listener_config(config)
    except ValueError:
        return
    raise AssertionError("Uvicorn WebSocket support was accepted")


def _test_uvicorn_is_http_only_and_does_not_trust_forwarded_headers():
    source = inspect.getsource(_serve_proxy)
    compact = "".join(source.split())
    assert 'ws="none"' in compact
    assert "proxy_headers=False" in compact


def main():
    tests = (
        _test_base_config_is_used_without_environment_override,
        _test_csv_is_trimmed_normalized_and_deduplicated,
        _test_empty_and_malformed_values_are_rejected,
        _test_http_origin_is_always_rejected_for_wss,
        _test_listener_ports_and_http_only_contract,
        _test_uvicorn_is_http_only_and_does_not_trust_forwarded_headers,
    )
    for test in tests:
        test()
    print("PASS test_gateway_config ({} checks)".format(len(tests)))


if __name__ == "__main__":
    main()

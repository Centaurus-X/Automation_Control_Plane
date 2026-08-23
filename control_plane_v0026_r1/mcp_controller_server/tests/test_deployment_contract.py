# -*- coding: utf-8 -*-

"""Deployment secret-path and non-root staging command tests."""

import importlib
import os
import sys
import tempfile


def _tool_modules():
    tools_root = os.path.join(os.path.dirname(os.path.dirname(__file__)), "tools")
    sys.path.insert(0, tools_root)
    try:
        checker = importlib.import_module("check_runtime_secrets")
        staging = importlib.import_module("prepare_runtime_secrets")
        return checker, staging
    finally:
        sys.path.remove(tools_root)


def test_runtime_secret_permissions_are_fail_closed():
    checker, _staging = _tool_modules()
    with tempfile.TemporaryDirectory() as directory:
        paths = {}
        for name in checker.DEFAULTS:
            path = os.path.join(directory, name.lower())
            with open(path, "wb") as handle:
                handle.write(b"test")
            os.chmod(path, 0o600 if name in checker.PRIVATE_NAMES else 0o644)
            paths[name] = path
        assert checker.validate_secret_paths(paths)
        os.chmod(paths["MCP_AUTH_SOURCE"], 0o644)
        try:
            checker.validate_secret_paths(paths)
        except ValueError:
            pass
        else:
            raise AssertionError("broad authentication secret mode was accepted")


def test_secret_staging_command_has_no_network_and_exact_targets():
    _checker, staging = _tool_modules()
    paths = {
        name: "/secure/" + str(index)
        for index, name in enumerate(sorted(staging.TARGETS))
    }
    command = staging._docker_command(paths, "/release/runtime-secrets")
    joined = " ".join(command)
    assert "--network none" in joined
    assert "--read-only" in joined
    assert "--cap-drop ALL" in joined
    assert "chown 10001:10001" in joined
    for filename in staging.TARGETS.values():
        assert "/output/" + filename in joined

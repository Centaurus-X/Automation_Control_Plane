# -*- coding: utf-8 -*-

"""Configuration, authentication and rate-limit tests."""

import copy
import os
import tempfile

from mcp_controller_server.auth import authenticate_bearer, load_auth_state
from mcp_controller_server.config import validate_config
from tests.support import make_test_config, write_auth_file


def test_configuration_fails_closed():
    with tempfile.TemporaryDirectory() as directory:
        config = make_test_config(directory)
        assert validate_config(config, check_files=False)["ok"]
        insecure = copy.deepcopy(config)
        insecure["mqtt"]["allow_insecure_transport"] = False
        assert not validate_config(insecure, check_files=False)["ok"]
        unknown = copy.deepcopy(config)
        unknown["mqtt"]["unexpected"] = True
        assert not validate_config(unknown, check_files=False)["ok"]
        direct_role = copy.deepcopy(config)
        direct_role["mqtt"]["source_service"] = "worker"
        assert not validate_config(direct_role, check_files=False)["ok"]


def test_bearer_authentication_and_rate_limit():
    with tempfile.TemporaryDirectory() as directory:
        path = os.path.join(directory, "auth.json")
        token = write_auth_file(path)
        state = load_auth_state(path, 2)
        first = authenticate_bearer(state, "Bearer " + token, now=100.0)
        second = authenticate_bearer(state, "Bearer " + token, now=101.0)
        limited = authenticate_bearer(state, "Bearer " + token, now=102.0)
        assert first["ok"] and second["ok"]
        assert not limited["ok"] and limited["code"] == "rate_limited"
        assert authenticate_bearer(state, "Bearer wrong-token-value", now=200.0)["ok"] is False
        assert authenticate_bearer(state, None, now=200.0)["code"] == "authentication_required"

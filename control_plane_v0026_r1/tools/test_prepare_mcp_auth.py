#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Regression tests for external MCP authentication preparation."""

import json
import os
import subprocess
import sys
import tempfile


def _run(tool, credentials, output, key):
    return subprocess.run(
        (
            sys.executable,
            tool,
            "--credentials-file", credentials,
            "--credential-key", key,
            "--output", output,
            "--site-id", "site-main",
        ),
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
        check=False,
    )


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    tool = os.path.join(root, "tools", "prepare_mcp_auth.py")
    with tempfile.TemporaryDirectory() as directory:
        credentials = os.path.join(directory, "credentials.json")
        output = os.path.join(directory, "mcp_auth.json")
        token = "A" * 48
        with open(credentials, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"mcp_controller": {"local_agent": {"token": token}}}, handle)
            handle.write("\n")
        os.chmod(credentials, 0o600)
        completed = _run(
            tool,
            credentials,
            output,
            "mcp_controller.local_agent.token",
        )
        assert completed.returncode == 0, completed.stderr
        assert os.stat(output).st_mode & 0o077 == 0
        with open(output, "r", encoding="utf-8") as handle:
            document = json.load(handle)
        client = document["clients"][0]
        assert client["token"] == token
        assert client["principal"]["site_scopes"] == ["site-main"]
        assert "config.propose" in client["principal"]["permissions"]
        failed = _run(tool, credentials, output, "missing.token")
        assert failed.returncode != 0
        assert token not in failed.stdout + failed.stderr
        placeholder = "CHANGE_ME_MCP_BEARER_TOKEN_AT_LEAST_32_CHARACTERS"
        with open(credentials, "w", encoding="utf-8", newline="\n") as handle:
            json.dump({"mcp_controller": {"local_agent": {"token": placeholder}}}, handle)
            handle.write("\n")
        rejected = _run(
            tool,
            credentials,
            output,
            "mcp_controller.local_agent.token",
        )
        assert rejected.returncode != 0
        assert "placeholder" in rejected.stderr.lower()
        assert placeholder not in rejected.stdout + rejected.stderr
    print("PASS MCP authentication preparation tests")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

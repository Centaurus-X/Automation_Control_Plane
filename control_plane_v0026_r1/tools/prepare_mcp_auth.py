#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Create the external MCP authentication document from protected credentials."""

import argparse
import json
import os
import re
import tempfile

import credential_reader


IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
DEFAULT_PERMISSIONS = (
    "node.read",
    "config.read",
    "operation.read",
    "config.propose",
    "actuator.request",
    "node.drain",
)


def _arguments():
    parser = argparse.ArgumentParser(description="Prepare MCP Bearer authentication")
    parser.add_argument("--credentials-file", required=True)
    parser.add_argument("--credential-key", action="append", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--client-id", default="automation-orchestrator")
    parser.add_argument("--subject", default="mcp:automation-orchestrator")
    parser.add_argument("--site-id", default="site-main")
    parser.add_argument("--permission", action="append")
    return parser.parse_args()


def _validate_identifier(value, label):
    if not IDENTIFIER_PATTERN.fullmatch(str(value or "")):
        raise ValueError(label + " is invalid")
    return str(value)


def _validate_token(value):
    if not isinstance(value, str) or len(value) < 32 or len(value) > 4096:
        raise ValueError("MCP bearer token must contain between 32 and 4096 characters")
    if value != value.strip() or any(ord(character) < 33 for character in value):
        raise ValueError("MCP bearer token contains whitespace or control characters")
    if value.upper().startswith("CHANGE_ME_"):
        raise ValueError("MCP bearer token still contains the release placeholder")
    return value


def _atomic_private_json(path, document):
    absolute = os.path.abspath(path)
    directory = os.path.dirname(absolute)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    descriptor, temporary = tempfile.mkstemp(prefix=".mcp-auth-", dir=directory)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(document, handle, ensure_ascii=False, sort_keys=True, indent=2)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, absolute)
        os.chmod(absolute, 0o600)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def main():
    arguments = _arguments()
    token = _validate_token(credential_reader.load_credential(
        arguments.credentials_file,
        tuple(arguments.credential_key),
    ))
    permissions = tuple(arguments.permission or DEFAULT_PERMISSIONS)
    if not permissions or len(set(permissions)) != len(permissions):
        raise ValueError("MCP permissions must be a non-empty unique sequence")
    for permission in permissions:
        _validate_identifier(permission, "MCP permission")
    document = {
        "schema_version": 1,
        "clients": [{
            "client_id": _validate_identifier(arguments.client_id, "MCP client_id"),
            "token": token,
            "principal": {
                "subject": _validate_identifier(arguments.subject, "MCP subject"),
                "permissions": list(permissions),
                "site_scopes": [
                    _validate_identifier(arguments.site_id, "MCP site_id"),
                ],
                "resource_scopes": [],
            },
        }],
    }
    _atomic_private_json(arguments.output, document)
    print("PASS external MCP authentication document prepared")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print("MCP authentication preparation failed: " + str(exc), file=__import__("sys").stderr)
        raise SystemExit(65)

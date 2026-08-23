#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Generate a server authentication document and separate client token file."""

import argparse
import json
import os
import secrets
import tempfile


def _arguments():
    parser = argparse.ArgumentParser(description="Generate MCP bearer credentials")
    parser.add_argument("--auth-file", required=True)
    parser.add_argument("--token-file", required=True)
    parser.add_argument("--client-id", default="local-agent")
    parser.add_argument("--subject", default="mcp:local-agent")
    parser.add_argument("--site-id", default="site-main")
    parser.add_argument(
        "--permissions", nargs="+",
        default=["node.read", "config.read", "operation.read"],
    )
    return parser.parse_args()


def _atomic_private_text(path, text):
    absolute = os.path.abspath(path)
    os.makedirs(os.path.dirname(absolute), mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".mcp-auth-", dir=os.path.dirname(absolute))
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
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
    token = secrets.token_urlsafe(48)
    document = {
        "schema_version": 1,
        "clients": [{
            "client_id": arguments.client_id,
            "token": token,
            "principal": {
                "subject": arguments.subject,
                "permissions": list(arguments.permissions),
                "site_scopes": [arguments.site_id],
                "resource_scopes": [],
            },
        }],
    }
    serialized = json.dumps(document, ensure_ascii=False, sort_keys=True, indent=2) + "\n"
    _atomic_private_text(arguments.auth_file, serialized)
    _atomic_private_text(arguments.token_file, token + "\n")
    print("Authentication files created with mode 0600")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

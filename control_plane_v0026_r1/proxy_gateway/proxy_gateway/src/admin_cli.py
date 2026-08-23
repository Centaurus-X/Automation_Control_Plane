# -*- coding: utf-8 -*-

# proxy_gateway/src/admin_cli.py

"""Controlled local administration CLI for first-user bootstrap."""

import argparse
import copy
import getpass
import os

from src.core.auth_management import create_auth_management
from src.library.gateway_data_store import create_gateway_data_store
from src.library.postgresql import env_database_options, read_secret
from src.library.token_security import hash_password, verify_password
from src.system.bootstrap_system_interface import load_gateway_config


def _password(arguments):
    if arguments.password_file:
        return read_secret(arguments.password_file)
    return getpass.getpass("Initial administrator password: ")


def ensure_existing_admin(
        identity, username, password, pepper, site_id, minimum_length):
    """Converge one existing enabled platform administrator credential."""
    credentials = identity["get_credentials"](username)
    if not credentials or not credentials.get("enabled"):
        return {
            "ok": False,
            "code": "conflict",
            "message": "existing administrator is unavailable",
        }
    role_request = {
        "user_id": credentials["user_id"],
        "role_name": "platform_admin",
        "site_id": site_id,
    }
    if not identity["has_role"](role_request):
        return {
            "ok": False,
            "code": "authorization_denied",
            "message": "existing user is not the site platform administrator",
        }
    matches = verify_password(
        password, credentials.get("password_hash"), pepper)
    if matches and not credentials.get("locked_until") \
            and not credentials.get("must_change"):
        return {"ok": True, "changed": False}
    replacement = copy.deepcopy(role_request)
    replacement["password_hash"] = hash_password(
        password, pepper, minimum_length=minimum_length)
    updated = identity["replace_password_for_role"](replacement)
    if not updated.get("ok"):
        return updated
    return {"ok": True, "changed": True}


def create_admin(arguments, config):
    """Create the first platform administrator without printing credentials."""
    store = create_gateway_data_store(env_database_options())
    try:
        options = config["auth"]
        password = _password(arguments)
        password_pepper = read_secret(options["password_pepper_file"])
        auth = create_auth_management(
            identity=store["identity"], rate_limit=store["rate_limit"],
            password_pepper=password_pepper,
            token_pepper=read_secret(options["token_pepper_file"]),
            binding_pepper=read_secret(options["binding_pepper_file"]),
            options=options,
        )
        result = auth["create_user"]({
            "username": arguments.username,
            "password": password,
            "display_name": arguments.display_name,
            "enabled": True,
            "role_name": "platform_admin",
            "site_id": config["site_id"],
        })
        if not result.get("ok") and result.get("code") == "conflict" \
                and arguments.ensure_existing_password:
            ensured = ensure_existing_admin(
                store["identity"], arguments.username, password,
                password_pepper, config["site_id"],
                options["minimum_password_length"],
            )
            if not ensured.get("ok"):
                code = str(ensured.get("code") or "ensure_failed")
                message = str(ensured.get("message") or "").strip()
                print(code + (": " + message if message else ""))
                return 1
            if ensured.get("changed"):
                print(
                    "Administrator credential updated; active sessions revoked."
                )
            else:
                print("Existing administrator credential already matches.")
            return 0
        if not result.get("ok"):
            code = str(result.get("code") or "create_failed")
            message = str(result.get("message") or "").strip()
            print(code + (": " + message if message else ""))
            return 1
        print("Administrator created: " + str(result["user"]["username"]))
        return 0
    finally:
        store["close"]()


def build_parser():
    parser = argparse.ArgumentParser(prog="proxy-admin")
    parser.add_argument("command", choices=("create-admin",))
    parser.add_argument("--username", required=True)
    parser.add_argument("--display-name", default="Platform Administrator")
    parser.add_argument("--password-file")
    parser.add_argument("--ensure-existing-password", action="store_true")
    parser.add_argument("--config", default=os.environ.get(
        "PROXY_CONFIG_PATH", "_config/application/gateway_config.json"))
    return parser


def main():
    arguments = build_parser().parse_args()
    config = load_gateway_config(arguments.config)
    if arguments.command == "create-admin":
        return create_admin(arguments, config)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())

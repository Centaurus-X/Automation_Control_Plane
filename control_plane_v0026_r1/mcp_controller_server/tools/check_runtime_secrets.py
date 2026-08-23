#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Validate configured runtime secret files without printing their paths."""

import os
import ssl
import stat
import sys
import time


DEFAULTS = {
    "MCP_MQTT_CA_SOURCE": "secrets/mqtt_ca.crt",
    "MCP_MQTT_CERT_SOURCE": "secrets/mcp_mqtt.crt",
    "MCP_MQTT_KEY_SOURCE": "secrets/mcp_mqtt.key",
    "MCP_AUTH_SOURCE": "secrets/mcp_auth.json",
}
PRIVATE_NAMES = {"MCP_MQTT_KEY_SOURCE", "MCP_AUTH_SOURCE"}


def _dotenv(path):
    if not os.path.isfile(path) or os.path.islink(path):
        return {}
    result = {}
    with open(path, "r", encoding="utf-8") as handle:
        for line_number, line in enumerate(handle, 1):
            text = line.strip()
            if not text or text.startswith("#"):
                continue
            if "=" not in text:
                raise ValueError("invalid .env entry at line {}".format(line_number))
            key, value = text.split("=", 1)
            key = key.strip()
            value = value.strip()
            if key in result:
                raise ValueError("duplicate .env key at line {}".format(line_number))
            if len(value) >= 2 and value[0] == value[-1] and value[0] in ("'", '"'):
                value = value[1:-1]
            result[key] = value
    return result


def resolved_secret_paths(root):
    values = _dotenv(os.path.join(root, ".env"))
    result = {}
    for name, default in DEFAULTS.items():
        selected = os.environ.get(name) or values.get(name) or default
        path = selected if os.path.isabs(selected) else os.path.join(root, selected)
        result[name] = os.path.abspath(path)
    return result


def validate_secret_paths(paths):
    for name, path in paths.items():
        if not os.path.isfile(path) or os.path.islink(path):
            raise ValueError("required runtime secret is missing or symbolic")
        if "," in path or any(ord(character) < 32 for character in path):
            raise ValueError("runtime secret path is unsafe for an isolated mount")
        metadata = os.stat(path)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1:
            raise ValueError("runtime secret is not a non-empty regular file")
        if name in PRIVATE_NAMES and metadata.st_mode & 0o077:
            raise ValueError("private runtime secret permissions are too broad")
    return True


def _certificate_identities(document):
    identities = set()
    for group in document.get("subject") or ():
        for name, value in group:
            if name == "commonName":
                identities.add(str(value))
    for name, value in document.get("subjectAltName") or ():
        if name in ("URI", "uniformResourceIdentifier"):
            identities.add(str(value))
    return identities


def validate_mqtt_material(paths, site_id, source_instance_id):
    ca_path = paths["MCP_MQTT_CA_SOURCE"]
    certificate_path = paths["MCP_MQTT_CERT_SOURCE"]
    key_path = paths["MCP_MQTT_KEY_SOURCE"]
    context = ssl.create_default_context(cafile=ca_path)
    context.load_cert_chain(certfile=certificate_path, keyfile=key_path)
    document = ssl._ssl._test_decode_cert(certificate_path)
    expected = "urn:automation:{}:proxy:{}".format(site_id, source_instance_id)
    if expected not in _certificate_identities(document):
        raise ValueError("MQTT certificate identity does not match configured source")
    current = time.time()
    if ssl.cert_time_to_seconds(document["notBefore"]) > current:
        raise ValueError("MQTT client certificate is not valid yet")
    if ssl.cert_time_to_seconds(document["notAfter"]) <= current:
        raise ValueError("MQTT client certificate has expired")
    return True


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = resolved_secret_paths(root)
    validate_secret_paths(paths)
    values = _dotenv(os.path.join(root, ".env"))
    site_id = os.environ.get("AUTOMATION_SITE_ID") or values.get(
        "AUTOMATION_SITE_ID") or "site-main"
    source_instance_id = os.environ.get("MCP_MQTT_SOURCE_INSTANCE_ID") or values.get(
        "MCP_MQTT_SOURCE_INSTANCE_ID") or "mcp-controller-01"
    validate_mqtt_material(paths, site_id, source_instance_id)
    sys.path.insert(0, os.path.join(root, "src"))
    from mcp_controller_server.auth import load_auth_state
    load_auth_state(paths["MCP_AUTH_SOURCE"], 120)
    print("PASS runtime secrets, auth, permissions, key pair and MQTT identity")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError) as exc:
        print("runtime secret validation failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

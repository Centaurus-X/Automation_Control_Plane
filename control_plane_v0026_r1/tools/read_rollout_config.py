#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Validate rollout_automation.json and emit NUL-delimited assignments."""

import errno
import json
import os
import re
import stat
import sys

from pathlib import Path


MAX_FILE_BYTES = 64 * 1024
MAX_TEXT_CHARS = 4096

TEXT_KEYS = frozenset((
    "CONTROL_PLANE_BIND_ADDRESS",
    "MQTT_BIND_ADDRESS",
    "MQTT_TEST_HOST",
    "MQTT_TEST_BROKER_IP",
    "PROXY_BIND_ADDRESS",
    "PROXY_ORIGIN_ALLOWLIST",
    "MCP_BIND_ADDRESS",
    "MCP_MQTT_SOURCE_INSTANCE_ID",
    "MCP_AUTH_FILE",
    "MCP_AUTH_CREDENTIAL_KEY",
    "MCP_CLIENT_ID",
    "MCP_SUBJECT",
    "E2E_ORIGIN",
    "CONTROL_PLANE_PROFILE",
    "CONTROL_PLANE_STATE_SOURCE",
    "SITE_ID",
    "NODE_ID",
    "WORKER_SSH",
    "WORKER_PATH",
    "WORKER_PROFILE",
    "WORKER_PKI_STAGE",
    "WORKER_LOG_PATH",
    "WORKER_ARCHIVE",
    "WORKER_EXPECTED_VERSION",
    "WORKER_ARCHIVE_SHA256",
    "WORKER_MANIFEST_SHA256",
    "WORKER_CONFIG_DRIFT_POLICY",
    "REUSE_COMPATIBLE_REVISIONS",
    "CREDENTIALS_FILE",
    "SSH_CREDENTIAL_KEY",
    "ADMIN_CREDENTIAL_KEY",
    "ADMIN_USERNAME",
    "LOG_ROOT",
    "ACTUATOR_HOST",
))
INTEGER_KEYS = frozenset((
    "PROXY_WSS_PORT",
    "PROXY_HTTPS_PORT",
    "MCP_HTTP_PORT",
    "CONVERGENCE_TIMEOUT_S",
    "TELEMETRY_TIMEOUT_S",
    "ACTUATOR_PORT",
))
ALLOWED_KEYS = TEXT_KEYS | INTEGER_KEYS


def _reject_duplicate_pairs(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key: " + str(key))
        result[key] = value
    return result


def _reject_json_constant(value):
    raise ValueError("unsupported JSON numeric constant: " + value)


def _read_document(path):
    path = Path(path).expanduser()
    try:
        link_metadata = os.lstat(path)
    except FileNotFoundError as error:
        raise ValueError("rollout configuration not found: " + str(path)) from error
    if stat.S_ISLNK(link_metadata.st_mode):
        raise ValueError("rollout configuration must not be a symbolic link")

    flags = os.O_RDONLY
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise ValueError(
                "rollout configuration must not be a symbolic link"
            ) from error
        raise ValueError(
            "rollout configuration cannot be opened: " + str(path)
        ) from error

    try:
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode):
            raise ValueError("rollout configuration must be a regular file")
        if metadata.st_size > MAX_FILE_BYTES:
            raise ValueError("rollout configuration exceeds 64 KiB")
        chunks = []
        remaining = MAX_FILE_BYTES + 1
        while remaining > 0:
            chunk = os.read(descriptor, remaining)
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
        raw = b"".join(chunks)
    finally:
        os.close(descriptor)

    if len(raw) > MAX_FILE_BYTES:
        raise ValueError("rollout configuration exceeds 64 KiB")
    try:
        text = raw.decode("utf-8", errors="strict")
    except UnicodeDecodeError as error:
        raise ValueError("rollout configuration is not valid UTF-8") from error
    try:
        document = json.loads(
            text,
            object_pairs_hook=_reject_duplicate_pairs,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as error:
        raise ValueError(
            "rollout configuration is not valid JSON at line {}, column {}".format(
                error.lineno, error.colno
            )
        ) from error
    if not isinstance(document, dict):
        raise ValueError("rollout configuration root must be a JSON object")
    return document


def _validate_text(key, value):
    if not isinstance(value, str) or not value:
        raise ValueError(key + " must be a non-empty string")
    if len(value) > MAX_TEXT_CHARS:
        raise ValueError(key + " exceeds 4096 characters")
    if any(ord(character) < 32 or ord(character) == 127 for character in value):
        raise ValueError(key + " contains control characters")
    return value


def _validate_integer(key, value):
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValueError(key + " must be a positive integer")
    if value > 65535 and key.endswith("PORT"):
        raise ValueError(key + " exceeds TCP/UDP port range")
    return str(value)


def validate_document(document):
    unknown = sorted(set(document) - ALLOWED_KEYS)
    if unknown:
        raise ValueError("unknown rollout configuration keys: " + ", ".join(unknown))
    result = []
    for key in sorted(document):
        value = document[key]
        if key in TEXT_KEYS:
            normalized = _validate_text(key, value)
            if key == "WORKER_EXPECTED_VERSION" and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", normalized
            ) is None:
                raise ValueError("WORKER_EXPECTED_VERSION is invalid")
            if key == "MCP_MQTT_SOURCE_INSTANCE_ID" and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}", normalized
            ) is None:
                raise ValueError("MCP_MQTT_SOURCE_INSTANCE_ID is invalid")
            if key in ("MCP_CLIENT_ID", "MCP_SUBJECT") and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}", normalized
            ) is None:
                raise ValueError(key + " is invalid")
            if key == "MCP_AUTH_CREDENTIAL_KEY" and re.fullmatch(
                r"[A-Za-z0-9][A-Za-z0-9_.-]{0,255}", normalized
            ) is None:
                raise ValueError("MCP_AUTH_CREDENTIAL_KEY is invalid")
            if key in ("WORKER_ARCHIVE_SHA256", "WORKER_MANIFEST_SHA256") and re.fullmatch(
                r"[0-9a-f]{64}", normalized
            ) is None:
                raise ValueError(
                    key + " must be 64 lowercase hexadecimal characters"
                )
            if key == "WORKER_CONFIG_DRIFT_POLICY" and normalized not in (
                "preserve", "strict"
            ):
                raise ValueError(
                    "WORKER_CONFIG_DRIFT_POLICY must be preserve or strict"
                )
            if key == "REUSE_COMPATIBLE_REVISIONS":
                revisions = normalized.split(",")
                if not revisions or any(
                    re.fullmatch(r"v[0-9]{4,}-r[1-9][0-9]*", item) is None
                    for item in revisions
                ):
                    raise ValueError(
                        "REUSE_COMPATIBLE_REVISIONS contains an invalid revision"
                    )
        else:
            normalized = _validate_integer(key, value)
        result.append((key, normalized))
    return result


def main():
    if len(sys.argv) != 2:
        print("Usage: read_rollout_config.py FILE", file=sys.stderr)
        return 64
    try:
        assignments = validate_document(_read_document(Path(sys.argv[1])))
    except (ValueError, OSError) as error:
        print("rollout configuration error: " + str(error), file=sys.stderr)
        return 65
    output = sys.stdout.buffer
    for key, value in assignments:
        output.write(key.encode("ascii") + b"\0")
        output.write(value.encode("utf-8") + b"\0")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Stage Compose secrets for the non-root runtime through an isolated helper."""

import os
import stat
import subprocess
import sys

from check_runtime_secrets import resolved_secret_paths, validate_secret_paths


TARGETS = {
    "MCP_MQTT_CA_SOURCE": "mqtt_ca.crt",
    "MCP_MQTT_CERT_SOURCE": "mcp_mqtt.crt",
    "MCP_MQTT_KEY_SOURCE": "mcp_mqtt.key",
    "MCP_AUTH_SOURCE": "mcp_auth.json",
}
RUNTIME_UID = 10001
RUNTIME_GID = 10001
HELPER_IMAGE = "python:3.14.6-slim-bookworm"


def _docker_command(paths, target):
    command = [
        "docker", "run", "--rm", "--network", "none", "--read-only",
        "--user", "0:0", "--cap-drop", "ALL", "--cap-add", "CHOWN",
        "--cap-add", "FOWNER", "--cap-add", "DAC_OVERRIDE",
        "--tmpfs", "/tmp:rw,noexec,nosuid,size=8m",
    ]
    names = sorted(TARGETS)
    for index, name in enumerate(names):
        command.extend((
            "--mount",
            "type=bind,src={},dst=/source{},readonly".format(paths[name], index),
        ))
    command.extend((
        "--mount", "type=bind,src={},dst=/output".format(target),
        "--entrypoint", "/bin/sh", HELPER_IMAGE, "-eu", "-c",
        "rm -f /output/*; "
        "cp /source0 /output/{0}; cp /source1 /output/{1}; "
        "cp /source2 /output/{2}; cp /source3 /output/{3}; "
        "chown {4}:{5} /output/*; chmod 0400 /output/*".format(
            TARGETS[names[0]], TARGETS[names[1]], TARGETS[names[2]],
            TARGETS[names[3]], RUNTIME_UID, RUNTIME_GID,
        ),
    ))
    return command


def _verify_staged(target):
    for filename in TARGETS.values():
        path = os.path.join(target, filename)
        if not os.path.isfile(path) or os.path.islink(path):
            raise ValueError("staged runtime secret is missing or symbolic")
        metadata = os.stat(path)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size < 1:
            raise ValueError("staged runtime secret is invalid")
        if metadata.st_uid != RUNTIME_UID or metadata.st_gid != RUNTIME_GID:
            raise ValueError("staged runtime secret ownership is invalid")
        if stat.S_IMODE(metadata.st_mode) != 0o400:
            raise ValueError("staged runtime secret mode is invalid")


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    paths = resolved_secret_paths(root)
    validate_secret_paths(paths)
    target = os.path.join(root, "runtime-secrets")
    if "," in target or any(ord(character) < 32 for character in target):
        raise ValueError("release path is unsafe for an isolated mount")
    os.makedirs(target, mode=0o700, exist_ok=True)
    os.chmod(target, 0o700)
    completed = subprocess.run(
        _docker_command(paths, target),
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("isolated runtime secret staging failed")
    _verify_staged(target)
    print("PASS isolated non-root Compose secret staging")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, RuntimeError, ValueError) as exc:
        print("runtime secret staging failed: " + str(exc), file=sys.stderr)
        raise SystemExit(1)

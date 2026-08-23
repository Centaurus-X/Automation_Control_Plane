#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Read the private Core-E2E node-local proposal proof for rollout chaining."""

import argparse
import json
import re
import sys

from pathlib import Path


FIELDS = (
    "operation_id",
    "relay_message_id",
    "authority_epoch",
    "base_scope_revision",
    "target_scope_revision",
    "target_scope_fingerprint",
    "node_replica_revision",
    "node_replica_fingerprint",
    "operation_format",
    "operation_count",
)


def _load(path):
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("node-local proposal proof must be an object")
    missing = [field for field in FIELDS if field not in value]
    if missing:
        raise ValueError("node-local proposal proof fields are missing")
    for field in FIELDS:
        if "\n" in str(value[field]) or "\r" in str(value[field]):
            raise ValueError("node-local proposal proof contains a line break")
    for field in (
            "authority_epoch", "base_scope_revision", "target_scope_revision",
            "node_replica_revision", "operation_count"):
        if isinstance(value[field], bool) or not isinstance(value[field], int):
            raise ValueError(field + " must be an integer")
    for field in ("target_scope_fingerprint", "node_replica_fingerprint"):
        if re.fullmatch(r"[0-9a-f]{64}", str(value[field])) is None:
            raise ValueError(field + " is invalid")
    return value


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--proof", type=Path, required=True)
    arguments = parser.parse_args()
    try:
        value = _load(arguments.proof.expanduser().resolve())
    except (OSError, ValueError, json.JSONDecodeError) as error:
        print("FAILED node-local proposal proof read: " + str(error), file=sys.stderr)
        return 1
    for field in FIELDS:
        print(value[field])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
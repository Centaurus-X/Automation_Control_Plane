#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Deterministic fake component adapter used by administration unit tests."""

import json
import os
import sys


def append_record(path, record):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, exist_ok=True)
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        payload = (
            json.dumps(record, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            + "\n"
        ).encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def main(argv=None):
    arguments = list(argv if argv is not None else sys.argv[1:])
    if len(arguments) not in (4, 8):
        print("usage: fake_adapter.py LOG ACTION COMPONENT TARGET "
              "[--release ROOT --worker-archive FILE]", file=sys.stderr)
        return 64
    log_path, action, component, target = arguments[:4]
    bindings = arguments[4:]
    if bindings and (bindings[0] != "--release"
                     or bindings[2] != "--worker-archive"):
        print("invalid deterministic release bindings", file=sys.stderr)
        return 64
    record = {
        "action": action,
        "component": component,
        "target": target,
        "plan_id": os.environ.get("ADMIN_PLAN_ID"),
        "step_id": os.environ.get("ADMIN_STEP_ID"),
        "idempotency_key": os.environ.get("ADMIN_IDEMPOTENCY_KEY"),
        "fencing_token": int(os.environ.get("ADMIN_FENCING_TOKEN") or 0),
    }
    if bindings:
        record["release_root"] = bindings[1]
        record["worker_archive"] = bindings[3]
    append_record(log_path, record)
    if os.environ.get("ADMIN_FAKE_EMIT_SECRET") == "1":
        print("password=super-secret-test-value")
    marker = ":".join((action, component, target))
    if marker == os.environ.get("ADMIN_FAKE_FAIL_MATCH"):
        print("forced failure for " + marker, file=sys.stderr)
        return 23
    print("fake adapter ok: " + marker)
    return 0


if __name__ == "__main__":
    sys.exit(main())

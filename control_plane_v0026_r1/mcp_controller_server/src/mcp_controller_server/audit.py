# -*- coding: utf-8 -*-

"""Append-only local audit evidence without payload or secret material."""

import copy
import os
import stat
import threading

from mcp_controller_server.canonical import canonical_text, format_timestamp, utc_now


def create_audit_writer(path):
    absolute = os.path.abspath(str(path))
    parent = os.path.dirname(absolute)
    os.makedirs(parent, mode=0o700, exist_ok=True)
    if os.path.lexists(absolute) and (
            os.path.islink(absolute) or not os.path.isfile(absolute)):
        raise ValueError("audit log must be a regular non-symbolic file")
    return {"path": absolute, "lock": threading.RLock()}


def append_audit(context, record):
    allowed = {
        "request_id", "client_id", "subject", "tool", "action", "decision",
        "reason_code", "plan_id", "payload_sha256", "mqtt_message_id",
    }
    sanitized = {
        key: copy.deepcopy(value) for key, value in record.items()
        if key in allowed and value is not None
    }
    sanitized["recorded_at"] = format_timestamp(utc_now())
    line = (canonical_text(sanitized) + "\n").encode("utf-8")
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    with context["lock"]:
        descriptor = os.open(context["path"], flags, 0o600)
        try:
            if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                raise ValueError("audit log is not a regular file")
            os.write(descriptor, line)
            os.fsync(descriptor)
        finally:
            os.close(descriptor)
    return True

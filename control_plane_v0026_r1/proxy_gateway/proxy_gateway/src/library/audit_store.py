# -*- coding: utf-8 -*-

# proxy_gateway/src/library/audit_store.py

"""Append-only Proxy audit and security-event persistence."""

import copy
import uuid

from functools import partial

from src.library.postgresql import jsonb


def append_audit(database, event):
    """Append one decision event with no credentials or bearer values."""
    values = copy.deepcopy(event)

    def write(connection):
        audit_id = str(values.get("audit_id") or uuid.uuid4())
        connection.execute(
            "INSERT INTO proxy_gateway.audit_log("
            "audit_id,request_id,correlation_id,principal_subject,session_id,source_ip,"
            "proxy_instance_id,site_id,resource,action,decision,reason_code,operation_id,details) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                audit_id, str(values.get("request_id") or audit_id),
                values.get("correlation_id"), values.get("principal_subject"),
                values.get("session_id"), values.get("source_ip"),
                values["proxy_instance_id"], values.get("site_id"),
                str(values.get("resource") or "unknown"),
                str(values.get("action") or "unknown"),
                str(values.get("decision") or "ERROR"),
                str(values.get("reason_code") or "unspecified"),
                values.get("operation_id"), jsonb(values.get("details") or {}),
            ),
        )
        return audit_id

    return database["transaction"](write, isolation_level="READ COMMITTED")


def append_security_event(database, event):
    """Append one sanitized security signal."""
    values = copy.deepcopy(event)

    def write(connection):
        event_id = str(values.get("security_event_id") or uuid.uuid4())
        connection.execute(
            "INSERT INTO proxy_gateway.security_events("
            "security_event_id,event_type,severity,principal_subject,source_ip,request_id,details) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                event_id, values["event_type"], values.get("severity", "WARNING"),
                values.get("principal_subject"), values.get("source_ip"),
                values.get("request_id"), jsonb(values.get("details") or {}),
            ),
        )
        return event_id

    return database["transaction"](write, isolation_level="READ COMMITTED")


def list_audit(database, **kwargs):
    """Return bounded, transport-safe audit entries for authorized readers.

    PostgreSQL ``inet`` values are loaded by psycopg 3 as objects from the
    standard-library ``ipaddress`` family.  The public API contract exposes
    ``source_ip`` as canonical text, so normalization belongs at this shared
    WSS/HTTPS source boundary.
    """
    limit = min(200, max(1, int(kwargs.get("limit", 100))))
    site_id = kwargs.get("site_id")

    def read(connection):
        rows = connection.execute(
            "SELECT * FROM proxy_gateway.audit_log WHERE (%s::text IS NULL OR site_id=%s) "
            "ORDER BY occurred_at DESC LIMIT %s",
            (site_id, site_id, limit),
        ).fetchall()
        entries = []
        for row in rows:
            entry = dict(row)
            if entry.get("source_ip") is not None:
                entry["source_ip"] = str(entry["source_ip"])
            entries.append(entry)
        return entries

    return database["transaction"](read, isolation_level="READ COMMITTED")


def build_audit_capabilities(database):
    """Expose append and bounded read capabilities."""
    return {
        "append": partial(append_audit, database),
        "security": partial(append_security_event, database),
        "list": partial(list_audit, database),
    }

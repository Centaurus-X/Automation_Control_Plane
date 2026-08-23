# -*- coding: utf-8 -*-

# master_node/src/library/audit_store.py

"""Append-only master audit persistence."""

import copy
import uuid

from functools import partial

from src.library.postgresql import jsonb


def append_audit(database, event):
    """Append one secret-free audit event in a short transaction."""
    prepared = copy.deepcopy(event)

    def write(connection):
        audit_id = str(prepared.get("audit_id") or uuid.uuid4())
        connection.execute(
            "INSERT INTO master_control.audit_log("
            "audit_id, request_id, correlation_id, principal_subject, session_id, "
            "source_ip, instance_id, site_id, resource, action, decision, "
            "reason_code, config_revision, operation_id, details) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (
                audit_id,
                str(prepared.get("request_id") or audit_id),
                prepared.get("correlation_id"),
                prepared.get("principal_subject"),
                prepared.get("session_id"),
                prepared.get("source_ip"),
                str(prepared.get("instance_id") or "master-unknown"),
                str(prepared.get("site_id") or "unknown"),
                str(prepared.get("resource") or "unknown"),
                str(prepared.get("action") or "unknown"),
                str(prepared.get("decision") or "ERROR"),
                str(prepared.get("reason_code") or "unspecified"),
                prepared.get("config_revision"),
                prepared.get("operation_id"),
                jsonb(prepared.get("details") or {}),
            ),
        )
        return audit_id

    return database["transaction"](write, isolation_level="READ COMMITTED")


def build_audit_capabilities(database):
    """Return the narrow audit capability port."""
    return {"append": partial(append_audit, database)}

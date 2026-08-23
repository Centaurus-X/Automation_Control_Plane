# -*- coding: utf-8 -*-

"""Durable external approval state with idempotent MQTT commit reservation."""

import copy
import json
import os
import sqlite3
import threading
import time
import uuid

from mcp_controller_server.canonical import canonical_text, fingerprint, is_fingerprint
from mcp_controller_server.errors import make_error, make_success


SCHEMA = """
CREATE TABLE IF NOT EXISTS approval_plans (
    plan_id TEXT PRIMARY KEY,
    subject TEXT NOT NULL,
    action TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    payload_sha256 TEXT NOT NULL,
    state TEXT NOT NULL,
    created_at_s REAL NOT NULL,
    expires_at_s REAL NOT NULL,
    approved_at_s REAL,
    approver TEXT,
    committed_at_s REAL,
    result_json TEXT
);
CREATE INDEX IF NOT EXISTS approval_plans_expiry_idx
ON approval_plans(state, expires_at_s);
"""


def _connect(context):
    if os.path.lexists(context["path"]) and (
            os.path.islink(context["path"])
            or not os.path.isfile(context["path"])):
        raise ValueError("approval database must be a regular non-symbolic file")
    connection = sqlite3.connect(
        context["path"], timeout=10.0, isolation_level=None,
    )
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA busy_timeout = 10000")
    return connection


def _initialize(context):
    connection = _connect(context)
    try:
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.executescript(SCHEMA)
    finally:
        connection.close()


def create_approval_store(path, ttl_s, automatic=False, max_plans=10000):
    absolute = os.path.abspath(str(path))
    if os.path.lexists(absolute) and os.path.islink(absolute):
        raise ValueError("approval database must not be symbolic")
    os.makedirs(os.path.dirname(absolute), mode=0o700, exist_ok=True)
    context = {
        "path": absolute,
        "ttl_s": max(30, min(86400, int(ttl_s))),
        "automatic": bool(automatic),
        "max_plans": max(100, min(1000000, int(max_plans))),
        "lock": threading.RLock(),
        "recoverable": set(),
    }
    _initialize(context)
    connection = _connect(context)
    try:
        context["recoverable"] = {
            row[0] for row in connection.execute(
                "SELECT plan_id FROM approval_plans WHERE state = 'COMMITTING'")
        }
    finally:
        connection.close()
    return context


def _row_result(row):
    if row is None:
        return None
    result = {
        "plan_id": row["plan_id"],
        "subject": row["subject"],
        "action": row["action"],
        "payload_sha256": row["payload_sha256"],
        "state": row["state"],
        "created_at_s": row["created_at_s"],
        "expires_at_s": row["expires_at_s"],
        "approved_at_s": row["approved_at_s"],
        "approver": row["approver"],
        "committed_at_s": row["committed_at_s"],
    }
    if row["result_json"]:
        result["result"] = json.loads(row["result_json"])
    return result


def expire_plans(context, now_s=None):
    now = time.time() if now_s is None else float(now_s)
    connection = _connect(context)
    try:
        cursor = connection.execute(
            "UPDATE approval_plans SET state = 'EXPIRED' "
            "WHERE state IN ('PREPARED', 'APPROVED') AND expires_at_s < ?",
            (now,),
        )
        return int(cursor.rowcount)
    finally:
        connection.close()


def prepare_plan(context, subject, action, payload, now_s=None):
    now = time.time() if now_s is None else float(now_s)
    detached = copy.deepcopy(payload)
    payload_json = canonical_text(detached)
    payload_sha256 = fingerprint({"action": str(action), "payload": detached})
    state = "APPROVED" if context["automatic"] else "PREPARED"
    approved_at = now if context["automatic"] else None
    approver = "policy:automatic" if context["automatic"] else None
    expires = now + context["ttl_s"]
    with context["lock"]:
        connection = _connect(context)
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE approval_plans SET state = 'EXPIRED' "
                "WHERE state IN ('PREPARED', 'APPROVED') AND expires_at_s < ?",
                (now,),
            )
            count = int(connection.execute(
                "SELECT COUNT(*) FROM approval_plans").fetchone()[0])
            if count >= context["max_plans"]:
                remove_count = count - context["max_plans"] + 1
                connection.execute(
                    "DELETE FROM approval_plans WHERE plan_id IN ("
                    "SELECT plan_id FROM approval_plans "
                    "WHERE state IN ('COMPLETED', 'FAILED', 'EXPIRED') "
                    "ORDER BY created_at_s ASC LIMIT ?)",
                    (remove_count,),
                )
                count = int(connection.execute(
                    "SELECT COUNT(*) FROM approval_plans").fetchone()[0])
            if count >= context["max_plans"]:
                connection.execute("ROLLBACK")
                return make_error(
                    "overloaded", "approval plan capacity is exhausted",
                    retryable=True,
                )
            plan_id = str(uuid.uuid4())
            connection.execute(
                "INSERT INTO approval_plans "
                "(plan_id, subject, action, payload_json, payload_sha256, state, "
                "created_at_s, expires_at_s, approved_at_s, approver) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    plan_id, str(subject), str(action), payload_json, payload_sha256,
                    state, now, expires, approved_at, approver,
                ),
            )
            connection.execute("COMMIT")
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            connection.close()
    return make_success(
        plan_id=plan_id, action=str(action), payload_sha256=payload_sha256,
        state=state, expires_at_s=expires,
        approval_required=state != "APPROVED",
    )


def approve_plan(context, plan_id, payload_sha256, approver, now_s=None):
    if not is_fingerprint(payload_sha256):
        return make_error("invalid_payload", "payload_sha256 is invalid")
    now = time.time() if now_s is None else float(now_s)
    connection = _connect(context)
    try:
        connection.execute("BEGIN IMMEDIATE")
        row = connection.execute(
            "SELECT * FROM approval_plans WHERE plan_id = ?", (str(plan_id),),
        ).fetchone()
        if row is None:
            connection.execute("ROLLBACK")
            return make_error("not_found", "approval plan is unknown")
        if row["payload_sha256"] != payload_sha256:
            connection.execute("ROLLBACK")
            return make_error("approval_mismatch", "approval digest differs")
        if row["expires_at_s"] < now and row["state"] in ("PREPARED", "APPROVED"):
            connection.execute(
                "UPDATE approval_plans SET state = 'EXPIRED' WHERE plan_id = ?",
                (str(plan_id),),
            )
            connection.execute("COMMIT")
            return make_error("approval_expired", "approval plan has expired")
        if row["state"] == "APPROVED":
            connection.execute("COMMIT")
            return make_success(duplicate=True, **_row_result(row))
        if row["state"] != "PREPARED":
            connection.execute("ROLLBACK")
            return make_error(
                "invalid_state", "approval plan cannot be approved from " + row["state"])
        connection.execute(
            "UPDATE approval_plans SET state = 'APPROVED', approved_at_s = ?, "
            "approver = ? WHERE plan_id = ?",
            (now, str(approver), str(plan_id)),
        )
        connection.execute("COMMIT")
        return make_success(
            plan_id=str(plan_id), payload_sha256=payload_sha256,
            state="APPROVED", approved_at_s=now, approver=str(approver),
        )
    except Exception:
        try:
            connection.execute("ROLLBACK")
        except Exception:
            pass
        raise
    finally:
        connection.close()


def reserve_plan(context, plan_id, payload_sha256, subject, now_s=None):
    if not is_fingerprint(payload_sha256):
        return make_error("invalid_payload", "payload_sha256 is invalid")
    now = time.time() if now_s is None else float(now_s)
    with context["lock"]:
        connection = _connect(context)
        try:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT * FROM approval_plans WHERE plan_id = ?", (str(plan_id),),
            ).fetchone()
            if row is None:
                connection.execute("ROLLBACK")
                return make_error("not_found", "approval plan is unknown")
            if row["subject"] != str(subject):
                connection.execute("ROLLBACK")
                return make_error(
                    "authorization_denied", "approval belongs to another subject")
            if row["payload_sha256"] != payload_sha256:
                connection.execute("ROLLBACK")
                return make_error("approval_mismatch", "approval digest differs")
            state = row["state"]
            if row["expires_at_s"] < now and state in ("PREPARED", "APPROVED"):
                connection.execute(
                    "UPDATE approval_plans SET state = 'EXPIRED' WHERE plan_id = ?",
                    (str(plan_id),),
                )
                connection.execute("COMMIT")
                return make_error("approval_expired", "approval plan has expired")
            if state == "PREPARED":
                connection.execute("ROLLBACK")
                return make_error("approval_required", "external approval is still required")
            if state in ("COMPLETED", "FAILED"):
                connection.execute("COMMIT")
                result = _row_result(row)
                return make_success(
                    terminal=True, duplicate=True, plan=result,
                    result=copy.deepcopy(result.get("result")),
                )
            if state == "COMMITTING" and str(plan_id) not in context["recoverable"]:
                connection.execute("ROLLBACK")
                return make_error(
                    "commit_in_progress", "approval plan commit is already in progress",
                    retryable=True,
                )
            if state not in ("APPROVED", "COMMITTING"):
                connection.execute("ROLLBACK")
                return make_error(
                    "invalid_state", "approval plan cannot be committed from " + state)
            resumed = state == "COMMITTING"
            context["recoverable"].discard(str(plan_id))
            if not resumed:
                connection.execute(
                    "UPDATE approval_plans SET state = 'COMMITTING' WHERE plan_id = ?",
                    (str(plan_id),),
                )
            connection.execute("COMMIT")
            return make_success(
                plan_id=row["plan_id"], action=row["action"],
                payload=json.loads(row["payload_json"]),
                payload_sha256=row["payload_sha256"], resumed=resumed,
            )
        except Exception:
            try:
                connection.execute("ROLLBACK")
            except Exception:
                pass
            raise
        finally:
            connection.close()


def finalize_plan(context, plan_id, result, now_s=None):
    now = time.time() if now_s is None else float(now_s)
    detached = copy.deepcopy(result)
    retryable = bool(detached.get("retryable")) if isinstance(detached, dict) else False
    ok = bool(detached.get("ok")) if isinstance(detached, dict) else False
    state = "COMPLETED" if ok else ("COMMITTING" if retryable else "FAILED")
    with context["lock"]:
        connection = _connect(context)
        try:
            cursor = connection.execute(
                "UPDATE approval_plans SET state = ?, committed_at_s = ?, result_json = ? "
                "WHERE plan_id = ? AND state = 'COMMITTING'",
                (state, now, canonical_text(detached), str(plan_id)),
            )
            if cursor.rowcount != 1:
                return make_error("invalid_state", "approval plan was not reserved")
            if state == "COMMITTING":
                context["recoverable"].add(str(plan_id))
            else:
                context["recoverable"].discard(str(plan_id))
            return make_success(plan_id=str(plan_id), state=state, result=detached)
        except Exception:
            context["recoverable"].add(str(plan_id))
            raise
        finally:
            connection.close()


def get_plan(context, plan_id, subject=None, now_s=None):
    expire_plans(context, now_s=now_s)
    connection = _connect(context)
    try:
        row = connection.execute(
            "SELECT * FROM approval_plans WHERE plan_id = ?", (str(plan_id),),
        ).fetchone()
    finally:
        connection.close()
    if row is None:
        return make_error("not_found", "approval plan is unknown")
    if subject is not None and row["subject"] != str(subject):
        return make_error("authorization_denied", "approval belongs to another subject")
    return make_success(plan=_row_result(row))

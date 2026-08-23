# -*- coding: utf-8 -*-

# proxy_gateway/src/library/gateway_store_postgresql.py

"""Cluster read-model, replay cache and command correlation persistence."""

import copy

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library.postgresql import jsonb


def cache_event(database, request):
    """Persist one bounded replay event and update a node read model when present."""
    values = copy.deepcopy(request)
    ttl_s = max(30, int(values.get("ttl_s", 3600)))

    def write(connection):
        row = connection.execute(
            "INSERT INTO proxy_gateway.event_replay_cache("
            "event_id,site_id,event_type,resource_type,resource_id,envelope,expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second')) "
            "ON CONFLICT (event_id) DO NOTHING RETURNING event_sequence",
            (
                values["event_id"], values["site_id"], values["event_type"],
                values.get("resource_type"), values.get("resource_id"),
                jsonb(values["envelope"]), ttl_s,
            ),
        ).fetchone()
        duplicate = row is None
        if duplicate:
            row = connection.execute(
                "SELECT event_sequence FROM proxy_gateway.event_replay_cache "
                "WHERE event_id=%s",
                (values["event_id"],),
            ).fetchone()
        if not duplicate and values.get("resource_type") == "node" and \
                values.get("resource_id"):
            connection.execute(
                "INSERT INTO proxy_gateway.worker_registry_cache("
                "site_id,node_id,payload,source_event_id) VALUES (%s,%s,%s,%s) "
                "ON CONFLICT (site_id,node_id) DO UPDATE SET payload=EXCLUDED.payload,"
                "source_event_id=EXCLUDED.source_event_id,observed_at=clock_timestamp()",
                (
                    values["site_id"], values["resource_id"],
                    jsonb(values.get("payload") or {}), values["event_id"],
                ),
            )
        return make_success(
            duplicate=duplicate,
            event_sequence=row.get("event_sequence") if row else None,
        )

    return database["transaction"](write, isolation_level="READ COMMITTED")


def replay_events(database, request):
    """Return ordered site-scoped events after a stable sequence cursor."""
    values = copy.deepcopy(request)
    limit = min(500, max(1, int(values.get("limit", 100))))

    def read(connection):
        rows = connection.execute(
            "SELECT event_sequence,event_id,event_type,resource_type,resource_id,envelope,occurred_at "
            "FROM proxy_gateway.event_replay_cache WHERE site_id=%s AND event_sequence>%s "
            "AND expires_at>clock_timestamp() ORDER BY event_sequence LIMIT %s",
            (values["site_id"], int(values.get("after_sequence", 0)), limit),
        ).fetchall()
        return [dict(row) for row in rows]

    return database["transaction"](read, isolation_level="READ COMMITTED")


def replay_window(database, site_id):
    """Return the retained replay floor and high-water mark for one site."""
    def read(connection):
        row = connection.execute(
            "SELECT COALESCE(MIN(event_sequence),0) AS retention_floor,"
            "COALESCE(MAX(event_sequence),0) AS high_watermark,"
            "statement_timestamp() AS snapshot_at "
            "FROM proxy_gateway.event_replay_cache WHERE site_id=%s "
            "AND expires_at>statement_timestamp()",
            (str(site_id),),
        ).fetchone()
        return {
            "retention_floor": int(row.get("retention_floor") or 0),
            "high_watermark": int(row.get("high_watermark") or 0),
            "snapshot_at": row.get("snapshot_at"),
        }

    return database["transaction"](read, isolation_level="REPEATABLE READ")


def replay_page(database, request):
    """Read one bounded replay page through an immutable high-water mark."""
    values = copy.deepcopy(request)
    after_sequence = max(0, int(values.get("after_sequence", 0)))
    through_sequence = max(0, int(values.get("through_sequence", 0)))
    limit = min(500, max(1, int(values.get("limit", 100))))
    snapshot_at = values.get("snapshot_at")
    if snapshot_at is None:
        raise ValueError("replay snapshot timestamp is required")

    def read(connection):
        window = connection.execute(
            "SELECT COALESCE(MIN(event_sequence),0) AS retention_floor "
            "FROM proxy_gateway.event_replay_cache WHERE site_id=%s "
            "AND expires_at>%s",
            (values["site_id"], snapshot_at),
        ).fetchone()
        retention_floor = int(window.get("retention_floor") or 0)
        gap = bool(
            after_sequence > 0
            and retention_floor > 0
            and after_sequence < retention_floor - 1
        )
        if gap:
            return {
                "records": [],
                "scanned_through": after_sequence,
                "complete": False,
                "gap": True,
                "retention_floor": retention_floor,
            }
        rows = connection.execute(
            "SELECT event_sequence,event_id,event_type,resource_type,resource_id,"
            "envelope,occurred_at FROM proxy_gateway.event_replay_cache "
            "WHERE site_id=%s AND event_sequence>%s AND event_sequence<=%s "
            "AND expires_at>%s ORDER BY event_sequence LIMIT %s",
            (
                values["site_id"], after_sequence,
                through_sequence, snapshot_at, limit,
            ),
        ).fetchall()
        records = [dict(row) for row in rows]
        scanned_through = int(records[-1]["event_sequence"]) \
            if len(records) == limit else through_sequence
        return {
            "records": records,
            "scanned_through": scanned_through,
            "complete": scanned_through >= through_sequence,
            "gap": False,
            "retention_floor": retention_floor,
        }

    return database["transaction"](read, isolation_level="REPEATABLE READ")


def list_nodes(database, site_id):
    """Return the non-authoritative node cache for one site."""
    def read(connection):
        rows = connection.execute(
            "SELECT node_id,payload,source_event_id,observed_at FROM "
            "proxy_gateway.worker_registry_cache WHERE site_id=%s ORDER BY node_id",
            (site_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    return database["transaction"](read, isolation_level="READ COMMITTED")


def create_command(database, request):
    """Persist command correlation and idempotency before MQTT publication."""
    values = copy.deepcopy(request)

    def write(connection):
        row = connection.execute(
            "INSERT INTO proxy_gateway.command_requests("
            "request_id,correlation_id,idempotency_key,proxy_instance_id,user_id,site_id,"
            "event_type,state,request_payload,envelope) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'PENDING',%s,%s) "
            "ON CONFLICT DO NOTHING RETURNING *",
            (
                values["request_id"], values["correlation_id"], values.get("idempotency_key"),
                values["proxy_instance_id"], values["user_id"], values["site_id"],
                values["event_type"], jsonb(values["request_payload"]),
                jsonb(values["envelope"]),
            ),
        ).fetchone()
        if row:
            return make_success(duplicate=False, command=dict(row))
        if not values.get("idempotency_key"):
            return make_error("conflict", "command request already exists")
        previous = connection.execute(
            "SELECT * FROM proxy_gateway.command_requests WHERE "
            "user_id=%s AND site_id=%s AND event_type=%s AND idempotency_key=%s",
            (
                values["user_id"], values["site_id"], values["event_type"],
                values["idempotency_key"],
            ),
        ).fetchone()
        if not previous:
            return make_error("conflict", "command idempotency state is unavailable")
        if previous["request_payload"] != values["request_payload"]:
            return make_error(
                "conflict", "idempotency key was reused with other content")
        return make_success(duplicate=True, command=dict(previous))

    return database["transaction"](write)


def update_command(database, request_id, result):
    """Persist an application ACK and terminal correlation state."""
    retryable = bool(result.get("retryable")) or result.get("code") in (
        "database_unavailable", "internal_error", "not_ready")
    state = "COMPLETED" if result.get("ok") else ("PENDING" if retryable else "FAILED")
    operation_id = result.get("operation_id")
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
        operation_id = None

    def write(connection):
        row = connection.execute(
            "UPDATE proxy_gateway.command_requests SET state=%s,result=%s,"
            "operation_id=COALESCE(%s,operation_id),"
            "next_attempt_at=CASE WHEN %s='PENDING' THEN clock_timestamp()+interval '1 second' "
            "ELSE next_attempt_at END,updated_at=clock_timestamp() "
            "WHERE request_id=%s RETURNING *",
            (state, jsonb(result), operation_id, state, request_id),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](write, isolation_level="READ COMMITTED")


def mark_command_attempt(database, request_id, **kwargs):
    """Record one durable MQTT publication attempt and retry deadline."""
    published = bool(kwargs.get("published"))
    retry_s = max(0.5, float(kwargs.get("retry_s", 2.0)))

    def write(connection):
        row = connection.execute(
            "UPDATE proxy_gateway.command_requests SET state=%s,"
            "attempt_count=attempt_count+1,next_attempt_at=clock_timestamp()+"
            "(%s*interval '1 second'),updated_at=clock_timestamp() "
            "WHERE request_id=%s AND state IN ('PENDING','ACCEPTED') RETURNING *",
            ("ACCEPTED" if published else "PENDING", retry_s, request_id),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](write, isolation_level="READ COMMITTED")


def due_commands(database, proxy_instance_id, **kwargs):
    """Return a bounded retry batch owned by one stable Proxy identity."""
    limit = min(128, max(1, int(kwargs.get("limit", 32))))

    def read(connection):
        rows = connection.execute(
            "SELECT * FROM proxy_gateway.command_requests WHERE proxy_instance_id=%s "
            "AND state IN ('PENDING','ACCEPTED') AND next_attempt_at<=clock_timestamp() "
            "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s",
            (proxy_instance_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    return database["transaction"](read, isolation_level="READ COMMITTED")


def expire_command(database, request_id):
    """Terminally expire an undelivered command after its signed lifetime."""
    result = make_error("expired", "command lifetime elapsed before acknowledgement")

    def write(connection):
        row = connection.execute(
            "UPDATE proxy_gateway.command_requests SET state='EXPIRED',result=%s,"
            "updated_at=clock_timestamp() WHERE request_id=%s "
            "AND state IN ('PENDING','ACCEPTED') RETURNING *",
            (jsonb(result), request_id),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](write, isolation_level="READ COMMITTED")


def get_command(database, request_id):
    """Return one command result by request ID."""
    def read(connection):
        row = connection.execute(
            "SELECT * FROM proxy_gateway.command_requests WHERE request_id=%s",
            (request_id,),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](read, isolation_level="READ COMMITTED")


def operation_owners(database, site_id, operation_ids):
    """Resolve unambiguous operation owners from durable Proxy command rows."""
    normalized = sorted({
        str(value) for value in operation_ids or ()
        if isinstance(value, str) and 1 <= len(value) <= 128
    })
    if not normalized:
        return make_success(owners={})
    if len(normalized) > 500:
        return make_error("invalid_payload", "operation owner lookup exceeds its limit")

    def read(connection):
        rows = connection.execute(
            "SELECT operation_id,user_id::text AS owner_subject FROM "
            "proxy_gateway.command_requests WHERE site_id=%s "
            "AND operation_id=ANY(%s::text[]) ORDER BY operation_id,user_id",
            (str(site_id), normalized),
        ).fetchall()
        candidates = {}
        for row in rows:
            operation_id = str(row.get("operation_id") or "")
            owner_subject = str(row.get("owner_subject") or "")
            if operation_id and owner_subject:
                candidates.setdefault(operation_id, set()).add(owner_subject)
        owners = {
            operation_id: next(iter(subjects))
            for operation_id, subjects in candidates.items()
            if len(subjects) == 1
        }
        return make_success(owners=owners)

    return database["transaction"](read, isolation_level="READ COMMITTED")


def claim_local_action(database, request):
    """Atomically claim or resolve one local mutation idempotency key."""
    values = copy.deepcopy(request)

    def write(connection):
        row = connection.execute(
            "INSERT INTO proxy_gateway.local_action_idempotency("
            "request_id,user_id,site_id,action,idempotency_key,payload_fingerprint,state) "
            "VALUES (%s,%s,%s,%s,%s,%s,'PENDING') "
            "ON CONFLICT DO NOTHING RETURNING *",
            (
                values["request_id"], values["user_id"], values["site_id"],
                values["action"], values["idempotency_key"],
                values["payload_fingerprint"],
            ),
        ).fetchone()
        if row:
            return make_success(duplicate=False, action_record=dict(row))
        previous = connection.execute(
            "SELECT * FROM proxy_gateway.local_action_idempotency WHERE "
            "user_id=%s AND site_id=%s AND action=%s AND idempotency_key=%s",
            (
                values["user_id"], values["site_id"], values["action"],
                values["idempotency_key"],
            ),
        ).fetchone()
        if not previous:
            return make_error("conflict", "local idempotency state is unavailable")
        if str(previous["payload_fingerprint"]) != str(
                values["payload_fingerprint"]):
            return make_error(
                "conflict", "idempotency key was reused with other content")
        if previous.get("state") == "PENDING":
            indeterminate = make_error(
                "conflict",
                "previous local mutation exceeded its recovery lease; "
                "its outcome requires operator verification",
                recovery_required=True,
            )
            recovered = connection.execute(
                "UPDATE proxy_gateway.local_action_idempotency "
                "SET state='FAILED',result=%s,updated_at=clock_timestamp() "
                "WHERE request_id=%s AND state='PENDING' "
                "AND updated_at<clock_timestamp()-interval '15 minutes' "
                "RETURNING *",
                (jsonb(indeterminate), previous["request_id"]),
            ).fetchone()
            if recovered:
                previous = recovered
        return make_success(duplicate=True, action_record=dict(previous))

    return database["transaction"](write)


def complete_local_action(database, request_id, result):
    """Persist a credential-free terminal result for a claimed local action."""
    state = "COMPLETED" if result.get("ok") else "FAILED"

    def write(connection):
        row = connection.execute(
            "UPDATE proxy_gateway.local_action_idempotency SET state=%s,result=%s,"
            "updated_at=clock_timestamp() WHERE request_id=%s AND state='PENDING' "
            "RETURNING *",
            (state, jsonb(result), str(request_id)),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](write, isolation_level="READ COMMITTED")


def cleanup_cache(database):
    """Delete expired replay and old limiter rows in one bounded maintenance task."""
    def write(connection):
        events = connection.execute(
            "DELETE FROM proxy_gateway.event_replay_cache WHERE event_sequence IN ("
            "SELECT event_sequence FROM proxy_gateway.event_replay_cache "
            "WHERE expires_at<clock_timestamp()-interval '10 minutes' "
            "ORDER BY event_sequence LIMIT 5000)"
        ).rowcount
        limits = connection.execute(
            "DELETE FROM proxy_gateway.rate_limit_state WHERE bucket_key IN ("
            "SELECT bucket_key FROM proxy_gateway.rate_limit_state "
            "WHERE updated_at<clock_timestamp()-interval '1 day' LIMIT 5000)"
        ).rowcount
        sessions = connection.execute(
            "DELETE FROM proxy_gateway.sessions WHERE session_id IN ("
            "SELECT session_id FROM proxy_gateway.sessions WHERE "
            "(absolute_expires_at<clock_timestamp()-interval '7 days' OR "
            "revoked_at<clock_timestamp()-interval '7 days') LIMIT 5000)"
        ).rowcount
        attempts = connection.execute(
            "DELETE FROM proxy_gateway.login_attempts WHERE attempt_id IN ("
            "SELECT attempt_id FROM proxy_gateway.login_attempts "
            "WHERE occurred_at<clock_timestamp()-interval '30 days' "
            "ORDER BY attempt_id LIMIT 5000)"
        ).rowcount
        return {
            "events": events, "limits": limits,
            "sessions": sessions, "login_attempts": attempts,
        }

    return database["transaction"](write, isolation_level="READ COMMITTED")


def build_gateway_store_capabilities(database):
    """Expose read-model, replay and correlation ports."""
    return {
        "cache_event": partial(cache_event, database),
        "replay_events": partial(replay_events, database),
        "replay_window": partial(replay_window, database),
        "replay_page": partial(replay_page, database),
        "list_nodes": partial(list_nodes, database),
        "create_command": partial(create_command, database),
        "update_command": partial(update_command, database),
        "mark_command_attempt": partial(mark_command_attempt, database),
        "due_commands": partial(due_commands, database),
        "expire_command": partial(expire_command, database),
        "get_command": partial(get_command, database),
        "operation_owners": partial(operation_owners, database),
        "claim_local_action": partial(claim_local_action, database),
        "complete_local_action": partial(complete_local_action, database),
        "cleanup": partial(cleanup_cache, database),
    }

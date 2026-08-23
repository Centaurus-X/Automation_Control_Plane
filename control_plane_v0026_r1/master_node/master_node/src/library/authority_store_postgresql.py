# -*- coding: utf-8 -*-

"""PostgreSQL leader authority plus idempotent inbox and durable outbox."""

import copy
import uuid

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library._generic_type_library import json_ready
from src.library.postgresql import jsonb


def _authority_row(connection, site_id):
    connection.execute(
        "INSERT INTO master_control.authority_state(site_id) VALUES (%s) "
        "ON CONFLICT (site_id) DO NOTHING", (site_id,))
    return connection.execute(
        "SELECT *,COALESCE(lease_until>clock_timestamp(),false) AS lease_active "
        "FROM master_control.authority_state WHERE site_id=%s FOR UPDATE",
        (site_id,),
    ).fetchone()


def acquire_or_renew(database, site_id, instance_id, **kwargs):
    """Acquire or renew the single PostgreSQL-fenced authority lease."""
    eligible = bool(kwargs.get("eligible", False))
    lease_s = max(3.0, float(kwargs.get("lease_s", 15.0)))
    supplied_token = kwargs.get("lease_token")

    def write(connection):
        row = _authority_row(connection, site_id)
        same = row.get("leader_instance_id") == instance_id
        token_matches = str(row.get("lease_token") or "") == str(supplied_token or "")
        active = bool(row.get("lease_active"))
        if same and active and token_matches:
            renewed = connection.execute(
                "UPDATE master_control.authority_state SET lease_until="
                "clock_timestamp()+(%s*interval '1 second'),updated_at=clock_timestamp() "
                "WHERE site_id=%s RETURNING *",
                (lease_s, site_id),
            ).fetchone()
            renewed = dict(renewed)
            renewed["lease_active"] = True
            return make_success(role="leader", authority=renewed)
        if active or not eligible:
            return make_success(role="standby", authority=dict(row))
        token = uuid.uuid4()
        term = max(
            int(row.get("authority_term") or 0),
            max(0, int(kwargs.get("term_floor") or 0)),
        ) + 1
        updated = connection.execute(
            "UPDATE master_control.authority_state SET authority_term=%s,"
            "leader_instance_id=%s,lease_token=%s,"
            "lease_until=clock_timestamp()+(%s*interval '1 second'),"
            "updated_at=clock_timestamp() WHERE site_id=%s RETURNING *",
            (term, instance_id, token, lease_s, site_id),
        ).fetchone()
        result = dict(updated)
        result["lease_active"] = True
        return make_success(role="leader", authority=result)

    return database["transaction"](write)


def get_authority(database, site_id):
    """Return current authority state without mutating its lease."""
    def read(connection):
        row = connection.execute(
            "SELECT *,lease_until>clock_timestamp() AS lease_active "
            "FROM master_control.authority_state WHERE site_id=%s", (site_id,)
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](read, isolation_level="READ COMMITTED")


def register_inbox(database, message):
    """Persistently deduplicate one received message by identity and content."""
    values = copy.deepcopy(message)

    def write(connection):
        existing = connection.execute(
            "SELECT * FROM master_control.inbox WHERE site_id=%s AND source_service=%s "
            "AND source_instance_id=%s AND message_id=%s FOR UPDATE",
            (
                values["site_id"], values["source_service"],
                values["source_instance_id"], values["message_id"],
            ),
        ).fetchone()
        if existing:
            if existing["payload_fingerprint"] != values["payload_fingerprint"]:
                return make_error("replay_detected", "message ID was reused with other content")
            if existing.get("state") == "RECEIVED":
                return make_success(duplicate=False, recovered=True, record=dict(existing))
            return make_success(duplicate=True, record=dict(existing))
        connection.execute(
            "INSERT INTO master_control.inbox("
            "site_id,source_service,source_instance_id,message_id,event_type,"
            "payload_fingerprint,state) VALUES (%s,%s,%s,%s,%s,%s,'RECEIVED')",
            (
                values["site_id"], values["source_service"],
                values["source_instance_id"], values["message_id"],
                values["event_type"], values["payload_fingerprint"],
            ),
        )
        return make_success(duplicate=False)

    return database["transaction"](write)


def complete_inbox(database, message, result):
    """Persist one cached inbox result for safe QoS-1 retries."""
    def write(connection):
        connection.execute(
            "UPDATE master_control.inbox SET state=%s,result=%s,"
            "completed_at=clock_timestamp() WHERE site_id=%s AND source_service=%s "
            "AND source_instance_id=%s AND message_id=%s",
            (
                "COMPLETED" if result.get("ok") else "REJECTED",
                jsonb(json_ready(result)),
                message["site_id"], message["source_service"],
                message["source_instance_id"], message["message_id"],
            ),
        )
        return copy.deepcopy(result)

    return database["transaction"](write, isolation_level="READ COMMITTED")


def enqueue_outbox(database, record, **kwargs):
    """Insert one durable outgoing message without changing existing content."""
    values = copy.deepcopy(record)

    def write(connection):
        if kwargs.get("site_id") is not None:
            authority = connection.execute(
                "SELECT 1 AS owned FROM master_control.authority_state WHERE site_id=%s "
                "AND leader_instance_id=%s AND lease_token=%s AND authority_term=%s "
                "AND lease_until>clock_timestamp() FOR UPDATE",
                (
                    kwargs.get("site_id"), kwargs.get("instance_id"),
                    kwargs.get("lease_token"), kwargs.get("authority_term"),
                ),
            ).fetchone()
            if not authority:
                return make_error(
                    "not_config_authority", "outbox enqueue lost its authority lease")
        existing = connection.execute(
            "SELECT * FROM master_control.outbox WHERE message_id=%s FOR UPDATE",
            (values["message_id"],),
        ).fetchone()
        if existing:
            same = (
                existing["site_id"] == values["site_id"]
                and existing["target_service"] == values["target_service"]
                and existing["target_instance_id"] == values["target_instance_id"]
                and existing["event_type"] == values["event_type"]
                and existing["envelope"] == values["envelope"]
            )
            return make_success(
                duplicate=True,
                message_id=values["message_id"],
                state=str(existing.get("state") or "PENDING"),
                last_error_code=existing.get("last_error_code"),
            ) if same else make_error(
                "conflict", "outbox message ID was reused with other content")
        connection.execute(
            "INSERT INTO master_control.outbox("
            "message_id,site_id,target_service,target_instance_id,event_type,envelope) "
            "VALUES (%s,%s,%s,%s,%s,%s)",
            (
                values["message_id"], values["site_id"], values["target_service"],
                values["target_instance_id"], values["event_type"],
                jsonb(values["envelope"]),
            ),
        )
        return make_success(
            duplicate=False, message_id=values["message_id"], state="PENDING")

    return database["transaction"](write, isolation_level="READ COMMITTED")


def due_outbox(database, **kwargs):
    """Claim a bounded due outbox set under the current leader fence."""
    limit = min(256, max(1, int(kwargs.get("limit", 64))))
    remaining_s = max(0.1, float(kwargs.get("minimum_lease_remaining_s", 1.0)))

    def write(connection):
        authority = connection.execute(
            "SELECT 1 AS owned FROM master_control.authority_state WHERE site_id=%s "
            "AND leader_instance_id=%s AND lease_token=%s AND authority_term=%s "
            "AND lease_until>clock_timestamp()+(%s*interval '1 second') FOR UPDATE",
            (
                kwargs.get("site_id"), kwargs.get("instance_id"),
                kwargs.get("lease_token"), kwargs.get("authority_term"), remaining_s,
            ),
        ).fetchone()
        if not authority:
            return []
        rows = connection.execute(
            "SELECT * FROM master_control.outbox WHERE state IN ('PENDING','SENT') "
            "AND site_id=%s AND next_attempt_at<=clock_timestamp() "
            "ORDER BY (event_type='distributed.actuator.execute') DESC, created_at "
            "FOR UPDATE SKIP LOCKED LIMIT %s",
            (kwargs.get("site_id"), limit),
        ).fetchall()
        return [dict(row) for row in rows]

    return database["transaction"](write, isolation_level="READ COMMITTED")


def get_outbox(database, message_id):
    """Return one outgoing record for ACK correlation."""
    def read(connection):
        row = connection.execute(
            "SELECT * FROM master_control.outbox WHERE message_id=%s", (message_id,)
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](read, isolation_level="READ COMMITTED")


def mark_outbox(database, message_id, **kwargs):
    """Record a publish attempt or terminal application ACK."""
    state = str(kwargs.get("state") or "SENT")
    retry_s = max(0.1, float(kwargs.get("retry_s", 2.0)))

    def write(connection):
        if kwargs.get("site_id") is not None:
            authority = connection.execute(
                "SELECT 1 AS owned FROM master_control.authority_state WHERE site_id=%s "
                "AND leader_instance_id=%s AND lease_token=%s AND authority_term=%s "
                "AND lease_until>clock_timestamp() FOR UPDATE",
                (
                    kwargs.get("site_id"), kwargs.get("instance_id"),
                    kwargs.get("lease_token"), kwargs.get("authority_term"),
                ),
            ).fetchone()
            if not authority:
                return None
        row = connection.execute(
            "UPDATE master_control.outbox SET state=%s,attempt_count=attempt_count+1,"
            "next_attempt_at=clock_timestamp()+(%s*interval '1 second'),"
            "last_error_code=%s,updated_at=clock_timestamp() "
            "WHERE message_id=%s RETURNING *",
            (state, retry_s, kwargs.get("error_code"), message_id),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](write, isolation_level="READ COMMITTED")


def build_authority_capabilities(database):
    """Expose leader, inbox and outbox persistence only."""
    return {
        "acquire_or_renew": partial(acquire_or_renew, database),
        "get_authority": partial(get_authority, database),
        "register_inbox": partial(register_inbox, database),
        "complete_inbox": partial(complete_inbox, database),
        "enqueue_outbox": partial(enqueue_outbox, database),
        "due_outbox": partial(due_outbox, database),
        "get_outbox": partial(get_outbox, database),
        "mark_outbox": partial(mark_outbox, database),
    }

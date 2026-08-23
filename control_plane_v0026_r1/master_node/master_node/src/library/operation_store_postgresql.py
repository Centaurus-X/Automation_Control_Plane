# -*- coding: utf-8 -*-

# master_node/src/library/operation_store_postgresql.py

"""Durable remote-actuator journal with monotonic terminal states."""

import copy
import uuid

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library.postgresql import jsonb


TERMINAL_STATES = (
    "CONFIRMED", "WRITE_UNVERIFIED", "FAILED", "REJECTED", "UNCERTAIN", "CANCELLED",
)
TRANSITIONS = {
    "RECEIVED": ("ROUTED", "REJECTED", "CANCELLED"),
    "ROUTED": ("DISPATCHED", "FAILED", "CANCELLED"),
    "DISPATCHED": TERMINAL_STATES,
}


def _active_leader(connection, values, **kwargs):
    row = connection.execute(
        "SELECT *,lease_until>clock_timestamp() AS lease_active "
        "FROM master_control.authority_state WHERE site_id=%s FOR UPDATE",
        (values["site_id"],),
    ).fetchone()
    matches = (
        row
        and row.get("lease_active")
        and row.get("leader_instance_id") == values.get("instance_id")
        and str(row.get("lease_token") or "") == str(values.get("leader_lease_token") or "")
        and int(row.get("authority_term") or 0) == int(values["authority_term"])
        and (
            not kwargs.get("require_current_revision", True)
            or int(row.get("config_revision") or 0) == int(values["config_revision"])
        )
    )
    return bool(matches)


def create_operation(database, request):
    """Create or return an idempotent actuator operation."""
    values = copy.deepcopy(request)

    def write(connection):
        if not _active_leader(connection, values):
            return make_error("not_config_authority", "operation authority lease is not held")
        previous = connection.execute(
            "SELECT * FROM master_control.actuator_operations WHERE site_id=%s "
            "AND source_service=%s AND principal_subject=%s "
            "AND idempotency_key=%s FOR UPDATE",
            (
                values["site_id"], values["source_service"], values["principal_subject"],
                values["idempotency_key"],
            ),
        ).fetchone()
        if previous:
            if previous["request_payload"] != values["request_payload"]:
                return make_error("conflict", "idempotency key was reused with other content")
            return make_success(duplicate=True, operation=dict(previous))
        operation_id = str(values.get("operation_id") or uuid.uuid4())
        row = connection.execute(
            "INSERT INTO master_control.actuator_operations("
            "operation_id,site_id,idempotency_key,source_service,source_instance_id,"
            "principal_subject,controller_uuid,actuator_id,requested_value,owner_node_id,"
            "authority_term,config_revision,lease_token,state,request_payload,expires_at,"
            "execution_scope,execution_epoch) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,'RECEIVED',%s,%s,%s,%s) RETURNING *",
            (
                operation_id, values["site_id"], values["idempotency_key"],
                values["source_service"], values["source_instance_id"],
                values["principal_subject"], values["controller_uuid"],
                str(values["actuator_id"]), jsonb(values.get("requested_value")),
                values["owner_node_id"], values["authority_term"], values["config_revision"],
                values["lease_token"], jsonb(values["request_payload"]), values["expires_at"],
                values.get("execution_scope"), values.get("execution_epoch"),
            ),
        ).fetchone()
        _append_event(connection, operation_id, "RECEIVED", {"created": True})
        return make_success(duplicate=False, operation=dict(row))

    return database["transaction"](write)


def _append_event(connection, operation_id, state, details):
    connection.execute(
        "INSERT INTO master_control.operation_events(event_id,operation_id,state,details) "
        "VALUES (%s,%s,%s,%s)",
        (str(uuid.uuid4()), operation_id, state, jsonb(details or {})),
    )


def get_operation(database, operation_id):
    """Return one detached operation and its ordered state history."""
    def read(connection):
        row = connection.execute(
            "SELECT * FROM master_control.actuator_operations WHERE operation_id=%s",
            (operation_id,),
        ).fetchone()
        if not row:
            return None
        events = connection.execute(
            "SELECT state, details, created_at FROM master_control.operation_events "
            "WHERE operation_id=%s ORDER BY created_at, event_id",
            (operation_id,),
        ).fetchall()
        result = dict(row)
        result["events"] = [dict(item) for item in events]
        return result

    return database["transaction"](read, isolation_level="READ COMMITTED")


def transition_operation(database, operation_id, new_state, **kwargs):
    """Apply exactly one allowed state transition under a row lock."""
    target = str(new_state).upper()
    details = copy.deepcopy(kwargs.get("details") or {})
    result = copy.deepcopy(kwargs.get("result"))

    def write(connection):
        if kwargs.get("site_id") is not None and not _active_leader(connection, kwargs):
            return make_error(
                "not_config_authority", "operation transition lost its authority lease")
        row = connection.execute(
            "SELECT * FROM master_control.actuator_operations WHERE operation_id=%s FOR UPDATE",
            (operation_id,),
        ).fetchone()
        if not row:
            return make_error("not_found", "operation is unknown")
        current = row["state"]
        if current == target:
            return make_success(duplicate=True, operation=dict(row))
        if target not in TRANSITIONS.get(current, ()):
            return make_error("conflict", "operation transition is not allowed", state=current)
        updated = connection.execute(
            "UPDATE master_control.actuator_operations SET state=%s, result=COALESCE(%s,result), "
            "updated_at=clock_timestamp() WHERE operation_id=%s RETURNING *",
            (target, jsonb(result) if result is not None else None, operation_id),
        ).fetchone()
        _append_event(connection, operation_id, target, details)
        return make_success(duplicate=False, operation=dict(updated))

    return database["transaction"](write)


def apply_worker_result(database, request):
    """Validate the fenced owner ACK before setting a terminal operation state."""
    values = copy.deepcopy(request)

    def write(connection):
        if not _active_leader(connection, values, require_current_revision=False):
            return make_error("not_config_authority", "operation authority lease is not held")
        row = connection.execute(
            "SELECT * FROM master_control.actuator_operations WHERE operation_id=%s FOR UPDATE",
            (values["operation_id"],),
        ).fetchone()
        if not row:
            return make_error("not_found", "operation is unknown")
        checks = (
            row["owner_node_id"] == values["source_node_id"],
            int(row["authority_term"]) == int(values["authority_term"]),
            int(row["config_revision"]) == int(values["config_revision"]),
        )
        if not all(checks):
            return make_error("identity_mismatch", "operation result fencing data differs")
        state = str(values.get("terminal_state") or "FAILED").upper()
        if state not in TERMINAL_STATES:
            state = "FAILED"
        result_payload = values.get("result") if isinstance(values.get("result"), dict) else {}
        if state == "CONFIRMED" and not all((
                result_payload.get("success"),
                result_payload.get("readback_checked"),
                result_payload.get("readback_verified"))):
            state = "FAILED"
            result_payload = copy.deepcopy(result_payload)
            result_payload["master_validation_error"] = "confirmed result lacks verified readback"
        if state == "WRITE_UNVERIFIED":
            state = "FAILED"
            result_payload = copy.deepcopy(result_payload)
            result_payload["master_validation_error"] = "hardware readback is required"
        if row["state"] in TERMINAL_STATES:
            persisted_result = row.get("result") \
                if isinstance(row.get("result"), dict) else {}
            if row["state"] != state or persisted_result != result_payload:
                return make_error(
                    "conflict", "operation already has another terminal result",
                    state=row["state"],
                )
            return make_success(duplicate=True, operation=dict(row))
        updated = connection.execute(
            "UPDATE master_control.actuator_operations SET state=%s,result=%s,"
            "updated_at=clock_timestamp() WHERE operation_id=%s AND state IN ('ROUTED','DISPATCHED') "
            "RETURNING *",
            (state, jsonb(result_payload), values["operation_id"]),
        ).fetchone()
        if not updated:
            return make_error("conflict", "operation is not awaiting a worker result")
        _append_event(connection, values["operation_id"], state, result_payload)
        return make_success(operation=dict(updated))

    return database["transaction"](write)


def recover_uncertain(database, site_id, authority_term):
    """Fence old-term outbox work and classify every prior hardware window."""
    def write(connection):
        uncertain = connection.execute(
            "UPDATE master_control.actuator_operations SET state='UNCERTAIN', "
            "result=%s, updated_at=clock_timestamp() WHERE site_id=%s AND authority_term<%s "
            "AND (state='DISPATCHED' OR (state='ROUTED' AND EXISTS ("
            "SELECT 1 FROM master_control.outbox o WHERE o.message_id="
            "'actuator-execute:'||actuator_operations.authority_term::text||':'||"
            "actuator_operations.operation_id AND o.state IN ('SENT','ACKED')))) "
            "RETURNING operation_id",
            (
                jsonb({"reason": "authority term changed after hardware dispatch"}),
                site_id, authority_term,
            ),
        ).fetchall()
        cancelled = connection.execute(
            "UPDATE master_control.actuator_operations SET state='CANCELLED',"
            "result=%s,updated_at=clock_timestamp() WHERE site_id=%s AND authority_term<%s "
            "AND state IN ('RECEIVED','ROUTED') RETURNING operation_id",
            (
                jsonb({"reason": "old authority term fenced before confirmed dispatch"}),
                site_id, authority_term,
            ),
        ).fetchall()
        for row in uncertain:
            _append_event(
                connection, row["operation_id"], "UNCERTAIN",
                {"reason": "authority term changed after hardware dispatch"},
            )
        for row in cancelled:
            _append_event(
                connection, row["operation_id"], "CANCELLED",
                {"reason": "old authority term fenced before confirmed dispatch"},
            )
        fenced = connection.execute(
            "UPDATE master_control.outbox SET state='CANCELLED',last_error_code='stale_authority_term',"
            "updated_at=clock_timestamp() WHERE site_id=%s AND state IN ('PENDING','SENT') "
            "AND COALESCE(NULLIF(envelope->'payload'->>'coordinator_term','')::bigint,0)<%s "
            "RETURNING message_id,event_type",
            (site_id, authority_term),
        ).fetchall()
        connection.execute(
            "UPDATE master_control.shard_rollouts SET state='superseded',"
            "detail=jsonb_build_object('reason','stale_authority_term'),"
            "updated_at=clock_timestamp() WHERE coordinator_term<%s "
            "AND state NOT IN ('converged','failed','superseded')",
            (authority_term,),
        )
        return {
            "uncertain": [row["operation_id"] for row in uncertain],
            "cancelled": [row["operation_id"] for row in cancelled],
            "outbox_fenced": [row["message_id"] for row in fenced],
        }

    return database["transaction"](write)


def expire_due_operations(database, site_id, **kwargs):
    """Terminalize expired intents and fence every not-yet-terminal delivery."""
    limit = min(256, max(1, int(kwargs.get("limit", 64))))

    def write(connection):
        rows = connection.execute(
            "SELECT operation_id,authority_term,state FROM "
            "master_control.actuator_operations WHERE site_id=%s "
            "AND state IN ('RECEIVED','ROUTED','DISPATCHED') "
            "AND expires_at<=clock_timestamp() ORDER BY expires_at "
            "FOR UPDATE SKIP LOCKED LIMIT %s",
            (site_id, limit),
        ).fetchall()
        expired = []
        for row in rows:
            previous = str(row["state"])
            target = "UNCERTAIN" if previous == "DISPATCHED" else "FAILED"
            result = {
                "ok": False,
                "code": "operation_expired",
                "error": "actuator intent expired before verified hardware completion",
                "previous_state": previous,
            }
            updated = connection.execute(
                "UPDATE master_control.actuator_operations SET state=%s,result=%s,"
                "updated_at=clock_timestamp() WHERE operation_id=%s RETURNING *",
                (target, jsonb(result), row["operation_id"]),
            ).fetchone()
            _append_event(
                connection, row["operation_id"], target,
                {"code": "operation_expired", "previous_state": previous},
            )
            connection.execute(
                "UPDATE master_control.outbox SET state='CANCELLED',"
                "last_error_code='operation_expired',updated_at=clock_timestamp() "
                "WHERE message_id=%s AND state IN ('PENDING','SENT')",
                ("actuator-execute:{}:{}".format(
                    row["authority_term"], row["operation_id"]),),
            )
            expired.append(dict(updated))
        return {"count": len(expired), "operations": expired}

    return database["transaction"](write, isolation_level="READ COMMITTED")


def list_operations(database, site_id, **kwargs):
    """Return a bounded newest-first operation list."""
    limit = min(200, max(1, int(kwargs.get("limit", 50))))

    def read(connection):
        rows = connection.execute(
            "SELECT * FROM master_control.actuator_operations WHERE site_id=%s "
            "ORDER BY created_at DESC LIMIT %s",
            (site_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    return database["transaction"](read, isolation_level="READ COMMITTED")


def build_operation_capabilities(database):
    """Expose the narrow operation journal port."""
    return {
        "create": partial(create_operation, database),
        "get": partial(get_operation, database),
        "transition": partial(transition_operation, database),
        "apply_worker_result": partial(apply_worker_result, database),
        "expire_due": partial(expire_due_operations, database),
        "recover_uncertain": partial(recover_uncertain, database),
        "list": partial(list_operations, database),
    }

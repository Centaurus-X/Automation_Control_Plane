# -*- coding: utf-8 -*-

"""Durable Worker mirrors, Cross Registry heads and Authority V5 flows."""

import copy
import uuid

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library.postgresql import jsonb


def _leader_guard(connection, values):
    row = connection.execute(
        "SELECT *, lease_until>clock_timestamp() AS lease_active "
        "FROM master_control.authority_state WHERE site_id=%s FOR UPDATE",
        (values["site_id"],),
    ).fetchone()
    matches = row and row.get("lease_active") and (
        row.get("leader_instance_id") == values["instance_id"]
        and str(row.get("lease_token") or "") == str(values.get("lease_token") or "")
        and int(row.get("authority_term") or 0) == int(values["authority_term"])
    )
    return row if matches else None


def _mirror_result(row):
    if not row:
        return None
    result = dict(row)
    result["authority_epoch"] = int(result.get("authority_epoch") or 0)
    result["scope_revision"] = int(result.get("scope_revision") or 0)
    result["execution_epoch"] = int(result.get("execution_epoch") or 0)
    result["snapshot"] = copy.deepcopy(result.get("snapshot"))
    return result


def get_worker_mirror(database, scope):
    """Return the current persisted Worker mirror for one scope."""
    def read(connection):
        row = connection.execute(
            "SELECT h.*,s.snapshot FROM master_control.worker_mirror_heads h "
            "JOIN master_control.worker_mirror_snapshots s ON s.scope=h.scope "
            "AND s.authority_epoch=h.authority_epoch "
            "AND s.scope_revision=h.scope_revision AND s.execution_epoch=h.execution_epoch WHERE h.scope=%s",
            (str(scope),),
        ).fetchone()
        return _mirror_result(row)

    return database["transaction"](read, isolation_level="REPEATABLE READ")


def list_worker_mirrors(database):
    """Return all current detached Worker mirrors in stable scope order."""
    def read(connection):
        rows = connection.execute(
            "SELECT h.*,s.snapshot FROM master_control.worker_mirror_heads h "
            "JOIN master_control.worker_mirror_snapshots s ON s.scope=h.scope "
            "AND s.authority_epoch=h.authority_epoch "
            "AND s.scope_revision=h.scope_revision AND s.execution_epoch=h.execution_epoch ORDER BY h.scope"
        ).fetchall()
        return tuple(_mirror_result(row) for row in rows)

    return database["transaction"](read, isolation_level="REPEATABLE READ")


def _mirror_transition_error(current, values):
    if not current:
        return None
    current_epoch = int(current.get("authority_epoch") or 0)
    current_revision = int(current.get("scope_revision") or 0)
    current_execution = int(current.get("execution_epoch") or 0)
    received_epoch = int(values["authority_epoch"])
    received_revision = int(values["scope_revision"])
    received_execution = int(values["execution_epoch"])
    if received_epoch < current_epoch or received_epoch > current_epoch + 1:
        return make_error(
            "authority_epoch_stale",
            "mirror authority epoch is outside the accepted transition",
            authority_epoch=current_epoch,
        )
    if received_epoch == current_epoch and received_revision < current_revision:
        return make_error(
            "stale_revision",
            "mirror scope revision is older than the persisted head",
            scope_revision=current_revision,
        )
    if received_execution < current_execution or received_execution > current_execution + 1:
        return make_error(
            "execution_epoch_stale",
            "mirror execution epoch is outside the accepted Registry transition",
            execution_epoch=current_execution,
        )
    if values["scope_kind"] != "cross" and received_execution != current_execution:
        return make_error(
            "execution_epoch_stale",
            "Node mirror cannot change its execution epoch",
            execution_epoch=current_execution,
        )
    same_revision = (
        received_epoch == current_epoch
        and received_revision == current_revision
    )
    if same_revision and received_execution != current_execution:
        return None
    if same_revision:
        if str(values["fingerprint"]) != str(current.get("fingerprint") or ""):
            return make_error(
                "stale_revision",
                "mirror content conflicts with the persisted scope revision",
                scope_revision=current_revision,
            )
        if (
            str(values.get("owner_node_id") or "")
            != str(current.get("owner_node_id") or "")
            or str(values.get("source_node_id") or "")
            != str(current.get("source_node_id") or "")
        ):
            return make_error(
                "execution_epoch_stale",
                "mirror owner changed without a Registry execution epoch",
                execution_epoch=current_execution,
            )
        return make_success(duplicate=True, mirror=_mirror_result(current))
    return None

def persist_worker_mirror(database, request, **kwargs):
    """Persist one validated immutable Worker snapshot and advance its mirror head."""
    values = copy.deepcopy(request)
    values.update(copy.deepcopy(kwargs))

    def write(connection):
        if not _leader_guard(connection, values):
            return make_error(
                "not_config_authority",
                "mirror persistence requires the active Registry leader",
                retryable=True,
            )
        current = connection.execute(
            "SELECT h.*,s.snapshot FROM master_control.worker_mirror_heads h "
            "LEFT JOIN master_control.worker_mirror_snapshots s ON s.scope=h.scope "
            "AND s.authority_epoch=h.authority_epoch "
            "AND s.scope_revision=h.scope_revision AND s.execution_epoch=h.execution_epoch WHERE h.scope=%s FOR UPDATE OF h",
            (values["scope"],),
        ).fetchone()
        transition = _mirror_transition_error(current, values)
        if transition is not None:
            return transition
        registry_head = None
        if values["scope_kind"] == "cross":
            registry_head = connection.execute(
                "SELECT * FROM master_control.cross_registry_heads "
                "WHERE scope=%s FOR UPDATE",
                (values["scope"],),
            ).fetchone()
            if registry_head and (
                str(registry_head.get("owner_node_id") or "")
                != str(values.get("owner_node_id") or "")
                or int(registry_head.get("execution_epoch") or 0)
                != int(values["execution_epoch"])
            ):
                return make_error(
                    "execution_epoch_stale",
                    "Cross mirror differs from the persisted Registry head",
                    execution_epoch=int(
                        registry_head.get("execution_epoch") or 0),
                )
        historical = connection.execute(
            "SELECT * FROM master_control.worker_mirror_snapshots "
            "WHERE scope=%s AND authority_epoch=%s AND scope_revision=%s "
            "AND execution_epoch=%s",
            (
                values["scope"], values["authority_epoch"],
                values["scope_revision"], values["execution_epoch"],
            ),
        ).fetchone()
        if historical:
            same = (
                str(historical.get("fingerprint") or "") == values["fingerprint"]
                and historical.get("snapshot") == values["snapshot"]
            )
            if not same:
                return make_error(
                    "stale_revision",
                    "immutable mirror history conflicts with received content",
                )
        else:
            connection.execute(
                "INSERT INTO master_control.worker_mirror_snapshots("
                "scope,authority_epoch,scope_revision,execution_epoch,fingerprint,"
                "contracts_version,type_schema_version,config_norm_version,"
                "source_node_id,snapshot) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
                (
                    values["scope"], values["authority_epoch"],
                    values["scope_revision"], values["execution_epoch"],
                    values["fingerprint"], values["contracts_version"],
                    values["type_schema_version"], jsonb(values["config_norm_version"]),
                    values["source_node_id"], jsonb(values["snapshot"]),
                ),
            )
        row = connection.execute(
            "INSERT INTO master_control.worker_mirror_heads("
            "scope,scope_kind,owner_node_id,source_node_id,authority_epoch,"
            "scope_revision,execution_epoch,fingerprint,contracts_version,"
            "type_schema_version,config_norm_version) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(scope) DO UPDATE SET scope_kind=EXCLUDED.scope_kind,"
            "owner_node_id=EXCLUDED.owner_node_id,source_node_id=EXCLUDED.source_node_id,"
            "authority_epoch=EXCLUDED.authority_epoch,"
            "scope_revision=EXCLUDED.scope_revision,"
            "execution_epoch=EXCLUDED.execution_epoch,fingerprint=EXCLUDED.fingerprint,"
            "contracts_version=EXCLUDED.contracts_version,"
            "type_schema_version=EXCLUDED.type_schema_version,"
            "config_norm_version=EXCLUDED.config_norm_version,"
            "updated_at=clock_timestamp() RETURNING *",
            (
                values["scope"], values["scope_kind"], values.get("owner_node_id"),
                values["source_node_id"], values["authority_epoch"],
                values["scope_revision"], values["execution_epoch"],
                values["fingerprint"], values["contracts_version"],
                values["type_schema_version"], jsonb(values["config_norm_version"]),
            ),
        ).fetchone()
        if values["scope_kind"] == "cross" and not registry_head:
            transition_id = "initial-mirror:" + str(uuid.uuid4())
            proof = {
                "initial_mirror": True,
                "scope": values["scope"],
                "owner_node_id": values["owner_node_id"],
                "execution_epoch": values["execution_epoch"],
                "fingerprint": values["fingerprint"],
            }
            connection.execute(
                "INSERT INTO master_control.cross_registry_heads("
                "scope,owner_node_id,execution_epoch,transition_id,activation_proof) "
                "VALUES (%s,%s,%s,%s,%s)",
                (
                    values["scope"], values["owner_node_id"],
                    values["execution_epoch"], transition_id, jsonb(proof),
                ),
            )
        audit_id = str(uuid.uuid4())
        request_id = str(
            values.get("mirror_id")
            or "mirror:{}:{}:{}".format(
                values["scope"], values["authority_epoch"],
                values["scope_revision"]))
        connection.execute(
            "INSERT INTO master_control.audit_log("
            "audit_id,request_id,principal_subject,instance_id,site_id,"
            "resource,action,decision,reason_code,details) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,'ALLOW','accepted',%s)",
            (
                audit_id, request_id,
                "worker:" + str(values["source_node_id"]),
                values["instance_id"], values["site_id"], values["scope"],
                "worker_mirror.persist",
                jsonb({
                    "authority_epoch": values["authority_epoch"],
                    "scope_revision": values["scope_revision"],
                    "execution_epoch": values["execution_epoch"],
                    "fingerprint": values["fingerprint"],
                }),
            ),
        )
        result = _mirror_result(row)
        result["snapshot"] = copy.deepcopy(values["snapshot"])
        return make_success(
            duplicate=bool(historical), mirror=result, audit_id=audit_id)

    return database["transaction"](write, isolation_level="SERIALIZABLE")


def get_registry_head(database, scope):
    """Return the current Cross Registry head."""
    def read(connection):
        row = connection.execute(
            "SELECT * FROM master_control.cross_registry_heads WHERE scope=%s",
            (str(scope),),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](read, isolation_level="READ COMMITTED")


def persist_registry_transition(database, request, **kwargs):
    """Issue exactly one next Cross execution epoch after a durable freeze proof."""
    values = copy.deepcopy(request)
    values.update(copy.deepcopy(kwargs))

    def write(connection):
        if not _leader_guard(connection, values):
            return make_error(
                "not_config_authority",
                "Registry transition requires the active Registry leader",
                retryable=True,
            )
        current = connection.execute(
            "SELECT * FROM master_control.cross_registry_heads "
            "WHERE scope=%s FOR UPDATE",
            (values["scope"],),
        ).fetchone()
        previous_epoch = int(current.get("execution_epoch") or 0) if current else -1
        previous_owner = str(current.get("owner_node_id") or "") if current else ""
        expected_epoch = previous_epoch + 1
        if int(values["execution_epoch"]) != expected_epoch:
            return make_error(
                "execution_epoch_stale",
                "Registry may issue only the next execution epoch",
                execution_epoch=previous_epoch,
            )
        if current and str(values["old_owner_node_id"]) != previous_owner:
            return make_error(
                "execution_epoch_stale",
                "Registry transition old owner differs from the persisted owner",
            )
        proof = values.get("activation_proof") or {}
        if not proof.get("old_owner_frozen"):
            return make_error(
                "execution_epoch_stale",
                "Registry transition requires a durable old-owner freeze proof",
            )
        connection.execute(
            "INSERT INTO master_control.cross_registry_transitions("
            "transition_id,scope,old_owner_node_id,new_owner_node_id,"
            "previous_execution_epoch,execution_epoch,state,activation_proof) "
            "VALUES (%s,%s,%s,%s,%s,%s,'registry_persisted',%s)",
            (
                values["transition_id"], values["scope"],
                values["old_owner_node_id"], values["new_owner_node_id"],
                previous_epoch, values["execution_epoch"],
                jsonb(proof),
            ),
        )
        row = connection.execute(
            "INSERT INTO master_control.cross_registry_heads("
            "scope,owner_node_id,execution_epoch,transition_id,activation_proof) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(scope) DO UPDATE SET "
            "owner_node_id=EXCLUDED.owner_node_id,"
            "execution_epoch=EXCLUDED.execution_epoch,"
            "transition_id=EXCLUDED.transition_id,"
            "activation_proof=EXCLUDED.activation_proof,"
            "updated_at=clock_timestamp() RETURNING *",
            (
                values["scope"], values["new_owner_node_id"],
                values["execution_epoch"], values["transition_id"], jsonb(proof),
            ),
        ).fetchone()
        return make_success(registry_head=dict(row), state="registry_persisted")

    return database["transaction"](write, isolation_level="SERIALIZABLE")


def _save_flow(database, table_name, identity_name, record, **kwargs):
    values = copy.deepcopy(kwargs)
    values["record"] = copy.deepcopy(record)
    identity = str(record[identity_name])
    state = str(record["state"])
    allowed_tables = {
        "authority_transfers": "transfer_id",
        "worker_restores": "restore_id",
    }
    if allowed_tables.get(table_name) != identity_name:
        raise ValueError("Authority flow table is not allowed")

    def write(connection):
        if not _leader_guard(connection, values):
            return make_error(
                "not_config_authority",
                "Authority flow persistence requires the active Registry leader",
                retryable=True,
            )
        query = (
            "INSERT INTO master_control." + table_name + "(" + identity_name
            + ",scope,state,record) VALUES (%s,%s,%s,%s) ON CONFLICT("
            + identity_name + ") DO UPDATE SET state=EXCLUDED.state,"
            "record=EXCLUDED.record,updated_at=clock_timestamp() RETURNING *"
        )
        row = connection.execute(
            query,
            (identity, record["scope"], state, jsonb(record)),
        ).fetchone()
        return make_success(record=copy.deepcopy(row.get("record")), state=state)

    return database["transaction"](write, isolation_level="SERIALIZABLE")


def get_authority_transfer(database, transfer_id):
    """Return one durable Authority transfer record."""
    def read(connection):
        row = connection.execute(
            "SELECT record FROM master_control.authority_transfers WHERE transfer_id=%s",
            (str(transfer_id),),
        ).fetchone()
        return copy.deepcopy(row.get("record")) if row else None

    return database["transaction"](read, isolation_level="READ COMMITTED")


def get_worker_restore(database, restore_id):
    """Return one durable Worker restore record."""
    def read(connection):
        row = connection.execute(
            "SELECT record FROM master_control.worker_restores WHERE restore_id=%s",
            (str(restore_id),),
        ).fetchone()
        return copy.deepcopy(row.get("record")) if row else None

    return database["transaction"](read, isolation_level="READ COMMITTED")

def save_authority_transfer(database, record, **kwargs):
    """Persist one complete controlled Authority transfer state."""
    return _save_flow(
        database, "authority_transfers", "transfer_id", record, **kwargs)


def save_worker_restore(database, record, **kwargs):
    """Persist one quarantined Worker restore state."""
    return _save_flow(
        database, "worker_restores", "restore_id", record, **kwargs)


def build_master_mirror_capabilities(database):
    """Expose narrow Mirror, Registry and transition persistence ports."""
    return {
        "get_worker_mirror": partial(get_worker_mirror, database),
        "list_worker_mirrors": partial(list_worker_mirrors, database),
        "persist_worker_mirror": partial(persist_worker_mirror, database),
        "get_registry_head": partial(get_registry_head, database),
        "persist_registry_transition": partial(persist_registry_transition, database),
        "get_authority_transfer": partial(get_authority_transfer, database),
        "get_worker_restore": partial(get_worker_restore, database),
        "save_authority_transfer": partial(save_authority_transfer, database),
        "save_worker_restore": partial(save_worker_restore, database),
    }

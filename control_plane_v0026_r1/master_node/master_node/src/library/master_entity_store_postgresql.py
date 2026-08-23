# -*- coding: utf-8 -*-

"""Atomic PostgreSQL entity, scope, rollout and proposal persistence."""

import copy
import hashlib
import uuid

from functools import partial

from src.library._generic_system_contract_library import entity_registry
from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library._generic_type_library import fingerprint
from src.library._generic_system_contract_library.config_sharding_contracts import diff_configs
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


def _record_index(records):
    return {
        (record["section"], record["entity_key"]): record
        for record in records
    }


def _current_records(connection):
    rows = connection.execute(
        "SELECT payload FROM master_control.config_entities "
        "WHERE deleted=false ORDER BY section,entity_key"
    ).fetchall()
    return tuple(copy.deepcopy(row["payload"]) for row in rows)


def _config_from_records(records):
    return entity_registry.materialize(records) if records else None


def _scope_heads(connection):
    rows = connection.execute(
        "SELECT * FROM master_control.scope_heads ORDER BY scope"
    ).fetchall()
    return {row["scope"]: dict(row) for row in rows}


def _scope_snapshots(connection):
    rows = connection.execute(
        "SELECT h.*,s.canonical_config FROM master_control.scope_heads h "
        "JOIN master_control.scope_snapshots s ON s.scope=h.scope "
        "AND s.scope_revision=h.scope_revision ORDER BY h.scope"
    ).fetchall()
    return {
        row["scope"]: {
            "scope": row["scope"],
            "scope_kind": row["scope_kind"],
            "owner_node_id": row.get("owner_node_id"),
            "scope_revision": int(row["scope_revision"]),
            "global_revision": int(row["global_revision"]),
            "fingerprint": row["fingerprint"],
            "contracts_version": row["contracts_version"],
            "execution_epoch": int(row["execution_epoch"]),
            "config": copy.deepcopy(row["canonical_config"]),
        }
        for row in rows
    }


def _activations(connection):
    rows = connection.execute(
        "SELECT * FROM master_control.scope_activations ORDER BY entity_key"
    ).fetchall()
    return {row["entity_key"]: dict(row) for row in rows}


def load_state(database, _site_id=None):
    """Load the complete heap rebuild source in one repeatable transaction."""
    def read(connection):
        authority = connection.execute(
            "SELECT * FROM master_control.authority_state ORDER BY site_id LIMIT 1"
        ).fetchone()
        records = _current_records(connection)
        config = _config_from_records(records)
        revision = int(authority.get("config_revision") or 0) if authority else 0
        return {
            "global": {
                "global_revision": revision,
                "revision": revision,
                "fingerprint": authority.get("config_fingerprint") if authority else None,
                "config_data": config,
            } if config is not None else None,
            "scopes": _scope_snapshots(connection),
            "activations": _activations(connection),
        }

    return database["transaction"](read, isolation_level="REPEATABLE READ")


def get_scope_heads(database):
    """Return current scope head metadata."""
    return database["transaction"](_scope_heads, isolation_level="READ COMMITTED")


def get_scope_snapshots(database):
    """Return all current detached scope snapshots."""
    return database["transaction"](_scope_snapshots, isolation_level="REPEATABLE READ")


def get_activations(database):
    """Return current execution activation fences."""
    return database["transaction"](_activations, isolation_level="READ COMMITTED")


def _historical_records(connection, revision):
    rows = connection.execute(
        "SELECT DISTINCT ON (v.section,v.entity_key) v.section,v.entity_key,"
        "v.after_payload,c.global_revision,v.op_index "
        "FROM master_control.config_entity_versions v "
        "JOIN master_control.config_commits c ON c.commit_id=v.commit_id "
        "WHERE c.global_revision<=%s "
        "ORDER BY v.section,v.entity_key,c.global_revision DESC,v.op_index DESC",
        (revision,),
    ).fetchall()
    return tuple(copy.deepcopy(row["after_payload"]) for row in rows if row.get("after_payload"))


def get_config(database, _site_id=None, **kwargs):
    """Read current or historical global config reconstructed from entity versions."""
    requested = kwargs.get("revision")

    def read(connection):
        if requested is None:
            authority = connection.execute(
                "SELECT config_revision,config_fingerprint,authority_term "
                "FROM master_control.authority_state ORDER BY site_id LIMIT 1"
            ).fetchone()
            revision = int(authority.get("config_revision") or 0) if authority else 0
            records = _current_records(connection)
            config = _config_from_records(records)
            return {
                "revision": revision,
                "global_revision": revision,
                "fingerprint": authority.get("config_fingerprint") if authority else None,
                "authority_term": int(authority.get("authority_term") or 0) if authority else 0,
                "config_data": config,
            } if config is not None else None
        revision = int(requested)
        commit = connection.execute(
            "SELECT * FROM master_control.config_commits WHERE global_revision=%s",
            (revision,),
        ).fetchone()
        if not commit:
            return None
        config = _config_from_records(_historical_records(connection, revision))
        return {
            "revision": revision,
            "global_revision": revision,
            "fingerprint": fingerprint(config),
            "authority_term": None,
            "config_data": config,
            "created_at": commit.get("committed_at"),
            "created_by": commit.get("source_node_id") or commit.get("source_kind"),
        }

    return database["transaction"](read, isolation_level="REPEATABLE READ")


def get_legacy_config(database, site_id):
    """Read the final immutable v0011 snapshot retained as an audit source."""
    def read(connection):
        relation = connection.execute(
            "SELECT to_regclass('master_control.legacy_config_snapshots_v1') AS value"
        ).fetchone()
        if not relation or not relation.get("value"):
            return None
        row = connection.execute(
            "SELECT site_id,revision,fingerprint,config_data,authority_term,"
            "created_at,created_by FROM master_control.legacy_config_snapshots_v1 "
            "WHERE site_id=%s ORDER BY revision DESC LIMIT 1",
            (site_id,),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](read, isolation_level="REPEATABLE READ")


def config_history(database, _site_id=None, **kwargs):
    """Return bounded immutable global commit history."""
    limit = min(200, max(1, int(kwargs.get("limit", 50))))

    def read(connection):
        rows = connection.execute(
            "SELECT commit_id,operation_id,global_revision,source_kind,source_node_id,"
            "base_global_revision,status,result_payload,committed_at "
            "FROM master_control.config_commits ORDER BY global_revision DESC LIMIT %s",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    return database["transaction"](read, isolation_level="READ COMMITTED")


def _insert_commit(connection, values, result):
    connection.execute(
        "INSERT INTO master_control.config_commits("
        "commit_id,operation_id,global_revision,source_kind,source_node_id,"
        "base_global_revision,status,request_payload,result_payload) "
        "VALUES (%s,%s,%s,%s,%s,%s,'accepted',%s,%s)",
        (
            result["commit_id"], values["operation_id"], result["global_revision"],
            values.get("source_kind") or "engineering",
            values.get("source_node_id"), values.get("base_revision"),
            jsonb(values.get("audit_request") or {}), jsonb(result),
        ),
    )


def _persist_entities(connection, values, commit_id, global_revision):
    current_records = _current_records(connection)
    operations = diff_configs(
        _config_from_records(current_records) if current_records else values["empty_config"],
        values["config"],
        allow_legacy_v5=values.get("source_kind") == "legacy_import",
    ) if current_records else tuple({
        "op": "upsert_entity",
        "section": record["section"],
        "entity_key": record["entity_key"],
        "value": record,
    } for record in entity_registry.decompose(values["config"]))
    current = _record_index(current_records)
    for index, operation in enumerate(operations):
        identity = (operation["section"], operation["entity_key"])
        before = current.get(identity)
        after = operation.get("value") if operation["op"] == "upsert_entity" else None
        connection.execute(
            "INSERT INTO master_control.config_entity_versions("
            "commit_id,op_index,section,entity_key,operation,before_payload,after_payload) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s)",
            (
                commit_id, index, operation["section"], operation["entity_key"],
                operation["op"], jsonb(before) if before is not None else None,
                jsonb(after) if after is not None else None,
            ),
        )
        connection.execute(
            "INSERT INTO master_control.config_entities("
            "section,entity_key,entity_revision,payload,deleted,last_commit_id,updated_by) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT(section,entity_key) DO UPDATE SET "
            "entity_revision=EXCLUDED.entity_revision,payload=EXCLUDED.payload,"
            "deleted=EXCLUDED.deleted,last_commit_id=EXCLUDED.last_commit_id,"
            "updated_by=EXCLUDED.updated_by,updated_at=clock_timestamp()",
            (
                operation["section"], operation["entity_key"], global_revision,
                jsonb(after if after is not None else {}), after is None,
                commit_id, values["principal_subject"],
            ),
        )
    return operations


def _persist_scope(connection, snapshot, commit_id):
    if not snapshot.get("changed"):
        return None
    connection.execute(
        "INSERT INTO master_control.scope_snapshots("
        "scope,scope_revision,global_revision,fingerprint,canonical_config) "
        "VALUES (%s,%s,%s,%s,%s)",
        (
            snapshot["scope"], snapshot["scope_revision"],
            snapshot["global_revision"], snapshot["fingerprint"],
            jsonb(snapshot["config"]),
        ),
    )
    connection.execute(
        "INSERT INTO master_control.scope_heads("
        "scope,scope_kind,owner_node_id,scope_revision,global_revision,fingerprint,"
        "contracts_version,execution_epoch) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
        "ON CONFLICT(scope) DO UPDATE SET scope_kind=EXCLUDED.scope_kind,"
        "owner_node_id=EXCLUDED.owner_node_id,scope_revision=EXCLUDED.scope_revision,"
        "global_revision=EXCLUDED.global_revision,fingerprint=EXCLUDED.fingerprint,"
        "contracts_version=EXCLUDED.contracts_version,execution_epoch=EXCLUDED.execution_epoch,"
        "updated_at=clock_timestamp()",
        (
            snapshot["scope"], snapshot["scope_kind"], snapshot["owner_node_id"],
            snapshot["scope_revision"], snapshot["global_revision"],
            snapshot["fingerprint"], snapshot["contracts_version"],
            snapshot["execution_epoch"],
        ),
    )
    for index, operation in enumerate(snapshot.get("operations") or ()):
        connection.execute(
            "INSERT INTO master_control.scope_ops("
            "scope,target_scope_revision,op_index,operation,section,entity_key,"
            "json_pointer,value,expected_value_hash,value_hash,commit_id) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)",
            (
                snapshot["scope"], snapshot["scope_revision"], index,
                operation["op"], operation["section"], operation["entity_key"],
                operation.get("json_pointer"),
                jsonb(operation.get("value")) if "value" in operation else None,
                operation.get("expected_value_hash"), operation.get("value_hash"),
                commit_id,
            ),
        )


def _runtime_delivery_generation(node):
    """Return a non-secret, stable token for one authenticated Worker runtime."""
    runtime_nonce = str(node.get("runtime_nonce") or "").strip() \
        if isinstance(node, dict) else ""
    if not runtime_nonce:
        return None
    return hashlib.sha256(runtime_nonce.encode("utf-8")).hexdigest()


def _rollout_identity(term, snapshot, delivery_generation=None):
    identity = "rollout:{}:{}:{}:{}".format(
        term, snapshot["scope"], snapshot["scope_revision"],
        snapshot["fingerprint"])
    if delivery_generation:
        return identity + ":delivery:" + str(delivery_generation)
    return identity


def _persist_rollout(connection, values, snapshot):
    if not snapshot.get("changed"):
        return None
    owner = snapshot.get("owner_node_id")
    requested = tuple(values.get("targets") or ())
    if requested and owner not in requested:
        return None
    rollout_id = _rollout_identity(values["authority_term"], snapshot)
    connection.execute(
        "UPDATE master_control.shard_rollouts SET state='superseded',"
        "updated_at=clock_timestamp() WHERE node_id=%s AND scope=%s "
        "AND state NOT IN ('converged','failed','superseded')",
        (owner, snapshot["scope"]),
    )
    connection.execute(
        "INSERT INTO master_control.shard_rollouts("
        "rollout_id,node_id,scope,target_scope_revision,target_fingerprint,"
        "coordinator_term,state,next_attempt_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,'staged',clock_timestamp()) "
        "ON CONFLICT(node_id,scope,target_scope_revision) DO NOTHING",
        (
            rollout_id, owner, snapshot["scope"], snapshot["scope_revision"],
            snapshot["fingerprint"], values["authority_term"],
        ),
    )
    return rollout_id


def _persist_activations(connection, values, commit_id):
    activations = values.get("activations", {})
    active_keys = list(sorted(activations))
    connection.execute(
        "UPDATE master_control.scope_activations SET state='inactive',"
        "execution_epoch=execution_epoch+1,last_commit_id=%s,"
        "updated_at=clock_timestamp() WHERE state<>'inactive' "
        "AND NOT (entity_key=ANY(%s))",
        (commit_id, active_keys),
    )
    for entity_key, activation in sorted(activations.items()):
        connection.execute(
            "INSERT INTO master_control.scope_activations("
            "entity_key,active_scope,execution_epoch,state,last_commit_id) "
            "VALUES (%s,%s,%s,%s,%s) ON CONFLICT(entity_key) DO UPDATE SET "
            "active_scope=EXCLUDED.active_scope,execution_epoch=EXCLUDED.execution_epoch,"
            "state=EXCLUDED.state,last_commit_id=EXCLUDED.last_commit_id,"
            "updated_at=clock_timestamp()",
            (
                entity_key, activation["active_scope"], activation["execution_epoch"],
                activation.get("state") or "active", commit_id,
            ),
        )


def commit_config(database, request):
    """Commit global entities and all derived scope changes atomically."""
    values = copy.deepcopy(request)

    def write(connection):
        authority = _leader_guard(connection, values)
        if not authority:
            return make_error("not_config_authority", "active authority lease is not held")
        previous = connection.execute(
            "SELECT result_payload FROM master_control.config_commits WHERE operation_id=%s",
            (values["operation_id"],),
        ).fetchone()
        if previous:
            result = copy.deepcopy(previous["result_payload"])
            result["duplicate"] = True
            return result
        current = int(authority.get("config_revision") or 0)
        if int(values["base_revision"]) != current:
            return make_error("stale_revision", "base revision differs", current_revision=current)
        global_revision = current + 1
        materialized = values["materialization"]
        expected_fingerprint = fingerprint(values["config"])
        if int(materialized.get("global_revision") or -1) != global_revision:
            return make_error("materialization", "materialized global revision differs")
        if materialized.get("global_fingerprint") != expected_fingerprint:
            return make_error("materialization", "materialized global fingerprint differs")
        commit_id = str(values.get("commit_id") or uuid.uuid4())
        changed_scopes = [
            snapshot for snapshot in materialized["scopes"].values()
            if snapshot.get("changed")
        ]
        rollout_ids = tuple(
            _rollout_identity(values["authority_term"], snapshot)
            for snapshot in changed_scopes
            if not values.get("targets") or snapshot.get("owner_node_id") in values["targets"]
        )
        targets = tuple(sorted({
            snapshot.get("owner_node_id") for snapshot in changed_scopes
            if snapshot.get("owner_node_id")
            and (not values.get("targets") or snapshot.get("owner_node_id") in values["targets"])
        }))
        scope_targets = {
            snapshot["scope"]: {
                "scope_revision": int(snapshot["scope_revision"]),
                "fingerprint": snapshot["fingerprint"],
                "owner_node_id": snapshot.get("owner_node_id"),
                "execution_epoch": int(snapshot["execution_epoch"]),
            }
            for snapshot in changed_scopes
        }
        result = make_success(
            commit_id=commit_id,
            revision=global_revision,
            global_revision=global_revision,
            fingerprint=expected_fingerprint,
            authority_term=int(values["authority_term"]),
            rollout_id=rollout_ids[0] if rollout_ids else None,
            rollout_ids=rollout_ids,
            scope_targets=scope_targets,
            targets=targets,
            changed=True,
        )
        _insert_commit(connection, values, result)
        _persist_entities(connection, values, commit_id, global_revision)
        for snapshot in materialized["scopes"].values():
            _persist_scope(connection, snapshot, commit_id)
            _persist_rollout(connection, values, snapshot)
        for scope in materialized.get("removed_scopes") or ():
            connection.execute(
                "DELETE FROM master_control.scope_heads WHERE scope=%s", (scope,))
            connection.execute(
                "UPDATE master_control.shard_rollouts SET state='superseded',"
                "updated_at=clock_timestamp() WHERE scope=%s "
                "AND state NOT IN ('converged','failed','superseded')", (scope,))
        _persist_activations(connection, values, commit_id)
        connection.execute(
            "UPDATE master_control.authority_state SET config_revision=%s,"
            "config_fingerprint=%s,updated_at=clock_timestamp() WHERE site_id=%s",
            (global_revision, expected_fingerprint, values["site_id"]),
        )
        return result

    return database["transaction"](write)


def get_scope(database, scope, **kwargs):
    """Read one current or historical scope snapshot."""
    revision = kwargs.get("revision")

    def read(connection):
        selected = revision
        if selected is None:
            head = connection.execute(
                "SELECT scope_revision FROM master_control.scope_heads WHERE scope=%s",
                (scope,),
            ).fetchone()
            selected = head.get("scope_revision") if head else None
        if selected is None:
            return None
        row = connection.execute(
            "SELECT s.*,h.scope_kind,h.owner_node_id,h.contracts_version,h.execution_epoch "
            "FROM master_control.scope_snapshots s "
            "LEFT JOIN master_control.scope_heads h ON h.scope=s.scope "
            "WHERE s.scope=%s AND s.scope_revision=%s",
            (scope, selected),
        ).fetchone()
        if not row:
            return None
        result = dict(row)
        result["config"] = result.pop("canonical_config")
        return result

    return database["transaction"](read, isolation_level="READ COMMITTED")


def get_scope_ops(database, scope, base_revision, target_revision):
    """Read a continuous bounded scope patch range."""
    start = int(base_revision) + 1
    target = int(target_revision)
    if target < start:
        return ()

    def read(connection):
        rows = connection.execute(
            "SELECT * FROM master_control.scope_ops WHERE scope=%s "
            "AND target_scope_revision BETWEEN %s AND %s "
            "ORDER BY target_scope_revision,op_index",
            (scope, start, target),
        ).fetchall()
        revisions = {int(row["target_scope_revision"]) for row in rows}
        if revisions != set(range(start, target + 1)):
            return None
        return tuple(dict(row) for row in rows)

    return database["transaction"](read, isolation_level="READ COMMITTED")


def due_rollouts(database, **kwargs):
    """Claim a bounded due rollout set after verifying the authority fence."""
    limit = min(256, max(1, int(kwargs.get("limit", 64))))

    def write(connection):
        authority = _leader_guard(connection, kwargs)
        if not authority:
            return []
        rows = connection.execute(
            "SELECT * FROM master_control.shard_rollouts "
            "WHERE state IN ('staged','retry_wait') "
            "AND COALESCE(next_attempt_at,clock_timestamp())<=clock_timestamp() "
            "ORDER BY created_at FOR UPDATE SKIP LOCKED LIMIT %s",
            (limit,),
        ).fetchall()
        return [dict(row) for row in rows]

    return database["transaction"](write, isolation_level="READ COMMITTED")


def _rollout_transition_allowed(current, target):
    if current == "failed":
        return target in ("failed", "superseded")
    if current == "superseded":
        return target == "superseded"
    if current == "converged":
        return target in ("converged", "superseded")
    if current == "applied":
        return target in ("applied", "converged", "superseded")
    return True


def _rollout_rearm_allowed(existing, authority_term):
    """Allow authority-fenced recovery that cannot reuse a terminal delivery."""
    state = str(existing.get("state") or "")
    detail = existing.get("detail") if isinstance(existing.get("detail"), dict) else {}
    if state == "failed":
        return detail.get("code") == "target_missing"
    return (
        state in ("staged", "sending", "retry_wait")
        and int(existing.get("coordinator_term") or 0) != int(authority_term)
    )


def _replica_anchor(node, head):
    prefix = "cross" if str(head.get("scope_kind") or "") == "cross" else "node"
    return {
        "revision": int(node.get(prefix + "_replica_revision") or 0),
        "fingerprint": str(node.get(prefix + "_replica_fingerprint") or ""),
    }


def _rollout_rearm_plan(existing, authority_term, node, head):
    """Plan one safe replay after authority recovery or Worker runtime loss.

    A Worker runtime generation is a recovery boundary.  When the authenticated
    Worker reports a lower replica revision under a new runtime nonce, the old
    rollout/outbox identity must never be reused: the previous message may be
    terminal and the local replica was explicitly lost.  Equal-or-newer
    divergent replicas remain fail-closed and require operator intervention.
    """
    state = str(existing.get("state") or "")
    if state == "superseded":
        return None
    generation = _runtime_delivery_generation(node)
    detail = existing.get("detail") if isinstance(existing.get("detail"), dict) else {}
    anchor = _replica_anchor(node, head)
    target_revision = int(head.get("scope_revision") or 0)
    target_fingerprint = str(head.get("fingerprint") or "")

    authority_recovery = _rollout_rearm_allowed(existing, authority_term)
    runtime_recovery = bool(
        generation
        and generation != str(detail.get("delivery_generation") or "")
        and anchor["fingerprint"] != target_fingerprint
        and anchor["revision"] < target_revision
    )
    if not authority_recovery and not runtime_recovery:
        return None

    reason = "authority_term_recovery" if authority_recovery \
        else "worker_runtime_replica_recovery"
    rollout_id = _rollout_identity(
        authority_term,
        head,
        generation,
    )
    replay_detail = {
        "rearm_reason": reason,
        "rearmed_from": state,
        "previous_rollout_id": str(existing.get("rollout_id") or ""),
        "observed_replica_revision": anchor["revision"],
        "observed_replica_fingerprint": anchor["fingerprint"],
    }
    if generation:
        replay_detail["delivery_generation"] = generation
    previous_code = detail.get("code")
    if previous_code:
        replay_detail["previous_code"] = str(previous_code)
    return {
        "rollout_id": rollout_id,
        "detail": replay_detail,
        "reason": reason,
    }


def mark_rollout(database, rollout_id, **kwargs):
    """Apply one monotone rollout transition and retain bounded detail."""
    state = str(kwargs.get("state") or "sending")
    allowed = (
        "staged", "sending", "received", "applied", "converged",
        "retry_wait", "failed", "superseded",
    )
    if state not in allowed:
        return make_error("invalid_payload", "rollout state is invalid")
    retry_s = max(0.0, float(kwargs.get("retry_s", 0.0)))

    def write(connection):
        if kwargs.get("site_id") is not None and not _leader_guard(connection, kwargs):
            return make_error(
                "not_config_authority", "rollout transition lost its authority lease")
        current = connection.execute(
            "SELECT * FROM master_control.shard_rollouts WHERE rollout_id=%s FOR UPDATE",
            (rollout_id,),
        ).fetchone()
        if not current:
            return make_error("not_found", "rollout is unknown")
        if not _rollout_transition_allowed(str(current["state"]), state):
            return make_success(
                rollout=dict(current), duplicate=True, ignored=True)
        row = connection.execute(
            "UPDATE master_control.shard_rollouts SET state=%s,"
            "attempts=attempts+%s,next_attempt_at=CASE WHEN %s>0 "
            "THEN clock_timestamp()+(%s*interval '1 second') ELSE NULL END,"
            "transfer_id=COALESCE(%s,transfer_id),detail=detail || %s,"
            "updated_at=clock_timestamp() "
            "WHERE rollout_id=%s RETURNING *",
            (
                state, int(kwargs.get("attempted", False)), retry_s, retry_s,
                kwargs.get("transfer_id"), jsonb(kwargs.get("detail") or {}), rollout_id,
            ),
        ).fetchone()
        return make_success(rollout=dict(row)) if row else make_error(
            "not_found", "rollout is unknown")

    return database["transaction"](write, isolation_level="READ COMMITTED")


def get_rollout(database, rollout_id):
    """Read one rollout for ACK correlation."""
    def read(connection):
        row = connection.execute(
            "SELECT * FROM master_control.shard_rollouts WHERE rollout_id=%s",
            (rollout_id,),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](read, isolation_level="READ COMMITTED")


def ensure_node_rollouts(database, node_id, authority_term):
    """Ensure current owned scopes have a rollout when replica anchors differ."""
    def write(connection):
        node = connection.execute(
            "SELECT * FROM master_control.nodes WHERE node_id=%s "
            "AND admission_state='APPROVED' AND desired_state='ACTIVE' FOR UPDATE",
            (node_id,),
        ).fetchone()
        if not node:
            return 0
        capabilities = node.get("capabilities") or {}
        features = capabilities.get("features") or {}
        heads = connection.execute(
            "SELECT * FROM master_control.scope_heads WHERE owner_node_id=%s ORDER BY scope",
            (node_id,),
        ).fetchall()
        inserted = 0
        for head in heads:
            cross = head["scope_kind"] == "cross"
            if cross and not (
                features.get("cross_runtime") and features.get("cross_full")
            ):
                continue
            observed_revision = int(
                (node.get("cross_replica_revision") if cross
                 else node.get("node_replica_revision")) or 0
            )
            observed_fingerprint = node.get("cross_replica_fingerprint") if cross \
                else node.get("node_replica_fingerprint")
            if (
                observed_revision == int(head["scope_revision"])
                and observed_fingerprint == head["fingerprint"]
            ):
                connection.execute(
                    "UPDATE master_control.shard_rollouts SET state='converged',"
                    "updated_at=clock_timestamp() WHERE node_id=%s AND scope=%s "
                    "AND target_scope_revision=%s AND target_fingerprint=%s "
                    "AND state NOT IN ('failed','superseded')",
                    (
                        node_id, head["scope"], head["scope_revision"],
                        head["fingerprint"],
                    ),
                )
                continue
            rollout_id = _rollout_identity(authority_term, head)
            existing = connection.execute(
                "SELECT * FROM master_control.shard_rollouts WHERE node_id=%s "
                "AND scope=%s AND target_scope_revision=%s FOR UPDATE",
                (node_id, head["scope"], head["scope_revision"]),
            ).fetchone()
            rearm = _rollout_rearm_plan(
                dict(existing), authority_term, dict(node), dict(head)) \
                if existing else None
            if rearm:
                row = connection.execute(
                    "UPDATE master_control.shard_rollouts SET rollout_id=%s,"
                    "coordinator_term=%s,target_fingerprint=%s,state='staged',"
                    "attempts=0,transfer_id=NULL,next_attempt_at=clock_timestamp(),"
                    "detail=%s,updated_at=clock_timestamp() "
                    "WHERE rollout_id=%s RETURNING rollout_id",
                    (
                        rearm["rollout_id"], authority_term, head["fingerprint"],
                        jsonb(rearm["detail"]), existing["rollout_id"],
                    ),
                ).fetchone()
            elif existing:
                row = None
            else:
                delivery_generation = _runtime_delivery_generation(dict(node))
                initial_detail = {
                    "delivery_generation": delivery_generation,
                    "rearm_reason": "initial_worker_delivery",
                } if delivery_generation else {}
                row = connection.execute(
                    "INSERT INTO master_control.shard_rollouts("
                    "rollout_id,node_id,scope,target_scope_revision,target_fingerprint,"
                    "coordinator_term,state,next_attempt_at,detail) "
                    "VALUES (%s,%s,%s,%s,%s,%s,'staged',clock_timestamp(),%s) "
                    "ON CONFLICT(node_id,scope,target_scope_revision) DO NOTHING "
                    "RETURNING rollout_id",
                    (
                        rollout_id, node_id, head["scope"], head["scope_revision"],
                        head["fingerprint"], authority_term, jsonb(initial_detail),
                    ),
                ).fetchone()
            inserted += int(bool(row))
        return inserted

    return database["transaction"](write, isolation_level="READ COMMITTED")


def converge_node_rollouts(database, node_id):
    """Converge current rollouts from independently reported replica fingerprints."""
    def write(connection):
        node = connection.execute(
            "SELECT * FROM master_control.nodes WHERE node_id=%s FOR UPDATE",
            (node_id,),
        ).fetchone()
        if not node:
            return 0
        rows = connection.execute(
            "UPDATE master_control.shard_rollouts r SET state='converged',"
            "updated_at=clock_timestamp() FROM master_control.scope_heads h "
            "WHERE r.node_id=%s AND r.scope=h.scope "
            "AND r.target_scope_revision=h.scope_revision "
            "AND r.target_scope_revision=CASE WHEN h.scope_kind='cross' "
            "THEN %s ELSE %s END "
            "AND r.target_fingerprint=CASE WHEN h.scope_kind='cross' "
            "THEN %s ELSE %s END AND r.state NOT IN ('failed','superseded','converged') "
            "RETURNING r.rollout_id",
            (
                node_id,
                int(node.get("cross_replica_revision") or 0),
                int(node.get("node_replica_revision") or 0),
                node.get("cross_replica_fingerprint"),
                node.get("node_replica_fingerprint"),
            ),
        ).fetchall()
        return len(rows)

    return database["transaction"](write, isolation_level="READ COMMITTED")


def converge_node_proposals(database, node_id):
    """Mark accepted proposals only after their exact scope baseline is reported."""
    def write(connection):
        node = connection.execute(
            "SELECT * FROM master_control.nodes WHERE node_id=%s FOR UPDATE",
            (node_id,),
        ).fetchone()
        if not node:
            return 0
        rows = connection.execute(
            "UPDATE master_control.worker_proposals p SET state='converged',"
            "result=p.result || jsonb_build_object('converged_scope_revision',"
            "CASE WHEN h.scope_kind='cross' THEN %s ELSE %s END,"
            "'converged_scope_fingerprint',CASE WHEN h.scope_kind='cross' "
            "THEN %s ELSE %s END),updated_at=clock_timestamp() "
            "FROM master_control.scope_heads h WHERE p.node_id=%s AND p.scope=h.scope "
            "AND p.state='accepted' "
            "AND (p.result->'scope_targets'->p.scope->>'scope_revision')::bigint="
            "CASE WHEN h.scope_kind='cross' THEN %s ELSE %s END "
            "AND p.result->'scope_targets'->p.scope->>'fingerprint'="
            "CASE WHEN h.scope_kind='cross' THEN %s ELSE %s END "
            "RETURNING p.proposal_id",
            (
                int(node.get("cross_replica_revision") or 0),
                int(node.get("node_replica_revision") or 0),
                node.get("cross_replica_fingerprint"),
                node.get("node_replica_fingerprint"), node_id,
                int(node.get("cross_replica_revision") or 0),
                int(node.get("node_replica_revision") or 0),
                node.get("cross_replica_fingerprint"),
                node.get("node_replica_fingerprint"),
            ),
        ).fetchall()
        return len(rows)

    return database["transaction"](write, isolation_level="READ COMMITTED")


def record_proposal(database, request):
    """Durably create or return one worker proposal."""
    values = copy.deepcopy(request)

    def write(connection):
        existing = connection.execute(
            "SELECT * FROM master_control.worker_proposals WHERE operation_id=%s FOR UPDATE",
            (values["operation_id"],),
        ).fetchone()
        if existing:
            same = (
                existing["node_id"] == values["node_id"]
                and existing["scope"] == values["scope"]
                and existing["operations"] == values["operations"]
            )
            return make_success(duplicate=True, proposal=dict(existing)) if same else make_error(
                "conflict", "proposal operation ID was reused with other content")
        connection.execute(
            "INSERT INTO master_control.worker_proposals("
            "proposal_id,node_id,scope,base_scope_revision,base_scope_fingerprint,"
            "operation_id,operations,state) VALUES (%s,%s,%s,%s,%s,%s,%s,'received')",
            (
                values["proposal_id"], values["node_id"], values["scope"],
                values["base_scope_revision"], values["base_scope_fingerprint"],
                values["operation_id"], jsonb(values["operations"]),
            ),
        )
        return make_success(duplicate=False, proposal_id=values["proposal_id"])

    return database["transaction"](write)


def complete_proposal(database, operation_id, state, result):
    """Persist the visible terminal or convergence proposal result."""
    def write(connection):
        row = connection.execute(
            "UPDATE master_control.worker_proposals SET state=%s,result=%s,"
            "updated_at=clock_timestamp() WHERE operation_id=%s RETURNING *",
            (state, jsonb(result), operation_id),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](write, isolation_level="READ COMMITTED")


def build_master_entity_capabilities(database):
    """Expose the narrow v2 persistence capabilities."""
    return {
        "commit_config": partial(commit_config, database),
        "get_config": partial(get_config, database),
        "get_legacy_config": partial(get_legacy_config, database),
        "config_history": partial(config_history, database),
        "load_state": partial(load_state, database),
        "get_scope_heads": partial(get_scope_heads, database),
        "get_scope_snapshots": partial(get_scope_snapshots, database),
        "get_activations": partial(get_activations, database),
        "get_scope": partial(get_scope, database),
        "get_scope_ops": partial(get_scope_ops, database),
        "due_rollouts": partial(due_rollouts, database),
        "mark_rollout": partial(mark_rollout, database),
        "get_rollout": partial(get_rollout, database),
        "ensure_node_rollouts": partial(ensure_node_rollouts, database),
        "converge_node_rollouts": partial(converge_node_rollouts, database),
        "converge_node_proposals": partial(converge_node_proposals, database),
        "record_proposal": partial(record_proposal, database),
        "complete_proposal": partial(complete_proposal, database),
    }

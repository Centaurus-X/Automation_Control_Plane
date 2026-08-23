# -*- coding: utf-8 -*-

# master_node/src/library/node_store_postgresql.py

"""Worker admission, health, desired state and controller ownership store."""

import copy
import uuid

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library.postgresql import jsonb


def register_node(database, request):
    """Register an unknown node as pending and preserve prior decisions."""
    values = copy.deepcopy(request)

    def write(connection):
        existing = connection.execute(
            "SELECT * FROM master_control.nodes WHERE site_id=%s AND node_id=%s FOR UPDATE",
            (values["site_id"], values["node_id"]),
        ).fetchone()
        identity = values.get("certificate_identity")
        identity_owner = connection.execute(
            "SELECT node_id FROM master_control.nodes WHERE site_id=%s "
            "AND certificate_identity=%s FOR UPDATE",
            (values["site_id"], identity),
        ).fetchone() if identity else None
        if identity_owner and identity_owner["node_id"] != values["node_id"]:
            return make_error("identity_mismatch", "worker certificate is bound to another node")
        if existing and existing.get("certificate_identity") not in (None, identity):
            connection.execute(
                "UPDATE master_control.nodes SET admission_state='QUARANTINED', "
                "desired_state='QUARANTINED', observed_state='QUARANTINED', "
                "updated_at=clock_timestamp() WHERE site_id=%s AND node_id=%s",
                (values["site_id"], values["node_id"]),
            )
            return make_error("identity_mismatch", "node certificate identity changed")
        connection.execute(
            "INSERT INTO master_control.nodes("
            "site_id, node_id, certificate_identity, software_version, protocol_version, "
            "authority_term, config_revision, config_fingerprint, capabilities, runtime_nonce,"
            "contracts_version,protocol_versions,observed_global_revision,"
            "node_replica_scope,node_replica_revision,node_replica_fingerprint,"
            "cross_replica_scope,cross_replica_revision,cross_replica_fingerprint,"
            "live_config_revision,pending_proposal_count,max_packet_size) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (site_id,node_id) DO UPDATE SET "
            "certificate_identity=COALESCE(nodes.certificate_identity,EXCLUDED.certificate_identity), "
            "software_version=EXCLUDED.software_version, protocol_version=EXCLUDED.protocol_version, "
            "authority_term=EXCLUDED.authority_term, config_revision=EXCLUDED.config_revision, "
            "config_fingerprint=EXCLUDED.config_fingerprint, capabilities=EXCLUDED.capabilities, "
            "runtime_nonce=EXCLUDED.runtime_nonce,contracts_version=EXCLUDED.contracts_version,"
            "protocol_versions=EXCLUDED.protocol_versions,"
            "observed_global_revision=EXCLUDED.observed_global_revision,"
            "node_replica_scope=EXCLUDED.node_replica_scope,"
            "node_replica_revision=EXCLUDED.node_replica_revision,"
            "node_replica_fingerprint=EXCLUDED.node_replica_fingerprint,"
            "cross_replica_scope=EXCLUDED.cross_replica_scope,"
            "cross_replica_revision=EXCLUDED.cross_replica_revision,"
            "cross_replica_fingerprint=EXCLUDED.cross_replica_fingerprint,"
            "live_config_revision=EXCLUDED.live_config_revision,"
            "pending_proposal_count=EXCLUDED.pending_proposal_count,"
            "max_packet_size=EXCLUDED.max_packet_size,last_seen_at=clock_timestamp(), "
            "updated_at=clock_timestamp()",
            (
                values["site_id"], values["node_id"], identity,
                values.get("software_version"), values.get("protocol_version"),
                int(values.get("authority_term") or 0), int(values.get("config_revision") or 0),
                values.get("config_fingerprint"), jsonb(values.get("capabilities") or {}),
                values.get("runtime_nonce"),
                values.get("contracts_version"), jsonb(values.get("protocol_versions") or ()),
                int(values.get("observed_global_revision") or 0),
                (values.get("node_replica") or {}).get("scope"),
                int((values.get("node_replica") or {}).get("revision") or 0),
                (values.get("node_replica") or {}).get("fingerprint"),
                (values.get("cross_replica") or {}).get("scope"),
                int((values.get("cross_replica") or {}).get("revision") or 0),
                (values.get("cross_replica") or {}).get("fingerprint"),
                int(values.get("live_config_revision") or 0),
                int(values.get("pending_proposal_count") or 0),
                int(values.get("max_packet_size") or 1048576),
            ),
        )
        current = connection.execute(
            "SELECT * FROM master_control.nodes WHERE site_id=%s AND node_id=%s",
            (values["site_id"], values["node_id"]),
        ).fetchone()
        if not existing:
            connection.execute(
                "INSERT INTO master_control.node_admissions("
                "admission_id, site_id, node_id, requested_identity, fingerprint, decision) "
                "VALUES (%s,%s,%s,%s,%s,'PENDING')",
                (
                    str(uuid.uuid4()), values["site_id"], values["node_id"], identity,
                    values.get("config_fingerprint"),
                ),
            )
        return make_success(node=dict(current))

    return database["transaction"](write)


def decide_admission(database, request):
    """Apply one explicit admission decision and audit-friendly reason."""
    values = copy.deepcopy(request)
    decision = str(values.get("decision") or "DENIED").upper()
    if decision not in ("APPROVED", "DENIED", "QUARANTINED"):
        return make_error("invalid_payload", "invalid admission decision")

    def write(connection):
        state = "ACTIVE" if decision == "APPROVED" else "QUARANTINED"
        row = connection.execute(
            "UPDATE master_control.nodes SET admission_state=%s, desired_state=%s, "
            "observed_state=CASE WHEN %s='APPROVED' THEN 'DEGRADED' ELSE 'QUARANTINED' END, "
            "updated_at=clock_timestamp() WHERE site_id=%s AND node_id=%s RETURNING *",
            (decision, state, decision, values["site_id"], values["node_id"]),
        ).fetchone()
        if not row:
            return make_error("not_found", "node is unknown")
        connection.execute(
            "UPDATE master_control.node_admissions SET decision=%s, reason=%s, "
            "decided_by=%s, decided_at=clock_timestamp() WHERE admission_id=("
            "SELECT admission_id FROM master_control.node_admissions "
            "WHERE site_id=%s AND node_id=%s AND decided_at IS NULL "
            "ORDER BY created_at DESC LIMIT 1)",
            (
                decision, values.get("reason"), values["principal_subject"],
                values["site_id"], values["node_id"],
            ),
        )
        return make_success(node=dict(row))

    return database["transaction"](write)


def _monotone_revision(current_revision, current_value, supplied_revision, supplied_value):
    """Advance one revision/value pair without accepting a stale rewrite."""
    current = int(current_revision or 0)
    if supplied_revision is None:
        return current, current_value
    supplied = int(supplied_revision or 0)
    if supplied < current:
        return current, current_value
    if supplied == current and current_value is not None:
        return current, current_value
    return supplied, supplied_value


def _monotone_replica(node, supplied, prefix):
    """Advance one independently versioned replica anchor."""
    scope_key = prefix + "_replica_scope"
    revision_key = prefix + "_replica_revision"
    fingerprint_key = prefix + "_replica_fingerprint"
    if not isinstance(supplied, dict):
        return node.get(scope_key), int(node.get(revision_key) or 0), node.get(fingerprint_key)
    revision, fingerprint = _monotone_revision(
        node.get(revision_key), node.get(fingerprint_key),
        supplied.get("revision"), supplied.get("fingerprint"),
    )
    if revision == int(node.get(revision_key) or 0) and node.get(scope_key) is not None:
        scope = node.get(scope_key)
    else:
        scope = supplied.get("scope")
    return scope, revision, fingerprint


def _runtime_generation_changed(node, values):
    supplied = str(values.get("runtime_nonce") or "").strip()
    current = str(node.get("runtime_nonce") or "").strip()
    return bool(supplied and supplied != current)


def _heartbeat_is_stale(node, values):
    """Reject reordered data inside one runtime generation."""
    if _runtime_generation_changed(node, values):
        return False
    if int(values.get("authority_term") or 0) < int(node.get("authority_term") or 0):
        return True
    if int(values.get("config_revision") or 0) < int(node.get("config_revision") or 0):
        return True
    for prefix in ("node", "cross"):
        supplied = values.get(prefix + "_replica")
        if not isinstance(supplied, dict):
            continue
        current = int(node.get(prefix + "_replica_revision") or 0)
        if int(supplied.get("revision") or 0) < current:
            return True
    return False


def _reported_replica(node, supplied, prefix, reset_generation):
    if not reset_generation:
        return _monotone_replica(node, supplied, prefix)
    supplied = supplied if isinstance(supplied, dict) else {}
    return (
        supplied.get("scope"),
        int(supplied.get("revision") or 0),
        str(supplied.get("fingerprint") or ""),
    )


def record_heartbeat(database, request):
    """Record immutable heartbeat history and update the current node view."""
    values = copy.deepcopy(request)
    capabilities = values.get("capabilities")
    encoded_capabilities = jsonb(capabilities) if isinstance(capabilities, dict) else None

    def write(connection):
        node = connection.execute(
            "SELECT * FROM master_control.nodes WHERE site_id=%s AND node_id=%s FOR UPDATE",
            (values["site_id"], values["node_id"]),
        ).fetchone()
        if not node or node.get("admission_state") != "APPROVED":
            return make_error("authorization_denied", "node is not admitted")
        inserted = connection.execute(
            "INSERT INTO master_control.node_heartbeats("
            "site_id,node_id,message_id,authority_term,config_revision,config_fingerprint,health) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s) ON CONFLICT DO NOTHING RETURNING heartbeat_id",
            (
                values["site_id"], values["node_id"], values["message_id"],
                int(values.get("authority_term") or 0), int(values.get("config_revision") or 0),
                values.get("config_fingerprint"), jsonb(values.get("health") or {}),
            ),
        ).fetchone()
        if not inserted:
            return make_success(duplicate=True, node=dict(node))
        supplied_term = int(values.get("authority_term") or 0)
        reset_generation = _runtime_generation_changed(node, values)
        if _heartbeat_is_stale(node, values):
            return make_success(updated=False, stale=True, node=dict(node))
        observed = str(values.get("observed_state") or "ACTIVE").upper()
        allowed = ("ACTIVE", "DEGRADED", "DRAINING", "MAINTENANCE")
        if observed not in allowed:
            observed = "DEGRADED"
        if reset_generation:
            config_revision = int(values.get("config_revision") or 0)
            config_fingerprint = values.get("config_fingerprint")
        else:
            config_revision, config_fingerprint = _monotone_revision(
                node.get("config_revision"), node.get("config_fingerprint"),
                values.get("config_revision"), values.get("config_fingerprint"),
            )
        node_replica = _reported_replica(
            node, values.get("node_replica"), "node", reset_generation)
        cross_replica = _reported_replica(
            node, values.get("cross_replica"), "cross", reset_generation)
        observed_global_revision = int(values.get("observed_global_revision") or 0) \
            if reset_generation else max(
                int(node.get("observed_global_revision") or 0),
                int(values.get("observed_global_revision") or 0),
            )
        live_config_revision = int(values.get("live_config_revision") or 0) \
            if reset_generation else max(
                int(node.get("live_config_revision") or 0),
                int(values.get("live_config_revision") or 0),
            )
        updated = connection.execute(
            "UPDATE master_control.nodes SET observed_state=%s, authority_term=%s, "
            "config_revision=%s, config_fingerprint=%s, "
            "capabilities=COALESCE(%s,capabilities), "
            "runtime_nonce=COALESCE(%s,runtime_nonce), last_seen_at=clock_timestamp(), "
            "protocol_version=COALESCE(%s,protocol_version),"
            "protocol_versions=COALESCE(%s,protocol_versions),"
            "contracts_version=COALESCE(%s,contracts_version),"
            "observed_global_revision=COALESCE(%s,observed_global_revision),"
            "node_replica_scope=COALESCE(%s,node_replica_scope),"
            "node_replica_revision=COALESCE(%s,node_replica_revision),"
            "node_replica_fingerprint=COALESCE(%s,node_replica_fingerprint),"
            "cross_replica_scope=COALESCE(%s,cross_replica_scope),"
            "cross_replica_revision=COALESCE(%s,cross_replica_revision),"
            "cross_replica_fingerprint=COALESCE(%s,cross_replica_fingerprint),"
            "live_config_revision=COALESCE(%s,live_config_revision),"
            "pending_proposal_count=COALESCE(%s,pending_proposal_count),"
            "max_packet_size=COALESCE(%s,max_packet_size),"
            "updated_at=clock_timestamp() WHERE site_id=%s AND node_id=%s RETURNING *",
            (
                observed, supplied_term, config_revision, config_fingerprint,
                encoded_capabilities, values.get("runtime_nonce"),
                values.get("protocol_version"),
                jsonb(values.get("protocol_versions")) if values.get("protocol_versions") else None,
                values.get("contracts_version"), observed_global_revision,
                node_replica[0], node_replica[1], node_replica[2],
                cross_replica[0], cross_replica[1], cross_replica[2],
                live_config_revision, values.get("pending_proposal_count"),
                values.get("max_packet_size"),
                values["site_id"], values["node_id"],
            ),
        ).fetchone()
        return make_success(updated=True, stale=False, node=dict(updated))

    return database["transaction"](write, isolation_level="READ COMMITTED")


def list_nodes(database, site_id, **kwargs):
    """Return bounded current node records and computed online state."""
    timeout_s = max(1.0, float(kwargs.get("timeout_s", 45.0)))

    def read(connection):
        rows = connection.execute(
            "SELECT *, last_seen_at >= clock_timestamp() - (%s * interval '1 second') AS online "
            "FROM master_control.nodes WHERE site_id=%s ORDER BY node_id",
            (timeout_s, site_id),
        ).fetchall()
        return [dict(row) for row in rows]

    return database["transaction"](read, isolation_level="READ COMMITTED")


def get_node(database, site_id, node_id):
    """Return one current node record."""
    def read(connection):
        row = connection.execute(
            "SELECT * FROM master_control.nodes WHERE site_id=%s AND node_id=%s",
            (site_id, node_id),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](read, isolation_level="READ COMMITTED")


def set_desired_state(database, request):
    """Set desired state without overwriting the independently observed state."""
    values = copy.deepcopy(request)
    desired = str(values["desired_state"]).upper()
    if desired not in ("ACTIVE", "DRAINING", "MAINTENANCE", "QUARANTINED"):
        return make_error("invalid_payload", "desired node state is invalid")

    def write(connection):
        row = connection.execute(
            "UPDATE master_control.nodes SET desired_state=%s, updated_at=clock_timestamp() "
            "WHERE site_id=%s AND node_id=%s RETURNING *",
            (desired, values["site_id"], values["node_id"]),
        ).fetchone()
        if not row:
            return make_error("not_found", "node is unknown")
        connection.execute(
            "INSERT INTO master_control.node_desired_state("
            "site_id,node_id,desired_state,reason,changed_by) VALUES (%s,%s,%s,%s,%s) "
            "ON CONFLICT (site_id,node_id) DO UPDATE SET desired_state=EXCLUDED.desired_state, "
            "reason=EXCLUDED.reason, changed_by=EXCLUDED.changed_by, changed_at=clock_timestamp()",
            (
                values["site_id"], values["node_id"], desired,
                values.get("reason"), values["principal_subject"],
            ),
        )
        if desired in ("MAINTENANCE", "QUARANTINED"):
            _release_node_ownership(connection, values["site_id"], values["node_id"], desired)
        return make_success(node=dict(row))

    return database["transaction"](write)


def _release_node_ownership(connection, site_id, node_id, reason):
    leases = connection.execute(
        "SELECT lease_token FROM master_control.controller_ownership "
        "WHERE site_id=%s AND owner_node_id=%s",
        (site_id, node_id),
    ).fetchall()
    tokens = [row["lease_token"] for row in leases]
    connection.execute(
        "DELETE FROM master_control.controller_ownership WHERE site_id=%s AND owner_node_id=%s",
        (site_id, node_id),
    )
    if tokens:
        connection.execute(
            "UPDATE master_control.controller_leases SET released_at=clock_timestamp(), "
            "release_reason=%s WHERE lease_id=ANY(%s) AND released_at IS NULL",
            (str(reason), tokens),
        )


def reconcile_ownership(database, request):
    """Issue one fenced lease for every configured admitted controller owner."""
    values = copy.deepcopy(request)
    routes = _owner_routes(values.get("config") or {}, values["site_id"])
    lease_s = max(5.0, float(values.get("lease_s", 30.0)))

    def write(connection):
        authority = connection.execute(
            "SELECT * FROM master_control.authority_state WHERE site_id=%s FOR UPDATE",
            (values["site_id"],),
        ).fetchone()
        owned = (
            authority
            and authority.get("leader_instance_id") == values.get("instance_id")
            and str(authority.get("lease_token") or "") == str(values.get("lease_token") or "")
            and int(authority.get("authority_term") or 0) == int(values["authority_term"])
            and int(authority.get("config_revision") or 0) == int(values["config_revision"])
            and authority.get("lease_until") is not None
        )
        if not owned:
            return make_error("not_config_authority", "ownership lease is not held")
        active = connection.execute(
            "SELECT lease_until>clock_timestamp() AS lease_active FROM "
            "master_control.authority_state WHERE site_id=%s",
            (values["site_id"],),
        ).fetchone()
        if not active or not active.get("lease_active"):
            return make_error("not_config_authority", "ownership lease has expired")
        results = []
        for route in routes:
            result = _upsert_ownership(connection, route, values, lease_s)
            results.append(result)
        _release_unconfigured_ownership(connection, values, routes)
        return make_success(ownership=results)

    return database["transaction"](write)


def _release_unconfigured_ownership(connection, values, routes):
    configured = {route["controller_uuid"] for route in routes}
    rows = connection.execute(
        "SELECT controller_uuid,lease_token FROM master_control.controller_ownership "
        "WHERE site_id=%s FOR UPDATE",
        (values["site_id"],),
    ).fetchall()
    obsolete = [row for row in rows if str(row["controller_uuid"]) not in configured]
    for row in obsolete:
        connection.execute(
            "DELETE FROM master_control.controller_ownership "
            "WHERE site_id=%s AND controller_uuid=%s",
            (values["site_id"], row["controller_uuid"]),
        )
        connection.execute(
            "UPDATE master_control.controller_leases SET released_at=clock_timestamp(),"
            "release_reason='removed_from_config' WHERE lease_id=%s AND released_at IS NULL",
            (row["lease_token"],),
        )


def _owner_routes(config, site_id):
    routes = []
    for item in config.get("site_controller_map") or []:
        if not isinstance(item, dict) or not item.get("enabled", True):
            continue
        if str(item.get("routing_role") or "owner") != "owner":
            continue
        if str(item.get("site_id") or "") != str(site_id):
            continue
        routes.append({
            "site_id": site_id,
            "controller_uuid": str(item.get("controller_uuid") or ""),
            "node_id": str(item.get("node_id") or ""),
        })
    return sorted(routes, key=_route_key)


def _route_key(route):
    return (route["node_id"], route["controller_uuid"])


def _upsert_ownership(connection, route, values, lease_s):
    node = connection.execute(
        "SELECT admission_state,desired_state,observed_state,"
        "last_seen_at>=clock_timestamp()-(%s*interval '1 second') AS online "
        "FROM master_control.nodes WHERE site_id=%s AND node_id=%s",
        (values.get("node_timeout_s", 45.0), route["site_id"], route["node_id"]),
    ).fetchone()
    available = (
        node
        and node["admission_state"] == "APPROVED"
        and node["desired_state"] == "ACTIVE"
        and node["observed_state"] == "ACTIVE"
        and node.get("online")
    )
    if not available:
        _release_controller_ownership(
            connection, route["site_id"], route["controller_uuid"], "owner_unavailable")
        return {"controller_uuid": route["controller_uuid"], "issued": False}
    token = uuid.uuid4()
    existing = connection.execute(
        "SELECT *, lease_until>clock_timestamp() AS lease_active "
        "FROM master_control.controller_ownership "
        "WHERE site_id=%s AND controller_uuid=%s FOR UPDATE",
        (route["site_id"], route["controller_uuid"]),
    ).fetchone()
    if existing and existing["lease_active"]:
        same_term = int(existing["authority_term"]) == int(values["authority_term"])
        same_revision = int(existing["config_revision"]) == int(values["config_revision"])
        if same_term and existing["owner_node_id"] != route["node_id"]:
            return {"controller_uuid": route["controller_uuid"], "issued": False, "conflict": True}
        if same_term and same_revision:
            token = existing["lease_token"]
    if existing and str(existing["lease_token"]) != str(token):
        connection.execute(
            "UPDATE master_control.controller_leases SET released_at=clock_timestamp(),"
            "release_reason='superseded_by_new_fence' WHERE lease_id=%s AND released_at IS NULL",
            (existing["lease_token"],),
        )
    connection.execute(
        "INSERT INTO master_control.controller_ownership("
        "site_id,controller_uuid,owner_node_id,authority_term,lease_token,lease_until,config_revision) "
        "VALUES (%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second'),%s) "
        "ON CONFLICT (site_id,controller_uuid) DO UPDATE SET "
        "owner_node_id=EXCLUDED.owner_node_id,authority_term=EXCLUDED.authority_term,"
        "lease_token=EXCLUDED.lease_token,lease_until=EXCLUDED.lease_until,"
        "config_revision=EXCLUDED.config_revision,updated_at=clock_timestamp()",
        (
            route["site_id"], route["controller_uuid"], route["node_id"],
            values["authority_term"], token, lease_s, values["config_revision"],
        ),
    )
    connection.execute(
        "INSERT INTO master_control.controller_leases("
        "lease_id,site_id,controller_uuid,owner_node_id,authority_term,config_revision,expires_at) "
        "VALUES (%s,%s,%s,%s,%s,%s,clock_timestamp()+(%s*interval '1 second')) "
        "ON CONFLICT (lease_id) DO UPDATE SET expires_at=EXCLUDED.expires_at",
        (
            token, route["site_id"], route["controller_uuid"], route["node_id"],
            values["authority_term"], values["config_revision"], lease_s,
        ),
    )
    return {"controller_uuid": route["controller_uuid"], "node_id": route["node_id"], "issued": True}


def _release_controller_ownership(connection, site_id, controller_uuid, reason):
    row = connection.execute(
        "DELETE FROM master_control.controller_ownership WHERE site_id=%s "
        "AND controller_uuid=%s RETURNING lease_token",
        (site_id, controller_uuid),
    ).fetchone()
    if row:
        connection.execute(
            "UPDATE master_control.controller_leases SET released_at=clock_timestamp(),"
            "release_reason=%s WHERE lease_id=%s AND released_at IS NULL",
            (str(reason), row["lease_token"]),
        )
    return bool(row)


def resolve_owner(database, site_id, controller_uuid, **kwargs):
    """Resolve only a current, fenced, unexpired owner."""
    term = kwargs.get("authority_term")
    revision = kwargs.get("config_revision")

    def read(connection):
        row = connection.execute(
            "SELECT * FROM master_control.controller_ownership WHERE site_id=%s "
            "AND controller_uuid=%s AND lease_until>clock_timestamp() "
            "AND (%s::bigint IS NULL OR authority_term=%s) "
            "AND (%s::bigint IS NULL OR config_revision=%s)",
            (site_id, controller_uuid, term, term, revision, revision),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](read, isolation_level="READ COMMITTED")


def upsert_sensor(database, request):
    """Write latest sensor state only after ownership was validated by core."""
    values = copy.deepcopy(request)

    def write(connection):
        ownership = connection.execute(
            "SELECT 1 AS owned FROM master_control.controller_ownership WHERE site_id=%s "
            "AND controller_uuid=%s AND owner_node_id=%s AND authority_term=%s "
            "AND config_revision=%s AND lease_until>clock_timestamp()",
            (
                values["site_id"], values["controller_uuid"], values["source_node_id"],
                values["authority_term"], values["config_revision"],
            ),
        ).fetchone()
        if not ownership:
            return make_error("stale_authority_term", "sensor owner lease is no longer active")
        row = connection.execute(
            "INSERT INTO master_control.sensor_latest("
            "site_id,controller_uuid,sensor_id,source_node_id,value,quality,"
            "source_sequence,measured_at) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) "
            "ON CONFLICT (site_id,controller_uuid,sensor_id) DO UPDATE SET "
            "source_node_id=EXCLUDED.source_node_id,value=EXCLUDED.value,quality=EXCLUDED.quality,"
            "source_sequence=EXCLUDED.source_sequence,measured_at=EXCLUDED.measured_at,"
            "received_at=clock_timestamp() WHERE EXCLUDED.source_sequence IS NOT NULL AND ("
            "sensor_latest.source_sequence IS NULL OR "
            "EXCLUDED.source_sequence>sensor_latest.source_sequence) RETURNING sensor_id",
            (
                values["site_id"], values["controller_uuid"], str(values["sensor_id"]),
                values["source_node_id"], jsonb(values.get("value")),
                str(values.get("quality") or "unknown"), values.get("source_sequence"),
                values.get("measured_at"),
            ),
        ).fetchone()
        return make_success(updated=bool(row), stale=not bool(row))

    return database["transaction"](write, isolation_level="READ COMMITTED")


def get_sensor_sequence_floor(database, site_id, node_id):
    """Return the highest telemetry sequence durably accepted for one Worker."""
    def read(connection):
        row = connection.execute(
            "SELECT COALESCE(MAX(source_sequence),0) AS sequence_floor "
            "FROM master_control.sensor_latest WHERE site_id=%s AND source_node_id=%s",
            (str(site_id), str(node_id)),
        ).fetchone()
        return int((row or {}).get("sequence_floor") or 0)

    return database["transaction"](read, isolation_level="READ COMMITTED")


def build_node_capabilities(database):
    """Expose narrow worker-registry and ownership persistence ports."""
    return {
        "register": partial(register_node, database),
        "decide_admission": partial(decide_admission, database),
        "heartbeat": partial(record_heartbeat, database),
        "list": partial(list_nodes, database),
        "get": partial(get_node, database),
        "set_desired_state": partial(set_desired_state, database),
        "reconcile_ownership": partial(reconcile_ownership, database),
        "resolve_owner": partial(resolve_owner, database),
        "upsert_sensor": partial(upsert_sensor, database),
        "sensor_sequence_floor": partial(get_sensor_sequence_floor, database),
    }

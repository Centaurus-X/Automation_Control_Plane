# -*- coding: utf-8 -*-

"""Functional non-executing Master composition root and staged lifecycle."""

import copy
import json
import logging
import threading

from functools import partial

from src.library import _generic_type_library as type_authority
from src.library._generic_system_contract_library import config_compatibility, config_graph
from src.library._generic_system_contract_library.errors import make_error
from src.library._generic_type_library import fingerprint
from src.core.authority_transition_orchestrator import create_authority_transition_orchestrator
from src.core.cluster_orchestrator import create_cluster_orchestrator
from src.core.config_authority import create_config_authority
from src.core.coordinator_service import create_coordinator_service
from src.core.cross_fact_router import create_cross_fact_router
from src.core.node_registry import create_node_registry
from src.core.operation_management import create_operation_management
from src.core.ownership_management import create_ownership_management
from src.core.proposal_ingest import create_proposal_ingest
from src.core.rollout_orchestrator import create_rollout_orchestrator
from src.core.shard_materializer import create_shard_materializer
from src.core.scope_activation import validate_scope_intent
from src.interfaces.admin_command_interface import create_admin_command_interface
from src.interfaces.legacy_worker_mqtt_interface import create_mqtt_interface
from src.interfaces.metrics_interface import create_metrics_interface
from src.interfaces.mqtt_control_interface import create_control_mqtt_interface
from src.library.master_data_store import create_master_data_store
from src.library.master_heap_store import create_master_heap_store
from src.library.postgresql import env_database_options


LOGGER = logging.getLogger("master_node.bootstrap")


def _legacy_mqtt_options(config):
    mqtt = copy.deepcopy(config["mqtt"])
    transport = {
        "host": mqtt["host"],
        "port": mqtt["port"],
        "keepalive_s": mqtt.get("keepalive_s", 30),
        "connect_timeout_s": mqtt.get("connect_timeout_s", 5.0),
        "socket_timeout_s": mqtt.get("socket_timeout_s", 2.0),
        "reconnect_min_s": mqtt.get("reconnect_min_s", 1.0),
        "reconnect_max_s": mqtt.get("reconnect_max_s", 30.0),
        "outbound_queue_maxsize": mqtt.get("outbound_queue_maxsize", 4096),
        "max_packet_size": mqtt.get("max_packet_size", 4 * 1024 * 1024),
        "allow_insecure_transport": mqtt.get("allow_insecure_transport", False),
        "tls": copy.deepcopy(mqtt.get("tls") or {}),
    }
    return {
        "enabled": True,
        "required": bool(mqtt.get("required", True)),
        "site_id": config["site_id"],
        "node_id": config["legacy_coordinator_node_id"],
        "topic_prefix": "automation/v1",
        "command_queue_maxsize": mqtt.get("command_queue_maxsize", 512),
        "max_command_age_s": 300.0,
        "max_clock_skew_s": 30.0,
        "allow_retained_commands": False,
        "connect_wait_s": mqtt.get("connect_timeout_s", 5.0),
        "transport": transport,
        "command_auth": {
            "require_hmac": False,
            "sign_outbound": False,
            "secret_env": "MQTT_COMMAND_HMAC_SECRET",
        },
        "commands": {"require_base_revision": True, "allowed": []},
        "event_publication": {
            "qos": 1,
            "event_types": ["REMOTE_ACTUATOR_RESULT", "SENSOR_VALUE_UPDATE"],
        },
    }


def _load_initial_config(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _build_registry_config(application_config):
    """Build administrative Registry identity without loading Worker config."""
    site_id = str(application_config.get("site_id") or "").strip()
    instance_id = str(application_config.get("instance_id") or "").strip()
    coordinator_node_id = str(
        application_config.get("legacy_coordinator_node_id") or "").strip()
    worker_compatibility = application_config.get("worker_compatibility") or {}
    allowed_worker_node_ids = tuple(sorted(
        str(node_id)
        for node_id in worker_compatibility.get("allowed_worker_node_ids", ())
        if str(node_id)
    ))
    if not site_id or not instance_id or not coordinator_node_id:
        raise ValueError("Registry identity is incomplete")
    return {
        "registry_schema": "automation_registry_v1",
        "authority_mode": "node_local",
        "site_id": site_id,
        "instance_id": instance_id,
        "coordinator_node_id": coordinator_node_id,
        "allowed_worker_node_ids": allowed_worker_node_ids,
        "allowed_proxy_instance_ids": tuple(sorted(
            str(value)
            for value in application_config.get("control", {}).get(
                "allowed_proxy_instance_ids", ())
            if str(value)
        )),
    }

def _registry_config_snapshot(context):
    """Expose static Registry identity with zero global-authority revision."""
    config = context.get("registry_config")
    if not isinstance(config, dict):
        return None
    return {
        "authority_mode": "node_local",
        "global_revision": 0,
        "revision": 0,
        "fingerprint": type_authority.fingerprint(config),
        "config_data": copy.deepcopy(config),
        "source_kind": "administrative_registry_identity",
    }

def _node_local_topology_snapshot(context):
    """Derive routing-only topology from validated Worker mirror content."""
    routes = {}
    heads = {}
    mirrors = context["store"]["authority"]["list_worker_mirrors"]()
    for mirror in mirrors:
        if not isinstance(mirror, dict) or mirror.get("scope_kind") != "node":
            continue
        owner = str(mirror.get("owner_node_id") or "")
        scope = str(mirror.get("scope") or "")
        heads[scope] = {
            "authority_epoch": int(mirror.get("authority_epoch") or 0),
            "scope_revision": int(mirror.get("scope_revision") or 0),
            "fingerprint": str(mirror.get("fingerprint") or ""),
        }
        snapshot = mirror.get("snapshot") if isinstance(mirror.get("snapshot"), dict) else {}
        config = snapshot.get("config") if isinstance(snapshot.get("config"), dict) else {}
        for item in config.get("site_controller_map") or ():
            if not isinstance(item, dict) or not item.get("enabled", True):
                continue
            if str(item.get("routing_role") or "owner") != "owner":
                continue
            controller_uuid = str(item.get("controller_uuid") or "")
            route_owner = str(item.get("node_id") or "")
            if not controller_uuid or route_owner != owner:
                raise ValueError("Worker mirror contains an invalid controller owner route")
            route = copy.deepcopy(item)
            # Registry leases use the effective operational site identity. A
            # mirrored V5 business site label remains authoritative content.
            route["site_id"] = context["config"]["site_id"]
            current = routes.get(controller_uuid)
            if current is not None and current != route:
                raise ValueError(
                    "Worker mirrors disagree on controller route: " + controller_uuid)
            routes[controller_uuid] = route
    routing_config = {
        "site_controller_map": [
            routes[key] for key in sorted(routes)
        ],
    }
    return {
        "authority_mode": "node_local",
        "global_revision": 0,
        "revision": 0,
        "fingerprint": type_authority.fingerprint(routing_config),
        "config_data": routing_config,
        "scope_heads": heads,
        "source_kind": "worker_mirrors",
    }

def _deny_node_local_config_commit(**_kwargs):
    """Fail closed if a legacy Master config-write path is reached."""
    return make_error(
        "not_config_authority",
        "Master config commits are disabled in node_local authority mode",
    )


def _node_local_config_authority():
    """Return a complete denied compatibility port for legacy paths."""
    denied = _deny_node_local_config_commit
    return {
        "seed": denied,
        "migrate_legacy": denied,
        "propose": denied,
        "rollback": denied,
        "propose_action_plan": denied,
        "commit_prepared": denied,
        "commit_materialized": denied,
    }


def _read_config_revision(reader, site_id, revision):
    return reader(site_id, revision=revision)


def _validate_operation_scope(scope_reader, source_node, payload):
    execution_scope = str(payload.get("execution_scope") or "")
    if execution_scope != "cross" and not execution_scope.startswith("cross:"):
        return {"ok": True, "execution_scope": None, "execution_epoch": None}
    domain = str(payload.get("cross_domain_id") or (
        execution_scope if execution_scope.startswith("cross:") else ""))
    if not domain:
        return {"ok": False, "code": "activation_missing", "message": "Cross domain is missing"}
    if domain.startswith("cross:"):
        scope = domain
    elif domain.startswith("cross-") and domain.endswith("-default"):
        scope = "cross:" + domain[6:-8] + ".default"
    else:
        scope = "cross:" + domain
    reader = scope_reader if callable(scope_reader) else scope_reader["get_scope"]
    snapshot = reader(scope)
    return validate_scope_intent(snapshot, source_node, payload)

def _set_authority_services(context, role, **_kwargs):
    """Activate Worker ingress only on the PostgreSQL-fenced Registry leader."""
    with context["role_lock"]:
        with context["lock"]:
            stopping = context["state"] in ("stopping", "stopped", "failed")
        effective = "standby" if stopping else str(role)
        node_local = context["config"].get("authority_mode") == "node_local"
        if effective == "leader":
            if node_local:
                if not _authority_config_ready(context):
                    context["legacy_mqtt"]["stop"](join_timeout_s=3.0)
                    return False
                _set_bootstrap_state(
                    context, "ready", status="registry_ready", error=None,
                    global_revision=0, required_node_scopes=(),
                )
            else:
                seeded = _ensure_authority_config(context)
                if not seeded.get("ok"):
                    context["legacy_mqtt"]["stop"](join_timeout_s=3.0)
                    return False
            context["legacy_mqtt"]["start"]()
            return True
        context["legacy_mqtt"]["stop"](join_timeout_s=3.0)
        if stopping:
            return True
        if node_local:
            _set_bootstrap_state(
                context, "registry_standby", status="standby", error=None,
                global_revision=0, required_node_scopes=(),
            )
            return _authority_config_ready(context)
        refreshed = _refresh_standby_authority_state(context)
        return bool(refreshed.get("ok"))

def _publish_cross_fact(legacy_mqtt, target_node, envelope):
    return legacy_mqtt["publish_event"](
        "SENSOR_VALUE_UPDATE", copy.deepcopy(envelope),
        target_node=target_node, message_id=envelope.get("event_id"), qos=1,
    )


def _refresh_derived_runtime(context):
    """Refresh routing only from mirrors accepted by the current Registry."""
    if context["config"].get("authority_mode") == "node_local":
        authority_store = context["store"]["authority"]
        mirrors = authority_store["list_worker_mirrors"]()
        scopes = {}
        controller_owners = {}
        for mirror in mirrors:
            if not isinstance(mirror, dict) or not mirror.get("scope"):
                continue
            scope_kind = mirror.get("scope_kind")
            if scope_kind == "cross":
                registry_head = authority_store["get_registry_head"](
                    mirror["scope"])
                accepted = bool(
                    registry_head
                    and str(registry_head.get("owner_node_id") or "")
                    == str(mirror.get("owner_node_id") or "")
                    and int(registry_head.get("execution_epoch") or 0)
                    == int(mirror.get("execution_epoch") or 0)
                )
                if not accepted:
                    continue
            snapshot = copy.deepcopy(mirror.get("snapshot") or {})
            snapshot.setdefault("scope", mirror["scope"])
            snapshot.setdefault("scope_kind", scope_kind)
            snapshot.setdefault("owner_node_id", mirror.get("owner_node_id"))
            snapshot.setdefault("execution_epoch", mirror.get("execution_epoch"))
            scopes[mirror["scope"]] = snapshot
            if scope_kind != "node":
                continue
            graph = config_graph.build_graph(snapshot.get("config") or {})
            for controller_uuid, owner_node_id in (
                    graph.get("controller_owners") or {}).items():
                existing = controller_owners.get(controller_uuid)
                if existing is not None and existing != owner_node_id:
                    raise ValueError(
                        "Worker mirrors disagree on controller ownership: "
                        + str(controller_uuid))
                controller_owners[controller_uuid] = owner_node_id
        context["cross_router"]["replace_dependencies"](
            scopes, controller_owners)
        return True
    global_snapshot = context["heap"]["get_global"]()
    if not global_snapshot or not isinstance(global_snapshot.get("config_data"), dict):
        return False
    graph = config_graph.build_graph(global_snapshot["config_data"])
    scopes = context["store"]["authority"]["get_scope_snapshots"]()
    context["cross_router"]["replace_dependencies"](
        scopes, graph.get("controller_owners") or {})
    return True

def _required_node_scopes(config):
    """Return the baseline Node scopes; Cross scopes are independent opt-ins."""
    return tuple(sorted({
        "node:" + str(node.get("node_id"))
        for node in config.get("nodes") or ()
        if isinstance(node, dict)
        and node.get("enabled") is not False
        and str(node.get("node_role") or "").lower() == "worker"
        and node.get("node_id")
    })) if isinstance(config, dict) else ()


def _set_bootstrap_state(context, state, **kwargs):
    with context["lock"]:
        bootstrap = context["bootstrap"]
        bootstrap["state"] = str(state)
        bootstrap["attempts"] += int(bool(kwargs.get("attempt")))
        if "status" in kwargs:
            bootstrap["last_status"] = kwargs.get("status")
        if "error" in kwargs:
            bootstrap["last_error"] = kwargs.get("error")
        if "global_revision" in kwargs:
            bootstrap["global_revision"] = int(kwargs.get("global_revision") or 0)
        if "required_node_scopes" in kwargs:
            bootstrap["required_node_scopes"] = tuple(
                kwargs.get("required_node_scopes") or ())


def _authority_config_ready(context):
    """Report Registry identity readiness without implying config authority."""
    if context["config"].get("authority_mode") == "node_local":
        config = context.get("registry_config")
        return bool(
            isinstance(config, dict)
            and config.get("registry_schema") == "automation_registry_v1"
            and config.get("authority_mode") == "node_local"
            and str(config.get("site_id") or "") == context["config"]["site_id"]
            and str(config.get("instance_id") or "")
            == context["config"]["instance_id"]
            and str(config.get("coordinator_node_id") or "")
        )
    heap = context.get("heap")
    if not heap:
        return False
    global_snapshot = heap["get_global"]()
    if not isinstance(global_snapshot, dict):
        return False
    config = global_snapshot.get("config_data")
    if not isinstance(config, dict):
        return False
    try:
        config_compatibility.ensure_v9_storage_config(config)
    except ValueError:
        return False
    if int(global_snapshot.get("global_revision") or 0) < 1:
        return False
    global_fingerprint = str(global_snapshot.get("fingerprint") or "").strip()
    if (
        not global_fingerprint
        or set(global_fingerprint) == {"0"}
        or global_fingerprint != fingerprint(config)
    ):
        return False
    heads = heap["get_scope_heads"]()
    for scope in _required_node_scopes(config):
        head = heads.get(scope) if isinstance(heads, dict) else None
        if not _valid_node_scope_head(scope, head, global_snapshot):
            return False
    return True

def _valid_node_scope_head(scope, head, global_snapshot):
    owner = str(scope).partition(":")[2]
    return bool(
        isinstance(head, dict)
        and head.get("scope_kind") == "node"
        and str(head.get("owner_node_id") or "") == owner
        and int(head.get("scope_revision") or 0) >= 1
        and int(head.get("global_revision") or 0) <= int(
            global_snapshot.get("global_revision") or 0)
        and str(head.get("fingerprint") or "").strip()
    )


def _heap_matches_config(context, snapshot):
    if not isinstance(snapshot, dict):
        return False
    heap_snapshot = context["heap"]["get_global"]()
    return bool(
        isinstance(heap_snapshot, dict)
        and int(heap_snapshot.get("global_revision") or 0)
        == int(snapshot.get("global_revision") or snapshot.get("revision") or 0)
        and str(heap_snapshot.get("fingerprint") or "")
        == str(snapshot.get("fingerprint") or "")
        and _authority_config_ready(context)
    )


def _validate_authority_state(state):
    global_snapshot = state.get("global") if isinstance(state, dict) else None
    if not isinstance(global_snapshot, dict):
        return {
            "ok": False,
            "message": "authority bootstrap produced no durable global configuration",
        }
    config = global_snapshot.get("config_data")
    revision = int(global_snapshot.get("global_revision") or 0)
    global_fingerprint = str(global_snapshot.get("fingerprint") or "").strip()
    try:
        config_compatibility.ensure_v9_storage_config(config)
    except ValueError:
        return {
            "ok": False,
            "message": "authority bootstrap requires canonical V9 storage",
        }
    if (
        not isinstance(config, dict)
        or revision < 1
        or not global_fingerprint
        or global_fingerprint != fingerprint(config)
    ):
        return {
            "ok": False,
            "message": "authority bootstrap global revision is incomplete",
        }
    scopes = state.get("scopes") if isinstance(state.get("scopes"), dict) else {}
    required = _required_node_scopes(config)
    missing = tuple(sorted(
        scope for scope in required
        if not _valid_node_scope_head(scope, scopes.get(scope), global_snapshot)
    ))
    if missing:
        return {
            "ok": False,
            "message": "authority bootstrap is missing Node scopes: " + ",".join(missing),
        }
    return {
        "ok": True,
        "global_revision": revision,
        "required_node_scopes": required,
    }


def _bootstrap_failure(context, message, **kwargs):
    text = str(message or "authority bootstrap failed")
    _set_bootstrap_state(
        context, "failed", status=kwargs.get("status"), error=text)
    with context["lock"]:
        context["last_error"] = "authority bootstrap: " + text
    return {"ok": False, "code": "authority_bootstrap", "message": text}


def _reload_authority_state(context):
    state = context["store"]["authority"]["load_state"](
        context["config"]["site_id"])
    checked = _validate_authority_state(state)
    if not checked.get("ok"):
        return checked
    context["heap"]["replace"](state["global"], state.get("scopes") or {})
    if not _refresh_derived_runtime(context):
        return {
            "ok": False,
            "message": "authority bootstrap could not refresh derived runtime state",
        }
    return checked


def _refresh_standby_authority_state_once(context):
    """Keep a standby cache current without ever creating authority state."""
    existing = context["config_reader"]()
    if not existing:
        _set_bootstrap_state(
            context, "waiting_for_leader", status="standby", error=None)
        return {"ok": True, "status": "standby"}
    version = config_compatibility.get_config_version(
        existing.get("config_data") if isinstance(existing, dict) else None)
    if version == config_compatibility.LEGACY_VERSION:
        _set_bootstrap_state(
            context, "waiting_for_leader", status="legacy_v5_waiting", error=None)
        return {"ok": True, "status": "legacy_v5_waiting"}
    if version != config_compatibility.CANONICAL_VERSION:
        return _bootstrap_failure(
            context, "stored authority configuration has an unsupported version",
            status="standby_reload")
    if _heap_matches_config(context, existing):
        return {"ok": True, "status": "standby_loaded"}
    loaded = _reload_authority_state(context)
    if not loaded.get("ok"):
        return _bootstrap_failure(
            context, loaded.get("message"), status="standby_reload")
    _set_bootstrap_state(
        context, "ready", status="standby_loaded", error=None,
        global_revision=loaded["global_revision"],
        required_node_scopes=loaded["required_node_scopes"],
    )
    with context["lock"]:
        if str(context.get("last_error") or "").startswith("authority bootstrap:"):
            context["last_error"] = None
    return {"ok": True, "status": "standby_loaded"}


def _bootstrap_exception(context, operation, exception):
    """Expose one bounded bootstrap exception without leaking configuration."""
    detail = " ".join(str(exception).split())[:512]
    message = type(exception).__name__ + (": " + detail if detail else "")
    LOGGER.exception("Master authority %s failed", operation)
    return _bootstrap_failure(
        context, message,
        status="{}_exception".format(str(operation).replace(" ", "_")),
    )


def _refresh_standby_authority_state(context):
    try:
        return _refresh_standby_authority_state_once(context)
    except Exception as exc:
        return _bootstrap_exception(context, "standby reload", exc)


def _ensure_authority_config_once(context):
    """Idempotently seed/reload authority state whenever this instance is leader."""
    existing = context["config_reader"]()
    existing_version = config_compatibility.get_config_version(
        existing.get("config_data") if isinstance(existing, dict) else None)
    if existing and existing_version == config_compatibility.LEGACY_VERSION:
        _set_bootstrap_state(context, "migrating_v9", attempt=True, error=None)
        result = context["config_authority"]["migrate_legacy"]()
        if not result.get("ok"):
            return _bootstrap_failure(
                context, result.get("message") or "legacy V5 migration failed",
                status=result.get("code"))
        loaded = _reload_authority_state(context)
        if not loaded.get("ok"):
            return _bootstrap_failure(
                context, loaded.get("message"), status="legacy_v5_migration")
        _set_bootstrap_state(
            context, "ready", status="migrated_v9", error=None,
            global_revision=loaded["global_revision"],
            required_node_scopes=loaded["required_node_scopes"],
        )
        return {
            "ok": True,
            "status": "migrated_v9",
            "global_revision": loaded["global_revision"],
            "required_node_scopes": loaded["required_node_scopes"],
        }
    if existing and existing_version != config_compatibility.CANONICAL_VERSION:
        return _bootstrap_failure(
            context, "stored authority configuration has an unsupported version",
            status="authority_version")
    if existing and _heap_matches_config(context, existing):
        global_snapshot = context["heap"]["get_global"]()
        required = _required_node_scopes(global_snapshot.get("config_data") or {})
        _set_bootstrap_state(
            context, "ready", status="already_loaded", error=None,
            global_revision=global_snapshot.get("global_revision"),
            required_node_scopes=required,
        )
        return {
            "ok": True,
            "status": "already_loaded",
            "global_revision": global_snapshot.get("global_revision"),
        }
    _set_bootstrap_state(context, "seeding", attempt=True, error=None)
    if existing:
        result = {"ok": True, "status": "already_seeded"}
    else:
        legacy = context["store"]["authority"]["get_legacy_config"](
            context["config"]["site_id"])
        if legacy:
            config = legacy["config_data"]
            request_id = "migration-master-v2:{}:{}".format(
                legacy["revision"], legacy["fingerprint"])
            result = context["config_authority"]["seed"](
                config, source_kind="migration", request_id=request_id,
                principal_subject="system:migration-v0011-to-v0013",
            )
        else:
            config = _load_initial_config(context["config"]["initial_config_path"])
            result = context["config_authority"]["seed"](config)
    if not result.get("ok"):
        return _bootstrap_failure(
            context, result.get("message") or "initial configuration was rejected",
            status=result.get("code"),
        )
    loaded = _reload_authority_state(context)
    if not loaded.get("ok"):
        return _bootstrap_failure(
            context, loaded.get("message"), status=result.get("status"))
    _set_bootstrap_state(
        context, "ready", status=result.get("status") or "seeded", error=None,
        global_revision=loaded["global_revision"],
        required_node_scopes=loaded["required_node_scopes"],
    )
    with context["lock"]:
        if str(context.get("last_error") or "").startswith("authority bootstrap:"):
            context["last_error"] = None
    return {
        "ok": True,
        "status": result.get("status") or "seeded",
        "global_revision": loaded["global_revision"],
        "required_node_scopes": loaded["required_node_scopes"],
    }


def _ensure_authority_config(context):
    try:
        return _ensure_authority_config_once(context)
    except Exception as exc:
        return _bootstrap_exception(context, "configuration seed", exc)


def _wire_config_services(context, authority_store, authority_reader, config_reader):
    config = context["config"]
    context["heap"] = create_master_heap_store()
    if config.get("authority_mode") == "node_local":
        context["materializer"] = None
        context["config_authority"] = _node_local_config_authority()
        return
    stored = authority_store["load_state"](config["site_id"])
    if stored.get("global"):
        context["heap"]["replace"](stored["global"], stored.get("scopes") or {})
    context["materializer"] = create_shard_materializer(
        profile=config.get("release_profile") or "lab_v2")
    context["config_authority"] = create_config_authority(
        site_id=config["site_id"], instance_id=config["instance_id"],
        authority_reader=authority_reader, config_reader=config_reader,
        config_by_revision=partial(
            _read_config_revision, authority_store["get_config"], config["site_id"]),
        commit=authority_store["commit_config"], policy=config.get("config_policy") or {},
        profile=config.get("release_profile") or "lab_v2",
        materializer=context["materializer"]["materialize"],
        scope_heads_reader=authority_store["get_scope_heads"],
        scope_snapshots_reader=authority_store["get_scope_snapshots"],
        activations_reader=authority_store["get_activations"],
        state_loader=authority_store["load_state"], heap=context["heap"],
    )

def _wire_worker_services(context, authority_store, node_store, operation_store):
    config = context["config"]
    authority_reader = context["authority_reader"]
    config_reader = context["topology_reader"]
    scope_reader = authority_store["get_worker_mirror"] \
        if config.get("authority_mode") == "node_local" \
        else authority_store["get_scope"]
    context["nodes"] = create_node_registry(
        site_id=config["site_id"], instance_id=config["instance_id"],
        coordinator_node_id=config["legacy_coordinator_node_id"],
        authority_mode=config.get("authority_mode") or "node_local",
        trust_broker_acl_identity=config.get("worker_compatibility", {}).get(
            "trust_broker_acl_identity", False),
        store=node_store, authority_reader=authority_reader,
        scope_reader=scope_reader,
    )
    context["ownership"] = create_ownership_management(
        site_id=config["site_id"], instance_id=config["instance_id"],
        authority_reader=authority_reader, store=node_store,
        lease_s=config["authority"]["ownership_lease_s"],
        node_timeout_s=config["authority"].get("node_timeout_s", 45.0),
    )
    context["operations"] = create_operation_management(
        site_id=config["site_id"], instance_id=config["instance_id"],
        legacy_coordinator_node_id=config["legacy_coordinator_node_id"],
        authority_reader=authority_reader, config_reader=config_reader,
        ownership=context["ownership"], store=operation_store,
        scope_validator=partial(_validate_operation_scope, scope_reader),
        scope_reader=scope_reader,
        node_reader=context["nodes"]["get"],
    )
    context["legacy_mqtt"] = create_mqtt_interface(
        options=_legacy_mqtt_options(config), config_capabilities={},
        runtime_status_reader=partial(get_master_snapshot, context),
        transport_factory=context["factories"].get("legacy_transport_factory"),
    )
    context["rollouts"] = create_rollout_orchestrator(
        site_id=config["site_id"], instance_id=config["instance_id"],
        coordinator_node_id=config["legacy_coordinator_node_id"],
        command_ttl_s=config["outbox"]["command_ttl_s"],
        max_packet_size=config["mqtt"].get("max_packet_size", 4 * 1024 * 1024),
        authority_reader=authority_reader, config_reader=config_reader,
        node_reader=context["nodes"]["get"],
        authority_store=authority_store, entity_store=authority_store,
    )
    context["proposals"] = create_proposal_ingest(
        config_reader=config_reader,
        scope_head_reader=authority_store["get_scope_heads"],
        record_proposal=authority_store["record_proposal"],
        complete_proposal=authority_store["complete_proposal"],
        file_policy=config.get("worker_file_proposals") or {},
    )
    context["cross_router"] = create_cross_fact_router(
        max_signal_age_s=config.get("cross_routing", {}).get("max_signal_age_s", 30.0),
        publisher=partial(_publish_cross_fact, context["legacy_mqtt"]),
    )

def _wire_components(context, **kwargs):
    config = context["config"]
    context["factories"] = dict(kwargs)
    store_factory = kwargs.get("data_store_factory") or create_master_data_store
    context["store"] = store_factory(kwargs.get("database_options") or env_database_options())
    authority_store = context["store"]["authority"]
    node_store = context["store"]["nodes"]
    operation_store = context["store"]["operations"]
    context["authority_reader"] = partial(
        authority_store["get_authority"], config["site_id"])
    database_config_reader = partial(
        authority_store["get_config"], config["site_id"])
    if config.get("authority_mode") == "node_local":
        context["registry_config"] = _build_registry_config(config)
        context["registry_reader"] = partial(_registry_config_snapshot, context)
        context["topology_reader"] = partial(
            _node_local_topology_snapshot, context)
        context["config_reader"] = context["topology_reader"]
    else:
        context["registry_config"] = None
        context["registry_reader"] = database_config_reader
        context["topology_reader"] = database_config_reader
        context["config_reader"] = database_config_reader
    _wire_config_services(
        context, authority_store, context["authority_reader"],
        context["config_reader"])
    _wire_worker_services(context, authority_store, node_store, operation_store)
    history_reader = authority_store["list_worker_mirrors"] \
        if config.get("authority_mode") == "node_local" \
        else partial(authority_store["config_history"], config["site_id"])
    context["admin"] = create_admin_command_interface(
        instance_id=config["instance_id"], nodes=context["nodes"],
        authority_mode=config.get("authority_mode") or "node_local",
        operations=context["operations"], authority_reader=context["authority_reader"],
        config_history=history_reader,
    )
    context["coordinator"] = create_coordinator_service(
        site_id=config["site_id"], instance_id=config["instance_id"],
        authority_mode=config.get("authority_mode") or "node_local",
        legacy_coordinator_node_id=config["legacy_coordinator_node_id"],
        authority_store=authority_store, node_store=node_store,
        operation_store=operation_store, audit=context["store"]["audit"],
        config_authority=context["config_authority"], config_reader=context["config_reader"],
        authority_reader=context["authority_reader"], nodes=context["nodes"],
        ownership=context["ownership"], operations=context["operations"],
        admin=context["admin"], legacy_mqtt=context["legacy_mqtt"],
        proposal_ingest=context["proposals"], rollouts=context["rollouts"],
        cross_router=context["cross_router"],
        derived_refresh=partial(_refresh_derived_runtime, context),
        runtime_snapshot=partial(get_master_snapshot, context),
        worker_compatibility=config.get("worker_compatibility") or {},
        command_ttl_s=config["outbox"]["command_ttl_s"],
        retry_s=config["outbox"]["retry_s"],
    )
    context["authority_transitions"] = create_authority_transition_orchestrator(
        site_id=config["site_id"], instance_id=config["instance_id"],
        authority_reader=context["authority_reader"], store=authority_store,
        mirror_reader=authority_store["get_worker_mirror"],
        stage_worker_command=context["coordinator"]["stage_worker_command"],
    )
    context["coordinator"]["context"]["authority_transitions"] = \
        context["authority_transitions"]
    context["control_mqtt"] = create_control_mqtt_interface(
        site_id=config["site_id"], instance_id=config["instance_id"],
        options=config["mqtt"], handler=context["coordinator"]["handle_control"],
        authority_reader=context["authority_reader"],
        allowed_proxy_instance_ids=config.get("control", {}).get(
            "allowed_proxy_instance_ids", ()),
        transport_factory=kwargs.get("control_transport_factory"),
    )
    context["coordinator"]["set_event_publisher"](
        context["control_mqtt"]["publish_event"])
    context["orchestrator"] = create_cluster_orchestrator(
        site_id=config["site_id"], instance_id=config["instance_id"],
        leader_eligible=config["leader_eligible"], authority_store=authority_store,
        operation_store=operation_store, ownership=context["ownership"],
        config_reader=context["topology_reader"],
        publish_due_outbox=context["coordinator"]["publish_due_outbox"],
        expire_operations=context["coordinator"]["expire_operations"],
        role_change_handler=partial(_set_authority_services, context),
        authority_term_floor=config["authority"].get("term_floor", 0),
        lease_s=config["authority"]["lease_s"],
        renew_interval_s=config["authority"]["renew_interval_s"],
        ownership_interval_s=config["authority"]["ownership_interval_s"],
        outbox_batch_size=config["outbox"]["batch_size"],
        retention_cleanup=context["store"]["maintenance"]["cleanup"],
        retention_options=config.get("retention") or {},
        retention_interval_s=config.get("retention", {}).get(
            "maintenance_interval_s", 300.0),
    )
    context["metrics"] = create_metrics_interface(
        host=config["metrics"]["host"], port=config["metrics"]["port"],
        snapshot_reader=partial(get_master_snapshot, context),
    )
    _refresh_derived_runtime(context)

def create_master_runtime(config, **kwargs):
    """Create all Registry and relay service contexts without starting owners."""
    context = {
        "config": copy.deepcopy(config),
        "lock": threading.RLock(),
        "role_lock": threading.RLock(),
        "state": "created",
        "last_error": None,
        "bootstrap": {
            "state": "not_started",
            "attempts": 0,
            "last_status": None,
            "last_error": None,
            "global_revision": 0,
            "required_node_scopes": (),
        },
        "store": None,
        "authority_reader": None,
        "config_reader": None,
        "topology_reader": None,
        "registry_reader": None,
        "registry_config": None,
        "config_authority": None,
        "heap": None,
        "materializer": None,
        "nodes": None,
        "ownership": None,
        "operations": None,
        "legacy_mqtt": None,
        "control_mqtt": None,
        "coordinator": None,
        "authority_transitions": None,
        "rollouts": None,
        "proposals": None,
        "cross_router": None,
        "orchestrator": None,
        "metrics": None,
        "factories": {},
    }
    _wire_components(context, **kwargs)
    return {
        "context": context,
        "start": partial(start_master_runtime, context),
        "stop": partial(stop_master_runtime, context),
        "snapshot": partial(get_master_snapshot, context),
    }

def _seed_if_leader(context):
    """Acquire the Registry lease without seeding Master config in node_local."""
    renewed = context["orchestrator"]["renew_now"]()
    if renewed.get("role") != "leader":
        state = "registry_standby" \
            if context["config"].get("authority_mode") == "node_local" \
            else "waiting_for_leader"
        _set_bootstrap_state(
            context, state, status="standby", error=None,
            global_revision=0, required_node_scopes=(),
        )
        return {"ok": True, "status": "standby"}
    if context["config"].get("authority_mode") == "node_local":
        if not _authority_config_ready(context):
            return _bootstrap_failure(
                context,
                "static Registry topology failed Type Authority validation",
                status="registry_topology",
            )
        _set_bootstrap_state(
            context, "ready", status="registry_ready", error=None,
            global_revision=0, required_node_scopes=(),
        )
        return {"ok": True, "status": "registry_ready"}
    return _ensure_authority_config(context)

def _mqtt_ready(context):
    timeout = float(context["config"]["mqtt"].get("connect_timeout_s", 5.0))
    control = context["control_mqtt"]["wait_connected"](timeout)
    role = context["orchestrator"]["snapshot"]().get("role")
    if role != "leader":
        return bool(control)
    legacy = context["legacy_mqtt"]["wait_connected"](timeout)
    return bool(legacy and control)


def start_master_runtime(context):
    """Start authority, MQTT and diagnostics in dependency order."""
    with context["lock"]:
        if context["state"] == "running":
            return get_master_snapshot(context)
        context["state"] = "starting"
    started = []
    try:
        seeded = _seed_if_leader(context)
        if not seeded.get("ok"):
            raise RuntimeError(seeded.get("message") or "initial config failed")
        context["control_mqtt"]["start"]()
        started.append(context["control_mqtt"])
        role = context["orchestrator"]["snapshot"]().get("role")
        if not _set_authority_services(context, role):
            raise RuntimeError(
                context.get("last_error") or "authority services failed to activate")
        if role == "leader":
            started.append(context["legacy_mqtt"])
        context["orchestrator"]["start"]()
        started.append(context["orchestrator"])
        if context["config"]["mqtt"].get("required", True) and not _mqtt_ready(context):
            raise RuntimeError("required MQTT broker connections are unavailable")
        context["metrics"]["start"]()
        started.append(context["metrics"])
    except Exception as exc:
        context["last_error"] = str(exc)
        _rollback_started(started)
        try:
            context["store"]["close"]()
        except Exception:
            pass
        with context["lock"]:
            context["state"] = "failed"
        raise
    with context["lock"]:
        context["state"] = "running"
    return get_master_snapshot(context)


def _rollback_started(started):
    for component in reversed(started):
        try:
            component["stop"](join_timeout_s=3.0)
        except Exception:
            continue


def stop_master_runtime(context, **kwargs):
    """Quiesce ingress, transport and persistence."""
    with context["lock"]:
        if context["state"] == "stopped":
            return get_master_snapshot(context)
        context["state"] = "stopping"
    timeout = float(kwargs.get("join_timeout_s", 5.0))
    components = (
        context["metrics"], context["control_mqtt"], context["orchestrator"],
        context["legacy_mqtt"],
    )
    for component in components:
        try:
            component["stop"](join_timeout_s=timeout)
        except Exception as exc:
            context["last_error"] = str(exc)
    context["store"]["close"]()
    with context["lock"]:
        context["state"] = "stopped"
    return get_master_snapshot(context, include_database=False)


def get_master_snapshot(context, **kwargs):
    """Return complete secret-free Registry liveness/readiness diagnostics."""
    include_database = kwargs.get("include_database", True)
    database_ready = context["store"]["ping"]() \
        if include_database and context["store"] else False
    legacy = context["legacy_mqtt"]["snapshot"]() \
        if context.get("legacy_mqtt") else {}
    control = context["control_mqtt"]["snapshot"]() \
        if context.get("control_mqtt") else {}
    mqtt_required = context["config"]["mqtt"].get("required", True)
    role = context["orchestrator"]["snapshot"]().get("role") \
        if context.get("orchestrator") else "standby"
    control_ready = bool(control.get("connected"))
    legacy_ready = bool(legacy.get("transport", {}).get("connected"))
    mqtt_ready = bool(control_ready and (legacy_ready if role == "leader" else True))
    config_ready = _authority_config_ready(context)
    heap = context["heap"]["snapshot"]() if context.get("heap") else {}
    authority_mode = context["config"].get("authority_mode") or "node_local"
    mirrors = ()
    if authority_mode == "node_local" and include_database and database_ready:
        mirrors = context["store"]["authority"]["list_worker_mirrors"]()
    mirror_scopes = tuple(sorted({
        str(item.get("scope") or "")
        for item in mirrors
        if isinstance(item, dict) and str(item.get("scope") or "")
    }))
    registry_snapshot = context["registry_reader"]() \
        if callable(context.get("registry_reader")) else None
    with context["lock"]:
        state = context["state"]
        error = context["last_error"]
        bootstrap = copy.deepcopy(context["bootstrap"])
    return {
        "service": "master-node",
        "site_id": context["config"]["site_id"],
        "instance_id": context["config"]["instance_id"],
        "authority_mode": authority_mode,
        "config_authority": "worker" if authority_mode == "node_local" else "master",
        "state": state,
        "ready": bool(
            state == "running" and database_ready and config_ready
            and (mqtt_ready or not mqtt_required)),
        "database_ready": database_ready,
        "config_ready": config_ready,
        "bootstrap": bootstrap,
        "registry": {
            "role": role,
            "mirror_count": len(mirrors),
            "mirror_scopes": mirror_scopes,
            "static_topology_fingerprint": (
                registry_snapshot or {}).get("fingerprint"),
            "assigns_worker_revision": False if authority_mode == "node_local" else True,
        },
        "orchestrator": context["orchestrator"]["snapshot"]()
        if context.get("orchestrator") else {},
        "master_automation_execution": False,
        "heap": heap,
        "cross_router": context["cross_router"]["snapshot"]()
        if context.get("cross_router") else {},
        "mqtt": {"connected": mqtt_ready, "legacy": legacy, "control": control},
        "metrics": context["metrics"]["snapshot"]()
        if context.get("metrics") else {},
        "last_error": error,
    }

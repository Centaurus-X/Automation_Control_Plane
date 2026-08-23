# -*- coding: utf-8 -*-

# master_node/src/core/config_authority.py

"""
Single-authority configuration mutation and validation.

Every proposal is copied, path-policy checked, schema verified and committed as
one immutable PostgreSQL revision. Worker rollout remains a separate state.
"""

import copy
import re
import threading

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library._generic_system_contract_library import config_compatibility, config_graph, type_library
from src.library._generic_type_library import fingerprint
from src.core.scope_activation import plan_activations


PATH_TOKEN_PATTERN = re.compile(r"([A-Za-z_][A-Za-z0-9_-]*|\d+)")
DEFAULT_ALLOWED_PREFIXES = (
    "actuator_types", "actuators", "control_method_templates",
    "control_methods", "controllers", "filters", "local_node",
    "modbus_functions", "nodes", "parameter_generic_templates",
    "pid_configs", "process_modes", "process_states", "process_values",
    "processes", "rule_engine", "rules", "scaling_profiles",
    "schema_mappings", "sensor_types", "sensors", "site_controller_map",
    "sites", "timers", "virtual_controllers",
)
DENIED_PREFIXES = ("certificates", "credentials", "secrets", "tls")


def _active_authority(context):
    authority = context["authority_reader"]()
    owned = (
        authority
        and authority.get("lease_active")
        and authority.get("leader_instance_id") == context["instance_id"]
        and authority.get("lease_token")
    )
    return authority if owned else None


def parse_path(value):
    """Parse dot, slash and bracket path syntax into safe tokens."""
    text = str(value or "").replace("[", ".").replace("]", "").replace("/", ".")
    tokens = [token for token in text.split(".") if token]
    if not tokens or any(not PATH_TOKEN_PATTERN.fullmatch(token) for token in tokens):
        raise ValueError("configuration path is invalid")
    return tuple(int(token) if token.isdigit() else token for token in tokens)


def _path_allowed(tokens, options):
    first = str(tokens[0]).lower()
    if first in tuple(options.get("denied_prefixes") or DENIED_PREFIXES):
        return False
    allowed = tuple(options.get("allowed_prefixes") or DEFAULT_ALLOWED_PREFIXES)
    return not allowed or first in allowed


def _read_path(root, tokens):
    value = root
    for token in tokens:
        if isinstance(token, int) and isinstance(value, list) and token < len(value):
            value = value[token]
            continue
        if isinstance(token, str) and isinstance(value, dict) and token in value:
            value = value[token]
            continue
        raise KeyError("configuration path does not exist")
    return value


def _parent(root, tokens):
    if len(tokens) < 1:
        raise ValueError("configuration path has no target")
    return _read_path(root, tokens[:-1]) if len(tokens) > 1 else root


def _set_value(parent, token, value):
    if isinstance(parent, dict) and isinstance(token, str):
        parent[token] = copy.deepcopy(value)
        return
    if isinstance(parent, list) and isinstance(token, int) and token < len(parent):
        parent[token] = copy.deepcopy(value)
        return
    raise KeyError("configuration target is invalid")


def _update_value(parent, token, value):
    current = parent[token]
    if not isinstance(current, dict) or not isinstance(value, dict):
        raise ValueError("update requires dictionary values")
    current.update(copy.deepcopy(value))


def _append_value(parent, token, value):
    current = parent[token]
    if not isinstance(current, list):
        raise ValueError("append requires a list target")
    if (
        token in ("sensors", "actuators")
        and isinstance(value, dict)
        and "rows" not in value
        and str(value.get("controller_uuid") or "")
    ):
        controller_uuid = str(value["controller_uuid"])
        positions = [
            index for index, row in enumerate(current)
            if isinstance(row, dict)
            and "rows" not in row
            and str(row.get("controller_uuid") or "") == controller_uuid
        ]
        if positions:
            current.insert(positions[-1] + 1, copy.deepcopy(value))
            return
    current.append(copy.deepcopy(value))


def _delete_value(parent, token):
    if isinstance(parent, dict) and token in parent:
        del parent[token]
        return
    if isinstance(parent, list) and isinstance(token, int) and token < len(parent):
        del parent[token]
        return
    raise KeyError("configuration delete target is invalid")


def apply_changes(config, changes, **kwargs):
    """Apply a complete proposal copy and return audit-ready before/after data."""
    maximum = min(512, max(1, int(kwargs.get("max_changes", 128))))
    if not isinstance(changes, list) or not changes or len(changes) > maximum:
        return make_error("invalid_payload", "changes must be a bounded non-empty list")
    result = copy.deepcopy(config)
    applied = []
    for item in changes:
        changed = _apply_one_change(result, item, kwargs)
        if not changed.get("ok"):
            return changed
        applied.append(changed["change"])
    return make_success(config=result, changes=applied)


def _apply_one_change(config, item, options):
    if not isinstance(item, dict):
        return make_error("invalid_payload", "each change must be an object")
    operation = str(item.get("operation") or "set").lower()
    if operation not in ("set", "update", "append", "delete"):
        return make_error("invalid_payload", "configuration operation is invalid")
    try:
        tokens = parse_path(item.get("path"))
        if not _path_allowed(tokens, options):
            return make_error("authorization_denied", "configuration path is denied")
        before = copy.deepcopy(_read_path(config, tokens))
        parent = _parent(config, tokens)
        handlers = {
            "set": _set_value,
            "update": _update_value,
            "append": _append_value,
            "delete": _delete_value,
        }
        if operation == "delete":
            handlers[operation](parent, tokens[-1])
            after = None
        else:
            handlers[operation](parent, tokens[-1], item.get("value"))
            after = copy.deepcopy(_read_path(config, tokens))
    except (KeyError, TypeError, ValueError) as exc:
        return make_error("invalid_payload", str(exc))
    return make_success(change={
        "operation": operation,
        "path": str(item.get("path")),
        "before": before,
        "after": after,
    })


def validate_worker_config(config, **kwargs):
    """Validate shared schema and, for v2 configs, graph/registry invariants."""
    if not isinstance(config, dict):
        return make_error("invalid_payload", "configuration must be an object")
    try:
        report = type_library.verify_config_data(config)
    except Exception as exc:
        return make_error("invalid_payload", "configuration verification failed", detail=str(exc))
    if report.get("status") != "OK":
        return make_error("invalid_payload", "worker schema rejected configuration", report=report)
    has_status = all(
        isinstance(rule, dict) and rule.get("deployment_status")
        for rule in config.get("rules") or []
    )
    if not has_status and not kwargs.get("require_v2", False):
        return make_success(report=report, mode="legacy_schema")
    graph_report = config_graph.validate_authority_config(
        config, profile=kwargs.get("profile") or "lab_v2")
    if not graph_report.get("ok"):
        return make_error(
            "invalid_payload", "authority graph rejected configuration",
            report=graph_report,
        )
    return make_success(report=report, graph=graph_report, mode="master_v2")


def _commit_candidate(context, candidate, request, changes, **kwargs):
    try:
        candidate = config_compatibility.ensure_v9_storage_config(candidate)
    except ValueError as exc:
        return make_error(
            "invalid_payload", "authority commits require canonical V9",
            detail=str(exc))
    authority = _active_authority(context)
    if not authority:
        return make_error("not_config_authority", "master authority changed before commit")
    current_revision = int(authority.get("config_revision") or 0)
    payload = {
        "site_id": context["site_id"],
        "instance_id": context["instance_id"],
        "lease_token": authority.get("lease_token"),
        "authority_term": authority["authority_term"],
        "base_revision": current_revision,
        "config": copy.deepcopy(candidate),
        "config_format_version": config_compatibility.CANONICAL_VERSION,
        "changes": copy.deepcopy(changes),
        "principal_subject": request["principal_subject"],
        "request_id": request["request_id"],
        "operation_id": request["request_id"],
        "reason": request.get("reason"),
        "targets": request.get("rollout_targets"),
        "source_kind": kwargs.get("source_kind") or "engineering",
        "source_node_id": kwargs.get("source_node_id"),
        "audit_request": {
            "reason": request.get("reason"),
            "changes": copy.deepcopy(changes),
        },
    }
    if not context.get("materializer"):
        return context["commit"](payload)
    heads = context["scope_heads_reader"]()
    snapshots = context["scope_snapshots_reader"]()
    materialized = context["materializer"](
        candidate, current_revision + 1,
        previous_heads=heads, previous_snapshots=snapshots,
        profile=context["profile"],
    )
    activations = plan_activations(
        materialized["scopes"], context["activations_reader"]())
    payload["materialization"] = materialized
    payload["activations"] = activations
    result = context["commit"](payload)
    if result.get("ok") and context.get("state_loader") and context.get("heap"):
        state = context["state_loader"](context["site_id"])
        context["heap"]["replace"](state.get("global"), state.get("scopes") or {})
    return result


def propose_config(context, request):
    """Validate and atomically commit one revisions-bound proposal."""
    authority = _active_authority(context)
    if not authority:
        return make_error("not_config_authority", "master is not active authority")
    current = context["config_reader"]()
    base_revision = int(request.get("base_revision", -1))
    current_revision = int(authority.get("config_revision") or 0)
    if base_revision != current_revision:
        return make_error("stale_revision", "base revision differs", current_revision=current_revision)
    source = current.get("config_data") if current else {}
    prepared = apply_changes(source, request.get("changes"), **context["policy"])
    if not prepared.get("ok"):
        return prepared
    effective_changes = [
        change for change in prepared["changes"]
        if change.get("before") != change.get("after")
    ]
    if not effective_changes:
        return make_success(
            changed=False,
            revision=current_revision,
            fingerprint=authority.get("config_fingerprint"),
            targets=(),
        )
    verified = validate_worker_config(
        prepared["config"], profile=context["profile"],
        require_v2=bool(context.get("materializer")))
    if not verified.get("ok"):
        return verified
    return _commit_candidate(
        context, prepared["config"], request, effective_changes,
        source_kind="engineering")


def propose_action_plan(context, request):
    """Commit the existing worker RuleEngine action-plan shape centrally."""
    authority = _active_authority(context)
    if not authority:
        return make_error("not_config_authority", "master is not active authority")
    base_revision = int(request.get("base_revision", -1))
    current_revision = int(authority.get("config_revision") or 0)
    if base_revision != current_revision:
        return make_error("stale_revision", "action plan is stale", current_revision=current_revision)
    current = context["config_reader"]()
    config = copy.deepcopy(current.get("config_data") if current else {})
    applied = _apply_worker_actions(config, request.get("actions") or [])
    if not applied.get("ok"):
        return applied
    if not applied["changes"]:
        return make_success(
            changed=False,
            revision=current_revision,
            fingerprint=authority.get("config_fingerprint"),
            targets=(),
        )
    verified = validate_worker_config(
        applied["config"], profile=context["profile"],
        require_v2=bool(context.get("materializer")))
    if not verified.get("ok"):
        return verified
    prepared = copy.deepcopy(request)
    prepared["reason"] = "worker RuleEngine action plan"
    return _commit_candidate(
        context, applied["config"], prepared, applied["changes"],
        source_kind="worker_proposal",
        source_node_id=request.get("source_node_id"),
    )


def rollback_config(context, request):
    """Create a new immutable revision from one verified historical snapshot."""
    authority = _active_authority(context)
    if not authority:
        return make_error("not_config_authority", "master is not active authority")
    current_revision = int(authority.get("config_revision") or 0)
    base_revision = int(request.get("base_revision", -1))
    target_revision = int(request.get("target_revision", -1))
    if base_revision != current_revision:
        return make_error("stale_revision", "base revision differs", current_revision=current_revision)
    if target_revision >= current_revision or target_revision < 1:
        return make_error("invalid_payload", "rollback target must be an older revision")
    target = context["config_by_revision"](target_revision)
    if not target:
        return make_error("not_found", "rollback target revision is unknown")
    target_config = copy.deepcopy(target["config_data"])
    if config_compatibility.get_config_version(target_config) == \
            config_compatibility.LEGACY_VERSION:
        migrated = config_graph.migrate_legacy_config(
            target_config, profile=context["profile"])
        if not migrated.get("ok"):
            return make_error(
                "migration_required",
                "legacy rollback target cannot be converted safely",
                report=migrated.get("report"),
            )
        target_config = migrated["config"]
    verified = validate_worker_config(
        target_config, profile=context["profile"],
        require_v2=bool(context.get("materializer")))
    if not verified.get("ok"):
        return verified
    changes = [{
            "operation": "set",
            "path": "$rollback",
            "before": {"revision": current_revision, "fingerprint": authority.get("config_fingerprint")},
            "after": {"revision": target_revision, "fingerprint": target.get("fingerprint")},
        }]
    prepared = copy.deepcopy(request)
    prepared["reason"] = request.get("reason") or "rollback to revision " + str(target_revision)
    return _commit_candidate(
        context, target_config, prepared, changes,
        source_kind="rollback")


def commit_prepared_config(context, request):
    """Validate and commit an owner-mapped v2 proposal candidate."""
    authority = _active_authority(context)
    if not authority:
        return make_error("not_config_authority", "master is not active authority")
    current_revision = int(authority.get("config_revision") or 0)
    if int(request.get("base_revision", -1)) != current_revision:
        return make_error("stale_revision", "proposal global base differs", current_revision=current_revision)
    candidate = copy.deepcopy(request.get("config"))
    current = context["config_reader"]()
    current_config = current.get("config_data") if isinstance(current, dict) else None
    try:
        unchanged = isinstance(current_config, dict) \
            and fingerprint(candidate) == fingerprint(current_config)
    except (TypeError, ValueError):
        unchanged = False
    if unchanged:
        return make_success(
            changed=False,
            revision=current_revision,
            fingerprint=current.get("fingerprint") or authority.get("config_fingerprint"),
            targets=(),
        )
    verified = validate_worker_config(
        candidate, profile=context["profile"], require_v2=bool(context.get("materializer")))
    if not verified.get("ok"):
        return verified
    return _commit_candidate(
        context, candidate, request, request.get("changes") or [],
        source_kind="worker_proposal",
        source_node_id=request.get("source_node_id"),
    )


def _apply_worker_actions(config, actions):
    states = config.get("process_states")
    if not isinstance(states, list) or not isinstance(actions, list):
        return make_error("invalid_payload", "action plan or process states are invalid")
    prepared = []
    for item in actions:
        action = item.get("action") if isinstance(item, dict) else None
        action = action if isinstance(action, dict) else item
        if not isinstance(action, dict):
            return make_error("invalid_payload", "action plan entry is invalid")
        try:
            required_mode = _required_mode_for_action(config, item)
        except ValueError as exc:
            return make_error("invalid_payload", str(exc))
        prepared.append((action, required_mode))
    changes = []
    for action, required_mode in prepared:
        for index, row in enumerate(states):
            if (
                _action_matches(row, action)
                and _action_mode_allowed(row, required_mode)
            ):
                _apply_worker_action_row(row, action, index, changes)
    if not changes:
        return make_success(config=config, changes=[])
    return make_success(config=config, changes=changes)


def _required_mode_for_action(config, item):
    requested = item.get("engine_id") if isinstance(item, dict) else None
    rule_id = item.get("rule_id") if isinstance(item, dict) else None
    if rule_id is not None:
        rule_engine_id = None
        matched_rule = False
        for rule in config.get("rules") or []:
            if isinstance(rule, dict) and str(rule.get("rule_id")) == str(rule_id):
                rule_engine_id = rule.get("engine_id")
                matched_rule = True
                break
        if not matched_rule or rule_engine_id is None:
            raise ValueError("action plan rule has no known engine")
        if requested is not None and str(requested) != str(rule_engine_id):
            raise ValueError("action plan engine differs from its rule")
        requested = rule_engine_id
    if requested is None:
        return None
    for engine in config.get("rule_engine") or []:
        if not isinstance(engine, dict):
            continue
        if str(engine.get("engine_id")) != str(requested):
            continue
        if engine.get("enabled") is False:
            raise ValueError("action plan engine is disabled")
        return engine.get("required_mode_id")
    raise ValueError("action plan engine is unknown")


def _action_mode_allowed(row, required_mode):
    if required_mode is None:
        return True
    return str(row.get("mode_id")) == str(required_mode)


def _action_matches(row, action):
    if not isinstance(row, dict):
        return False
    state_ids = {str(value) for value in action.get("state_ids") or []}
    process_ids = {str(value) for value in action.get("p_ids") or []}
    return (
        bool(state_ids and str(row.get("state_id")) in state_ids)
        or bool(process_ids and str(row.get("p_id")) in process_ids)
    )


def _apply_worker_action_row(row, action, index, changes):
    if "value_id" in action and row.get("value_id") != action["value_id"]:
        before = copy.deepcopy(row.get("value_id"))
        row["value_id"] = copy.deepcopy(action["value_id"])
        changes.append({
            "operation": "set",
            "path": "process_states.{}.value_id".format(index),
            "before": before,
            "after": copy.deepcopy(action["value_id"]),
        })
    parameters = action.get("parameters")
    if not isinstance(parameters, dict):
        return
    before = copy.deepcopy(row.get("parameters") or {})
    row.setdefault("parameters", {}).update(copy.deepcopy(parameters))
    if before != row["parameters"]:
        changes.append({
            "operation": "update",
            "path": "process_states.{}.parameters".format(index),
            "before": before,
            "after": copy.deepcopy(row["parameters"]),
        })


def seed_config(context, config, **kwargs):
    """Create revision one only while the active authority is still empty."""
    authority = _active_authority(context)
    if not authority:
        return make_error("not_config_authority", "master is not active authority")
    existing = context["config_reader"]()
    if existing:
        return make_success(status="already_seeded")
    current_revision = int(authority.get("config_revision") or 0)
    source_kind = str(kwargs.get("source_kind") or "bootstrap")
    if current_revision != 0 and source_kind != "migration":
        return make_error(
            "inconsistent_storage",
            "authority revision exists without Master-v2 entities or a legacy migration source",
        )
    candidate = copy.deepcopy(config)
    version = config_compatibility.get_config_version(candidate)
    if version == config_compatibility.LEGACY_VERSION:
        if source_kind != "migration":
            return make_error(
                "migration_required", "V5 is accepted only by explicit legacy import")
        migrated = config_graph.migrate_legacy_config(
            candidate, profile=context["profile"])
        if not migrated.get("ok"):
            return make_error(
                "migration_required", "legacy config cannot be released",
                report=migrated.get("report"))
        candidate = migrated["config"]
    elif version != config_compatibility.CANONICAL_VERSION:
        return make_error(
            "invalid_payload", "initial authority configuration must be V9")
    has_status = all(
        isinstance(rule, dict) and rule.get("deployment_status")
        for rule in candidate.get("rules") or []
    )
    if context.get("materializer") and not has_status:
        migrated = config_graph.migrate_legacy_config(candidate, profile=context["profile"])
        if not migrated.get("ok"):
            return make_error("migration_required", "initial config cannot be released")
        candidate = migrated["config"]
    verified = validate_worker_config(
        candidate, profile=context["profile"],
        require_v2=bool(context.get("materializer")))
    if not verified.get("ok"):
        return verified
    request = {
        "principal_subject": str(kwargs.get("principal_subject") or "system:bootstrap"),
        "request_id": str(kwargs.get("request_id") or "bootstrap-master-v2"),
        "reason": "initial verified worker-compatible configuration",
        "rollout_targets": None,
    }
    return _commit_candidate(
        context, candidate, request, [], source_kind=source_kind)


def migrate_current_legacy_config(context, **kwargs):
    """Commit the current entity-store V5 snapshot once as canonical V9."""
    authority = _active_authority(context)
    if not authority:
        return make_error("not_config_authority", "master is not active authority")
    current = context["config_reader"]()
    if not current or not isinstance(current.get("config_data"), dict):
        return make_error("not_found", "current authority configuration is missing")
    source = current["config_data"]
    version = config_compatibility.get_config_version(source)
    if version == config_compatibility.CANONICAL_VERSION:
        return make_success(
            changed=False, status="already_v9",
            revision=int(current.get("revision") or authority.get("config_revision") or 0),
            fingerprint=current.get("fingerprint") or authority.get("config_fingerprint"),
        )
    if version != config_compatibility.LEGACY_VERSION:
        return make_error("migration_required", "stored authority config is neither V5 nor V9")
    migrated = config_graph.migrate_legacy_config(
        source, profile=context["profile"])
    if not migrated.get("ok"):
        return make_error(
            "migration_required", "stored V5 config cannot be migrated safely",
            report=migrated.get("report"))
    current_revision = int(authority.get("config_revision") or 0)
    request_id = str(kwargs.get("request_id") or (
        "legacy-v5-to-v9:{}:{}".format(
            current_revision, current.get("fingerprint") or "unknown")))
    request = {
        "principal_subject": str(
            kwargs.get("principal_subject") or "system:legacy-v5-import"),
        "request_id": request_id,
        "reason": "one-time canonical V5 to V9 authority migration",
        "rollout_targets": None,
    }
    changes = [{
        "operation": "migrate_schema",
        "path": "meta[0].version",
        "before": config_compatibility.LEGACY_VERSION,
        "after": config_compatibility.CANONICAL_VERSION,
    }]
    return _commit_candidate(
        context, migrated["config"], request, changes,
        source_kind="legacy_import")


def _serialized_request(context, handler, request):
    with context["writer_lock"]:
        return handler(context, request)


def _serialized_seed(context, config, **kwargs):
    with context["writer_lock"]:
        return seed_config(context, config, **kwargs)


def _serialized_legacy_migration(context, **kwargs):
    with context["writer_lock"]:
        return migrate_current_legacy_config(context, **kwargs)


def create_config_authority(**kwargs):
    """Create a narrow functional config-authority capability dictionary."""
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "authority_reader": kwargs["authority_reader"],
        "config_reader": kwargs["config_reader"],
        "config_by_revision": kwargs["config_by_revision"],
        "commit": kwargs["commit"],
        "policy": copy.deepcopy(kwargs.get("policy") or {}),
        "profile": str(kwargs.get("profile") or "lab_v2"),
        "materializer": kwargs.get("materializer"),
        "scope_heads_reader": kwargs.get("scope_heads_reader"),
        "scope_snapshots_reader": kwargs.get("scope_snapshots_reader"),
        "activations_reader": kwargs.get("activations_reader"),
        "state_loader": kwargs.get("state_loader"),
        "heap": kwargs.get("heap"),
        "writer_lock": threading.RLock(),
    }
    return {
        "context": context,
        "propose": partial(_serialized_request, context, propose_config),
        "propose_action_plan": partial(_serialized_request, context, propose_action_plan),
        "rollback": partial(_serialized_request, context, rollback_config),
        "commit_prepared": partial(_serialized_request, context, commit_prepared_config),
        "seed": partial(_serialized_seed, context),
        "migrate_legacy": partial(_serialized_legacy_migration, context),
        "validate": validate_worker_config,
    }

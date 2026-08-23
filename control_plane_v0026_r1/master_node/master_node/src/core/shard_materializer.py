# -*- coding: utf-8 -*-

"""Validated materialization of independently versioned Node/Cross scopes."""

import copy

from functools import partial

from src.library._generic_system_contract_library import (
    config_compatibility, config_graph, shard_library, type_library,
)
from src.library._generic_system_contract_library.protocol_contracts import CONTRACTS_VERSION
from src.library._generic_type_library import fingerprint
from src.library._generic_system_contract_library.config_sharding_contracts import diff_configs


def _enabled_nodes(config):
    return tuple(sorted(
        str(node.get("node_id"))
        for node in config.get("nodes") or []
        if isinstance(node, dict)
        and node.get("enabled") is not False
        and str(node.get("node_role") or "").lower() == "worker"
        and node.get("node_id")
    ))


def _previous_snapshot(previous_snapshots, scope):
    value = previous_snapshots.get(scope) if isinstance(previous_snapshots, dict) else None
    return value if isinstance(value, dict) else None


def _next_scope(scope, kind, owner, config, global_revision, previous_heads, previous_snapshots):
    if kind == "cross":
        config = config_compatibility.ensure_v8_cross_storage_config(config)
        scope_fingerprint = fingerprint(config)
    else:
        config = config_compatibility.ensure_v9_storage_config(config)
        scope_fingerprint = type_library.config_fingerprint(config)
    previous = previous_heads.get(scope) if isinstance(previous_heads, dict) else None
    previous = previous if isinstance(previous, dict) else {}
    changed = previous.get("fingerprint") != scope_fingerprint
    revision = int(previous.get("scope_revision") or 0) + int(changed)
    if revision < 1:
        revision = 1
    same_owner = previous.get("owner_node_id") in (None, owner)
    epoch = int(previous.get("execution_epoch") or 0)
    if epoch < 1 or not same_owner:
        epoch += 1
    snapshot = {
        "scope": scope,
        "scope_kind": kind,
        "owner_node_id": owner,
        "scope_revision": revision,
        "global_revision": int(global_revision),
        "fingerprint": scope_fingerprint,
        "contracts_version": CONTRACTS_VERSION,
        "execution_epoch": epoch,
        "changed": changed,
        "config": copy.deepcopy(config),
    }
    old_snapshot = _previous_snapshot(previous_snapshots, scope)
    old_config = old_snapshot.get("config") if old_snapshot else None
    old_version = config_compatibility.get_config_version(old_config)
    compatible_node = kind == "node" \
        and old_version == config_compatibility.CANONICAL_VERSION
    compatible_cross = kind == "cross" \
        and old_version == config_compatibility.CROSS_VERSION
    snapshot["operations"] = diff_configs(
        old_config, config, allow_cross_v8=compatible_cross) \
        if changed and old_config and (compatible_node or compatible_cross) \
        else ()
    return snapshot


def materialize_scopes(context, config, global_revision, **kwargs):
    """Validate one authority config and materialize all changed scope heads."""
    config = config_compatibility.ensure_v9_storage_config(config)
    profile = str(kwargs.get("profile") or context["profile"])
    validation = config_graph.validate_authority_config(config, profile=profile)
    if not validation.get("ok"):
        raise ValueError("authority config rejected: " + "; ".join(validation["errors"]))
    active = config_graph.active_projection(config)
    previous_heads = kwargs.get("previous_heads") or {}
    previous_snapshots = kwargs.get("previous_snapshots") or {}
    scopes = {}
    for node_id in _enabled_nodes(active):
        local = shard_library.build_local_execution_shard(active, node_id)
        local_validation = shard_library.validate_local_execution_shard(local, node_id)
        type_report = type_library.verify_config_data(local)
        if not local_validation.get("ok") or type_report.get("status") != "OK":
            raise ValueError("invalid Node shard for " + node_id)
        scope = "node:" + node_id
        scopes[scope] = _next_scope(
            scope, "node", node_id, local, global_revision,
            previous_heads, previous_snapshots,
        )
        cross = shard_library.build_cross_execution_shard(active, node_id)
        if not cross.get("rules"):
            continue
        cross_validation = shard_library.validate_cross_execution_shard(cross, node_id)
        if not cross_validation.get("ok"):
            raise ValueError("invalid Cross shard for " + node_id)
        cross_scope = "cross:" + node_id + ".default"
        scopes[cross_scope] = _next_scope(
            cross_scope, "cross", node_id, cross, global_revision,
            previous_heads, previous_snapshots,
        )
    removed = tuple(sorted(set(previous_heads) - set(scopes)))
    return {
        "global_revision": int(global_revision),
        "global_fingerprint": type_library.config_fingerprint(config),
        "graph": config_graph.build_graph(config),
        "scopes": scopes,
        "removed_scopes": removed,
        "validation": validation,
    }


def create_shard_materializer(**kwargs):
    """Create a bound materializer with an explicit release profile."""
    context = {"profile": str(kwargs.get("profile") or "lab_v2")}
    return {
        "context": context,
        "materialize": partial(materialize_scopes, context),
    }

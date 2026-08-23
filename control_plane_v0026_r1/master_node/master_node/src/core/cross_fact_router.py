# -*- coding: utf-8 -*-

"""Ownership-checked latest-value routing to affected Cross owners only."""

import copy
import datetime
import threading
import time

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success


def _sensor_references(value, result):
    if isinstance(value, dict):
        controller_uuid = value.get("controller_uuid")
        sensor_id = value.get("sensor_id")
        if controller_uuid is not None and sensor_id is not None:
            result.add((str(controller_uuid), str(sensor_id)))
        for child in value.values():
            _sensor_references(child, result)
    elif isinstance(value, list):
        for child in value:
            _sensor_references(child, result)


def build_dependency_map(scope_snapshots):
    """Build a map from logical sensor identities to Cross scope owners."""
    dependencies = {}
    for scope, snapshot in sorted(scope_snapshots.items()):
        if snapshot.get("scope_kind") != "cross":
            continue
        references = set()
        _sensor_references(snapshot.get("config") or {}, references)
        for identity in references:
            dependencies.setdefault(identity, []).append({
                "scope": scope,
                "owner_node_id": snapshot.get("owner_node_id"),
                "execution_epoch": snapshot.get("execution_epoch"),
            })
    return {
        identity: tuple(sorted(rows, key=dependency_key))
        for identity, rows in dependencies.items()
    }


def dependency_key(item):
    """Sort Cross targets deterministically."""
    return (str(item.get("owner_node_id") or ""), str(item.get("scope") or ""))


def replace_dependencies(context, scope_snapshots, controller_owners):
    """Atomically replace the derived dependency and ownership maps."""
    dependencies = build_dependency_map(scope_snapshots)
    with context["lock"]:
        context["dependencies"] = dependencies
        context["controller_owners"] = copy.deepcopy(controller_owners)
    return len(dependencies)


def route_fact(context, fact, **kwargs):
    """Validate source ownership, sequence and age, then route without evaluation."""
    controller_uuid = str(fact.get("controller_uuid") or "")
    sensor_id = str(fact.get("sensor_id") or "")
    source_node = str(kwargs.get("source_node") or fact.get("source_node_id") or "")
    if not controller_uuid or not sensor_id:
        return make_error("invalid_payload", "sensor identity is incomplete")
    now = float(kwargs.get("now_s", time.time()))
    measured = fact.get("timestamp")
    try:
        if isinstance(measured, str):
            normalized = measured.replace("Z", "+00:00")
            measured_s = datetime.datetime.fromisoformat(normalized).timestamp()
        else:
            measured_s = float(measured) if measured is not None else now
    except Exception:
        return make_error("invalid_payload", "sensor timestamp is invalid")
    if now - measured_s > context["max_signal_age_s"] or measured_s > now + 30.0:
        return make_error("stale_fact", "sensor fact is outside the accepted time window")
    sequence = fact.get("sequence")
    sequence = int(sequence) if sequence is not None else None
    identity = (controller_uuid, sensor_id)
    with context["lock"]:
        expected_owner = context["controller_owners"].get(controller_uuid)
        if expected_owner != source_node:
            return make_error("ownership", "sensor source is not the controller owner")
        previous = context["latest"].get(identity)
        if sequence is not None and previous and previous.get("sequence") is not None:
            if sequence <= previous["sequence"]:
                return make_success(duplicate=True, routed=0)
        context["latest"][identity] = {
            "sequence": sequence,
            "received_at_s": now,
        }
        targets = copy.deepcopy(context["dependencies"].get(identity, ()))
    routed = []
    for target in targets:
        envelope = {
            "event_id": str(fact.get("event_id") or "fact:{}:{}:{}".format(
                controller_uuid, sensor_id, sequence if sequence is not None else measured_s)),
            "event_type": "SENSOR_VALUE_UPDATE",
            "payload": copy.deepcopy(fact),
            "cross_scope": target["scope"],
            "execution_epoch": target["execution_epoch"],
        }
        publisher = context.get("publisher")
        accepted = bool(publisher(target["owner_node_id"], envelope)) if callable(publisher) else True
        if accepted:
            routed.append(target["owner_node_id"])
    return make_success(routed=len(routed), target_nodes=tuple(routed))


def snapshot_router(context):
    """Return bounded routing diagnostics."""
    with context["lock"]:
        return {
            "dependency_count": len(context["dependencies"]),
            "latest_fact_count": len(context["latest"]),
            "owner_count": len(context["controller_owners"]),
        }


def create_cross_fact_router(**kwargs):
    """Create an isolated Cross fact routing capability."""
    context = {
        "lock": threading.RLock(),
        "dependencies": {},
        "controller_owners": {},
        "latest": {},
        "max_signal_age_s": max(0.1, float(kwargs.get("max_signal_age_s", 30.0))),
        "publisher": kwargs.get("publisher"),
    }
    return {
        "context": context,
        "replace_dependencies": partial(replace_dependencies, context),
        "route": partial(route_fact, context),
        "snapshot": partial(snapshot_router, context),
    }

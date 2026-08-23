# -*- coding: utf-8 -*-

"""No-GIL-safe atomic in-memory snapshots reconstructed from PostgreSQL."""

import copy
import threading

from functools import partial


def replace_heap(context, global_snapshot, scope_snapshots):
    """Atomically replace all immutable heap pointers after a durable commit."""
    prepared_global = copy.deepcopy(global_snapshot)
    prepared_scopes = copy.deepcopy(scope_snapshots)
    with context["lock"]:
        context["global_snapshot"] = prepared_global
        context["scope_snapshots"] = prepared_scopes
        context["generation"] += 1
        return context["generation"]


def get_global_snapshot(context):
    """Return a detached global snapshot."""
    with context["lock"]:
        return copy.deepcopy(context["global_snapshot"])


def get_scope_snapshot(context, scope):
    """Return a detached current scope snapshot."""
    with context["lock"]:
        return copy.deepcopy(context["scope_snapshots"].get(str(scope)))


def get_scope_heads(context):
    """Return detached head metadata without full configs."""
    with context["lock"]:
        result = {}
        for scope, snapshot in context["scope_snapshots"].items():
            result[scope] = {
                key: copy.deepcopy(value)
                for key, value in snapshot.items()
                if key != "config"
            }
        return result


def get_heap_snapshot(context):
    """Return secret-free heap diagnostics."""
    with context["lock"]:
        return {
            "generation": context["generation"],
            "global_revision": (context["global_snapshot"] or {}).get("global_revision", 0),
            "global_fingerprint": (context["global_snapshot"] or {}).get("fingerprint"),
            "scope_count": len(context["scope_snapshots"]),
            "scopes": tuple(sorted(context["scope_snapshots"])),
        }


def create_master_heap_store():
    """Create the functional heap capability dictionary."""
    context = {
        "lock": threading.RLock(),
        "generation": 0,
        "global_snapshot": None,
        "scope_snapshots": {},
    }
    return {
        "context": context,
        "replace": partial(replace_heap, context),
        "get_global": partial(get_global_snapshot, context),
        "get_scope": partial(get_scope_snapshot, context),
        "get_scope_heads": partial(get_scope_heads, context),
        "snapshot": partial(get_heap_snapshot, context),
    }


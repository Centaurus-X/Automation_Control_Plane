# -*- coding: utf-8 -*-

# master_node/src/core/cluster_orchestrator.py

"""Leader lease renewal, fenced maintenance and durable outbox dispatch."""

import copy
import threading
import time

from functools import partial


def _no_operation_expiry(**_kwargs):
    return 0


def _renew(context, **kwargs):
    with context["lock"]:
        token = context.get("lease_token")
        previous_term = context.get("authority_term", 0)
        previous_role = context.get("role", "standby")
    result = context["authority_store"]["acquire_or_renew"](
        context["site_id"], context["instance_id"],
        eligible=context["leader_eligible"],
        lease_token=token,
        lease_s=context["lease_s"],
        term_floor=context["authority_term_floor"],
    )
    authority = result.get("authority") if result.get("ok") else {}
    with context["lock"]:
        context["role"] = result.get("role", "standby")
        context["lease_token"] = authority.get("lease_token")
        context["authority_term"] = int(authority.get("authority_term") or 0)
        context["last_renew_monotonic"] = time.monotonic()
        context["last_error"] = None if result.get("ok") else result.get("message")
    role = result.get("role", "standby")
    notified = kwargs.get("notify", True)
    accepted = _notify_role(context, role, previous_role) if notified else False
    with context["lock"]:
        context["authority_services_ready"] = bool(role == "leader" and accepted)
    current_term = int(authority.get("authority_term") or 0)
    recover_new_term = bool(accepted or kwargs.get("recover_new_term", False))
    if role == "leader" and recover_new_term and current_term > previous_term:
        context["operation_store"]["recover_uncertain"](context["site_id"], current_term)
    return result


def _notify_role(context, role, previous_role):
    handler = context.get("role_change_handler")
    if not callable(handler):
        return True
    try:
        accepted = bool(handler(str(role), previous_role=str(previous_role)))
        if accepted:
            return True
        with context["lock"]:
            context["last_error"] = "authority role handler rejected " + str(role)
            context["statistics"]["maintenance_errors"] += 1
        return False
    except Exception as exc:
        with context["lock"]:
            context["last_error"] = str(exc)
            context["statistics"]["maintenance_errors"] += 1
        return False


def _leader_maintenance(context):
    expired = context["expire_operations"](limit=context["outbox_batch_size"])
    published = context["publish_due_outbox"](limit=context["outbox_batch_size"])
    with context["lock"]:
        context["statistics"]["operations_expired"] += int(expired)
        context["statistics"]["outbox_published"] += int(published)
    now = time.monotonic()
    with context["lock"]:
        due = now - context["last_ownership_monotonic"] >= context["ownership_interval_s"]
    if not due:
        _run_retention_if_due(context, now)
        return
    snapshot = context["config_reader"]()
    if snapshot:
        result = context["ownership"]["reconcile"](snapshot["config_data"])
        with context["lock"]:
            context["statistics"]["ownership_reconciles"] += int(bool(result.get("ok")))
            context["last_ownership_monotonic"] = now
    _run_retention_if_due(context, now)


def _run_retention_if_due(context, now):
    if not callable(context.get("retention_cleanup")):
        return False
    with context["lock"]:
        due = now - context["last_retention_monotonic"] >= context["retention_interval_s"]
    if not due:
        return False
    context["retention_cleanup"](**context["retention_options"])
    with context["lock"]:
        context["last_retention_monotonic"] = now
        context["statistics"]["retention_runs"] += 1
    return True


def _orchestrator_loop(context):
    with context["lock"]:
        context["state"] = "running"
    next_renew = 0.0
    while not context["stop_event"].is_set():
        now = time.monotonic()
        if now >= next_renew:
            with context["lock"]:
                previous_role = context["role"]
            try:
                renewed = _renew(context)
            except Exception as exc:
                renewed = {"ok": False, "role": "standby", "message": str(exc)}
                with context["lock"]:
                    context["role"] = "standby"
                    context["authority_services_ready"] = False
                    context["last_error"] = str(exc)
                _notify_role(context, "standby", previous_role)
            next_renew = now + context["renew_interval_s"]
        else:
            renewed = {"role": context["role"]}
        with context["lock"]:
            authority_services_ready = context["authority_services_ready"]
        if renewed.get("role") == "leader" and authority_services_ready:
            try:
                _leader_maintenance(context)
            except Exception as exc:
                with context["lock"]:
                    context["last_error"] = str(exc)
                    context["statistics"]["maintenance_errors"] += 1
        context["stop_event"].wait(context["loop_interval_s"])
    with context["lock"]:
        context["state"] = "stopped"


def start_cluster_orchestrator(context):
    """Start the single bounded orchestrator thread."""
    thread = context.get("thread")
    if thread and thread.is_alive():
        return thread
    context["stop_event"].clear()
    thread = threading.Thread(
        target=partial(_orchestrator_loop, context),
        name="MasterOrchestrator-" + context["instance_id"],
        daemon=False,
    )
    context["thread"] = thread
    thread.start()
    return thread


def stop_cluster_orchestrator(context, **kwargs):
    """Stop the orchestrator without promoting another instance implicitly."""
    context["stop_event"].set()
    thread = context.get("thread")
    if thread and thread is not threading.current_thread():
        thread.join(timeout=float(kwargs.get("join_timeout_s", 5.0)))
    return snapshot_cluster_orchestrator(context)


def snapshot_cluster_orchestrator(context):
    """Return secret-free authority and maintenance state."""
    with context["lock"]:
        return {
            "state": context["state"],
            "role": context["role"],
            "leader_eligible": context["leader_eligible"],
            "authority_term": context["authority_term"],
            "authority_services_ready": context["authority_services_ready"],
            "thread_alive": bool(context["thread"] and context["thread"].is_alive()),
            "statistics": copy.deepcopy(context["statistics"]),
            "last_error": context["last_error"],
        }


def create_cluster_orchestrator(**kwargs):
    """Create the functional cluster orchestrator."""
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "leader_eligible": bool(kwargs.get("leader_eligible", False)),
        "authority_store": kwargs["authority_store"],
        "operation_store": kwargs["operation_store"],
        "ownership": kwargs["ownership"],
        "config_reader": kwargs["config_reader"],
        "publish_due_outbox": kwargs["publish_due_outbox"],
        "expire_operations": kwargs.get("expire_operations") or _no_operation_expiry,
        "role_change_handler": kwargs.get("role_change_handler"),
        "retention_cleanup": kwargs.get("retention_cleanup"),
        "retention_options": copy.deepcopy(kwargs.get("retention_options") or {}),
        "authority_term_floor": max(0, int(kwargs.get("authority_term_floor", 0))),
        "lease_s": max(5.0, float(kwargs.get("lease_s", 15.0))),
        "renew_interval_s": max(1.0, float(kwargs.get("renew_interval_s", 5.0))),
        "ownership_interval_s": max(1.0, float(kwargs.get("ownership_interval_s", 10.0))),
        "loop_interval_s": max(0.05, float(kwargs.get("loop_interval_s", 0.2))),
        "outbox_batch_size": min(256, max(1, int(kwargs.get("outbox_batch_size", 64)))),
        "retention_interval_s": max(30.0, float(kwargs.get("retention_interval_s", 300.0))),
        "lock": threading.RLock(),
        "stop_event": threading.Event(),
        "thread": None,
        "state": "created",
        "role": "standby",
        "lease_token": None,
        "authority_term": 0,
        "authority_services_ready": False,
        "last_renew_monotonic": 0.0,
        "last_ownership_monotonic": 0.0,
        "last_retention_monotonic": 0.0,
        "last_error": None,
        "statistics": {
            "outbox_published": 0, "ownership_reconciles": 0,
            "operations_expired": 0, "retention_runs": 0,
            "maintenance_errors": 0,
        },
    }
    return {
        "context": context,
        "start": partial(start_cluster_orchestrator, context),
        "stop": partial(stop_cluster_orchestrator, context),
        "snapshot": partial(snapshot_cluster_orchestrator, context),
        "renew_now": partial(
            _renew, context, notify=False, recover_new_term=True),
    }

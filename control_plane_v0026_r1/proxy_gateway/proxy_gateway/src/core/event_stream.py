# -*- coding: utf-8 -*-

# proxy_gateway/src/core/event_stream.py

"""Authorized, bounded event fanout with atomic replay/live handoff."""

import asyncio
import copy
import fnmatch
import threading
import time
import uuid

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.core.access_policy import authorize


CRITICAL_EVENT_TYPES = frozenset((
    "command.ack",
    "operation.result",
    "config.proposal.result",
    "config.rollout",
    "security.event",
))


def validate_filters(filters):
    """Validate logical resource filters and reject raw MQTT syntax."""
    if not isinstance(filters, list) or len(filters) > 32:
        return make_error("invalid_payload", "subscriptions must be a bounded list")
    normalized = []
    for item in filters:
        if not isinstance(item, dict) or set(item) != {
                "resource_type", "resource_pattern"}:
            return make_error(
                "invalid_payload", "subscription fields are invalid")
        resource_type = item.get("resource_type")
        pattern = item.get("resource_pattern")
        if not isinstance(resource_type, str) or not isinstance(pattern, str):
            return make_error(
                "invalid_payload", "subscription values must be strings")
        if not resource_type or not pattern or any(
                token in resource_type + pattern for token in ("/", "#", "+")):
            return make_error("invalid_payload", "raw topic syntax is forbidden")
        if len(resource_type) > 64 or len(pattern) > 256:
            return make_error("invalid_payload", "subscription value is too long")
        normalized.append({
            "resource_type": resource_type,
            "resource_pattern": pattern,
        })
    return make_success(filters=normalized)


def _filter_matches(client, resource_type, resource_id):
    filters = client["filters"]
    if not filters:
        return True
    return any(
        item["resource_type"] == str(resource_type)
        and fnmatch.fnmatchcase(str(resource_id), item["resource_pattern"])
        for item in filters
    )


def _event_permission(envelope, resource_type):
    event_type = str(envelope.get("event_type") or "")
    if event_type == "command.ack":
        return "command.ack"
    if event_type.startswith("operation."):
        return "operation.read"
    if event_type.startswith("config."):
        return "config.read"
    if event_type.startswith("security.") or event_type.startswith("audit."):
        return "audit.read"
    if event_type.startswith("sensor.") or event_type.startswith("node.") or \
            event_type.startswith("controller.") or \
            event_type.startswith("actuator."):
        return "node.read"
    return None


def operation_id_from_envelope(envelope):
    """Return the bounded operation identity carried by one operation event."""
    if not isinstance(envelope, dict) or not str(
            envelope.get("event_type") or "").startswith("operation."):
        return None
    payload = envelope.get("payload") \
        if isinstance(envelope.get("payload"), dict) else {}
    operation_id = payload.get("operation_id")
    if not isinstance(operation_id, str) or not 1 <= len(operation_id) <= 128:
        return None
    return operation_id


def bind_operation_owner(envelope, owner_subject):
    """Bind a Proxy-resolved request owner while retaining producer identity."""
    subject = str(owner_subject or "")
    if not subject:
        return copy.deepcopy(envelope)
    result = copy.deepcopy(envelope)
    principal = result.get("principal") \
        if isinstance(result.get("principal"), dict) else {}
    producer_subject = str(principal.get("subject") or "")
    result["principal"] = {"subject": subject}
    if producer_subject and producer_subject != subject:
        extensions = result.setdefault("extensions", {})
        if isinstance(extensions, dict):
            extensions["producer_subject"] = producer_subject
    return result


def _principal_projection(principal):
    resources = copy.deepcopy(principal.get("resource_scopes") or [])
    resources.sort(key=_scope_sort_key)
    return {
        "subject": str(principal.get("subject") or ""),
        "auth_type": str(principal.get("auth_type") or ""),
        "permissions": sorted(str(value) for value in principal.get("permissions") or []),
        "site_scopes": sorted(str(value) for value in principal.get("site_scopes") or []),
        "resource_scopes": resources,
    }


def _scope_sort_key(value):
    """Return a deterministic key without anonymous functions."""
    return repr(sorted(value.items())) if isinstance(value, dict) else repr(value)


def _principal_can_receive(client, envelope, resource_type, resource_id):
    principal = client["principal"]
    sites = set(principal.get("site_scopes") or [])
    if client["site_id"] not in sites and "*" not in sites:
        return False
    permission = _event_permission(envelope, resource_type)
    event_principal = envelope.get("principal") \
        if isinstance(envelope.get("principal"), dict) else {}
    if permission in ("command.ack", "operation.read"):
        if str(event_principal.get("subject") or "") == str(
                principal.get("subject") or ""):
            return True
        permission = "audit.read"
    if permission is None:
        return False
    return authorize(
        principal,
        permission,
        site_id=client["site_id"],
        resource_type=resource_type,
        resource_id=resource_id,
    ).get("ok") is True


def client_accepts(client, resource_type, resource_id):
    """Expose logical filter matching for compatibility callers."""
    with client["lock"]:
        return _filter_matches(client, resource_type, resource_id)


def replay_allowed(client, envelope, resource_type, resource_id):
    """Apply filter and event authorization to one replay record."""
    with client["lock"]:
        return bool(
            _filter_matches(client, resource_type, resource_id)
            and _principal_can_receive(
                client, envelope, resource_type, resource_id)
        )


def _coalesce_key(resource_type, resource_id, envelope):
    return "{}:{}:{}".format(
        resource_type, resource_id, envelope.get("event_type"))


def _coalesced_sequence(coalesced, key):
    return int(coalesced[key].get("event_sequence") or 0)


def _signal_disconnect(client):
    client["disconnect_event"].set()


def _enqueue_client(client, envelope, resource_type, resource_id, event_sequence):
    with client["lock"]:
        if client["disconnect"] or not _filter_matches(
                client, resource_type, resource_id) or not _principal_can_receive(
                    client, envelope, resource_type, resource_id):
            return
        generation = client["generation"]
        record = {
            "event_sequence": event_sequence,
            "generation": generation,
            "envelope": copy.deepcopy(envelope),
            "queued_monotonic": time.monotonic(),
        }
        try:
            client["queue"].put_nowait(record)
            return
        except asyncio.QueueFull:
            pass
        critical = str(envelope.get("event_type") or "") in CRITICAL_EVENT_TYPES
        if critical:
            client["disconnect"] = True
            client["statistics"]["critical_overflow"] += 1
            client["loop"].call_soon(_signal_disconnect, client)
            return
        key = _coalesce_key(resource_type, resource_id, envelope)
        if key not in client["coalesced"] and len(client["coalesced"]) >= \
                client["coalesced_maxsize"]:
            client["disconnect"] = True
            client["statistics"]["telemetry_overflow_disconnect"] += 1
            client["loop"].call_soon(_signal_disconnect, client)
            return
        client["coalesced"][key] = record
        client["statistics"]["coalesced"] += 1


def register_client(context, **kwargs):
    """Register one loop-owned bounded outbound queue."""
    checked = validate_filters(kwargs.get("filters") or [])
    if not checked.get("ok"):
        return checked
    site_id = str(kwargs.get("site_id") or "")
    principal = copy.deepcopy(kwargs.get("principal") or {})
    sites = set(principal.get("site_scopes") or [])
    if site_id not in sites and "*" not in sites:
        return make_error("authorization_denied", "site scope is denied")
    queue_size = min(
        context["queue_maxsize"],
        max(1, int(kwargs.get("queue_maxsize", context["queue_maxsize"]))),
    )
    client_id = str(kwargs.get("client_id") or uuid.uuid4())
    client = {
        "client_id": client_id,
        "loop": kwargs.get("loop") or asyncio.get_running_loop(),
        "queue": asyncio.Queue(maxsize=queue_size),
        "disconnect_event": asyncio.Event(),
        "filters": checked["filters"],
        "site_id": site_id,
        "principal": principal,
        "lock": threading.RLock(),
        "coalesced": {},
        "coalesced_maxsize": context["coalesced_maxsize"],
        "disconnect": False,
        "generation": 1,
        "statistics": {
            "sent": 0,
            "coalesced": 0,
            "critical_overflow": 0,
            "telemetry_dropped": 0,
            "telemetry_overflow_disconnect": 0,
            "stale_generation_dropped": 0,
        },
    }
    with context["lock"]:
        if client_id in context["clients"]:
            return make_error("conflict", "event client already exists")
        context["clients"][client_id] = client
    return {"ok": True, "client": client}


def unregister_client(context, client_id):
    """Remove one client without touching another Proxy instance."""
    with context["lock"]:
        client = context["clients"].pop(str(client_id), None)
        if client is None:
            return False
        with client["lock"]:
            context["lifetime_statistics"]["critical_overflow"] += int(
                client["statistics"]["critical_overflow"])
            context["lifetime_statistics"]["telemetry_overflow_disconnect"] += int(
                client["statistics"]["telemetry_overflow_disconnect"])
            context["lifetime_statistics"]["telemetry_dropped"] += int(
                client["statistics"]["telemetry_dropped"])
        return True


async def open_subscription(context, request):
    """Register live delivery before reading its durable high-water mark."""
    registered = register_client(
        context,
        client_id=request.get("client_id"),
        site_id=request.get("site_id"),
        principal=request.get("principal"),
        filters=request.get("filters"),
        queue_maxsize=request.get("live_queue_maxsize"),
    )
    if not registered.get("ok"):
        return registered
    client = registered["client"]
    try:
        window = await asyncio.to_thread(
            context["gateway_store"]["replay_window"], client["site_id"])
    except asyncio.CancelledError:
        unregister_client(context, client["client_id"])
        raise
    except Exception:
        unregister_client(context, client["client_id"])
        return make_error("not_ready", "replay store is unavailable")
    high_watermark = int(window.get("high_watermark") or 0)
    retention_floor = int(window.get("retention_floor") or 0)
    client["replay_snapshot_at"] = window.get("snapshot_at")
    after_sequence = int(request.get("after_sequence") or 0)
    if after_sequence > 0 and retention_floor > 0 and \
            after_sequence < retention_floor - 1:
        unregister_client(context, client["client_id"])
        return make_error(
            "replay_resync_required", "replay cursor predates retention")
    return {
        "ok": True,
        "subscription_id": client["client_id"],
        "live_queue": client["queue"],
        "disconnect_event": client["disconnect_event"],
        "generation": client["generation"],
        "high_watermark": high_watermark,
        "retention_floor": retention_floor,
    }


def _subscription(context, request):
    with context["lock"]:
        return context["clients"].get(str(request.get("subscription_id") or ""))


async def read_replay_page(context, request):
    """Read and authorize one durable replay page at one generation."""
    client = _subscription(context, request)
    if not client:
        return make_error("not_found", "event subscription is unknown")
    with client["lock"]:
        if int(request.get("generation", -1)) != client["generation"]:
            return make_error(
                "replay_resync_required", "subscription generation changed")
        principal = copy.deepcopy(client["principal"])
        filters = copy.deepcopy(client["filters"])
    if principal != request.get("principal"):
        return make_error(
            "replay_resync_required", "principal changed during replay")
    try:
        page = await asyncio.to_thread(
            context["gateway_store"]["replay_page"],
            {
                "site_id": client["site_id"],
                "after_sequence": request.get("after_sequence"),
                "through_sequence": request.get("through_sequence"),
                "limit": request.get("limit"),
                "snapshot_at": client.get("replay_snapshot_at"),
            },
        )
    except Exception:
        return make_error("not_ready", "replay store is unavailable")
    if page.get("gap"):
        return make_success(
            records=[], gap=True,
            next_sequence=int(request.get("after_sequence") or 0),
            complete=False,
        )
    operation_ids = {
        operation_id_from_envelope(record.get("envelope"))
        for record in page.get("records") or []
        if isinstance(record, dict)
    }
    operation_ids.discard(None)
    owners = {}
    resolver = context["gateway_store"].get("operation_owners")
    if operation_ids and callable(resolver):
        try:
            resolved = await asyncio.to_thread(
                resolver, client["site_id"], sorted(operation_ids))
        except Exception:
            resolved = None
        if isinstance(resolved, dict) and resolved.get("ok") is True \
                and isinstance(resolved.get("owners"), dict):
            owners = resolved["owners"]
    detached = {
        "site_id": client["site_id"],
        "principal": principal,
        "filters": filters,
        "lock": threading.RLock(),
    }
    records = []
    for record in page.get("records") or []:
        envelope = record.get("envelope") or {}
        operation_id = operation_id_from_envelope(envelope)
        if operation_id and operation_id in owners:
            envelope = bind_operation_owner(
                envelope, owners[operation_id])
        resource_type = str(record.get("resource_type") or "event")
        resource_id = str(
            record.get("resource_id") or record.get("event_type") or "unknown")
        if replay_allowed(detached, envelope, resource_type, resource_id):
            records.append({
                "event_sequence": int(record["event_sequence"]),
                "envelope": copy.deepcopy(envelope),
            })
    return make_success(
        records=records,
        gap=False,
        next_sequence=int(page["scanned_through"]),
        complete=bool(page.get("complete")),
    )


async def update_subscription(context, request):
    """Apply a generation-CAS filter or authorization update."""
    client = _subscription(context, request)
    if not client:
        return make_error("not_found", "event subscription is unknown")
    checked = validate_filters(request.get("filters") or [])
    if not checked.get("ok"):
        return checked
    principal = copy.deepcopy(request.get("principal") or {})
    sites = set(principal.get("site_scopes") or [])
    if client["site_id"] not in sites and "*" not in sites:
        return make_error("authorization_denied", "site scope is denied")
    with client["lock"]:
        supplied_generation = request.get("generation")
        if isinstance(supplied_generation, bool) or not isinstance(
                supplied_generation, int) or supplied_generation != client["generation"]:
            return make_error(
                "replay_resync_required", "subscription generation conflict")
        changed = bool(
            _principal_projection(principal) != _principal_projection(client["principal"])
            or checked["filters"] != client["filters"]
            or request.get("reason") == "client_update"
        )
        if changed:
            client["generation"] += 1
            client["filters"] = checked["filters"]
            client["coalesced"].clear()
        client["principal"] = principal
        return make_success(generation=client["generation"])


async def close_subscription(context, request):
    """Close exactly one logical subscription."""
    return make_success(closed=unregister_client(
        context, request.get("subscription_id")))


def refill_coalesced(client):
    """Move one latest telemetry record back after queue capacity returns."""
    with client["lock"]:
        if client["queue"].full() or not client["coalesced"] or client["disconnect"]:
            return False
        key = min(
            client["coalesced"],
            key=partial(_coalesced_sequence, client["coalesced"]),
        )
        record = client["coalesced"].pop(key)
        if record["generation"] != client["generation"]:
            client["statistics"]["stale_generation_dropped"] += 1
            return False
        client["queue"].put_nowait(record)
        return True


async def event_consumed(context, request):
    """Acknowledge queue consumption and refill one coalesced value."""
    client = _subscription(context, request)
    if not client:
        return make_error("not_found", "event subscription is unknown")
    generation = request.get("generation")
    with client["lock"]:
        stale = generation != client["generation"]
        if not stale:
            client["statistics"]["sent"] += 1
    return make_success(
        refilled=refill_coalesced(client), stale=stale)


def _event_sequence(context, envelope, supplied):
    if isinstance(supplied, int) and not isinstance(supplied, bool) and supplied > 0:
        return supplied
    extensions = envelope.get("extensions") \
        if isinstance(envelope.get("extensions"), dict) else {}
    sequence = extensions.get("replay_sequence")
    if isinstance(sequence, int) and not isinstance(sequence, bool) and sequence > 0:
        return sequence
    with context["lock"]:
        context["local_sequence"] += 1
        return context["local_sequence"]


def publish_event(context, envelope, **kwargs):
    """Fan one already-persisted logical event into authorized live queues."""
    if not isinstance(envelope, dict):
        return 0
    resource_type = str(kwargs.get("resource_type") or "event")
    resource_id = str(
        kwargs.get("resource_id") or envelope.get("event_type") or "unknown")
    sequence = _event_sequence(context, envelope, kwargs.get("event_sequence"))
    with context["lock"]:
        clients = tuple(context["clients"].values())
    accepted = 0
    for client in clients:
        if client["site_id"] != envelope.get("site_id"):
            continue
        client["loop"].call_soon_threadsafe(partial(
            _enqueue_client,
            client,
            copy.deepcopy(envelope),
            resource_type,
            resource_id,
            sequence,
        ))
        accepted += 1
    return accepted


def snapshot_stream(context):
    """Return aggregate client and bounded-backpressure state."""
    with context["lock"]:
        clients = tuple(context["clients"].values())
        lifetime = copy.deepcopy(context["lifetime_statistics"])
    snapshots = []
    for client in clients:
        with client["lock"]:
            snapshots.append({
                "queue_depth": client["queue"].qsize(),
                "coalesced": len(client["coalesced"]),
                "critical_overflow": client["statistics"]["critical_overflow"],
                "telemetry_dropped": client["statistics"]["telemetry_dropped"],
                "telemetry_overflow_disconnect": client["statistics"][
                    "telemetry_overflow_disconnect"],
            })
    return {
        "clients": len(clients),
        "queue_depth": sum(item["queue_depth"] for item in snapshots),
        "coalesced": sum(item["coalesced"] for item in snapshots),
        "critical_disconnects": sum(
            item["critical_overflow"] for item in snapshots)
            + lifetime["critical_overflow"],
        "telemetry_overflow_disconnects": sum(
            item["telemetry_overflow_disconnect"] for item in snapshots)
            + lifetime["telemetry_overflow_disconnect"],
        "telemetry_dropped": sum(
            item["telemetry_dropped"] for item in snapshots)
            + lifetime["telemetry_dropped"],
    }


def create_event_stream(**kwargs):
    """Create the event core; PostgreSQL remains the durable replay owner."""
    queue_maxsize = min(4096, max(8, int(kwargs.get("queue_maxsize", 256))))
    context = {
        "queue_maxsize": queue_maxsize,
        "coalesced_maxsize": min(4096, max(8, int(
            kwargs.get("coalesced_maxsize", queue_maxsize)))),
        "gateway_store": kwargs.get("gateway_store"),
        "lock": threading.RLock(),
        "clients": {},
        "local_sequence": 0,
        "lifetime_statistics": {
            "critical_overflow": 0,
            "telemetry_overflow_disconnect": 0,
            "telemetry_dropped": 0,
        },
    }
    if not isinstance(context["gateway_store"], dict):
        context["gateway_store"] = {
            "replay_window": kwargs.get("replay_window"),
            "replay_page": kwargs.get("replay_page"),
        }
    return {
        "context": context,
        "register": partial(register_client, context),
        "unregister": partial(unregister_client, context),
        "publish": partial(publish_event, context),
        "snapshot": partial(snapshot_stream, context),
        "refill": refill_coalesced,
        "client_accepts": client_accepts,
        "replay_allowed": replay_allowed,
        "open_subscription": partial(open_subscription, context),
        "read_replay_page": partial(read_replay_page, context),
        "update_subscription": partial(update_subscription, context),
        "close_subscription": partial(close_subscription, context),
        "event_consumed": partial(event_consumed, context),
    }

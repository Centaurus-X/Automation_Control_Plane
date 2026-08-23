# -*- coding: utf-8 -*-

"""Compatible automation.control.v1 envelope, payload and topic subset."""

import copy
import datetime
import math
import re
import uuid

from mcp_controller_server.canonical import (
    canonical_bytes,
    fingerprint,
    format_timestamp,
    is_fingerprint,
    parse_timestamp,
    utc_now,
)
from mcp_controller_server.errors import make_error, make_success


CONTROL_PROTOCOL = "automation.control.v1"
SCHEMA_VERSION = 1
TOPIC_PREFIX = "automation/v1"
OPERATION_FORMAT = "config_scope_operations_v1"
TOKEN_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
SERVICE_ROLES = ("master", "proxy", "worker", "monitoring")
TOP_LEVEL_FIELDS = (
    "protocol", "schema_version", "message_id", "correlation_id",
    "causation_id", "idempotency_key", "event_type", "source", "target",
    "site_id", "principal", "issued_at", "expires_at", "authority_term",
    "config_revision", "payload", "extensions",
)
REQUIRED_FIELDS = (
    "protocol", "schema_version", "message_id", "correlation_id",
    "event_type", "source", "target", "site_id", "issued_at", "expires_at",
    "authority_term", "config_revision", "payload",
)
PAYLOAD_FIELDS = {
    "actuator.request": {
        "required": ("controller_uuid", "actuator_id", "value"),
        "allowed": (
            "controller_uuid", "actuator_id", "value", "idempotent",
            "device_command_id_supported", "timeout_s", "metadata",
        ),
    },
    "config.propose": {
        "required": (),
        "allowed": (
            "base_revision", "changes", "reason", "rollout_targets",
            "target_node_id", "operation", "operation_format", "scope",
            "base_authority_epoch", "base_scope_revision",
            "base_scope_fingerprint", "operations",
        ),
    },
    "config.rollback": {
        "required": ("base_revision", "target_revision"),
        "allowed": ("base_revision", "target_revision", "reason", "rollout_targets"),
    },
    "node.admission.approve": {
        "required": ("node_id",),
        "allowed": ("node_id", "certificate_identity", "reason"),
    },
    "node.drain": {
        "required": ("node_id",),
        "allowed": ("node_id", "deadline_s", "reason"),
    },
    "node.state.set": {
        "required": ("node_id", "desired_state"),
        "allowed": ("node_id", "desired_state", "reason"),
    },
    "nodes.list": {"required": (), "allowed": ()},
    "config.get": {"required": (), "allowed": ("revision", "scope")},
    "config.history": {"required": (), "allowed": ("limit",)},
    "operation.get": {"required": ("operation_id",), "allowed": ("operation_id",)},
    "runtime.status": {"required": (), "allowed": ()},
    "terminal.execute": {"required": ("command",), "allowed": ("command",)},
}


def normalize_token(value, label="identity"):
    token = str(value or "").strip().lower()
    if not TOKEN_PATTERN.fullmatch(token):
        raise ValueError(str(label) + " has an invalid format")
    return token


def validate_service_identity(value, allowed_services=None):
    if not isinstance(value, dict):
        return make_error("identity_mismatch", "service identity must be an object")
    if set(value) != {"service", "instance_id"}:
        return make_error("identity_mismatch", "service identity fields are invalid")
    try:
        service = normalize_token(value.get("service"), "service")
        instance_id = normalize_token(value.get("instance_id"), "instance_id")
    except ValueError as exc:
        return make_error("identity_mismatch", str(exc))
    allowed = tuple(allowed_services or SERVICE_ROLES)
    if service not in allowed:
        return make_error("identity_mismatch", "service role is not allowed")
    return make_success(service=service, instance_id=instance_id)


def certificate_uri(site_id, service, instance_id):
    return "urn:automation:{}:{}:{}".format(
        normalize_token(site_id, "site_id"),
        normalize_token(service, "service"),
        normalize_token(instance_id, "instance_id"),
    )


def _uuid_text(value, label):
    try:
        return str(uuid.UUID(str(value)))
    except Exception as exc:
        raise ValueError(str(label) + " must be a UUID") from exc


def _validate_uuid(value, label):
    try:
        _uuid_text(value, label)
        return make_success()
    except ValueError as exc:
        return make_error("invalid_payload", str(exc))


def _validate_actuator(payload):
    checked = _validate_uuid(payload.get("controller_uuid"), "controller_uuid")
    if not checked.get("ok"):
        return checked
    actuator_id = str(payload.get("actuator_id") or "")
    if not actuator_id or len(actuator_id) > 128:
        return make_error("invalid_payload", "actuator_id is invalid")
    value = payload.get("value")
    if isinstance(value, float) and not math.isfinite(value):
        return make_error("invalid_payload", "actuator value must be finite")
    if payload.get("timeout_s") is not None:
        try:
            timeout_s = float(payload["timeout_s"])
        except Exception:
            return make_error("invalid_payload", "timeout_s must be numeric")
        if not 0.1 <= timeout_s <= 120.0:
            return make_error("invalid_payload", "timeout_s is outside its bounds")
    return make_success()


def _validate_scope_operations(operations, maximum=500):
    if not isinstance(operations, (list, tuple)) or not operations \
            or len(operations) > max(1, min(500, int(maximum))):
        raise ValueError("unsupported_operation_format")
    common_fields = {"op", "section", "entity_key", "expected_value_hash"}
    operation_fields = {
        "replace_field": common_fields.union(("json_pointer", "value")),
        "upsert_entity": common_fields.union(("value", "value_hash")),
        "delete_entity": common_fields,
    }
    required_fields = {
        "replace_field": {"op", "section", "entity_key", "json_pointer", "value"},
        "upsert_entity": {"op", "section", "entity_key", "value", "value_hash"},
        "delete_entity": {"op", "section", "entity_key"},
    }
    normalized = []
    for operation in operations:
        if not isinstance(operation, dict):
            raise ValueError("unsupported_operation_format")
        kind = str(operation.get("op") or "")
        if kind not in operation_fields:
            raise ValueError("unsupported_operation_format")
        if set(operation) - operation_fields[kind] \
                or required_fields[kind] - set(operation):
            raise ValueError("unsupported_operation_format")
        if not str(operation.get("section") or "").strip() \
                or not str(operation.get("entity_key") or "").strip():
            raise ValueError("operation requires section and entity_key")
        expected = operation.get("expected_value_hash")
        value_hash = operation.get("value_hash")
        if expected is not None and not is_fingerprint(expected):
            raise ValueError("expected_value_hash must be lowercase raw 64-hex")
        if value_hash is not None and not is_fingerprint(value_hash):
            raise ValueError("value_hash must be lowercase raw 64-hex")
        if kind == "replace_field" and not str(
                operation.get("json_pointer") or "").startswith("/"):
            raise ValueError("replace_field requires a non-root JSON pointer")
        if kind == "upsert_entity":
            if not isinstance(operation.get("value"), dict):
                raise ValueError("upsert_entity requires an entity record")
            if value_hash is None or fingerprint(operation["value"]) != value_hash:
                raise ValueError("upsert_entity value_hash differs")
        normalized.append(copy.deepcopy(operation))
    return tuple(normalized)


def _validate_legacy_proposal(payload):
    revision = payload.get("base_revision")
    if isinstance(revision, bool) or not isinstance(revision, int) or revision < 0:
        return make_error("invalid_payload", "base_revision must be a non-negative integer")
    changes = payload.get("changes")
    if not isinstance(changes, list) or not changes or len(changes) > 128:
        return make_error("invalid_payload", "changes must be a bounded non-empty list")
    for change in changes:
        if not isinstance(change, dict):
            return make_error("invalid_payload", "each change must be an object")
        if set(change) - {"operation", "path", "value"} or "path" not in change:
            return make_error("invalid_payload", "configuration change fields are invalid")
        operation = str(change.get("operation") or "set").lower()
        if operation not in ("set", "update", "append", "delete"):
            return make_error("invalid_payload", "configuration operation is invalid")
        path = str(change.get("path") or "")
        if not path or len(path) > 512:
            return make_error("invalid_payload", "configuration path is invalid")
        if operation != "delete" and "value" not in change:
            return make_error("invalid_payload", "configuration value is required")
    targets = payload.get("rollout_targets")
    if targets is not None:
        if not isinstance(targets, list) or len(targets) > 256:
            return make_error("invalid_payload", "rollout_targets must be a bounded list")
        try:
            for node_id in targets:
                normalize_token(node_id, "node_id")
        except ValueError as exc:
            return make_error("invalid_payload", str(exc))
    return make_success()


def validate_config_proposal_payload(payload, authority_mode=None):
    if not isinstance(payload, dict):
        return make_error("invalid_payload", "proposal payload must be an object")
    canonical_fields = {"target_node_id", "operation_format", "scope", "operations"}
    mode = authority_mode or (
        "node_local" if canonical_fields.intersection(payload) else "master_global")
    if mode == "master_global":
        allowed = {"base_revision", "changes", "reason", "rollout_targets"}
        if set(payload) - allowed:
            return make_error("invalid_payload", "legacy proposal fields are invalid")
        return _validate_legacy_proposal(payload)
    if mode != "node_local":
        return make_error("invalid_payload", "authority mode is invalid")
    if {"base_revision", "changes", "rollout_targets"}.intersection(payload):
        return make_error(
            "unsupported_operation_format",
            "legacy path operations are not accepted by the node_local relay",
        )
    allowed = {
        "target_node_id", "operation", "operation_format", "scope",
        "base_authority_epoch", "base_scope_revision",
        "base_scope_fingerprint", "reason", "operations",
    }
    required = allowed - {"reason"}
    unknown = sorted(set(payload) - allowed)
    missing = sorted(required - set(payload))
    if unknown or missing:
        return make_error(
            "invalid_payload", "canonical proposal fields are invalid",
            unknown_fields=unknown, missing_fields=missing,
        )
    try:
        target = normalize_token(payload["target_node_id"], "target_node_id")
    except ValueError as exc:
        return make_error("invalid_payload", str(exc))
    scope = str(payload.get("scope") or "")
    if ":" not in scope or scope.split(":", 1)[0] not in ("node", "cross"):
        return make_error("invalid_payload", "proposal scope is invalid")
    try:
        normalize_token(scope.split(":", 1)[1], "scope_id")
    except ValueError as exc:
        return make_error("invalid_payload", str(exc))
    if scope.startswith("node:") and scope.split(":", 1)[1] != target:
        return make_error("invalid_payload", "target node and scope owner differ")
    for name in ("base_authority_epoch", "base_scope_revision"):
        value = payload.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return make_error("invalid_payload", name + " must be a non-negative integer")
    if not is_fingerprint(payload.get("base_scope_fingerprint")):
        return make_error(
            "invalid_payload",
            "base_scope_fingerprint must be lowercase raw 64-hex",
        )
    if payload.get("operation") != "scope_operations" \
            or payload.get("operation_format") != OPERATION_FORMAT:
        return make_error("unsupported_operation_format", "operation format is unsupported")
    reason = payload.get("reason")
    if reason is not None and (not isinstance(reason, str) or len(reason) > 2048):
        return make_error("invalid_payload", "proposal reason is invalid")
    try:
        operations = _validate_scope_operations(payload.get("operations"))
    except ValueError as exc:
        return make_error("unsupported_operation_format", str(exc))
    return make_success(
        payload=copy.deepcopy(payload), target_node_id=target,
        scope=scope, operations=operations,
    )


def _validate_node(payload):
    try:
        normalize_token(payload.get("node_id"), "node_id")
    except ValueError as exc:
        return make_error("invalid_payload", str(exc))
    deadline = payload.get("deadline_s")
    if deadline is not None:
        try:
            numeric = float(deadline)
        except Exception:
            return make_error("invalid_payload", "deadline_s must be numeric")
        if not 0.0 <= numeric <= 86400.0:
            return make_error("invalid_payload", "deadline_s is outside its bounds")
    return make_success()


def _validate_node_state(payload):
    checked = _validate_node(payload)
    if not checked.get("ok"):
        return checked
    if str(payload.get("desired_state") or "").upper() not in (
            "ACTIVE", "DRAINING", "MAINTENANCE", "QUARANTINED"):
        return make_error("invalid_payload", "desired_state is invalid")
    return make_success()


def _validate_config_get(payload):
    if payload.get("revision") is not None and payload.get("scope") is not None:
        return make_error("invalid_payload", "revision and scope are mutually exclusive")
    revision = payload.get("revision")
    if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 1):
        return make_error("invalid_payload", "revision must be a positive integer")
    scope = payload.get("scope")
    if scope is not None:
        text = str(scope)
        if ":" not in text or text.split(":", 1)[0] not in ("node", "cross"):
            return make_error("invalid_payload", "scope is invalid")
        try:
            normalize_token(text.split(":", 1)[1], "scope_id")
        except ValueError as exc:
            return make_error("invalid_payload", str(exc))
    return make_success()


def _validate_history(payload):
    limit = payload.get("limit")
    if limit is not None and (
            isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200):
        return make_error("invalid_payload", "limit must be between 1 and 200")
    return make_success()


def validate_payload(event_type, payload, allow_unknown_event=False):
    if not isinstance(payload, dict):
        return make_error("invalid_payload", "payload must be an object")
    schema = PAYLOAD_FIELDS.get(str(event_type or ""))
    if schema is None:
        return make_success(payload=payload) if allow_unknown_event else make_error(
            "invalid_payload", "event_type is not supported")
    missing = [key for key in schema["required"] if key not in payload]
    unknown = sorted(set(payload) - set(schema["allowed"]))
    if missing:
        return make_error("invalid_payload", "required payload fields are missing", fields=missing)
    if unknown:
        return make_error("invalid_payload", "unknown payload fields are not allowed", fields=unknown)
    validator = {
        "actuator.request": _validate_actuator,
        "config.propose": validate_config_proposal_payload,
        "node.admission.approve": _validate_node,
        "node.drain": _validate_node,
        "node.state.set": _validate_node_state,
        "config.get": _validate_config_get,
        "config.history": _validate_history,
        "operation.get": _validate_uuid_operation,
    }.get(str(event_type or ""))
    checked = validator(payload) if callable(validator) else make_success()
    return make_success(payload=payload) if checked.get("ok") else checked


def _validate_uuid_operation(payload):
    return _validate_uuid(payload.get("operation_id"), "operation_id")


def make_envelope(event_type, source, target, **kwargs):
    now = kwargs.get("now") or utc_now()
    ttl_s = float(kwargs.get("ttl_s", 30.0))
    message_id = _uuid_text(kwargs.get("message_id") or uuid.uuid4(), "message_id")
    correlation_id = _uuid_text(
        kwargs.get("correlation_id") or message_id, "correlation_id")
    causation_id = kwargs.get("causation_id")
    result = {
        "protocol": CONTROL_PROTOCOL,
        "schema_version": SCHEMA_VERSION,
        "message_id": message_id,
        "correlation_id": correlation_id,
        "causation_id": _uuid_text(causation_id, "causation_id") if causation_id else None,
        "idempotency_key": kwargs.get("idempotency_key"),
        "event_type": str(event_type),
        "source": copy.deepcopy(source),
        "target": copy.deepcopy(target),
        "site_id": normalize_token(kwargs.get("site_id"), "site_id"),
        "principal": copy.deepcopy(kwargs.get("principal")),
        "issued_at": format_timestamp(now),
        "expires_at": format_timestamp(now + datetime.timedelta(seconds=ttl_s)),
        "authority_term": int(kwargs.get("authority_term", 0)),
        "config_revision": int(kwargs.get("config_revision", 0)),
        "payload": copy.deepcopy(kwargs.get("payload") or {}),
    }
    if kwargs.get("extensions") is not None:
        result["extensions"] = copy.deepcopy(kwargs["extensions"])
    return result


def validate_envelope(envelope, **kwargs):
    if not isinstance(envelope, dict):
        return make_error("invalid_envelope", "envelope must be an object")
    missing = sorted(set(REQUIRED_FIELDS) - set(envelope))
    unknown = sorted(set(envelope) - set(TOP_LEVEL_FIELDS))
    if missing:
        return make_error("invalid_envelope", "required fields are missing", fields=missing)
    if unknown:
        return make_error("invalid_envelope", "unknown critical fields", fields=unknown)
    if envelope.get("protocol") != CONTROL_PROTOCOL or envelope.get("schema_version") != 1:
        return make_error("unsupported_protocol", "protocol or schema version is unsupported")
    if len(canonical_bytes(envelope)) > int(kwargs.get("maximum_bytes", 1024 * 1024)):
        return make_error("invalid_envelope", "envelope exceeds its size limit")
    for label in ("message_id", "correlation_id", "causation_id"):
        if label == "causation_id" and envelope.get(label) is None:
            continue
        try:
            _uuid_text(envelope.get(label), label)
        except ValueError as exc:
            return make_error("invalid_envelope", str(exc))
    source = validate_service_identity(envelope.get("source"))
    target = validate_service_identity(envelope.get("target"))
    if not source.get("ok") or not target.get("ok"):
        return make_error("identity_mismatch", "source or target identity is invalid")
    try:
        issued = parse_timestamp(envelope.get("issued_at"))
        expires = parse_timestamp(envelope.get("expires_at"))
    except Exception as exc:
        return make_error("invalid_envelope", str(exc))
    now = kwargs.get("now") or utc_now()
    skew = datetime.timedelta(seconds=float(kwargs.get("clock_skew_s", 30.0)))
    if issued > now + skew or expires < now - skew or expires <= issued:
        return make_error("expired", "message is expired or has an invalid lifetime")
    maximum_lifetime = datetime.timedelta(seconds=float(kwargs.get("maximum_ttl_s", 300.0)))
    if expires - issued > maximum_lifetime:
        return make_error("expired", "message lifetime exceeds its limit")
    expected_site = kwargs.get("expected_site_id")
    if expected_site and envelope.get("site_id") != normalize_token(expected_site, "site_id"):
        return make_error("identity_mismatch", "message belongs to another site")
    for name in ("authority_term", "config_revision"):
        value = envelope.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            return make_error("invalid_envelope", name + " is invalid")
    idempotency_key = envelope.get("idempotency_key")
    if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128):
        return make_error("invalid_envelope", "idempotency_key is invalid")
    checked = validate_payload(
        envelope.get("event_type"), envelope.get("payload"),
        allow_unknown_event=bool(kwargs.get("allow_unknown_event", False)),
    )
    return make_success(envelope=copy.deepcopy(envelope)) \
        if checked.get("ok") else checked


def site_root(site_id, prefix=TOPIC_PREFIX):
    return str(prefix or TOPIC_PREFIX).strip("/") + "/" + normalize_token(site_id, "site_id")


def master_command_topic(site_id, master_id, command, prefix=TOPIC_PREFIX):
    suffix = normalize_token(command, "command").replace(".", "/")
    return "/".join((
        site_root(site_id, prefix), "master",
        normalize_token(master_id, "master_id"), "commands", suffix,
    ))


def proxy_ack_topic(site_id, proxy_id, responder_id, message_id, prefix=TOPIC_PREFIX):
    return "/".join((
        site_root(site_id, prefix), "proxy", normalize_token(proxy_id, "proxy_id"),
        "acks", normalize_token(responder_id, "responder_id"),
        normalize_token(message_id, "message_id"),
    ))


def proxy_ack_filter(site_id, proxy_id, prefix=TOPIC_PREFIX):
    return "/".join((
        site_root(site_id, prefix), "proxy",
        normalize_token(proxy_id, "proxy_id"), "acks/#",
    ))


def control_event_topic(site_id, event_type, source_id, prefix=TOPIC_PREFIX):
    return "/".join((
        site_root(site_id, prefix), "events",
        normalize_token(event_type, "event_type"),
        normalize_token(source_id, "source_id"),
    ))


def control_event_filter(site_id, source_id, prefix=TOPIC_PREFIX):
    return "/".join((
        site_root(site_id, prefix), "events", "+",
        normalize_token(source_id, "source_id"),
    ))

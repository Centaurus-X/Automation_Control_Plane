# -*- coding: utf-8 -*-

# proxy_gateway/src/core/client_command_dispatcher.py

"""Shared, allowlisted REST/WSS command dispatcher for Proxy clients."""

import asyncio
import copy
import datetime
import hashlib
import hmac
import json
import math
import re
import uuid

from functools import partial

from src.library._generic_system_contract_library import (
    validate_authority_mode,
    validate_config_proposal_payload,
)
from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library._generic_type_library import json_ready
from src.core.access_policy import (
    authorize,
    authorize_config_changes,
    authorize_config_operations,
    authorize_role_grant,
)
from src.library.rate_limit_store import bucket_key


IDENTIFIER_PATTERN = re.compile(r"^[a-z0-9][a-z0-9._-]{0,127}$")
MUTATING_ACTIONS = frozenset((
    "user.create",
    "api_tokens.create",
    "api_tokens.revoke",
    "config.propose",
    "config.rollback",
    "actuator.request",
    "node.drain",
    "node.state.set",
    "node.admission.approve",
    "terminal.execute",
))
LOCAL_MUTATING_ACTIONS = frozenset((
    "user.create",
    "api_tokens.create",
    "api_tokens.revoke",
))
REGISTERED_ACTIONS = frozenset((
    "users.list",
    "user.create",
    "roles.list",
    "permissions.list",
    "api_tokens.list",
    "api_tokens.create",
    "api_tokens.revoke",
    "sites.list",
    "nodes.list",
    "node.get",
    "runtime.status",
    "config.get",
    "config.history",
    "config.diff",
    "config.propose",
    "config.rollback",
    "actuator.request",
    "operation.get",
    "audit.list",
    "node.drain",
    "node.state.set",
    "node.admission.approve",
    "terminal.execute",
))
SECRET_KEYS = frozenset((
    "authorization",
    "bearer",
    "csrf_token",
    "password",
    "session_token",
    "token",
    "token_hash",
))


def _error(message, **kwargs):
    return make_error("invalid_payload", message, **kwargs)


def _fields(payload, allowed, required=()):
    unknown = sorted(set(payload) - set(allowed))
    missing = sorted(set(required) - set(payload))
    if unknown:
        return _error("unknown payload fields are forbidden", fields=unknown)
    if missing:
        return _error("required payload fields are missing", fields=missing)
    return make_success()


def _integer(value, label, **kwargs):
    if isinstance(value, bool) or not isinstance(value, int):
        return _error(label + " must be an integer")
    minimum = kwargs.get("minimum")
    maximum = kwargs.get("maximum")
    if minimum is not None and value < minimum:
        return _error(label + " is below its minimum")
    if maximum is not None and value > maximum:
        return _error(label + " exceeds its maximum")
    return make_success()


def _limit(payload, default):
    value = payload.get("limit", default)
    checked = _integer(value, "limit", minimum=1, maximum=200)
    return checked if not checked.get("ok") else make_success(value=value)


def _identifier(value, label):
    if not isinstance(value, str) or not IDENTIFIER_PATTERN.fullmatch(value):
        return _error(label + " is invalid")
    return make_success()


def _uuid(value, label):
    try:
        normalized = str(uuid.UUID(str(value)))
    except (ValueError, TypeError, AttributeError):
        return _error(label + " must be a UUID")
    return make_success(value=normalized)


def _reason(payload):
    value = payload.get("reason")
    if value is not None and (not isinstance(value, str) or len(value) > 1024):
        return _error("reason is invalid")
    return make_success()


def _optional_text(payload, name, maximum):
    if name not in payload or payload[name] is None:
        return make_success()
    value = payload[name]
    if not isinstance(value, str) or len(value) > maximum:
        return _error(name + " is invalid")
    return make_success()


def _date_time(value):
    if value is None:
        return make_success()
    if not isinstance(value, str) or not value:
        return _error("expires_at must be an RFC 3339 date-time or null")
    normalized = value[:-1] + "+00:00" if value.endswith("Z") else value
    try:
        parsed = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        return _error("expires_at must be an RFC 3339 date-time or null")
    if parsed.tzinfo is None:
        return _error("expires_at must include a timezone")
    return make_success()


def _validate_change(change):
    if not isinstance(change, dict):
        return _error("each config change must be an object")
    checked = _fields(change, ("operation", "path", "value"), ("path",))
    if not checked.get("ok"):
        return checked
    operation = change.get("operation", "update")
    if operation not in ("set", "update", "append", "delete"):
        return _error("config change operation is unsupported")
    path = change.get("path")
    if not isinstance(path, str) or not path or len(path) > 512:
        return _error("config change path is invalid")
    if operation != "delete" and "value" not in change:
        return _error("config change value is required")
    return make_success()


def _validate_payload(action, payload, **kwargs):
    if not isinstance(payload, dict):
        return _error("payload must be an object")
    if action in ("roles.list", "permissions.list", "sites.list", "nodes.list",
                  "runtime.status"):
        return _fields(payload, ())
    if action in ("users.list", "api_tokens.list", "config.history", "audit.list"):
        checked = _fields(payload, ("limit",))
        return checked if not checked.get("ok") else _limit(
            payload, 50 if action == "config.history" else 100)
    if action == "user.create":
        checked = _fields(
            payload,
            ("username", "password", "display_name", "email", "enabled",
             "role_name", "site_id"),
            ("username", "password"),
        )
        if not checked.get("ok"):
            return checked
        if not isinstance(payload["username"], str) or not payload["username"] or \
                len(payload["username"]) > 256:
            return _error("username is invalid")
        if not isinstance(payload["password"], str) or not payload["password"] or \
                len(payload["password"]) > 4096:
            return _error("password is invalid")
        if "enabled" in payload and not isinstance(payload["enabled"], bool):
            return _error("enabled must be boolean")
        for name, maximum in (("display_name", 512), ("email", 512),
                              ("role_name", 128)):
            checked = _optional_text(payload, name, maximum)
            if not checked.get("ok"):
                return checked
        if "site_id" in payload:
            return _identifier(payload["site_id"], "site_id")
        return make_success()
    if action == "api_tokens.create":
        checked = _fields(
            payload, ("name", "permissions", "site_scopes", "expires_at"),
            ("name",))
        if not checked.get("ok"):
            return checked
        if not isinstance(payload["name"], str) or not payload["name"] or \
                len(payload["name"]) > 256:
            return _error("token name is invalid")
        for name in ("permissions", "site_scopes"):
            if name not in payload:
                continue
            values = payload[name]
            if not isinstance(values, list) or len(values) > 128 or \
                    len(values) != len(set(values)) or not all(
                        isinstance(value, str) and 0 < len(value) <= 128
                        and (name != "site_scopes" or value == "*" or
                             IDENTIFIER_PATTERN.fullmatch(value))
                        for value in values):
                return _error(name + " must be a bounded unique string list")
        return _date_time(payload.get("expires_at"))
    if action == "api_tokens.revoke":
        checked = _fields(payload, ("token_id",), ("token_id",))
        return checked if not checked.get("ok") else _uuid(
            payload["token_id"], "token_id")
    if action in ("node.get", "node.drain", "node.state.set",
                  "node.admission.approve"):
        allowed = {
            "node.get": ("node_id",),
            "node.drain": ("node_id", "reason", "deadline_s"),
            "node.state.set": ("node_id", "desired_state", "reason"),
            "node.admission.approve": (
                "node_id", "certificate_identity", "reason"),
        }[action]
        required = ("node_id", "desired_state") if action == "node.state.set" \
            else ("node_id",)
        checked = _fields(payload, allowed, required)
        if not checked.get("ok"):
            return checked
        checked = _identifier(payload["node_id"], "node_id")
        if not checked.get("ok"):
            return checked
        checked = _reason(payload)
        if not checked.get("ok"):
            return checked
        if action == "node.drain" and "deadline_s" in payload:
            value = payload["deadline_s"]
            if isinstance(value, bool) or not isinstance(value, (int, float)) or \
                    not math.isfinite(float(value)) or not 0 <= float(value) <= 86400:
                return _error("deadline_s is outside its allowed range")
        if action == "node.state.set" and payload["desired_state"] not in (
                "ACTIVE", "DRAINING", "MAINTENANCE", "QUARANTINED"):
            return _error("desired_state is unsupported")
        if action == "node.admission.approve" and \
                "certificate_identity" in payload:
            checked = _optional_text(payload, "certificate_identity", 512)
            if not checked.get("ok"):
                return checked
        return make_success()
    if action == "config.get":
        if kwargs["authority_mode"] == "node_local":
            checked = _fields(payload, ("scope",))
            if not checked.get("ok") or "scope" not in payload:
                return checked
            scope = str(payload["scope"])
            if ":" not in scope or scope.split(":", 1)[0] not in (
                    "node", "cross"):
                return _error("scope is invalid")
            return _identifier(scope.split(":", 1)[1], "scope_id")
        checked = _fields(payload, ("revision",))
        if not checked.get("ok") or "revision" not in payload:
            return checked
        return _integer(payload["revision"], "revision", minimum=1)
    if action == "config.diff":
        if kwargs["authority_mode"] == "node_local":
            return make_error(
                "unsupported_operation_format",
                "node_local configuration exposes scoped mirrors, not global diffs")
        checked = _fields(
            payload, ("from_revision", "to_revision"),
            ("from_revision", "to_revision"))
        if not checked.get("ok"):
            return checked
        for name in ("from_revision", "to_revision"):
            checked = _integer(payload[name], name, minimum=1)
            if not checked.get("ok"):
                return checked
        return make_success()
    if action == "config.propose":
        return validate_config_proposal_payload(
            payload, authority_mode=kwargs["authority_mode"])
    if action == "config.rollback":
        if kwargs["authority_mode"] == "node_local":
            return make_error(
                "unsupported_operation_format",
                "node_local rollback requires canonical target-scope operations")
        checked = _fields(
            payload,
            ("base_revision", "target_revision", "reason", "rollout_targets"),
            ("base_revision", "target_revision"))
        if not checked.get("ok"):
            return checked
        for name in ("base_revision", "target_revision"):
            checked = _integer(payload[name], name, minimum=1)
            if not checked.get("ok"):
                return checked
        return _validate_rollout_payload(payload)
    if action == "actuator.request":
        checked = _fields(
            payload,
            ("controller_uuid", "actuator_id", "value", "idempotent",
             "device_command_id_supported"),
            ("controller_uuid", "actuator_id", "value"))
        if not checked.get("ok"):
            return checked
        checked = _uuid(payload["controller_uuid"], "controller_uuid")
        if not checked.get("ok"):
            return checked
        actuator_id = payload["actuator_id"]
        if isinstance(actuator_id, bool) or not isinstance(actuator_id, (int, str)) or \
                isinstance(actuator_id, str) and not 0 < len(actuator_id) <= 128:
            return _error("actuator_id is invalid")
        for name in ("idempotent", "device_command_id_supported"):
            if name in payload and not isinstance(payload[name], bool):
                return _error(name + " must be boolean")
        return make_success()
    if action == "operation.get":
        checked = _fields(payload, ("operation_id",), ("operation_id",))
        return checked if not checked.get("ok") else _uuid(
            payload["operation_id"], "operation_id")
    if action == "terminal.execute":
        checked = _fields(payload, ("command",), ("command",))
        if not checked.get("ok"):
            return checked
        command = payload["command"]
        if not isinstance(command, str) or not command or len(command) > 512 or any(
                ord(character) < 32 or ord(character) == 127 for character in command):
            return _error("terminal command is invalid")
        return make_success()
    return _error("action is unsupported")


def _validate_rollout_payload(payload):
    checked = _reason(payload)
    if not checked.get("ok"):
        return checked
    if "rollout_targets" not in payload:
        return make_success()
    targets = payload["rollout_targets"]
    if not isinstance(targets, list) or len(targets) > 256 or \
            len(targets) != len(set(targets)):
        return _error("rollout_targets must be a bounded unique list")
    for target in targets:
        checked = _identifier(target, "rollout target")
        if not checked.get("ok"):
            return checked
    return make_success()


def _diff_values(before, after, path=""):
    if before == after:
        return []
    if isinstance(before, dict) and isinstance(after, dict):
        changes = []
        for key in sorted(set(before) | set(after)):
            child = key if not path else path + "." + key
            changes.extend(_diff_values(before.get(key), after.get(key), child))
        return changes
    return [{"path": path, "before": copy.deepcopy(before),
             "after": copy.deepcopy(after)}]


def _without_internal_principal_fields(principal):
    return {
        key: copy.deepcopy(value)
        for key, value in principal.items()
        if not str(key).startswith("_")
    }


def _preauthorize_action(context, request):
    """Authorize the concrete action before an ALLOW audit is persisted."""
    action = request["action"]
    payload = request["payload"]
    principal = _without_internal_principal_fields(request["principal"])
    site_id = context["site_id"]
    simple_permissions = {
        "users.list": "user.manage",
        "roles.list": "role.manage",
        "permissions.list": "role.manage",
        "nodes.list": "node.read",
        "runtime.status": "node.read",
        "config.get": "config.read",
        "config.history": "config.read",
        "config.diff": "config.read",
        "config.rollback": "config.rollback",
        "audit.list": "audit.read",
        "node.admission.approve": "node.admit",
        "terminal.execute": "terminal.execute",
    }
    if action in simple_permissions:
        return authorize(principal, simple_permissions[action], site_id=site_id)
    if action == "user.create":
        allowed = authorize(principal, "user.manage", site_id=site_id)
        if not allowed.get("ok"):
            return allowed
        target_site = str(payload.get("site_id") or site_id)
        if target_site != site_id:
            return make_error(
                "authorization_denied", "user role must belong to the managed site")
        if not payload.get("role_name"):
            return allowed
        requested_name = str(payload["role_name"])
        role = next((item for item in context["identity"]["list_roles"]()
                     if str(item.get("role_name") or "") == requested_name), None)
        return authorize_role_grant(
            principal, target_site, role, managed_site_id=site_id)
    if action == "api_tokens.create":
        requested = set(payload["permissions"]) if "permissions" in payload \
            else set(principal.get("permissions") or [])
        if not requested.issubset(set(principal.get("permissions") or [])):
            return make_error(
                "authorization_denied", "token permissions exceed principal")
        sites = set(payload["site_scopes"]) if "site_scopes" in payload \
            else set(principal.get("site_scopes") or [])
        if not sites.issubset(set(principal.get("site_scopes") or [])):
            return make_error(
                "authorization_denied", "token sites exceed principal")
        return make_success()
    if action == "node.get":
        return authorize(
            principal, "node.read", site_id=site_id,
            resource_type="node", resource_id=payload["node_id"])
    if action == "config.propose":
        if context["authority_mode"] == "node_local":
            return authorize_config_operations(
                principal, site_id, payload.get("operations"))
        return authorize_config_changes(
            principal, site_id, payload.get("changes"))
    if action == "actuator.request":
        return authorize(
            principal, "actuator.request", site_id=site_id,
            resource_type="controller", resource_id=payload["controller_uuid"])
    if action == "operation.get":
        return authorize(
            principal, "operation.read", site_id=site_id,
            resource_type="operation", resource_id=payload["operation_id"])
    if action in ("node.drain", "node.state.set"):
        return authorize(
            principal, "node.drain", site_id=site_id,
            resource_type="node", resource_id=payload["node_id"])
    return make_success()


def _run_action(context, request):
    action = request["action"]
    payload = copy.deepcopy(request["payload"])
    principal = _without_internal_principal_fields(request["principal"])
    commands = context["commands"]
    if action == "users.list":
        checked = authorize(principal, "user.manage", site_id=context["site_id"])
        if not checked.get("ok"):
            return checked
        limit = _limit(payload, 100)["value"]
        return make_success(users=context["identity"]["list_users"](limit=limit))
    if action == "user.create":
        checked = authorize(principal, "user.manage", site_id=context["site_id"])
        if not checked.get("ok"):
            return checked
        prepared = copy.deepcopy(payload)
        target_site = str(prepared.get("site_id") or context["site_id"])
        if target_site != context["site_id"]:
            return make_error(
                "authorization_denied", "user role must belong to the managed site")
        if prepared.get("role_name"):
            requested_name = str(prepared["role_name"])
            role = next((item for item in context["identity"]["list_roles"]()
                         if str(item.get("role_name") or "") == requested_name), None)
            grant = authorize_role_grant(
                principal, target_site, role, managed_site_id=context["site_id"])
            if not grant.get("ok"):
                return grant
            prepared["site_id"] = target_site
            prepared["granted_by"] = principal["subject"]
        else:
            prepared.pop("site_id", None)
        return context["auth"]["create_user"](prepared)
    if action in ("roles.list", "permissions.list"):
        checked = authorize(principal, "role.manage", site_id=context["site_id"])
        if not checked.get("ok"):
            return checked
        name = "roles" if action == "roles.list" else "permissions"
        function = context["identity"][
            "list_roles" if action == "roles.list" else "list_permissions"]
        return make_success(**{name: function()})
    if action == "api_tokens.list":
        limit = _limit(payload, 100)["value"]
        tokens = context["identity"]["list_api_tokens"](
            principal["subject"], limit=limit)
        return make_success(tokens=tokens)
    if action == "api_tokens.create":
        requested = set(payload["permissions"]) if "permissions" in payload \
            else set(principal.get("permissions") or [])
        if not requested.issubset(set(principal.get("permissions") or [])):
            return make_error(
                "authorization_denied", "token permissions exceed principal")
        sites = set(payload["site_scopes"]) if "site_scopes" in payload \
            else set(principal.get("site_scopes") or [])
        if not sites.issubset(set(principal.get("site_scopes") or [])):
            return make_error(
                "authorization_denied", "token sites exceed principal")
        return context["auth"]["issue_api_token"]({
            "user_id": principal["subject"],
            "name": payload["name"],
            "permissions": sorted(requested),
            "site_scopes": sorted(sites),
            "expires_at": payload.get("expires_at"),
        })
    if action == "api_tokens.revoke":
        return context["identity"]["revoke_api_token"]({
            "token_id": str(uuid.UUID(payload["token_id"])),
            "user_id": principal["subject"],
        })
    if action == "sites.list":
        sites = set(principal.get("site_scopes") or [])
        visible = [context["site_id"]] if context["site_id"] in sites or "*" in sites else []
        return make_success(sites=visible)
    if action == "nodes.list":
        return commands["list_nodes"](principal)
    if action == "node.get":
        checked = authorize(
            principal, "node.read", site_id=context["site_id"],
            resource_type="node", resource_id=payload["node_id"])
        if not checked.get("ok"):
            return checked
        result = commands["list_nodes"](principal)
        if not result.get("ok"):
            return result
        nodes = [item for item in result.get("nodes", [])
                 if item.get("node_id") == payload["node_id"]]
        return make_success(node=nodes[0]) if nodes else make_error(
            "not_found", "node is unknown")
    if action == "runtime.status":
        result = commands["runtime_status"](principal)
        if result.get("ok") and context.get("runtime_snapshot"):
            result = copy.deepcopy(result)
            result["proxy"] = context["runtime_snapshot"]()
        return result
    if action == "config.get":
        return commands["get_config"](
            principal,
            revision=payload.get("revision"),
            scope=payload.get("scope"),
        )
    if action == "config.history":
        return commands["config_history"](
            principal, limit=_limit(payload, 50)["value"])
    if action == "config.diff":
        before = commands["get_config"](
            principal, revision=payload["from_revision"])
        if not before.get("ok"):
            return before
        after = commands["get_config"](
            principal, revision=payload["to_revision"])
        if not after.get("ok"):
            return after
        return make_success(
            from_revision=payload["from_revision"],
            to_revision=payload["to_revision"],
            changes=_diff_values(
                before["config"]["config_data"], after["config"]["config_data"]),
        )
    metadata = {
        "idempotency_key": request.get("idempotency_key"),
        "ttl_s": request.get("ttl_s", 30.0),
        "request_id": request.get("request_id"),
    }
    prepared = copy.deepcopy(payload)
    prepared.update(metadata)
    if action == "config.propose":
        return commands["propose_config"](principal, prepared)
    if action == "config.rollback":
        return commands["rollback_config"](principal, prepared)
    if action == "actuator.request":
        return commands["request_actuator"](principal, prepared)
    if action == "operation.get":
        return commands["get_operation"](principal, payload["operation_id"])
    if action == "audit.list":
        checked = authorize(principal, "audit.read", site_id=context["site_id"])
        if not checked.get("ok"):
            return checked
        records = context["audit"]["list"](
            site_id=context["site_id"], limit=_limit(payload, 100)["value"])
        return make_success(audit=records)
    if action == "node.drain":
        return commands["drain_node"](principal, prepared)
    if action == "node.state.set":
        return commands["set_node_state"](principal, prepared)
    if action == "node.admission.approve":
        return commands["approve_node"](principal, prepared)
    if action == "terminal.execute":
        return commands["execute_terminal"](
            principal, payload["command"], **metadata)
    return make_error("invalid_payload", "action is unsupported")


def _redact(value):
    if isinstance(value, dict):
        return {
            str(key): "[REDACTED]" if str(key).lower() in SECRET_KEYS else _redact(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_redact(item) for item in value]
    return copy.deepcopy(value)


def _persistable_local_result(action, result):
    safe = json_ready(_redact(result))
    if action == "api_tokens.create" and isinstance(safe, dict):
        for key in tuple(safe):
            if "token" in str(key).lower() and key not in ("token_id",):
                safe.pop(key, None)
        safe["secret_already_delivered"] = bool(result.get("ok"))
    return safe


def _payload_fingerprint(context, request):
    canonical = json.dumps(
        {"action": request["action"], "payload": request["payload"]},
        ensure_ascii=False, separators=(",", ":"), sort_keys=True,
        allow_nan=False,
    ).encode("utf-8")
    key = context["idempotency_key"]
    return hmac.new(key, canonical, hashlib.sha256).hexdigest()


def _claim_local(context, request):
    claimed = context["gateway_store"]["claim_local_action"]({
        "request_id": request["request_id"],
        "user_id": request["principal"]["subject"],
        "site_id": context["site_id"],
        "action": request["action"],
        "idempotency_key": request["idempotency_key"],
        "payload_fingerprint": _payload_fingerprint(context, request),
    })
    if not claimed.get("ok") or not claimed.get("duplicate"):
        return claimed
    record = claimed.get("action_record") or {}
    result = copy.deepcopy(record.get("result"))
    if isinstance(result, dict):
        result["duplicate"] = True
        if request["action"] == "api_tokens.create" and result.get("ok"):
            result["secret_already_delivered"] = True
        return make_success(duplicate=True, result=result)
    return make_success(duplicate=True, result=make_success(
        accepted=True, duplicate=True, state=record.get("state") or "PENDING",
        request_id=record.get("request_id")))


def _audit_event(context, request, result, decision):
    principal = request.get("principal") or {}
    context["audit"]["append"]({
        "request_id": request.get("request_id"),
        "principal_subject": principal.get("subject"),
        "session_id": principal.get("session_id"),
        "source_ip": request.get("source_ip"),
        "proxy_instance_id": context["instance_id"],
        "site_id": context["site_id"],
        "resource": request.get("action") or "client.command",
        "action": request.get("action") or "unknown",
        "decision": decision,
        "reason_code": result.get("code") or (
            "dispatched" if decision == "ALLOW" else "internal_error"),
        "operation_id": result.get("operation_id"),
        "details": {"transport": request.get("transport") or "unknown"},
    })


def _normalize_result(result):
    if not isinstance(result, dict):
        return make_error("internal_error", "dispatcher returned an invalid result")
    if not result.get("ok"):
        details = {
            key: _redact(value)
            for key, value in result.items()
            if key not in ("ok", "code", "message") and str(key).lower() not in SECRET_KEYS
        }
        normalized = make_error(
            result.get("code") or "internal_error",
            result.get("message") or result.get("code") or "internal_error")
        if details:
            normalized["details"] = details
        return normalized
    state = result.get("state")
    if not state:
        state = "ACCEPTED" if result.get("accepted") else "COMPLETED"
    normalized = make_success(
        state=str(state),
        operation_id=result.get("operation_id"),
    )
    if not result.get("accepted") and str(state) not in ("ACCEPTED", "PENDING"):
        normalized["reply"] = copy.deepcopy(result)
    return normalized


async def _authenticate_mutation(context, request):
    principal = request["principal"]
    authentication_method = str(
        principal.get("_authentication_method") or principal.get("auth_type") or "")
    if authentication_method != "session":
        return make_success(principal=principal)
    result = await asyncio.to_thread(partial(
        context["auth"]["authenticate_session"],
        {
            "session_token": request.get("session_token"),
            "csrf_token": request.get("csrf_token"),
            "csrf_required": True,
            "source_ip": request.get("source_ip"),
            "user_agent": request.get("user_agent"),
        },
    ))
    if not result.get("ok") or str(
            (result.get("principal") or {}).get("subject") or "") != str(
                principal.get("subject") or ""):
        return make_error("authorization_denied", "CSRF or session validation failed")
    refreshed = copy.deepcopy(result["principal"])
    return make_success(principal=refreshed)


async def dispatch_client_command(context, request):
    """Validate, authorize, audit and dispatch one canonical client request."""
    if not isinstance(request, dict):
        return make_error("invalid_payload", "dispatcher request must be an object")
    action = request.get("action")
    if action not in REGISTERED_ACTIONS:
        return make_error("invalid_payload", "action is not registered")
    principal = request.get("principal")
    if not isinstance(principal, dict) or not principal.get("subject"):
        return make_error("authentication_required", "principal is missing")
    canonical = copy.deepcopy(request)
    canonical["payload"] = copy.deepcopy(request.get("payload"))
    canonical["request_id"] = str(request.get("request_id") or uuid.uuid4())
    sites = set(principal.get("site_scopes") or [])
    if context["site_id"] not in sites and "*" not in sites:
        result = make_error("authorization_denied", "site scope is denied")
        await asyncio.to_thread(partial(
            _audit_event, context, canonical, result, "DENY"))
        return result
    checked = _validate_payload(
        action,
        request.get("payload"),
        authority_mode=context["authority_mode"],
    )
    if not checked.get("ok"):
        await asyncio.to_thread(partial(
            _audit_event, context, canonical, checked, "DENY"))
        return _normalize_result(checked)
    try:
        canonical["ttl_s"] = float(request.get("ttl_s", 30.0))
    except (TypeError, ValueError):
        result = _error("ttl_s must be numeric")
        await asyncio.to_thread(partial(
            _audit_event, context, canonical, result, "DENY"))
        return _normalize_result(result)
    if action in MUTATING_ACTIONS:
        key = request.get("idempotency_key")
        if not isinstance(key, str) or not key or len(key) > 128:
            result = _error("idempotency_key is required for mutations")
            await asyncio.to_thread(partial(
                _audit_event, context, canonical, result, "DENY"))
            return _normalize_result(result)
        if not math.isfinite(canonical["ttl_s"]) or not 1 <= canonical["ttl_s"] <= 300:
            result = _error("ttl_s is outside its allowed range")
            await asyncio.to_thread(partial(
                _audit_event, context, canonical, result, "DENY"))
            return _normalize_result(result)
        authenticated = await _authenticate_mutation(context, canonical)
        if not authenticated.get("ok"):
            await asyncio.to_thread(partial(
                _audit_event, context, canonical, authenticated, "DENY"))
            return _normalize_result(authenticated)
        canonical["principal"] = authenticated["principal"]
        refreshed_sites = set(canonical["principal"].get("site_scopes") or [])
        if context["site_id"] not in refreshed_sites and "*" not in refreshed_sites:
            result = make_error(
                "authorization_denied", "site scope was revoked")
            await asyncio.to_thread(partial(
                _audit_event, context, canonical, result, "DENY"))
            return _normalize_result(result)
    authorized = await asyncio.to_thread(partial(
        _preauthorize_action, context, canonical))
    if not authorized.get("ok"):
        await asyncio.to_thread(partial(
            _audit_event, context, canonical, authorized, "DENY"))
        return _normalize_result(authorized)
    limited = await asyncio.to_thread(partial(
        context["rate_limit"]["consume"],
        bucket_key(
            "client-command", canonical.get("source_ip"),
            canonical["principal"]["subject"], action),
        limit=context["rate_limit_count"],
        window_s=context["rate_limit_window_s"],
    ))
    if not limited.get("allowed"):
        result = make_error(
            "rate_limited", "client command rate limit exceeded",
            reset_at=limited.get("reset_at"))
        await asyncio.to_thread(partial(
            _audit_event, context, canonical, result, "DENY"))
        return _normalize_result(result)
    intent = make_success()
    try:
        await asyncio.to_thread(partial(
            _audit_event, context, canonical, intent, "ALLOW"))
    except Exception as exc:
        context["logger"].error(
            "Client command audit failed: action=%s request_id=%s error_type=%s",
            action, canonical["request_id"], type(exc).__name__)
        return make_error("internal_error", "client command audit could not be persisted")
    if action in LOCAL_MUTATING_ACTIONS:
        claimed = await asyncio.to_thread(partial(_claim_local, context, canonical))
        if not claimed.get("ok"):
            return _normalize_result(claimed)
        if claimed.get("duplicate"):
            return _normalize_result(claimed["result"])
    try:
        result = await asyncio.to_thread(partial(_run_action, context, canonical))
    except Exception as exc:
        context["logger"].error(
            "Client command failed: action=%s request_id=%s error_type=%s",
            action, canonical["request_id"], type(exc).__name__)
        result = make_error("internal_error", "client command failed")
    if result.get("code") in ("authentication_required", "authorization_denied"):
        try:
            await asyncio.to_thread(partial(
                _audit_event, context, canonical, result, "DENY"))
        except Exception as exc:
            context["logger"].error(
                "Client command denial audit failed: action=%s request_id=%s "
                "error_type=%s", action, canonical["request_id"],
                type(exc).__name__)
            return make_error(
                "internal_error", "client command denial audit could not be persisted")
    if action in LOCAL_MUTATING_ACTIONS:
        safe = _persistable_local_result(action, result)
        try:
            completed = await asyncio.to_thread(partial(
                context["gateway_store"]["complete_local_action"],
                canonical["request_id"], safe))
            if not completed:
                raise RuntimeError("local action claim is no longer pending")
        except Exception as exc:
            context["logger"].error(
                "Local action result persistence failed: action=%s error_type=%s",
                action, type(exc).__name__)
            return make_error(
                "internal_error", "local idempotency result could not be persisted")
    return _normalize_result(result)


def create_client_command_dispatcher(**kwargs):
    """Create one dispatcher shared by HTTPS routes and direct WSS."""
    key = kwargs.get("idempotency_key")
    if isinstance(key, str):
        key = key.encode("utf-8")
    if not isinstance(key, bytes) or len(key) < 16:
        raise ValueError("idempotency HMAC key must contain at least 16 bytes")
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "authority_mode": validate_authority_mode(
            kwargs.get("authority_mode", "node_local")),
        "auth": kwargs["auth"],
        "identity": kwargs["identity"],
        "rate_limit": kwargs["rate_limit"],
        "audit": kwargs["audit"],
        "commands": kwargs["commands"],
        "gateway_store": kwargs["gateway_store"],
        "runtime_snapshot": kwargs.get("runtime_snapshot"),
        "idempotency_key": key,
        "rate_limit_count": max(1, int(kwargs.get("rate_limit_count", 120))),
        "rate_limit_window_s": max(1, int(kwargs.get("rate_limit_window_s", 60))),
        "logger": kwargs["logger"],
    }
    return {
        "context": context,
        "dispatch": partial(dispatch_client_command, context),
        "actions": tuple(sorted(REGISTERED_ACTIONS)),
        "mutations": tuple(sorted(MUTATING_ACTIONS)),
    }

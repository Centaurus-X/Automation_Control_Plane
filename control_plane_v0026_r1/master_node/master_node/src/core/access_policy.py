# -*- coding: utf-8 -*-

# master_node/src/core/access_policy.py

"""Fail-closed control-plane authorization for Proxy asserted principals."""

import fnmatch

from src.library._generic_system_contract_library.errors import make_error, make_success


EVENT_PERMISSIONS = {
    "actuator.request": "actuator.request",
    "config.propose": "config.propose",
    "config.rollback": "config.rollback",
    "node.admission.approve": "node.admit",
    "node.drain": "node.drain",
    "node.state.set": "node.drain",
    "nodes.list": "node.read",
    "config.get": "config.read",
    "config.history": "config.read",
    "operation.get": "operation.read",
    "runtime.status": "node.read",
    "terminal.execute": "terminal.execute",
}


def authorize_control(envelope, **kwargs):
    """Require subject, atomic permission and matching site scope."""
    principal = envelope.get("principal")
    if not isinstance(principal, dict) or not str(principal.get("subject") or ""):
        return make_error("authentication_required", "principal subject is required")
    required = EVENT_PERMISSIONS.get(str(envelope.get("event_type") or ""))
    permissions = {str(value) for value in principal.get("permissions") or []}
    if not required or required not in permissions:
        return make_error("authorization_denied", "required control permission is absent")
    site_id = str(kwargs.get("site_id") or envelope.get("site_id") or "")
    site_scopes = {str(value) for value in principal.get("site_scopes") or []}
    if site_id not in site_scopes and "*" not in site_scopes:
        return make_error("authorization_denied", "principal has no site scope")
    resource_type, resource_id = _command_resource(envelope)
    if resource_type and resource_id and not _resource_allowed(
            principal, site_id, resource_type, resource_id):
        return make_error("authorization_denied", "principal resource scope is denied")
    return make_success(permission=required)


def _command_resource(envelope):
    payload = envelope.get("payload") if isinstance(envelope.get("payload"), dict) else {}
    event_type = str(envelope.get("event_type") or "")
    if event_type == "actuator.request":
        return "controller", str(payload.get("controller_uuid") or "")
    if event_type in ("node.admission.approve", "node.drain", "node.state.set"):
        return "node", str(payload.get("node_id") or "")
    return None, None


def _resource_allowed(principal, site_id, resource_type, resource_id):
    scopes = []
    for scope in principal.get("resource_scopes") or []:
        if not isinstance(scope, dict):
            continue
        if scope.get("resource_type") != resource_type:
            continue
        if scope.get("site_id") not in (site_id, "*"):
            continue
        scopes.append(scope)
    if not scopes:
        return True
    return any(fnmatch.fnmatchcase(
        str(resource_id), str(scope.get("resource_pattern") or "")) for scope in scopes)

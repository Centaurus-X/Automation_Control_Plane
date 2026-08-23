# -*- coding: utf-8 -*-

# proxy_gateway/src/core/access_policy.py

"""Default-deny RBAC, site scopes, resource scopes and config path policy."""

import fnmatch

from src.library._generic_system_contract_library.config_sharding_contracts import (
    validate_scope_operations,
)
from src.library._generic_system_contract_library.errors import make_error, make_success


CONFIG_ALLOWED_SECTIONS = (
    "actuator_types", "actuators", "control_method_templates",
    "control_methods", "controllers", "filters", "local_node",
    "modbus_functions", "nodes", "parameter_generic_templates",
    "pid_configs", "process_modes", "process_states", "process_values",
    "processes", "rule_engine", "rules", "scaling_profiles",
    "schema_mappings", "sensor_types", "sensors", "site_controller_map",
    "sites", "timers", "virtual_controllers",
)
CONFIG_DENIED_PREFIXES = ("certificates", "credentials", "secrets", "tls")


def authorize(principal, permission, **kwargs):
    """Apply permission, site and optional resource scope checks."""
    if not isinstance(principal, dict) or not principal.get("subject"):
        return make_error("authentication_required", "principal is missing")
    permissions = {str(value) for value in principal.get("permissions") or []}
    if str(permission) not in permissions:
        return make_error("authorization_denied", "permission is denied")
    site_id = str(kwargs.get("site_id") or "")
    sites = {str(value) for value in principal.get("site_scopes") or []}
    if site_id and site_id not in sites and "*" not in sites:
        return make_error("authorization_denied", "site scope is denied")
    resource_type = kwargs.get("resource_type")
    resource_id = kwargs.get("resource_id")
    if resource_type and resource_id and not _resource_allowed(
            principal, site_id, resource_type, resource_id):
        return make_error("authorization_denied", "resource scope is denied")
    return make_success(permission=permission)


def authorize_role_grant(principal, site_id, role, **kwargs):
    """Allow only known roles whose permissions do not exceed the grantor."""
    managed_site_id = str(kwargs.get("managed_site_id") or site_id)
    if str(site_id) != managed_site_id:
        return make_error(
            "authorization_denied", "role grant belongs to another site")
    allowed = authorize(principal, "user.manage", site_id=site_id)
    if not allowed.get("ok"):
        return allowed
    if not isinstance(role, dict) or not str(role.get("role_name") or ""):
        return make_error("not_found", "role is unknown")
    role_permissions = role.get("permissions")
    if not isinstance(role_permissions, (list, tuple, set)):
        return make_error("authorization_denied", "role permissions are invalid")
    principal_permissions = {
        str(value) for value in principal.get("permissions") or []
    }
    requested_permissions = {str(value) for value in role_permissions}
    if not requested_permissions.issubset(principal_permissions):
        return make_error(
            "authorization_denied", "role permissions exceed the grantor")
    return make_success(
        role_name=str(role["role_name"]), site_id=str(site_id))


def _resource_allowed(principal, site_id, resource_type, resource_id):
    matching_type = []
    for scope in principal.get("resource_scopes") or []:
        if not isinstance(scope, dict):
            continue
        if scope.get("resource_type") != resource_type:
            continue
        if scope.get("site_id") not in (site_id, "*"):
            continue
        matching_type.append(scope)
    if not matching_type:
        return True
    return any(fnmatch.fnmatchcase(str(resource_id), str(scope["resource_pattern"]))
               for scope in matching_type)


def authorize_config_changes(principal, site_id, changes):
    """Authorize the explicit master-global legacy change format."""
    allowed = authorize(principal, "config.propose", site_id=site_id)
    if not allowed.get("ok"):
        return allowed
    if not isinstance(changes, list) or not changes or len(changes) > 128:
        return make_error("invalid_payload", "changes must be a bounded non-empty list")
    for change in changes:
        path = str(change.get("path") or "") if isinstance(change, dict) else ""
        section = path.replace("[", ".").replace("/", ".").split(".", 1)[0].lower()
        if section in CONFIG_DENIED_PREFIXES or section not in CONFIG_ALLOWED_SECTIONS:
            return make_error("authorization_denied", "configuration path is denied")
    return make_success()


def authorize_config_operations(principal, site_id, operations):
    """Authorize canonical entity-keyed operations without translating them."""
    allowed = authorize(principal, "config.propose", site_id=site_id)
    if not allowed.get("ok"):
        return allowed
    try:
        normalized = validate_scope_operations(operations)
    except ValueError as exc:
        return make_error("unsupported_operation_format", str(exc))
    for operation in normalized:
        section = str(operation.get("section") or "").strip().lower()
        if section in CONFIG_DENIED_PREFIXES or section not in CONFIG_ALLOWED_SECTIONS:
            return make_error("authorization_denied", "configuration section is denied")
    return make_success(operations=normalized)

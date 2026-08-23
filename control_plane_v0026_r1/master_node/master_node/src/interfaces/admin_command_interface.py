# -*- coding: utf-8 -*-

# master_node/src/interfaces/admin_command_interface.py

"""Allowlisted domain-command console; this is never an operating-system shell."""

import shlex

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success


def _command_key(tokens):
    if not tokens:
        return ""
    if len(tokens) == 1:
        return tokens[0].lower()
    return (tokens[0] + " " + tokens[1]).lower()


def execute_admin_command(context, command, **kwargs):
    """Parse and execute one exact registered domain command."""
    try:
        tokens = shlex.split(str(command or ""), posix=True)
    except ValueError as exc:
        return make_error("invalid_payload", str(exc))
    if len(tokens) > 4 or any(len(token) > 128 for token in tokens):
        return make_error("invalid_payload", "terminal command exceeds its limit")
    key = _command_key(tokens)
    permissions = set(kwargs.get("permissions") or ())
    required = context["permissions"].get(key)
    if not required or required not in permissions:
        return make_error("authorization_denied", "terminal command is denied")
    if key == "nodes list" and len(tokens) == 2:
        return make_success(nodes=context["nodes"]["list"]())
    if key == "node drain" and len(tokens) == 3:
        if not _is_authority(context):
            return make_error("not_config_authority", "node mutation requires active authority")
        return context["nodes"]["set_desired_state"]({
            "node_id": tokens[2], "desired_state": "DRAINING",
            "reason": "terminal command", "principal_subject": kwargs["principal_subject"],
        })
    if key == "node approve" and len(tokens) == 3:
        if not _is_authority(context):
            return make_error("not_config_authority", "node mutation requires active authority")
        return context["nodes"]["decide_admission"]({
            "node_id": tokens[2], "decision": "APPROVED",
            "reason": "terminal command", "principal_subject": kwargs["principal_subject"],
        })
    if key in ("node maintenance", "node quarantine", "node resume") and len(tokens) == 3:
        if not _is_authority(context):
            return make_error("not_config_authority", "node mutation requires active authority")
        states = {
            "node maintenance": "MAINTENANCE",
            "node quarantine": "QUARANTINED",
            "node resume": "ACTIVE",
        }
        return context["nodes"]["set_desired_state"]({
            "node_id": tokens[2], "desired_state": states[key],
            "reason": "terminal command", "principal_subject": kwargs["principal_subject"],
        })
    if key == "config revision" and len(tokens) == 2:
        authority = context["authority_reader"]() or {}
        if context["authority_mode"] == "node_local":
            return make_success(
                authority_mode="node_local",
                config_authority="worker",
                registry_term=authority.get("authority_term"),
                assigns_worker_revision=False,
            )
        return make_success(
            authority_term=authority.get("authority_term"),
            config_revision=authority.get("config_revision"),
            fingerprint=authority.get("config_fingerprint"),
        )
    if key == "config history" and len(tokens) == 2:
        return make_success(history=context["config_history"]())
    if key == "operation show" and len(tokens) == 3:
        operation = context["operations"]["get"](tokens[2])
        return make_success(operation=operation) if operation else make_error(
            "not_found", "operation is unknown")
    return make_error("invalid_payload", "terminal command syntax is invalid")


def _is_authority(context):
    authority = context["authority_reader"]() or {}
    return bool(
        authority.get("lease_active")
        and authority.get("leader_instance_id") == context["instance_id"]
        and authority.get("lease_token")
    )


def create_admin_command_interface(**kwargs):
    """Create the allowlisted domain-console capability."""
    context = {
        "nodes": kwargs["nodes"],
        "instance_id": kwargs["instance_id"],
        "authority_mode": str(kwargs.get("authority_mode") or "node_local"),
        "operations": kwargs["operations"],
        "authority_reader": kwargs["authority_reader"],
        "config_history": kwargs["config_history"],
        "permissions": {
            "nodes list": "node.read",
            "node drain": "node.drain",
            "node approve": "node.admit",
            "node maintenance": "node.drain",
            "node quarantine": "node.drain",
            "node resume": "node.drain",
            "config revision": "config.read",
            "config history": "config.read",
            "operation show": "operation.read",
        },
    }
    return {"execute": partial(execute_admin_command, context)}

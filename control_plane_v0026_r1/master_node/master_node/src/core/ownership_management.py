# -*- coding: utf-8 -*-

# master_node/src/core/ownership_management.py

"""Controller ownership leases bound to leader, term and config revision."""

from functools import partial

from src.library._generic_system_contract_library.errors import make_error


def reconcile(context, config):
    """Reconcile configured routes only while this instance owns authority."""
    authority = context["authority_reader"]()
    owned = (
        authority
        and authority.get("lease_active")
        and authority.get("leader_instance_id") == context["instance_id"]
        and authority.get("lease_token")
    )
    if not owned:
        return make_error("not_config_authority", "ownership requires active authority")
    request = {
        "site_id": context["site_id"],
        "instance_id": context["instance_id"],
        "lease_token": authority["lease_token"],
        "authority_term": authority["authority_term"],
        "config_revision": authority["config_revision"],
        "config": config,
        "lease_s": context["lease_s"],
        "node_timeout_s": context["node_timeout_s"],
    }
    return context["store"]["reconcile_ownership"](request)


def resolve(context, controller_uuid):
    """Resolve an owner using current term and revision as fencing values."""
    authority = context["authority_reader"]()
    if not authority or not authority.get("lease_active"):
        return None
    return context["store"]["resolve_owner"](
        context["site_id"], controller_uuid,
        authority_term=authority["authority_term"],
        config_revision=authority["config_revision"],
    )


def create_ownership_management(**kwargs):
    """Create narrow ownership capabilities."""
    context = {
        "site_id": kwargs["site_id"],
        "instance_id": kwargs["instance_id"],
        "authority_reader": kwargs["authority_reader"],
        "store": kwargs["store"],
        "lease_s": max(5.0, float(kwargs.get("lease_s", 30.0))),
        "node_timeout_s": max(5.0, float(kwargs.get("node_timeout_s", 45.0))),
    }
    return {
        "context": context,
        "reconcile": partial(reconcile, context),
        "resolve": partial(resolve, context),
    }

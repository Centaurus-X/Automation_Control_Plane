# -*- coding: utf-8 -*-

# master_node/src/library/master_data_store.py

"""Composition of PostgreSQL capabilities behind explicit dictionaries."""

from src.library.audit_store import build_audit_capabilities
from src.library.authority_store_postgresql import build_authority_capabilities
from src.library.master_entity_store_postgresql import build_master_entity_capabilities
from src.library.master_mirror_store_postgresql import build_master_mirror_capabilities
from src.library.node_store_postgresql import build_node_capabilities
from src.library.maintenance_store import build_maintenance_capabilities
from src.library.operation_store_postgresql import build_operation_capabilities
from src.library.postgresql import create_pool


def create_master_data_store(database_options):
    """Create the complete persistence context without exposing credentials."""
    database = create_pool(database_options)
    authority = build_authority_capabilities(database)
    authority.update(build_master_entity_capabilities(database))
    authority.update(build_master_mirror_capabilities(database))
    return {
        "database": database,
        "authority": authority,
        "nodes": build_node_capabilities(database),
        "operations": build_operation_capabilities(database),
        "maintenance": build_maintenance_capabilities(database),
        "audit": build_audit_capabilities(database),
        "ping": database["ping"],
        "close": database["close"],
    }

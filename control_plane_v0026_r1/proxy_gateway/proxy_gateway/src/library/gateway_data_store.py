# -*- coding: utf-8 -*-

# proxy_gateway/src/library/gateway_data_store.py

"""Compose all Proxy PostgreSQL capabilities."""

from src.library.audit_store import build_audit_capabilities
from src.library.gateway_store_postgresql import build_gateway_store_capabilities
from src.library.identity_store_postgresql import build_identity_capabilities
from src.library.postgresql import create_pool
from src.library.rate_limit_store import build_rate_limit_capabilities


def create_gateway_data_store(database_options):
    """Create the shared Proxy data context without exposing connection details."""
    database = create_pool(database_options)
    return {
        "database": database,
        "identity": build_identity_capabilities(database),
        "rate_limit": build_rate_limit_capabilities(database),
        "audit": build_audit_capabilities(database),
        "gateway": build_gateway_store_capabilities(database),
        "ping": database["ping"],
        "close": database["close"],
    }

# -*- coding: utf-8 -*-

# master_node/src/library/postgresql.py

"""Bounded PostgreSQL connection pool and short transaction helpers."""

import os
import time

from functools import partial


TRANSACTION_ISOLATION_STATEMENTS = {
    "READ COMMITTED": "SET TRANSACTION ISOLATION LEVEL READ COMMITTED",
    "REPEATABLE READ": "SET TRANSACTION ISOLATION LEVEL REPEATABLE READ",
    "SERIALIZABLE": "SET TRANSACTION ISOLATION LEVEL SERIALIZABLE",
}


def read_secret(path, **kwargs):
    """Read one secret file without logging its value."""
    if not path:
        if kwargs.get("required", True):
            raise ValueError("required secret file path is missing")
        return None
    with open(path, "r", encoding="utf-8") as handle:
        value = handle.read().strip()
    if not value and kwargs.get("required", True):
        raise ValueError("required secret file is empty")
    return value


def build_conninfo(options):
    """Build a libpq conninfo string from validated non-secret options."""
    password = read_secret(options.get("password_file"))
    fields = {
        "host": str(options.get("host") or "automation-postgres"),
        "port": int(options.get("port", 5432)),
        "dbname": str(options.get("database") or "automation_platform"),
        "user": str(options.get("user") or "master_app"),
        "password": password,
        "sslmode": str(options.get("sslmode") or "verify-full"),
        "connect_timeout": int(options.get("connect_timeout_s", 5)),
        "application_name": str(options.get("application_name") or "master-node"),
    }
    if options.get("sslrootcert"):
        fields["sslrootcert"] = str(options["sslrootcert"])
    return " ".join("{}={}".format(key, _quote_conninfo(value)) for key, value in fields.items())


def _quote_conninfo(value):
    text = str(value)
    escaped = text.replace("\\", "\\\\").replace("'", "\\'")
    return "'" + escaped + "'"


def create_pool(options):
    """Create and open one bounded Psycopg connection pool."""
    from psycopg.rows import dict_row
    from psycopg_pool import ConnectionPool

    minimum = max(1, int(options.get("pool_min_size", 1)))
    maximum = max(minimum, int(options.get("pool_max_size", 8)))
    pool = ConnectionPool(
        conninfo=build_conninfo(options),
        min_size=minimum,
        max_size=maximum,
        timeout=float(options.get("pool_timeout_s", 5.0)),
        max_idle=float(options.get("pool_max_idle_s", 60.0)),
        kwargs={"row_factory": dict_row, "autocommit": False},
        open=False,
    )
    pool.open(wait=True, timeout=float(options.get("connect_timeout_s", 5.0)))
    return {
        "pool": pool,
        "options": dict(options),
        "closed": False,
        "transaction": partial(run_transaction, pool, dict(options)),
        "ping": partial(ping, pool),
        "close": partial(close_pool, pool),
    }


def _set_transaction_limits(connection, options):
    lock_ms = max(100, int(options.get("lock_timeout_ms", 2000)))
    statement_ms = max(lock_ms, int(options.get("statement_timeout_ms", 10000)))
    connection.execute(
        "SELECT set_config('lock_timeout', %s, true)", (str(lock_ms) + "ms",))
    connection.execute(
        "SELECT set_config('statement_timeout', %s, true)",
        (str(statement_ms) + "ms",),
    )


def _transaction_isolation_statement(value):
    """Return one closed SQL statement for a supported isolation level."""
    normalized = str(value or "").strip().upper().replace("_", " ")
    statement = TRANSACTION_ISOLATION_STATEMENTS.get(normalized)
    if not statement:
        raise ValueError("unsupported transaction isolation level")
    return statement


def run_transaction(pool, options, operation, **kwargs):
    """Run a short transaction with bounded serialization/deadlock retries."""
    from psycopg import errors

    attempts = max(1, int(kwargs.get("max_attempts", 3)))
    isolation = _transaction_isolation_statement(
        kwargs.get("isolation_level", "SERIALIZABLE"))
    retry_errors = (errors.SerializationFailure, errors.DeadlockDetected)
    for attempt in range(attempts):
        try:
            with pool.connection() as connection:
                with connection.transaction():
                    connection.execute(isolation)
                    _set_transaction_limits(connection, options)
                    return operation(connection)
        except retry_errors:
            if attempt + 1 >= attempts:
                raise
            time.sleep(0.02 * (2 ** attempt))
    raise RuntimeError("transaction retry loop exhausted")


def ping(pool):
    """Verify that the pool can execute a bounded read."""
    try:
        with pool.connection() as connection:
            row = connection.execute("SELECT 1 AS value").fetchone()
        return bool(row and row.get("value") == 1)
    except Exception:
        return False


def close_pool(pool):
    """Close all database connections deterministically."""
    pool.close()
    return True


def jsonb(value):
    """Wrap JSON data for explicit PostgreSQL jsonb adaptation."""
    from psycopg.types.json import Jsonb

    return Jsonb(value)


def env_database_options(prefix="MASTER_DB_"):
    """Load database options without loading secret values into config."""
    return {
        "host": os.environ.get(prefix + "HOST", "automation-postgres"),
        "port": int(os.environ.get(prefix + "PORT", "5432")),
        "database": os.environ.get(prefix + "NAME", "automation_platform"),
        "user": os.environ.get(prefix + "USER", "master_app"),
        "password_file": os.environ.get(
            prefix + "PASSWORD_FILE", "/run/secrets/master_db_password"),
        "sslmode": os.environ.get(prefix + "SSLMODE", "verify-full"),
        "sslrootcert": os.environ.get(prefix + "SSLROOTCERT", "/run/secrets/postgres_ca"),
        "application_name": os.environ.get("MASTER_INSTANCE_ID", "master-01"),
        "pool_min_size": int(os.environ.get(prefix + "POOL_MIN", "1")),
        "pool_max_size": int(os.environ.get(prefix + "POOL_MAX", "8")),
        "connect_timeout_s": int(os.environ.get(prefix + "CONNECT_TIMEOUT", "5")),
        "lock_timeout_ms": int(os.environ.get(prefix + "LOCK_TIMEOUT_MS", "2000")),
        "statement_timeout_ms": int(os.environ.get(prefix + "STATEMENT_TIMEOUT_MS", "10000")),
    }

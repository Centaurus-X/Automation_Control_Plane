# -*- coding: utf-8 -*-

# master_node/src/database_transaction_probe.py

"""Real PostgreSQL commit/isolation/rollback probe for the release gate."""

import uuid

from functools import partial

from src.library.postgresql import create_pool, env_database_options


def _read_isolation(connection):
    row = connection.execute(
        "SELECT current_setting('transaction_isolation') AS value").fetchone()
    return str(row.get("value") or "")


def _insert_then_fail(connection, audit_id):
    connection.execute(
        "INSERT INTO master_control.audit_log("
        "audit_id,request_id,instance_id,site_id,resource,action,decision,reason_code) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (
            audit_id, "database-rollback-probe", "master-db-probe", "site-main",
            "database", "transaction.rollback.probe", "ALLOW", "test_only",
        ),
    )
    raise RuntimeError("expected database rollback probe")


def _audit_count(connection, audit_id):
    row = connection.execute(
        "SELECT count(*) AS value FROM master_control.audit_log WHERE audit_id=%s",
        (audit_id,),
    ).fetchone()
    return int(row.get("value") or 0)


def main():
    database = create_pool(env_database_options())
    audit_id = str(uuid.uuid4())
    try:
        isolation = database["transaction"](
            _read_isolation, isolation_level="SERIALIZABLE")
        if isolation != "serializable":
            raise RuntimeError("serializable isolation was not applied")
        try:
            database["transaction"](
                partial(_insert_then_fail, audit_id=audit_id),
                isolation_level="READ COMMITTED", max_attempts=1)
        except TypeError:
            raise
        except RuntimeError as exc:
            if str(exc) != "expected database rollback probe":
                raise
        else:
            raise RuntimeError("rollback probe did not raise")
        remaining = database["transaction"](
            partial(_audit_count, audit_id=audit_id),
            isolation_level="READ COMMITTED")
        if remaining != 0:
            raise RuntimeError("rollback probe left a persistent audit row")
    finally:
        database["close"]()
    print("Master PostgreSQL commit, isolation and rollback probe passed.")


if __name__ == "__main__":
    raise SystemExit(main())

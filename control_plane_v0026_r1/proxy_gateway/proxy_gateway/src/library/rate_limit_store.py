# -*- coding: utf-8 -*-

# proxy_gateway/src/library/rate_limit_store.py

"""Cross-instance fixed-window rate limits stored atomically in PostgreSQL."""

import hashlib

from functools import partial


def bucket_key(*parts):
    """Hash privacy-sensitive limiter dimensions before persistence."""
    text = "\x1f".join(str(part or "") for part in parts)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def consume(database, key, **kwargs):
    """Consume one request from a shared atomic time window."""
    limit = max(1, int(kwargs.get("limit", 60)))
    window_s = max(1, int(kwargs.get("window_s", 60)))

    def write(connection):
        row = connection.execute(
            "INSERT INTO proxy_gateway.rate_limit_state("
            "bucket_key,window_started_at,request_count) VALUES (%s,clock_timestamp(),1) "
            "ON CONFLICT (bucket_key) DO UPDATE SET "
            "window_started_at=CASE WHEN rate_limit_state.window_started_at + "
            "(%s*interval '1 second') <= clock_timestamp() THEN clock_timestamp() "
            "ELSE rate_limit_state.window_started_at END, "
            "request_count=CASE WHEN rate_limit_state.window_started_at + "
            "(%s*interval '1 second') <= clock_timestamp() THEN 1 "
            "ELSE rate_limit_state.request_count+1 END,updated_at=clock_timestamp() "
            "RETURNING request_count,window_started_at,"
            "window_started_at+(%s*interval '1 second') AS reset_at",
            (key, window_s, window_s, window_s),
        ).fetchone()
        allowed = int(row["request_count"]) <= limit
        return {
            "ok": allowed,
            "allowed": allowed,
            "limit": limit,
            "remaining": max(0, limit - int(row["request_count"])),
            "reset_at": row["reset_at"],
        }

    return database["transaction"](write, isolation_level="READ COMMITTED")


def build_rate_limit_capabilities(database):
    """Expose shared limiter capabilities."""
    return {"consume": partial(consume, database), "key": bucket_key}

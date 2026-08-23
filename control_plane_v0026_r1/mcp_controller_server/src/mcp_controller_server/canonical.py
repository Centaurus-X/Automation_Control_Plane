# -*- coding: utf-8 -*-

"""Deterministic JSON and time helpers for MCP and MQTT contracts."""

import datetime
import decimal
import hashlib
import json
import math
import uuid


def json_ready(value):
    if isinstance(value, dict):
        return {str(key): json_ready(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_ready(item) for item in value]
    if isinstance(value, (datetime.datetime, datetime.date, datetime.time)):
        return value.isoformat()
    if isinstance(value, decimal.Decimal):
        return str(value)
    if isinstance(value, uuid.UUID):
        return str(value)
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("non-finite numbers are not valid contract values")
    return value


def canonical_text(value):
    return json.dumps(
        json_ready(value), sort_keys=True, separators=(",", ":"),
        ensure_ascii=False,
    )


def canonical_bytes(value):
    return canonical_text(value).encode("utf-8")


def fingerprint(value):
    return hashlib.sha256(canonical_bytes(value)).hexdigest()


def is_fingerprint(value):
    if not isinstance(value, str) or len(value) != 64:
        return False
    return all(character in "0123456789abcdef" for character in value)


def decode_json_bytes(payload, maximum_bytes=4 * 1024 * 1024, require_object=False):
    raw = bytes(payload)
    if len(raw) > max(1, int(maximum_bytes)):
        raise ValueError("JSON payload exceeds the configured limit")
    value = json.loads(raw.decode("utf-8"))
    if require_object and not isinstance(value, dict):
        raise ValueError("JSON payload root must be an object")
    return value


def utc_now():
    return datetime.datetime.now(datetime.timezone.utc)


def format_timestamp(value):
    if not isinstance(value, datetime.datetime) or value.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    normalized = value.astimezone(datetime.timezone.utc)
    return normalized.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def parse_timestamp(value):
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        raise ValueError("timestamp must include a timezone")
    return parsed.astimezone(datetime.timezone.utc)

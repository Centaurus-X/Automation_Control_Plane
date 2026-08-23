# -*- coding: utf-8 -*-

# proxy_gateway/src/library/token_security.py

"""Argon2id passwords and peppered one-way session/API token storage."""

import hashlib
import hmac
import secrets


def validate_password(password, **kwargs):
    """Apply a length-first password policy without brittle composition rules."""
    text = str(password or "")
    minimum = max(12, int(kwargs.get("minimum_length", 14)))
    maximum = min(4096, max(minimum, int(kwargs.get("maximum_length", 1024))))
    if len(text) < minimum:
        return {"ok": False, "code": "password_too_short"}
    if len(text) > maximum:
        return {"ok": False, "code": "password_too_long"}
    return {"ok": True}


def hash_password(password, pepper, **kwargs):
    """Create an encoded Argon2id hash with explicit resource parameters."""
    from argon2.low_level import Type, hash_secret

    checked = validate_password(password, **kwargs)
    if not checked.get("ok"):
        raise ValueError(checked["code"])
    secret = (str(password) + str(pepper)).encode("utf-8")
    encoded = hash_secret(
        secret,
        secrets.token_bytes(16),
        time_cost=max(2, int(kwargs.get("time_cost", 3))),
        memory_cost=max(32768, int(kwargs.get("memory_cost_kib", 65536))),
        parallelism=max(1, int(kwargs.get("parallelism", 4))),
        hash_len=max(24, int(kwargs.get("hash_len", 32))),
        type=Type.ID,
        version=19,
    )
    return encoded.decode("ascii")


def verify_password(password, encoded_hash, pepper):
    """Verify an Argon2id hash without exposing mismatch details."""
    from argon2.low_level import Type, verify_secret

    try:
        return bool(verify_secret(
            str(encoded_hash).encode("ascii"),
            (str(password) + str(pepper)).encode("utf-8"),
            Type.ID,
        ))
    except Exception:
        return False


def generate_token(**kwargs):
    """Generate a high-entropy URL-safe bearer token."""
    return secrets.token_urlsafe(max(32, int(kwargs.get("entropy_bytes", 48))))


def token_hash(token, pepper):
    """Return a keyed non-reversible SHA-256 token digest."""
    return hmac.new(
        str(pepper).encode("utf-8"),
        str(token).encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()


def binding_hash(source_ip, user_agent, pepper):
    """Create a privacy-preserving client binding digest."""
    value = "{}\n{}".format(source_ip or "", user_agent or "")
    return token_hash(value, pepper)


def token_prefix(token):
    """Return a non-secret lookup/display prefix."""
    return str(token)[:10]

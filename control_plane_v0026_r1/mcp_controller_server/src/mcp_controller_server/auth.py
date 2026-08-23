# -*- coding: utf-8 -*-

"""Bearer authentication and bounded per-client rate limiting."""

import copy
import hashlib
import hmac
import json
import os
import threading
import time

from mcp_controller_server.config import _validate_principal
from mcp_controller_server.errors import make_error, make_success


def _token_digest(token):
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


def load_auth_state(path, request_limit_per_minute):
    absolute = os.path.abspath(str(path))
    if not os.path.isfile(absolute) or os.path.islink(absolute):
        raise ValueError("authentication file is missing or symbolic")
    if os.path.getsize(absolute) > 1024 * 1024:
        raise ValueError("authentication file exceeds its size limit")
    with open(absolute, "r", encoding="utf-8") as handle:
        document = json.load(handle)
    if not isinstance(document, dict) or set(document) != {"schema_version", "clients"}:
        raise ValueError("authentication document fields are invalid")
    if document.get("schema_version") != 1:
        raise ValueError("authentication schema_version must be 1")
    clients = document.get("clients")
    if not isinstance(clients, list) or not clients or len(clients) > 256:
        raise ValueError("authentication clients must be a bounded non-empty list")
    records = []
    identities = set()
    digests = set()
    for index, client in enumerate(clients):
        if not isinstance(client, dict) or set(client) != {"client_id", "token", "principal"}:
            raise ValueError("authentication client fields are invalid")
        client_id = str(client.get("client_id") or "")
        token = client.get("token")
        if not client_id or len(client_id) > 128 or client_id in identities:
            raise ValueError("authentication client_id is invalid or duplicated")
        if not isinstance(token, str) or len(token) < 32 or len(token) > 4096:
            raise ValueError("authentication token must contain at least 32 characters")
        principal = copy.deepcopy(client.get("principal"))
        _validate_principal(principal, "clients[{}].principal".format(index))
        digest = _token_digest(token)
        if digest in digests:
            raise ValueError("authentication token is duplicated")
        identities.add(client_id)
        digests.add(digest)
        records.append({
            "client_id": client_id,
            "token_digest": digest,
            "principal": principal,
            "requests": [],
        })
    return {
        "lock": threading.RLock(),
        "clients": records,
        "request_limit_per_minute": int(request_limit_per_minute),
    }


def _bearer_token(header):
    value = str(header or "")
    if not value.startswith("Bearer "):
        return None
    token = value[7:]
    if not token or token != token.strip() or any(ord(character) < 33 for character in token):
        return None
    return token


def _rate_allowed(state, record, now):
    cutoff = now - 60.0
    record["requests"] = [stamp for stamp in record["requests"] if stamp > cutoff]
    if len(record["requests"]) >= state["request_limit_per_minute"]:
        return False
    record["requests"].append(now)
    return True


def authenticate_bearer(state, authorization, now=None):
    token = _bearer_token(authorization)
    if token is None:
        return make_error("authentication_required", "valid Bearer authentication is required")
    supplied = _token_digest(token)
    current = time.monotonic() if now is None else float(now)
    selected = None
    with state["lock"]:
        for record in state["clients"]:
            if hmac.compare_digest(supplied, record["token_digest"]):
                selected = record
        if selected is None:
            return make_error("authentication_required", "Bearer authentication failed")
        if not _rate_allowed(state, selected, current):
            return make_error("rate_limited", "client request rate exceeded", retryable=True)
        return make_success(
            client_id=selected["client_id"],
            principal=copy.deepcopy(selected["principal"]),
        )

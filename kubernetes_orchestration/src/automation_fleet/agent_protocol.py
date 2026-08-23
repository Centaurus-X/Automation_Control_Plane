"""Per-target agent authentication and signed task envelopes."""

import hashlib
import hmac
import pathlib

from automation_fleet.canonical import canonical_bytes, read_json
from automation_fleet.validation import require, validate_identifier


def _read_token(path, target_id):
    target = pathlib.Path(path)
    stat_result = target.stat()
    require(stat_result.st_mode & 0o077 == 0,
            "agent_token_mode",
            target_id + " agent token file must not be group/world accessible")
    token = target.read_text(encoding="utf-8").strip()
    require(32 <= len(token) <= 4096
            and not any(character.isspace() for character in token),
            "agent_token", target_id + " agent token is invalid")
    require(not token.startswith("CHANGE_ME_"),
            "agent_token", target_id + " agent token is a placeholder")
    return token


def load_token(path, target_id):
    """Load one agent token with the same fail-closed registry checks."""
    validate_identifier(target_id, "agent target_id")
    return _read_token(path, target_id)


def load_registry(path):
    if not path:
        return {}
    value = read_json(path)
    require(value.get("schema_version") == 1,
            "agent_registry", "agent registry schema_version must be 1")
    agents = value.get("agents")
    require(isinstance(agents, list),
            "agent_registry", "agent registry agents must be a list")
    result = {}
    for item in agents:
        require(isinstance(item, dict),
                "agent_registry", "agent registry entry must be an object")
        target_id = validate_identifier(item.get("target_id"), "agent target_id")
        require(target_id not in result,
                "agent_registry", "duplicate agent target_id: " + target_id)
        require(item.get("enabled", True) is True,
                "agent_registry", "disabled agents must be removed from the active registry")
        token_path = item.get("token_file")
        require(isinstance(token_path, str) and pathlib.Path(token_path).is_absolute(),
                "agent_registry", target_id + " token_file must be absolute")
        tls_subject = item.get("tls_subject")
        require(tls_subject is None
                or isinstance(tls_subject, str) and 1 <= len(tls_subject) <= 1000,
                "agent_registry", target_id + " tls_subject is invalid")
        result[target_id] = {
            "target_id": target_id,
            "token": _read_token(token_path, target_id),
            "tls_subject": tls_subject,
        }
    return result


def authenticate(authorization, registry, target_id, tls_subject=None):
    entry = registry.get(target_id)
    if entry is None or not isinstance(authorization, str):
        return None
    if not authorization.startswith("Bearer "):
        return None
    supplied = authorization[7:]
    if not hmac.compare_digest(supplied, entry["token"]):
        return None
    expected_subject = entry.get("tls_subject")
    if expected_subject and not hmac.compare_digest(str(tls_subject or ""), expected_subject):
        return None
    return {"target_id": target_id, "token": entry["token"]}


def sign_payload(token, payload):
    return "sha256:" + hmac.new(
        token.encode("utf-8"), canonical_bytes(payload), hashlib.sha256).hexdigest()


def envelope(token, payload):
    return {"payload": payload, "signature": sign_payload(token, payload)}


def verify_envelope(token, value):
    require(isinstance(value, dict), "agent_envelope", "agent envelope must be an object")
    payload = value.get("payload")
    signature = value.get("signature")
    require(isinstance(payload, dict) and isinstance(signature, str),
            "agent_envelope", "agent envelope is incomplete")
    expected = sign_payload(token, payload)
    require(hmac.compare_digest(expected, signature),
            "agent_signature", "agent envelope signature is invalid")
    return payload

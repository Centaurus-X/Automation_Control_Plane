# -*- coding: utf-8 -*-

# proxy_gateway/src/system/bootstrap_system_interface.py

"""Proxy JSON configuration, environment overlay and logging bootstrap."""

import copy
import json
import logging
import os
import sys

from urllib.parse import urlsplit

from src.library._generic_system_contract_library import validate_authority_mode
from src.library._generic_system_contract_library.protocol_contracts import normalize_token


def _as_bool(value, default=False):
    if isinstance(value, bool):
        return value
    normalized = str(value or "").strip().lower()
    if normalized in ("1", "true", "yes", "on"):
        return True
    if normalized in ("0", "false", "no", "off"):
        return False
    return bool(default)


def _deep_merge(base, update):
    result = copy.deepcopy(base)
    for key, value in update.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = copy.deepcopy(value)
    return result


def _origin_allowlist(base):
    """Load and validate exact browser origins from one CSV environment value."""
    raw = os.environ.get("PROXY_ORIGIN_ALLOWLIST")
    values = list(base or []) if raw is None else raw.split(",")
    if not values:
        raise ValueError("HTTPS origin allowlist must not be empty")
    result = []
    for value in values:
        origin = str(value).strip()
        if not origin:
            raise ValueError("HTTPS origin allowlist contains an empty origin")
        parsed = urlsplit(origin)
        try:
            port = parsed.port
        except ValueError as exc:
            raise ValueError("HTTPS origin contains an invalid port") from exc
        if (
                parsed.scheme != "https" or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path or parsed.query or parsed.fragment):
            raise ValueError("HTTPS origin must be an exact scheme and authority")
        default_port = 443 if parsed.scheme == "https" else 80
        host = parsed.hostname.lower()
        if ":" in host:
            host = "[" + host + "]"
        authority = host if port in (None, default_port) else host + ":" + str(port)
        normalized = parsed.scheme + "://" + authority
        if normalized not in result:
            result.append(normalized)
    return result


def _validate_listener_config(config):
    wss = config.get("wss") or {}
    https = config.get("https") or {}
    if not wss.get("enabled", True) or not https.get("enabled", True):
        raise ValueError("WSS and HTTPS listeners must both be enabled")
    wss_port = int(wss.get("port", 8443))
    https_port = int(https.get("port", 8444))
    if not 1 <= wss_port <= 65535 or not 1 <= https_port <= 65535:
        raise ValueError("listener port is outside its allowed range")
    if wss_port == https_port and str(wss.get("host")) == str(https.get("host")):
        raise ValueError("WSS and HTTPS listeners must not share one socket")
    if https.get("websocket_enabled", False):
        raise ValueError("Uvicorn WebSocket support must remain disabled")
    if not https.get("tls_enabled", True) and not https.get("allow_insecure_http", False):
        raise ValueError("HTTPS TLS may only be disabled in explicit test mode")
    wss_tls = wss.get("tls") or {}
    if not wss_tls.get("certfile") or not wss_tls.get("keyfile"):
        raise ValueError("WSS TLS certfile and keyfile are required")
    if not https.get("cert_file") or not https.get("key_file"):
        raise ValueError("HTTPS TLS certfile and keyfile are required")
    if tuple(wss.get("origin_allowlist") or ()) != tuple(
            https.get("origin_allowlist") or ()):
        raise ValueError("WSS and HTTPS must share one exact Origin policy")
    if str(wss.get("session_cookie") or "") != str(
            https.get("session_cookie") or ""):
        raise ValueError("WSS and HTTPS must share one session cookie name")
    return config


def load_gateway_config(path):
    """Load base config and apply only documented environment controls."""
    with open(path, "r", encoding="utf-8") as handle:
        base = json.load(handle)
    origins = _origin_allowlist(
        base.get("wss", {}).get("origin_allowlist")
        or base.get("https", {}).get("origin_allowlist"))
    overlay = {
        "site_id": os.environ.get("AUTOMATION_SITE_ID", base.get("site_id")),
        "instance_id": os.environ.get("PROXY_INSTANCE_ID", base.get("instance_id")),
        "master_instance_id": os.environ.get(
            "MASTER_INSTANCE_ID", base.get("master_instance_id")),
        "authority_mode": os.environ.get(
            "PROXY_CONFIG_AUTHORITY",
            base.get("authority_mode", "node_local")),
        "mqtt": {
            "host": os.environ.get("MQTT_HOST", base.get("mqtt", {}).get("host")),
            "port": int(os.environ.get("MQTT_PORT", base.get("mqtt", {}).get("port", 8883))),
            "required": _as_bool(
                os.environ.get("MQTT_REQUIRED"), base.get("mqtt", {}).get("required", True)),
            "allow_insecure_transport": _as_bool(
                os.environ.get("MQTT_ALLOW_INSECURE_TRANSPORT"),
                base.get("mqtt", {}).get("allow_insecure_transport", False)),
            "tls": {
                "enabled": _as_bool(
                    os.environ.get("MQTT_TLS_ENABLED"),
                    base.get("mqtt", {}).get("tls", {}).get("enabled", True)),
            },
        },
        "wss": {
            "host": os.environ.get(
                "PROXY_WSS_HOST", base.get("wss", {}).get("host", "0.0.0.0")),
            "port": int(os.environ.get(
                "PROXY_WSS_PORT", base.get("wss", {}).get("port", 8443))),
            "origin_allowlist": origins,
            "tls": {
                "certfile": os.environ.get(
                    "PROXY_WSS_CERT_FILE",
                    base.get("wss", {}).get("tls", {}).get("certfile")),
                "keyfile": os.environ.get(
                    "PROXY_WSS_KEY_FILE",
                    base.get("wss", {}).get("tls", {}).get("keyfile")),
            },
        },
        "https": {
            "host": os.environ.get("PROXY_HTTPS_HOST", base.get("https", {}).get("host")),
            "port": int(os.environ.get(
                "PROXY_HTTPS_PORT", base.get("https", {}).get("port", 8444))),
            "origin_allowlist": origins,
            "tls_enabled": _as_bool(
                os.environ.get("PROXY_TLS_ENABLED"), base.get("https", {}).get("tls_enabled", True)),
            "cert_file": os.environ.get(
                "PROXY_TLS_CERT_FILE", base.get("https", {}).get("cert_file")),
            "key_file": os.environ.get(
                "PROXY_TLS_KEY_FILE", base.get("https", {}).get("key_file")),
        },
    }
    config = _deep_merge(base, overlay)
    config["authority_mode"] = validate_authority_mode(
        config["authority_mode"])
    config["site_id"] = normalize_token(config["site_id"], "site_id")
    config["instance_id"] = normalize_token(config["instance_id"], "instance_id")
    config["master_instance_id"] = normalize_token(
        config["master_instance_id"], "master_instance_id")
    return _validate_listener_config(config)


def configure_logging(options):
    """Configure secret-free stdout logging."""
    level = getattr(logging, str(options.get("level") or "INFO").upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(threadName)s | %(message)s")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for current in tuple(root.handlers):
        root.removeHandler(current)
        current.close()
    root.setLevel(level)
    root.addHandler(handler)
    return logging.getLogger("proxy_gateway")

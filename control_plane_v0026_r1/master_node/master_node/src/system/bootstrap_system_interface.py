# -*- coding: utf-8 -*-

# master_node/src/system/bootstrap_system_interface.py

"""Master configuration loading and secret-free logging bootstrap."""

import copy
import json
import logging
import logging.handlers
import os
import sys
import time

from src.library._generic_system_contract_library.protocol_contracts import normalize_token
from src.library._generic_system_contract_library.authority_flow_contracts import validate_authority_mode


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


def _authority_term_floor(base):
    configured = int(os.environ.get(
        "MASTER_AUTHORITY_TERM_FLOOR",
        base.get("authority", {}).get("term_floor", 0),
    ))
    if configured > 0:
        return configured
    return time.time_ns()


def _normalize_worker_node_ids(values):
    """Normalize one explicit administrative Worker allowlist."""
    if isinstance(values, (str, bytes, bytearray)):
        raise ValueError("allowed_worker_node_ids must be an array")
    normalized = []
    seen = set()
    for value in values or ():
        node_id = normalize_token(value, "allowed_worker_node_id")
        if node_id in seen:
            raise ValueError("allowed_worker_node_ids contains duplicates")
        seen.add(node_id)
        normalized.append(node_id)
    return sorted(normalized)


def load_master_config(path, **kwargs):
    """Load JSON base config and apply a small explicit environment overlay."""
    with open(path, "r", encoding="utf-8") as handle:
        base = json.load(handle)
    overlay = {
        "site_id": os.environ.get("AUTOMATION_SITE_ID", base.get("site_id")),
        "instance_id": os.environ.get("MASTER_INSTANCE_ID", base.get("instance_id")),
        "release_profile": os.environ.get(
            "MASTER_RELEASE_PROFILE", base.get("release_profile", "lab_v2")),
        "authority_mode": os.environ.get(
            "MASTER_CONFIG_AUTHORITY", base.get("authority_mode", "node_local")),
        "legacy_coordinator_node_id": os.environ.get(
            "LEGACY_COORDINATOR_NODE_ID", base.get("legacy_coordinator_node_id")),
        "leader_eligible": _as_bool(
            os.environ.get("MASTER_LEADER_ELIGIBLE"), base.get("leader_eligible", False)),
        "authority": {
            "term_floor": _authority_term_floor(base),
        },
        "worker_compatibility": {
            "auto_admit_broker_bound_workers": _as_bool(
                os.environ.get("MASTER_AUTO_ADMIT_BROKER_BOUND_WORKERS"),
                base.get("worker_compatibility", {}).get(
                    "auto_admit_broker_bound_workers", False)),
            "identity_proof_max_age_s": float(os.environ.get(
                "MASTER_IDENTITY_PROOF_MAX_AGE_S",
                base.get("worker_compatibility", {}).get(
                    "identity_proof_max_age_s", 45.0))),
        },
        "initial_config_path": os.environ.get(
            "MASTER_INITIAL_CONFIG_PATH", base.get("initial_config_path")),
        "mqtt": {
            "host": os.environ.get("MQTT_HOST", base.get("mqtt", {}).get("host")),
            "port": int(os.environ.get("MQTT_PORT", base.get("mqtt", {}).get("port", 8883))),
            "required": _as_bool(
                os.environ.get("MQTT_REQUIRED"), base.get("mqtt", {}).get("required", True)),
            "max_packet_size": int(os.environ.get(
                "MASTER_MQTT_MAX_PACKET_SIZE",
                base.get("mqtt", {}).get("max_packet_size", 4 * 1024 * 1024))),
            "allow_insecure_transport": _as_bool(
                os.environ.get("MQTT_ALLOW_INSECURE_TRANSPORT"),
                base.get("mqtt", {}).get("allow_insecure_transport", False)),
            "tls": {
                "enabled": _as_bool(
                    os.environ.get("MQTT_TLS_ENABLED"),
                    base.get("mqtt", {}).get("tls", {}).get("enabled", True)),
            },
        },
        "metrics": {
            "host": os.environ.get("MASTER_METRICS_HOST", base.get("metrics", {}).get("host")),
            "port": int(os.environ.get("MASTER_METRICS_PORT", base.get("metrics", {}).get("port", 9090))),
        },
    }
    result = _deep_merge(base, overlay)
    result["authority_mode"] = validate_authority_mode(result["authority_mode"])
    result["site_id"] = normalize_token(result["site_id"], "site_id")
    result["instance_id"] = normalize_token(result["instance_id"], "instance_id")
    result["legacy_coordinator_node_id"] = normalize_token(
        result["legacy_coordinator_node_id"], "legacy_coordinator_node_id")
    worker_compatibility = result.setdefault("worker_compatibility", {})
    worker_compatibility["allowed_worker_node_ids"] = _normalize_worker_node_ids(
        worker_compatibility.get("allowed_worker_node_ids", ()))
    if not os.path.isabs(result["initial_config_path"]):
        result["initial_config_path"] = os.path.abspath(os.path.join(
            kwargs.get("project_root") or os.getcwd(), result["initial_config_path"]))
    return result


def configure_logging(options):
    """Configure bounded stdout logging with no payload or secret logging."""
    level = getattr(logging, str(options.get("level") or "INFO").upper(), logging.INFO)
    formatter = logging.Formatter(
        "%(asctime)s | %(levelname)-8s | %(name)s | %(threadName)s | %(message)s")
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(formatter)
    root = logging.getLogger()
    for existing in tuple(root.handlers):
        root.removeHandler(existing)
        existing.close()
    root.setLevel(level)
    root.addHandler(handler)
    return logging.getLogger("master_node")

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Deterministic administration layer above the legacy rollout backend.

The module deliberately uses only the Python standard library and a functional
style.  It does not replace component deployment scripts.  It creates an
immutable plan, fences one environment and all selected targets, records every
transition and invokes either the legacy full rollout once or explicitly
registered component adapters.
"""

import argparse
import copy
import datetime
import hashlib
import json
import os
import re
import secrets
import shutil
import socket
import string
import subprocess
import sys
import tempfile
import time


SCHEMA_VERSION = 1
COMPONENT_ORDER = ("database", "mqtt", "master", "proxy", "mcp_controller_server", "worker")
EXECUTION_MODES = ("legacy_full_once", "component_adapters")
OPERATION_NAMES = ("status", "test", "restart")
ARCHIVABLE_CHECKPOINT_STATUSES = ("complete", "rolled_back", "failed_rolled_back")
ALLOWED_ROLES = ("control_plane", "worker")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
PLACEHOLDER_PATTERN = re.compile(r"^__[A-Z0-9_]+__$")
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
SENSITIVE_KEY_PATTERN = re.compile(
    r"password|passphrase|private[_-]?key|api[_-]?key|authorization|secret|credential",
    re.IGNORECASE,
)
SENSITIVE_TEXT_PATTERN = re.compile(
    r"(?i)\b(password|passphrase|private[_-]?key|api[_-]?key|authorization|secret|credential)"
    r"([\"']?\s*[:=]\s*[\"']?)([^\s,;\"'}]+)"
)
URI_CREDENTIAL_PATTERN = re.compile(
    r"(?i)\b([a-z][a-z0-9+.-]*://)([^/@\s:]+):([^/@\s]+)@"
)
SAFE_DIAGNOSTIC_KEYS = {
    "fencing_token", "fencing_tokens", "idempotency_key", "owner_id",
    "plan_id", "sha256", "expected_sha256", "observed_sha256",
}
ACTION_TOKENS = {
    "release_root", "legacy_backend", "worker_archive", "state_dir", "evidence_dir",
    "environment", "action", "component", "target_id", "host", "node_id",
    "plan_id", "step_id", "idempotency_key", "fencing_token", "target_options",
}
TARGET_ADAPTER_KEYS = {
    "deployment_profile", "transport", "worker_path", "site_id", "profile",
    "pki_stage_path", "pki_source_path", "worker_log_path", "config_drift_policy",
    "mqtt_bind_address", "mqtt_test_host",
    "mqtt_test_broker_ip", "mcp_bind_address", "mcp_http_port",
    "mcp_source_instance_id", "mcp_auth_source", "mcp_mqtt_ca_source",
    "mcp_mqtt_cert_source", "mcp_mqtt_key_source",
}


def utc_now():
    """Return a stable UTC timestamp representation for evidence only."""
    return datetime.datetime.now(datetime.timezone.utc).isoformat().replace("+00:00", "Z")


def canonical_text(value):
    """Serialize a value using the plan's canonical JSON contract."""
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"),
    )


def canonical_bytes(value):
    return canonical_text(value).encode("utf-8")


def sha256_bytes(value):
    return hashlib.sha256(value).hexdigest()


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            chunk = handle.read(1024 * 1024)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def ensure_directory(path):
    os.makedirs(path, exist_ok=True)


def _fsync_directory(path):
    if os.name == "nt":
        return
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def atomic_write_json(path, value):
    """Atomically replace one JSON file and fsync its containing directory."""
    directory = os.path.dirname(os.path.abspath(path))
    ensure_directory(directory)
    descriptor, temporary = tempfile.mkstemp(prefix=".admin-", suffix=".tmp", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(canonical_text(value))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        _fsync_directory(directory)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def read_json(path):
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def require(condition, message):
    if not condition:
        raise ValueError(message)


def is_identifier(value):
    return isinstance(value, str) and bool(IDENTIFIER_PATTERN.fullmatch(value))


def is_placeholder(value):
    return isinstance(value, str) and bool(PLACEHOLDER_PATTERN.fullmatch(value))


def validate_declared_hash(value, path):
    require(isinstance(value, str), path + " must be a SHA-256 string")
    require(
        bool(HASH_PATTERN.fullmatch(value.lower())) or is_placeholder(value),
        path + " must be 64 lowercase hexadecimal characters or a release placeholder",
    )


def _secret_key(key):
    return str(key) not in SAFE_DIAGNOSTIC_KEYS and bool(SENSITIVE_KEY_PATTERN.search(str(key)))


def reject_secret_material(value, path="document"):
    """Reject likely inline credentials from inventory/registry/manifest input."""
    if isinstance(value, dict):
        for key, child in value.items():
            require(not _secret_key(key), path + "." + str(key) + " may contain secret material")
            reject_secret_material(child, path + "." + str(key))
    elif isinstance(value, list):
        for index, child in enumerate(value):
            reject_secret_material(child, path + "[" + str(index) + "]")
    elif isinstance(value, str):
        require(not SENSITIVE_TEXT_PATTERN.search(value), path + " may contain inline secret material")
        require(not URI_CREDENTIAL_PATTERN.search(value), path + " may contain URI credentials")
        require("PRIVATE KEY-----" not in value.upper(), path + " may contain a private key")


def _redact_match(match):
    return match.group(1) + match.group(2) + "<redacted>"


def _redact_uri_match(match):
    return match.group(1) + "<redacted>@"


def redact_text(value):
    text = str(value)
    if "PRIVATE KEY-----" in text.upper():
        return "<redacted-private-key>"
    text = SENSITIVE_TEXT_PATTERN.sub(_redact_match, text)
    return URI_CREDENTIAL_PATTERN.sub(_redact_uri_match, text)


def redact_value(value):
    if isinstance(value, dict):
        result = {}
        for key, child in value.items():
            if _secret_key(key):
                result[key] = "<redacted>"
            else:
                result[key] = redact_value(child)
        return result
    if isinstance(value, list):
        return [redact_value(child) for child in value]
    if isinstance(value, tuple):
        return [redact_value(child) for child in value]
    if isinstance(value, str):
        return redact_text(value)
    return value


def append_audit(state_dir, plan_id, event_name, details):
    """Append one redacted, independently parseable JSON audit record."""
    ensure_directory(state_dir)
    path = os.path.join(state_dir, "audit.jsonl")
    record = {
        "timestamp": utc_now(),
        "plan_id": plan_id,
        "event": str(event_name),
        "details": redact_value(details or {}),
    }
    flags = os.O_APPEND | os.O_CREAT | os.O_WRONLY
    if hasattr(os, "O_BINARY"):
        flags |= os.O_BINARY
    descriptor = os.open(path, flags, 0o600)
    try:
        payload = (canonical_text(record) + "\n").encode("utf-8")
        os.write(descriptor, payload)
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def absolute_path(base_dir, supplied):
    require(isinstance(supplied, str) and supplied.strip(), "path must be a non-empty string")
    path = supplied if os.path.isabs(supplied) else os.path.join(base_dir, supplied)
    return os.path.abspath(os.path.normpath(path))


def path_is_within(path, root):
    try:
        return os.path.commonpath((os.path.abspath(path), os.path.abspath(root))) == os.path.abspath(root)
    except ValueError:
        return False


def _component_sort_key(name):
    try:
        return (COMPONENT_ORDER.index(name), name)
    except ValueError:
        return (len(COMPONENT_ORDER), name)


def _target_sort_key(target):
    role_rank = 1 if target.get("role") == "worker" else 0
    return (role_rank, str(target.get("node_id") or ""), str(target.get("target_id") or ""))


def _config_sort_key(item):
    return str(item.get("config_id") or "")


def _artifact_sort_key(item):
    return str(item.get("artifact_id") or "")


def _manifest_entry_sort_key(item):
    return item["path"]


def normalize_inventory(value):
    result = copy.deepcopy(value)
    result["targets"] = sorted(result.get("targets") or [], key=_target_sort_key)
    for target in result["targets"]:
        target["components"] = sorted(target.get("components") or [], key=_component_sort_key)
    result["config_files"] = sorted(result.get("config_files") or [], key=_config_sort_key)
    return result


def normalize_registry(value):
    result = copy.deepcopy(value)
    adapter_action = result.get("adapter_action") or {}
    for name in sorted((result.get("components") or {}).keys()):
        component = result["components"][name]
        component["depends_on"] = sorted(component.get("depends_on") or [], key=_component_sort_key)
        for action_name, action in (component.get("actions") or {}).items():
            resolved = copy.deepcopy(adapter_action)
            resolved.update(action)
            component["actions"][action_name] = resolved
    return result


def normalize_manifest(value):
    result = copy.deepcopy(value)
    result["artifacts"] = sorted(result.get("artifacts") or [], key=_artifact_sort_key)
    result["required_tests"] = sorted(result.get("required_tests") or [])
    return result


def normalize_document(kind, value):
    if kind == "inventory":
        return normalize_inventory(value)
    if kind == "registry":
        return normalize_registry(value)
    if kind == "manifest":
        return normalize_manifest(value)
    raise ValueError("unknown source document kind: " + str(kind))


def _validate_action(action, path):
    require(isinstance(action, dict), path + " must be an object")
    command = action.get("command")
    require(isinstance(command, list) and command, path + ".command must be a non-empty list")
    formatter = string.Formatter()
    for index, argument in enumerate(command):
        require(isinstance(argument, str) and argument, path + ".command arguments must be strings")
        for _literal, field, format_spec, conversion in formatter.parse(argument):
            if field is None:
                continue
            require(field in ACTION_TOKENS, path + " uses unsupported token {" + str(field) + "}")
            require(not format_spec and conversion is None, path + " command tokens cannot use formatting")
    timeout_s = action.get("timeout_s", 300)
    require(isinstance(timeout_s, int) and 1 <= timeout_s <= 86400, path + ".timeout_s is invalid")
    require(isinstance(action.get("idempotent", True), bool), path + ".idempotent must be boolean")


def validate_inventory(value):
    require(isinstance(value, dict), "inventory must be an object")
    reject_secret_material(value, "inventory")
    require(value.get("schema_version") == SCHEMA_VERSION, "inventory.schema_version must be 1")
    require(is_identifier(value.get("environment")), "inventory.environment is invalid")
    require(isinstance(value.get("release_root"), str), "inventory.release_root is required")
    require(isinstance(value.get("state_dir"), str), "inventory.state_dir is required")
    targets = value.get("targets")
    require(isinstance(targets, list) and targets, "inventory.targets must be a non-empty list")
    seen = set()
    seen_node_ids = set()
    covered_components = set()
    for index, target in enumerate(targets):
        path = "inventory.targets[" + str(index) + "]"
        require(isinstance(target, dict), path + " must be an object")
        target_id = target.get("target_id")
        require(is_identifier(target_id), path + ".target_id is invalid")
        require(target_id not in seen, "duplicate target_id: " + str(target_id))
        seen.add(target_id)
        require(target.get("role") in ALLOWED_ROLES, path + ".role is invalid")
        require(isinstance(target.get("host"), str) and target.get("host"), path + ".host is required")
        adapter = target.get("adapter", {})
        require(isinstance(adapter, dict), path + ".adapter must be an object")
        require(not (set(adapter) - TARGET_ADAPTER_KEYS),
                path + ".adapter contains unsupported keys")
        require(all(isinstance(item, str) and item for item in adapter.values()),
                path + ".adapter values must be non-empty strings")
        require(adapter.get("transport", "local") in ("local", "ssh"),
                path + ".adapter.transport must be local or ssh")
        require(adapter.get("deployment_profile", "test") in ("test", "production"),
                path + ".adapter.deployment_profile must be test or production")
        require(adapter.get("config_drift_policy", "preserve") in ("preserve", "strict"),
                path + ".adapter.config_drift_policy must be preserve or strict")
        components = target.get("components")
        require(isinstance(components, list) and components, path + ".components is required")
        require(len(set(components)) == len(components), path + ".components contains duplicates")
        require(all(component in COMPONENT_ORDER for component in components), path + " has unknown component")
        covered_components.update(components)
        if target.get("role") == "worker":
            require(is_identifier(target.get("node_id")), path + ".node_id is required for workers")
            require(target.get("node_id") not in seen_node_ids, "duplicate worker node_id: " + target.get("node_id"))
            seen_node_ids.add(target.get("node_id"))
            require(components == ["worker"] or set(components) == {"worker"}, path + " worker may only own worker")
    require(covered_components == set(COMPONENT_ORDER), "inventory must assign every required component")
    configs = value.get("config_files")
    require(isinstance(configs, list), "inventory.config_files must be a list")
    seen_configs = set()
    for index, config in enumerate(configs):
        path = "inventory.config_files[" + str(index) + "]"
        require(isinstance(config, dict), path + " must be an object")
        config_id = config.get("config_id")
        require(is_identifier(config_id), path + ".config_id is invalid")
        require(config_id not in seen_configs, "duplicate config_id: " + str(config_id))
        seen_configs.add(config_id)
        require(isinstance(config.get("path"), str), path + ".path is required")
        validate_declared_hash(config.get("sha256"), path + ".sha256")
    rollout = value.get("rollout")
    require(isinstance(rollout, dict), "inventory.rollout must be an object")
    require(isinstance(rollout.get("worker_canary_count"), int), "worker_canary_count must be integer")
    require(rollout.get("worker_canary_count") >= 1, "worker_canary_count must select at least one worker")
    require(isinstance(rollout.get("worker_batch_size"), int), "worker_batch_size must be integer")
    require(rollout.get("worker_batch_size") >= 1, "worker_batch_size must be positive")
    require(isinstance(rollout.get("lock_ttl_s", 14400), int), "lock_ttl_s must be integer")
    require(60 <= rollout.get("lock_ttl_s", 14400) <= 86400, "lock_ttl_s is outside 60..86400")


def validate_registry(value):
    require(isinstance(value, dict), "component registry must be an object")
    reject_secret_material(value, "component_registry")
    require(value.get("schema_version") == SCHEMA_VERSION, "component_registry.schema_version must be 1")
    require(value.get("execution_mode") in EXECUTION_MODES, "component_registry.execution_mode is invalid")
    adapter_action = value.get("adapter_action")
    if adapter_action is not None:
        _validate_action(adapter_action, "component_registry.adapter_action")
    require(tuple(value.get("required_component_order") or ()) == COMPONENT_ORDER,
            "required_component_order must be database,mqtt,master,proxy,mcp_controller_server,worker")
    components = value.get("components")
    require(isinstance(components, dict) and set(components) == set(COMPONENT_ORDER),
            "component registry must define exactly database,mqtt,master,proxy,mcp_controller_server,worker")
    previous = None
    for name in COMPONENT_ORDER:
        component = components[name]
        path = "component_registry.components." + name
        require(isinstance(component, dict), path + " must be an object")
        dependencies = component.get("depends_on")
        require(isinstance(dependencies, list), path + ".depends_on must be a list")
        if previous is None:
            require(not dependencies, path + " must not have dependencies")
        else:
            require(previous in dependencies, path + " must depend on " + previous)
        require(component.get("target_role") in ALLOWED_ROLES, path + ".target_role is invalid")
        if name == "worker":
            require(component.get("target_role") == "worker", path + " must target workers")
        else:
            require(component.get("target_role") == "control_plane", path + " must target control_plane")
        require(isinstance(component.get("reversible", False), bool), path + ".reversible must be boolean")
        if value.get("execution_mode") == "component_adapters":
            expected_strategy = "canary_batches" if name == "worker" else "serial"
            require(component.get("rollout_strategy") == expected_strategy,
                    path + ".rollout_strategy must be " + expected_strategy)
            actions = component.get("actions")
            require(isinstance(actions, dict), path + ".actions is required")
            for action_name in ("preflight", "apply", "ready", "status", "test", "restart"):
                require(action_name in actions, path + ".actions." + action_name + " is required")
                resolved = copy.deepcopy(adapter_action or {})
                resolved.update(actions[action_name])
                _validate_action(resolved, path + ".actions." + action_name)
            for action_name, action in actions.items():
                resolved = copy.deepcopy(adapter_action or {})
                resolved.update(action)
                _validate_action(resolved, path + ".actions." + action_name)
            if component.get("reversible"):
                require("backup" in actions and "rollback" in actions,
                        path + " reversible components require backup and rollback actions")
                backup = copy.deepcopy(adapter_action or {})
                backup.update(actions["backup"])
                rollback = copy.deepcopy(adapter_action or {})
                rollback.update(actions["rollback"])
                _validate_action(backup, path + ":actions.backup")
                _validate_action(rollback, path + ":actions.rollback")
        previous = name
    operations = value.get("operations")
    require(isinstance(operations, dict), "component_registry.operations is required")
    for action_name in ("status", "test"):
        require(action_name in operations, "component_registry.operations." + action_name + " is required")
        _validate_action(operations[action_name], "component_registry.operations." + action_name)
    if value.get("execution_mode") == "component_adapters":
        require(isinstance(value.get("legacy_full"), dict),
                "component adapter registry must retain legacy_full fallback")
    if value.get("execution_mode") == "legacy_full_once" or "legacy_full" in value:
        legacy = value.get("legacy_full")
        require(isinstance(legacy, dict), "component_registry.legacy_full is required")
        for action_name in ("apply", "ready", "test"):
            require(action_name in legacy, "component_registry.legacy_full." + action_name + " is required")
            _validate_action(legacy[action_name], "component_registry.legacy_full." + action_name)
        occurrences = sum(argument.count("{legacy_backend}") for argument in legacy["apply"]["command"])
        require(occurrences == 1, "legacy apply command must reference {legacy_backend} exactly once")
        command = legacy["apply"]["command"]
        require(command.count("--release") == 1 and command.count("{release_root}") == 1,
                "legacy apply command must pin --release {release_root}")
        require(command.count("--worker-archive") == 1
                and command.count("{worker_archive}") == 1,
                "legacy apply command must pin --worker-archive {worker_archive}")
        release_index = command.index("--release")
        worker_index = command.index("--worker-archive")
        require(release_index + 1 < len(command)
                and command[release_index + 1] == "{release_root}",
                "legacy --release must bind the immutable release root")
        require(worker_index + 1 < len(command)
                and command[worker_index + 1] == "{worker_archive}",
                "legacy --worker-archive must bind the planned Worker artifact")


def validate_manifest(value):
    require(isinstance(value, dict), "release manifest must be an object")
    reject_secret_material(value, "release_manifest")
    require(value.get("schema_version") == SCHEMA_VERSION, "release_manifest.schema_version must be 1")
    release = value.get("release")
    require(isinstance(release, dict), "release_manifest.release is required")
    require(isinstance(release.get("name"), str) and release.get("name"), "release.name is required")
    require(isinstance(release.get("version"), str) and release.get("version"), "release.version is required")
    worker = value.get("worker")
    require(isinstance(worker, dict), "release_manifest.worker is required")
    require(isinstance(worker.get("version"), str) and worker.get("version"), "worker.version is required")
    require(is_identifier(worker.get("artifact_id")),
            "worker.artifact_id must bind the exact Worker archive")
    compatibility = value.get("compatibility")
    require(isinstance(compatibility, dict), "release_manifest.compatibility is required")
    require(compatibility.get("node_storage") == [9], "node_storage must declare canonical V9 only")
    require(compatibility.get("legacy_import") == [5], "legacy_import must declare V5 only")
    require(compatibility.get("cross_storage") == [8], "cross_storage must declare separate V8 only")
    artifacts = value.get("artifacts")
    require(isinstance(artifacts, list) and artifacts, "release_manifest.artifacts is required")
    seen = set()
    for index, artifact in enumerate(artifacts):
        path = "release_manifest.artifacts[" + str(index) + "]"
        require(isinstance(artifact, dict), path + " must be an object")
        artifact_id = artifact.get("artifact_id")
        require(is_identifier(artifact_id), path + ".artifact_id is invalid")
        require(artifact_id not in seen, "duplicate artifact_id: " + str(artifact_id))
        seen.add(artifact_id)
        require(isinstance(artifact.get("path"), str), path + ".path is required")
        validate_declared_hash(artifact.get("sha256"), path + ".sha256")
        if "manifest_sha256" in artifact:
            validate_declared_hash(
                artifact.get("manifest_sha256"), path + ".manifest_sha256",
            )
    require(worker["artifact_id"] in seen,
            "worker.artifact_id does not reference a declared artifact")
    backend = value.get("legacy_backend")
    require(isinstance(backend, dict), "release_manifest.legacy_backend is required")
    require(isinstance(backend.get("path"), str), "legacy_backend.path is required")
    validate_declared_hash(backend.get("sha256"), "legacy_backend.sha256")
    require(tuple(backend.get("component_order") or ()) == COMPONENT_ORDER,
            "legacy backend must declare the binding component order")
    require(isinstance(value.get("required_tests"), list) and value.get("required_tests"),
            "release_manifest.required_tests is required")
    require(all(is_identifier(item) for item in value.get("required_tests")),
            "required_tests entries must be identifiers")


def _capture_file(item_id, supplied_path, expected_hash, base_dir, allow_placeholders):
    path = absolute_path(base_dir, supplied_path)
    placeholder = is_placeholder(expected_hash)
    exists = os.path.isfile(path)
    require(exists or (allow_placeholders and placeholder), "required file is missing: " + path)
    observed = sha256_file(path) if exists else None
    if not placeholder:
        require(observed == expected_hash.lower(), "SHA-256 mismatch for " + path)
    elif not allow_placeholders:
        raise ValueError("unresolved SHA-256 placeholder for " + path)
    return {
        "item_id": item_id,
        "path": path,
        "expected_sha256": expected_hash,
        "observed_sha256": observed,
        "placeholder": placeholder,
    }


def _worker_groups(targets, canary_count, batch_size):
    workers = [target for target in targets if target.get("role") == "worker"]
    workers = sorted(workers, key=_target_sort_key)
    if not workers:
        return [], []
    require(canary_count <= len(workers), "worker_canary_count exceeds worker target count")
    canary = workers[:canary_count]
    remaining = workers[canary_count:]
    batches = []
    index = 0
    while index < len(remaining):
        batches.append(remaining[index:index + batch_size])
        index += batch_size
    return canary, batches


def _action_copy(action):
    return {
        "command": list(action["command"]),
        "timeout_s": int(action.get("timeout_s", 300)),
        "idempotent": bool(action.get("idempotent", True)),
    }


def _target_view(target):
    return {
        "target_id": target.get("target_id"),
        "role": target.get("role"),
        "host": target.get("host"),
        "node_id": target.get("node_id"),
        "adapter": copy.deepcopy(target.get("adapter") or {}),
    }


def _new_step(order, phase, component, action_name, target, action, **kwargs):
    target_id = target.get("target_id") if target else "environment"
    step_id = "{:04d}:{}:{}:{}".format(order, phase, component, target_id)
    result = {
        "order": order,
        "step_id": step_id,
        "phase": phase,
        "component": component,
        "action": action_name,
        "target": _target_view(target) if target else {
            "target_id": "environment", "role": "environment", "host": None, "node_id": None,
        },
        "command": list(action["command"]),
        "timeout_s": int(action.get("timeout_s", 300)),
        "idempotent": bool(action.get("idempotent", True)),
        "reversible": bool(kwargs.get("reversible", False)),
        "backup_step_id": kwargs.get("backup_step_id"),
        "rollback_action": copy.deepcopy(kwargs.get("rollback_action")),
        "rollback_ready_action": copy.deepcopy(kwargs.get("rollback_ready_action")),
        "rollback_test_action": copy.deepcopy(kwargs.get("rollback_test_action")),
        "rollout_group": kwargs.get("rollout_group"),
    }
    return result


def _targets_for_component(targets, component_name, target_role):
    selected = [
        target for target in targets
        if target.get("role") == target_role and component_name in target.get("components", [])
    ]
    return sorted(selected, key=_target_sort_key)


def _ordered_worker_targets(canary, batches):
    result = list(canary)
    for batch in batches:
        result.extend(batch)
    return result


def _component_steps(registry, targets, canary, batches, component_order):
    steps = []
    order = 1
    canary_ids = {target["target_id"] for target in canary}
    batch_by_id = {}
    for batch_index, batch in enumerate(batches, 1):
        for target in batch:
            batch_by_id[target["target_id"]] = "batch-{:03d}".format(batch_index)
    for component_name in component_order:
        component = registry["components"][component_name]
        selected = _targets_for_component(targets, component_name, component["target_role"])
        require(selected, "no target registered for component " + component_name)
        if component_name == "worker":
            selected = _ordered_worker_targets(canary, batches)
        for target in selected:
            rollout_group = None
            if component_name == "worker":
                rollout_group = "canary" if target["target_id"] in canary_ids \
                    else batch_by_id.get(target["target_id"])
            actions = component["actions"]
            steps.append(_new_step(
                order, "preflight", component_name, "preflight", target,
                actions["preflight"], rollout_group=rollout_group,
            ))
            order += 1
            backup_id = None
            if "backup" in actions:
                backup_step = _new_step(
                    order, "backup", component_name, "backup", target, actions["backup"],
                    rollout_group=rollout_group,
                )
                backup_id = backup_step["step_id"]
                steps.append(backup_step)
                order += 1
            apply_step = _new_step(
                order, "apply", component_name, "apply", target, actions["apply"],
                reversible=component.get("reversible", False), backup_step_id=backup_id,
                rollback_action=_action_copy(actions["rollback"]) if "rollback" in actions else None,
                rollback_ready_action=_action_copy(actions["ready"]), rollout_group=rollout_group,
                rollback_test_action=_action_copy(actions["test"]),
            )
            steps.append(apply_step)
            order += 1
            steps.append(_new_step(
                order, "readiness", component_name, "ready", target, actions["ready"],
                rollout_group=rollout_group,
            ))
            order += 1
            steps.append(_new_step(
                order, "test", component_name, "test", target, actions["test"],
                rollout_group=rollout_group,
            ))
            order += 1
    return steps


def _component_operation_steps(registry, targets, canary, batches, component_order):
    """Build deterministic live operations against the same selected targets."""
    result = {name: [] for name in OPERATION_NAMES}
    worker_targets = _ordered_worker_targets(canary, batches)
    for operation_name in OPERATION_NAMES:
        order = 1
        for component_name in component_order:
            component = registry["components"][component_name]
            selected = _targets_for_component(
                targets, component_name, component["target_role"],
            )
            if component_name == "worker":
                selected = worker_targets
            for target in selected:
                action = component["actions"][operation_name]
                step = _new_step(
                    order, "operation-" + operation_name, component_name,
                    operation_name, target, action,
                )
                step["step_id"] = "operation:{}:{:04d}:{}:{}".format(
                    operation_name, order, component_name, target["target_id"],
                )
                result[operation_name].append(step)
                order += 1
    return result


def _legacy_steps(registry, component_order, canary, batches):
    legacy = registry["legacy_full"]
    environment_target = None
    apply_step = _new_step(
        1, "apply", "legacy_full", "apply", environment_target, legacy["apply"],
        reversible=False,
    )
    apply_step["orchestrated_components"] = list(component_order)
    apply_step["worker_canary"] = [target["target_id"] for target in canary]
    apply_step["worker_batches"] = [
        [target["target_id"] for target in batch] for batch in batches
    ]
    return [
        apply_step,
        _new_step(2, "readiness", "legacy_full", "ready", environment_target, legacy["ready"]),
        _new_step(3, "test", "legacy_full", "test", environment_target, legacy["test"]),
    ]


def _unsigned_plan(plan):
    result = copy.deepcopy(plan)
    result.pop("plan_id", None)
    for step in result.get("steps") or []:
        step.pop("idempotency_key", None)
    for action in (result.get("operations") or {}).values():
        action.pop("idempotency_key", None)
    for steps in (result.get("operation_steps") or {}).values():
        for step in steps:
            step.pop("idempotency_key", None)
    return result


def plan_identifier(plan):
    return sha256_bytes(canonical_bytes(_unsigned_plan(plan)))


def _attach_idempotency_keys(plan):
    plan_id = plan["plan_id"]
    for step in plan.get("steps") or []:
        step["idempotency_key"] = plan_id + ":" + step["step_id"]
    for name, action in (plan.get("operations") or {}).items():
        action["idempotency_key"] = plan_id + ":operation:" + name
    for name, steps in (plan.get("operation_steps") or {}).items():
        for step in steps:
            step["idempotency_key"] = plan_id + ":" + step["step_id"]


def _replace_static_tokens(argument, release_root, legacy_backend, state_dir):
    result = str(argument)
    result = result.replace("{release_root}", release_root)
    result = result.replace("{legacy_backend}", legacy_backend)
    result = result.replace("{state_dir}", state_dir)
    result = result.replace("{evidence_dir}", os.path.join(state_dir, "evidence"))
    return result


def _tracked_command_files(steps, operations, release_root, legacy_backend, state_dir):
    paths = set()
    action_sets = [step for step in steps]
    action_sets.extend(operations.values())
    for action in action_sets:
        commands = [action.get("command") or []]
        rollback = action.get("rollback_action")
        rollback_ready = action.get("rollback_ready_action")
        rollback_test = action.get("rollback_test_action")
        if rollback:
            commands.append(rollback.get("command") or [])
        if rollback_ready:
            commands.append(rollback_ready.get("command") or [])
        if rollback_test:
            commands.append(rollback_test.get("command") or [])
        for command in commands:
            for argument in command:
                candidate = _replace_static_tokens(argument, release_root, legacy_backend, state_dir)
                if "{" in candidate or "}" in candidate:
                    continue
                path = candidate if os.path.isabs(candidate) else os.path.join(release_root, candidate)
                path = os.path.abspath(os.path.normpath(path))
                if os.path.isfile(path) and path_is_within(path, release_root):
                    paths.add(path)
    return [
        {"path": path, "observed_sha256": sha256_file(path)}
        for path in sorted(paths)
    ]


def _selection_values(values, label):
    if values is None:
        return None
    result = set()
    raw_values = [values] if isinstance(values, str) else list(values)
    for raw in raw_values:
        require(isinstance(raw, str), label + " selections must be strings")
        for value in raw.split(","):
            value = value.strip()
            require(is_identifier(value), "invalid " + label + " selection: " + value)
            result.add(value)
    require(result, label + " selection must not be empty")
    return result


def _select_rollout_scope(inventory, selected_components=None, selected_targets=None):
    requested_components = _selection_values(selected_components, "component")
    requested_targets = _selection_values(selected_targets, "target")
    inventory_targets = inventory["targets"]
    known_targets = {target["target_id"] for target in inventory_targets}
    if requested_targets is not None:
        unknown = sorted(requested_targets - known_targets)
        require(not unknown, "unknown target selection: " + ",".join(unknown))
        candidates = [target for target in inventory_targets
                      if target["target_id"] in requested_targets]
    else:
        candidates = list(inventory_targets)
    if requested_components is not None:
        unknown = sorted(requested_components - set(COMPONENT_ORDER))
        require(not unknown, "unknown component selection: " + ",".join(unknown))
        component_set = requested_components
    else:
        component_set = {
            component for target in candidates for component in target["components"]
        }
    targets = [
        target for target in candidates
        if component_set.intersection(target["components"])
    ]
    require(targets, "rollout selection has no targets")
    if requested_targets is not None:
        selected_target_ids = {target["target_id"] for target in targets}
        unused = sorted(requested_targets - selected_target_ids)
        require(not unused, "selected target owns no selected component: " + ",".join(unused))
    component_order = [name for name in COMPONENT_ORDER if name in component_set]
    for component in component_order:
        require(any(component in target["components"] for target in targets),
                "selected component has no selected target: " + component)
    return component_order, sorted(targets, key=_target_sort_key)


def _read_release_integrity(release_root, verify_files=True):
    """Bind and optionally verify the complete root SHA256SUMS release set."""
    sums_path = os.path.join(release_root, "SHA256SUMS")
    require(os.path.isfile(sums_path) and not os.path.islink(sums_path),
            "release root SHA256SUMS is missing or unsafe")
    entries = []
    seen = set()
    with open(sums_path, "r", encoding="utf-8") as handle:
        for number, line in enumerate(handle, 1):
            line = line.rstrip("\r\n")
            if not line:
                continue
            match = re.fullmatch(r"([0-9a-f]{64})[ \t]+\*?(.+)", line)
            require(match is not None,
                    "malformed root SHA256SUMS line " + str(number))
            declared, relative = match.groups()
            if relative.startswith("./"):
                relative = relative[2:]
            require(relative and not os.path.isabs(relative),
                    "unsafe root SHA256SUMS path: " + relative)
            path = os.path.abspath(os.path.normpath(os.path.join(
                release_root, relative.replace("/", os.sep),
            )))
            require(path_is_within(path, release_root) and path != release_root,
                    "root SHA256SUMS path escapes release: " + relative)
            canonical_relative = os.path.relpath(path, release_root).replace(os.sep, "/")
            require(canonical_relative not in seen,
                    "duplicate root SHA256SUMS path: " + canonical_relative)
            seen.add(canonical_relative)
            if verify_files:
                require(os.path.isfile(path) and not os.path.islink(path),
                        "root SHA256SUMS file is missing or unsafe: " + path)
                require(sha256_file(path) == declared,
                        "root SHA256SUMS mismatch: " + path)
            entries.append({"path": canonical_relative, "sha256": declared})
    require(entries, "root SHA256SUMS is empty")
    entries.sort(key=_manifest_entry_sort_key)
    return {
        "root_sha256sums_path": sums_path,
        "root_sha256sums_sha256": sha256_file(sums_path),
        "full_release_digest": sha256_bytes(canonical_bytes(entries)),
        "entry_count": len(entries),
    }


def build_plan(inventory_path, registry_path, manifest_path, allow_placeholders=False,
               selected_components=None, selected_targets=None,
               worker_canary_count=None, worker_batch_size=None,
               force_legacy_full_once=False):
    """Validate inputs and return a deterministic, self-verifying plan."""
    inventory_path = os.path.abspath(inventory_path)
    registry_path = os.path.abspath(registry_path)
    manifest_path = os.path.abspath(manifest_path)
    inventory = read_json(inventory_path)
    registry = read_json(registry_path)
    manifest = read_json(manifest_path)
    validate_inventory(inventory)
    validate_registry(registry)
    validate_manifest(manifest)
    inventory = normalize_inventory(inventory)
    registry = normalize_registry(registry)
    manifest = normalize_manifest(manifest)
    inventory_base = os.path.dirname(inventory_path)
    release_root = absolute_path(inventory_base, inventory["release_root"])
    state_dir = absolute_path(inventory_base, inventory["state_dir"])
    require(os.path.isdir(release_root), "release_root does not exist: " + release_root)
    require(not path_is_within(state_dir, release_root),
            "state_dir must be outside the immutable release root")
    release_integrity = _read_release_integrity(release_root)
    source_documents = [
        {"kind": "inventory", "path": inventory_path,
         "semantic_sha256": sha256_bytes(canonical_bytes(inventory))},
        {"kind": "registry", "path": registry_path,
         "semantic_sha256": sha256_bytes(canonical_bytes(registry))},
        {"kind": "manifest", "path": manifest_path,
         "semantic_sha256": sha256_bytes(canonical_bytes(manifest))},
    ]
    artifacts = []
    for artifact in manifest["artifacts"]:
        artifacts.append(_capture_file(
            artifact["artifact_id"], artifact["path"], artifact["sha256"],
            release_root, allow_placeholders,
        ))
    configs = []
    for config in inventory["config_files"]:
        configs.append(_capture_file(
            config["config_id"], config["path"], config["sha256"],
            release_root, allow_placeholders,
        ))
    backend = _capture_file(
        "legacy_backend", manifest["legacy_backend"]["path"],
        manifest["legacy_backend"]["sha256"], release_root, allow_placeholders,
    )
    rollout = inventory["rollout"]
    component_order, targets = _select_rollout_scope(
        inventory, selected_components, selected_targets,
    )
    canary_count = rollout["worker_canary_count"] \
        if worker_canary_count is None else worker_canary_count
    batch_size = rollout["worker_batch_size"] \
        if worker_batch_size is None else worker_batch_size
    require(isinstance(canary_count, int) and canary_count >= 1,
            "worker_canary_count override must be positive")
    require(isinstance(batch_size, int) and batch_size >= 1,
            "worker_batch_size override must be positive")
    canary, batches = _worker_groups(
        targets, canary_count, batch_size,
    )
    execution_mode = "legacy_full_once" if force_legacy_full_once \
        else registry["execution_mode"]
    if execution_mode == "legacy_full_once":
        require(component_order == list(COMPONENT_ORDER)
                and len(targets) == len(inventory["targets"]),
                "legacy_full_once cannot execute a partial selection")
        steps = _legacy_steps(registry, COMPONENT_ORDER, canary, batches)
        operation_steps = {}
    else:
        steps = _component_steps(registry, targets, canary, batches, component_order)
        operation_steps = _component_operation_steps(
            registry, targets, canary, batches, component_order,
        )
    operations = {
        name: _action_copy(registry["operations"][name])
        for name in ("status", "test")
    }
    action_timeouts = [int(step.get("timeout_s", 300)) for step in steps]
    action_timeouts.extend(int(action.get("timeout_s", 300)) for action in operations.values())
    action_timeouts.extend(
        int(step.get("timeout_s", 300))
        for operation in operation_steps.values() for step in operation
    )
    for step in steps:
        if step.get("rollback_action"):
            action_timeouts.append(int(step["rollback_action"].get("timeout_s", 300)))
        if step.get("rollback_ready_action"):
            action_timeouts.append(int(step["rollback_ready_action"].get("timeout_s", 300)))
        if step.get("rollback_test_action"):
            action_timeouts.append(int(step["rollback_test_action"].get("timeout_s", 300)))
    require(
        int(rollout.get("lock_ttl_s", 14400)) >= max(action_timeouts) + 60,
        "lock_ttl_s must exceed every adapter timeout by at least 60 seconds",
    )
    tracked_actions = list(steps)
    for operation in operation_steps.values():
        tracked_actions.extend(operation)
    tracked_command_files = _tracked_command_files(
        tracked_actions, operations, release_root, backend["path"], state_dir,
    )
    plan = {
        "schema_version": SCHEMA_VERSION,
        "environment": inventory["environment"],
        "release": copy.deepcopy(manifest["release"]),
        "worker": copy.deepcopy(manifest["worker"]),
        "compatibility": copy.deepcopy(manifest["compatibility"]),
        "execution_mode": execution_mode,
        "release_root": release_root,
        "release_integrity": release_integrity,
        "state_dir": state_dir,
        "lock_ttl_s": int(rollout.get("lock_ttl_s", 14400)),
        "source_documents": source_documents,
        "artifacts": artifacts,
        "configs": configs,
        "legacy_backend": backend,
        "tracked_command_files": tracked_command_files,
        "component_order": list(COMPONENT_ORDER),
        "selected_components": component_order,
        "targets": [_target_view(target) for target in targets],
        "lock_targets": [target["target_id"] for target in targets],
        "worker_rollout": {
            "canary": [target["target_id"] for target in canary],
            "batches": [[target["target_id"] for target in batch] for batch in batches],
            "batch_size": batch_size,
        },
        "required_tests": list(manifest["required_tests"]),
        "steps": steps,
        "operations": operations,
        "operation_steps": operation_steps,
        "has_placeholders": any(item["placeholder"] for item in artifacts + configs + [backend]),
    }
    plan["plan_id"] = plan_identifier(plan)
    _attach_idempotency_keys(plan)
    validate_plan(plan)
    return plan


def validate_plan(plan):
    require(isinstance(plan, dict), "plan must be an object")
    require(plan.get("schema_version") == SCHEMA_VERSION, "plan.schema_version must be 1")
    require(tuple(plan.get("component_order") or ()) == COMPONENT_ORDER, "plan component order is invalid")
    require(plan.get("execution_mode") in EXECUTION_MODES, "plan execution mode is invalid")
    require(plan.get("plan_id") == plan_identifier(plan), "plan_id does not match canonical plan")
    selected_components = plan.get("selected_components")
    require(isinstance(selected_components, list) and selected_components,
            "plan selected_components is missing")
    require(selected_components == [name for name in COMPONENT_ORDER
                                    if name in set(selected_components)],
            "plan selected_components order is invalid")
    integrity = plan.get("release_integrity") or {}
    require(bool(HASH_PATTERN.fullmatch(str(integrity.get("root_sha256sums_sha256", "")))),
            "plan root SHA256SUMS digest is invalid")
    require(bool(HASH_PATTERN.fullmatch(str(integrity.get("full_release_digest", "")))),
            "plan full release digest is invalid")
    require(isinstance(integrity.get("entry_count"), int)
            and integrity["entry_count"] > 0,
            "plan release integrity entry count is invalid")
    worker = plan.get("worker") or {}
    worker_artifact_id = worker.get("artifact_id")
    artifact_ids = [item.get("item_id") for item in plan.get("artifacts") or []]
    require(is_identifier(worker_artifact_id)
            and artifact_ids.count(worker_artifact_id) == 1,
            "plan must bind exactly one Worker archive artifact")
    steps = plan.get("steps")
    require(isinstance(steps, list) and steps, "plan has no steps")
    seen = set()
    previous_order = 0
    legacy_apply_count = 0
    for step in steps:
        require(isinstance(step, dict), "plan step must be an object")
        require(step.get("step_id") not in seen, "plan has duplicate step_id")
        seen.add(step.get("step_id"))
        require(step.get("order") > previous_order, "plan step order is not strictly increasing")
        previous_order = step["order"]
        require(step.get("idempotency_key") == plan["plan_id"] + ":" + step["step_id"],
                "plan step idempotency key is invalid")
        _validate_action(step, "plan.steps." + step["step_id"])
        if step.get("component") == "legacy_full" and step.get("phase") == "apply":
            legacy_apply_count += 1
    if plan.get("execution_mode") == "legacy_full_once":
        require(legacy_apply_count == 1, "legacy plan must invoke the full backend exactly once")
    for name in ("status", "test"):
        require(name in (plan.get("operations") or {}), "plan operation is missing: " + name)
        operation = plan["operations"][name]
        require(operation.get("idempotency_key") == plan["plan_id"] + ":operation:" + name,
                "plan operation idempotency key is invalid")
        _validate_action(operation, "plan.operations." + name)
    operation_steps = plan.get("operation_steps") or {}
    if plan.get("execution_mode") == "component_adapters":
        require(set(operation_steps) == set(OPERATION_NAMES),
                "adapter plan operation steps are incomplete")
        for name in OPERATION_NAMES:
            require(operation_steps[name], "adapter operation has no steps: " + name)
            for step in operation_steps[name]:
                require(step.get("action") == name,
                        "adapter operation step action is invalid")
                require(step.get("component") in selected_components,
                        "adapter operation step escapes component selection")
                require(step.get("idempotency_key") == plan["plan_id"] + ":" + step["step_id"],
                        "adapter operation idempotency key is invalid")
                _validate_action(step, "plan.operation_steps." + name)
    return True


def load_plan(path):
    plan = read_json(path)
    validate_plan(plan)
    return plan


def _validate_source_document(descriptor):
    value = read_json(descriptor["path"])
    kind = descriptor["kind"]
    if kind == "inventory":
        validate_inventory(value)
    elif kind == "registry":
        validate_registry(value)
    elif kind == "manifest":
        validate_manifest(value)
    else:
        raise ValueError("unknown plan source kind: " + str(kind))
    normalized = normalize_document(kind, value)
    observed = sha256_bytes(canonical_bytes(normalized))
    require(observed == descriptor["semantic_sha256"], kind + " semantic drift detected")


def verify_drift_gate(plan):
    """Fail closed when an input, artifact, config or adapter changed after planning."""
    validate_plan(plan)
    require(not plan.get("has_placeholders"), "plan contains unresolved release placeholders")
    observed_integrity = _read_release_integrity(plan["release_root"])
    require(observed_integrity == plan["release_integrity"],
            "root SHA256SUMS or full release digest drift detected")
    for descriptor in plan["source_documents"]:
        _validate_source_document(descriptor)
    for descriptor in plan["artifacts"] + plan["configs"] + [plan["legacy_backend"]]:
        require(os.path.isfile(descriptor["path"]), "planned file is missing: " + descriptor["path"])
        observed = sha256_file(descriptor["path"])
        require(observed == descriptor["observed_sha256"], "file drift detected: " + descriptor["path"])
        require(observed == descriptor["expected_sha256"].lower(),
                "file no longer matches declared SHA-256: " + descriptor["path"])
    for descriptor in plan.get("tracked_command_files") or []:
        require(os.path.isfile(descriptor["path"]), "adapter file is missing: " + descriptor["path"])
        require(sha256_file(descriptor["path"]) == descriptor["observed_sha256"],
                "adapter drift detected: " + descriptor["path"])
    return True


def _safe_name(value):
    return re.sub(r"[^A-Za-z0-9_.-]", "_", str(value))[:160]


def _counter_token(lock_root, lock_name):
    ensure_directory(lock_root)
    counter_path = os.path.join(lock_root, _safe_name(lock_name) + ".fence")
    guard_path = counter_path + ".guard"
    deadline = time.time() + 5.0
    while True:
        try:
            os.mkdir(guard_path)
            break
        except FileExistsError:
            if time.time() >= deadline:
                raise RuntimeError("fencing counter is busy: " + lock_name)
            time.sleep(0.02)
    try:
        current = 0
        if os.path.isfile(counter_path):
            with open(counter_path, "r", encoding="ascii") as handle:
                text = handle.read().strip()
            current = int(text or "0")
        token = current + 1
        directory = os.path.dirname(counter_path)
        descriptor, temporary = tempfile.mkstemp(prefix=".fence-", dir=directory)
        try:
            with os.fdopen(descriptor, "w", encoding="ascii", newline="\n") as handle:
                handle.write(str(token) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, counter_path)
            _fsync_directory(directory)
        except Exception:
            try:
                os.unlink(temporary)
            except OSError:
                pass
            raise
        return token
    finally:
        os.rmdir(guard_path)


def _pid_is_active(hostname, pid):
    if hostname != socket.gethostname() or not isinstance(pid, int) or pid <= 0:
        return False
    if pid == os.getpid():
        return True
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _read_lock_metadata(lock_path):
    metadata_path = os.path.join(lock_path, "metadata.json")
    if not os.path.isfile(metadata_path):
        return {}
    try:
        return read_json(metadata_path)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def _archive_stale_lock(lock_path, metadata):
    suffix = ".stale.{}.{}".format(
        int(metadata.get("fencing_token") or 0), int(time.time() * 1000),
    )
    archived = lock_path + suffix
    os.rename(lock_path, archived)
    return archived


def acquire_lock(state_dir, lock_name, plan_id, owner_id, ttl_s):
    """Acquire one atomic directory lock and issue a monotonic fencing token."""
    lock_root = os.path.join(state_dir, "locks")
    ensure_directory(lock_root)
    lock_path = os.path.join(lock_root, _safe_name(lock_name) + ".lock")
    for _attempt in range(8):
        try:
            os.mkdir(lock_path)
            token = _counter_token(lock_root, lock_name)
            metadata = {
                "lock_name": lock_name,
                "plan_id": plan_id,
                "owner_id": owner_id,
                "hostname": socket.gethostname(),
                "pid": os.getpid(),
                "fencing_token": token,
                "acquired_at": utc_now(),
                "expires_at_epoch": time.time() + ttl_s,
            }
            atomic_write_json(os.path.join(lock_path, "metadata.json"), metadata)
            return metadata
        except FileExistsError:
            metadata = _read_lock_metadata(lock_path)
            same_process = (
                metadata.get("owner_id") == owner_id
                and metadata.get("plan_id") == plan_id
                and metadata.get("hostname") == socket.gethostname()
                and metadata.get("pid") == os.getpid()
            )
            if same_process:
                return metadata
            expired = float(metadata.get("expires_at_epoch") or 0) <= time.time()
            abandoned_resume = (
                metadata.get("owner_id") == owner_id
                and metadata.get("plan_id") == plan_id
                and not _pid_is_active(metadata.get("hostname"), metadata.get("pid"))
            )
            if not expired and not abandoned_resume:
                raise RuntimeError("active administration lock: " + lock_name)
            try:
                _archive_stale_lock(lock_path, metadata)
            except FileNotFoundError:
                continue
        except Exception:
            if os.path.isdir(lock_path) and not os.path.isfile(os.path.join(lock_path, "metadata.json")):
                try:
                    os.rmdir(lock_path)
                except OSError:
                    pass
            raise
    raise RuntimeError("could not acquire administration lock: " + lock_name)


def release_lock(state_dir, lock_name, owner_id, fencing_token):
    lock_path = os.path.join(state_dir, "locks", _safe_name(lock_name) + ".lock")
    if not os.path.isdir(lock_path):
        return False
    metadata = _read_lock_metadata(lock_path)
    if metadata.get("owner_id") != owner_id or metadata.get("fencing_token") != fencing_token:
        return False
    metadata_path = os.path.join(lock_path, "metadata.json")
    os.unlink(metadata_path)
    os.rmdir(lock_path)
    return True


def acquire_plan_locks(plan, state_dir, owner_id):
    acquired = []
    ttl_s = int(plan.get("lock_ttl_s") or 14400)
    names = [("environment", "environment--" + plan["environment"])]
    names.extend((target_id, "target--" + target_id) for target_id in sorted(plan["lock_targets"]))
    try:
        for key, lock_name in names:
            metadata = acquire_lock(state_dir, lock_name, plan["plan_id"], owner_id, ttl_s)
            metadata["key"] = key
            acquired.append(metadata)
        return acquired
    except Exception:
        release_plan_locks(state_dir, owner_id, acquired)
        raise


def release_plan_locks(state_dir, owner_id, locks):
    for metadata in reversed(locks or []):
        release_lock(
            state_dir, metadata["lock_name"], owner_id, metadata["fencing_token"],
        )


def renew_plan_locks(state_dir, locks, ttl_s):
    """Extend owned locks without changing their fencing tokens."""
    for metadata in locks:
        lock_path = os.path.join(
            state_dir, "locks", _safe_name(metadata["lock_name"]) + ".lock",
        )
        current = _read_lock_metadata(lock_path)
        require(current.get("owner_id") == metadata.get("owner_id"),
                "administration lock ownership changed: " + metadata["lock_name"])
        require(current.get("fencing_token") == metadata.get("fencing_token"),
                "administration fencing token changed: " + metadata["lock_name"])
        current["hostname"] = socket.gethostname()
        current["pid"] = os.getpid()
        current["renewed_at"] = utc_now()
        current["expires_at_epoch"] = time.time() + ttl_s
        atomic_write_json(os.path.join(lock_path, "metadata.json"), current)
        metadata.update(current)


def _checkpoint_path(state_dir):
    return os.path.join(state_dir, "checkpoint.json")


def _plan_state_dir(plan, override=None):
    """Resolve the one lock/checkpoint root signed into the immutable plan."""
    expected = os.path.abspath(plan["state_dir"])
    actual = os.path.abspath(override) if override else expected
    require(actual == expected,
            "state_dir override differs from the immutable plan lock root")
    return expected


def load_checkpoint(state_dir):
    path = _checkpoint_path(state_dir)
    return read_json(path) if os.path.isfile(path) else None


def write_checkpoint(state_dir, checkpoint):
    checkpoint["updated_at"] = utc_now()
    atomic_write_json(_checkpoint_path(state_dir), checkpoint)


def _new_checkpoint(plan, owner_id):
    return {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan["plan_id"],
        "environment": plan["environment"],
        "owner_id": owner_id,
        "status": "new",
        "active_step": None,
        "completed_step_ids": [],
        "rolled_back_step_ids": [],
        "step_results": {},
        "fencing_tokens": {},
        "failed_step_id": None,
        "manual_recovery": [],
        "created_at": utc_now(),
    }


def _copy_state_path(source, destination):
    require(not os.path.islink(source), "state archive source must not be linked")
    if os.path.isdir(source):
        for directory, directories, files in os.walk(source):
            for name in directories + files:
                require(
                    not os.path.islink(os.path.join(directory, name)),
                    "state archive content must not be linked",
                )
        shutil.copytree(source, destination)
    else:
        shutil.copy2(source, destination)


def _remove_current_plan_state(state_dir, plan_id):
    current = load_checkpoint(state_dir)
    require(current and current.get("plan_id") == plan_id,
            "terminal checkpoint changed during archival")
    for name in ("audit.jsonl", "evidence"):
        path = os.path.join(state_dir, name)
        if os.path.isdir(path):
            shutil.rmtree(path)
        elif os.path.isfile(path):
            os.unlink(path)
    backup = os.path.join(state_dir, "backups", plan_id)
    if os.path.isdir(backup):
        shutil.rmtree(backup)
    os.unlink(_checkpoint_path(state_dir))
    _fsync_directory(state_dir)


def _archive_terminal_checkpoint(state_dir, checkpoint, superseding_plan_id):
    plan_id = checkpoint.get("plan_id")
    status = checkpoint.get("status")
    require(bool(HASH_PATTERN.fullmatch(str(plan_id))),
            "terminal checkpoint plan_id is invalid")
    require(status in ARCHIVABLE_CHECKPOINT_STATUSES,
            "only a completed or cleanly rolled-back plan can be archived")
    require(not checkpoint.get("manual_recovery"),
            "manual recovery must be resolved outside the orchestrator")
    parent = os.path.join(state_dir, "__OLD__")
    destination = os.path.join(parent, plan_id)
    stage = destination + ".incomplete"
    if os.path.isdir(destination):
        handoff = read_json(os.path.join(destination, "handoff.json"))
        require(handoff.get("superseding_plan_id") == superseding_plan_id,
                "terminal checkpoint archive belongs to another handoff")
        _remove_current_plan_state(state_dir, plan_id)
        return destination
    require(not os.path.exists(destination),
            "terminal checkpoint archive path is unsafe")
    if os.path.isdir(stage):
        shutil.rmtree(stage)
    require(not os.path.exists(stage),
            "terminal checkpoint archive staging path is unsafe")
    ensure_directory(parent)
    os.mkdir(stage, mode=0o700)
    append_audit(state_dir, plan_id, "terminal_plan_archived", {
        "status": status, "superseding_plan_id": superseding_plan_id,
    })
    try:
        for name in ("checkpoint.json", "audit.jsonl", "evidence"):
            source = os.path.join(state_dir, name)
            if os.path.exists(source):
                _copy_state_path(source, os.path.join(stage, name))
        backup = os.path.join(state_dir, "backups", plan_id)
        if os.path.exists(backup):
            backup_parent = os.path.join(stage, "backups")
            ensure_directory(backup_parent)
            _copy_state_path(backup, os.path.join(backup_parent, plan_id))
        atomic_write_json(os.path.join(stage, "handoff.json"), {
            "archived_at": utc_now(),
            "plan_id": plan_id,
            "status": status,
            "superseding_plan_id": superseding_plan_id,
        })
        os.replace(stage, destination)
        _fsync_directory(parent)
    except Exception:
        if os.path.isdir(stage):
            shutil.rmtree(stage)
        raise
    _remove_current_plan_state(state_dir, plan_id)
    return destination


def _step_map(plan):
    return {step["step_id"]: step for step in plan["steps"]}


def _lock_token(locks, target_id):
    requested = target_id if target_id != "environment" else "environment"
    for metadata in locks:
        if metadata.get("key") == requested:
            return metadata["fencing_token"]
    for metadata in locks:
        if metadata.get("key") == "environment":
            return metadata["fencing_token"]
    raise RuntimeError("fencing token is unavailable")


def _format_command(command, values):
    result = []
    for argument in command:
        formatted = argument.format_map(values)
        result.append(formatted)
    return result


def _command_values(plan, state_dir, step_id, action_name, component, target, idempotency_key, token):
    target = target or {}
    worker_artifact_id = plan["worker"]["artifact_id"]
    worker_artifacts = [
        item for item in plan["artifacts"]
        if item.get("item_id") == worker_artifact_id
    ]
    require(len(worker_artifacts) == 1,
            "planned Worker archive artifact is unavailable")
    return {
        "release_root": plan["release_root"],
        "legacy_backend": plan["legacy_backend"]["path"],
        "worker_archive": worker_artifacts[0]["path"],
        "state_dir": state_dir,
        "evidence_dir": os.path.join(state_dir, "evidence"),
        "environment": plan["environment"],
        "action": action_name,
        "component": component,
        "target_id": str(target.get("target_id") or "environment"),
        "host": str(target.get("host") or ""),
        "node_id": str(target.get("node_id") or ""),
        "target_options": canonical_text(target.get("adapter") or {}),
        "plan_id": plan["plan_id"],
        "step_id": step_id,
        "idempotency_key": idempotency_key,
        "fencing_token": str(token),
    }


def _evidence_path(state_dir, step_id):
    return os.path.join(state_dir, "evidence", _safe_name(step_id) + ".json")


def execute_action(plan, state_dir, locks, step_id, action_name, component, target,
                   action, idempotency_key, runtime_env=None):
    """Execute one shell-free adapter command and persist redacted evidence."""
    renew_plan_locks(state_dir, locks, int(plan.get("lock_ttl_s") or 14400))
    target_id = str((target or {}).get("target_id") or "environment")
    token = _lock_token(locks, target_id)
    values = _command_values(
        plan, state_dir, step_id, action_name, component, target,
        idempotency_key, token,
    )
    command = _format_command(action["command"], values)
    environment = os.environ.copy()
    environment.update({
        "ADMIN_PLAN_ID": plan["plan_id"],
        "ADMIN_STEP_ID": step_id,
        "ADMIN_IDEMPOTENCY_KEY": idempotency_key,
        "ADMIN_FENCING_TOKEN": str(token),
        "ADMIN_ENVIRONMENT": plan["environment"],
        "ADMIN_TARGET_ID": target_id,
        "ADMIN_COMPONENT": component,
        "ADMIN_ACTION": action_name,
        "ADMIN_EVIDENCE_DIR": os.path.join(state_dir, "evidence"),
    })
    if runtime_env:
        environment.update({str(key): str(value) for key, value in runtime_env.items()})
    started_at = utc_now()
    started_monotonic = time.monotonic()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=plan["release_root"],
            env=environment,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=int(action.get("timeout_s", 300)),
            check=False,
        )
        return_code = completed.returncode
        stdout = completed.stdout or ""
        stderr = completed.stderr or ""
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        return_code = 124
        stdout = exc.stdout.decode("utf-8", "replace") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
        stderr = exc.stderr.decode("utf-8", "replace") if isinstance(exc.stderr, bytes) else (exc.stderr or "")
    duration_ms = int((time.monotonic() - started_monotonic) * 1000)
    evidence = {
        "schema_version": SCHEMA_VERSION,
        "plan_id": plan["plan_id"],
        "step_id": step_id,
        "idempotency_key": idempotency_key,
        "fencing_token": token,
        "component": component,
        "action": action_name,
        "target_id": target_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_ms": duration_ms,
        "command": command,
        "return_code": return_code,
        "timed_out": timed_out,
        "stdout_sha256": sha256_bytes(stdout.encode("utf-8")),
        "stderr_sha256": sha256_bytes(stderr.encode("utf-8")),
        "stdout": redact_text(stdout),
        "stderr": redact_text(stderr),
        "ok": return_code == 0,
    }
    atomic_write_json(_evidence_path(state_dir, step_id), redact_value(evidence))
    return evidence


def _checkpoint_result(evidence):
    return {
        "ok": evidence["ok"],
        "return_code": evidence["return_code"],
        "timed_out": evidence["timed_out"],
        "duration_ms": evidence["duration_ms"],
        "evidence": _safe_name(evidence["step_id"]) + ".json",
    }


def _execute_planned_step(plan, state_dir, locks, checkpoint, step, runtime_env):
    step_id = step["step_id"]
    checkpoint["active_step"] = {
        "step_id": step_id,
        "idempotent": step["idempotent"],
        "started_at": utc_now(),
    }
    write_checkpoint(state_dir, checkpoint)
    append_audit(state_dir, plan["plan_id"], "step_started", {
        "step_id": step_id,
        "phase": step["phase"],
        "component": step["component"],
        "target_id": step["target"]["target_id"],
        "idempotency_key": step["idempotency_key"],
    })
    evidence = execute_action(
        plan, state_dir, locks, step_id, step["action"], step["component"],
        step["target"], step, step["idempotency_key"], runtime_env,
    )
    checkpoint["step_results"][step_id] = _checkpoint_result(evidence)
    checkpoint["active_step"] = None
    if evidence["ok"]:
        if step_id not in checkpoint["completed_step_ids"]:
            checkpoint["completed_step_ids"].append(step_id)
        append_audit(state_dir, plan["plan_id"], "step_succeeded", {
            "step_id": step_id, "return_code": evidence["return_code"],
            "duration_ms": evidence["duration_ms"],
        })
    else:
        checkpoint["failed_step_id"] = step_id
        append_audit(state_dir, plan["plan_id"], "step_failed", {
            "step_id": step_id, "return_code": evidence["return_code"],
            "timed_out": evidence["timed_out"],
        })
    write_checkpoint(state_dir, checkpoint)
    return evidence["ok"]


def _rollback_candidates(plan, checkpoint):
    by_id = _step_map(plan)
    candidates = list(checkpoint.get("completed_step_ids") or [])
    failed = checkpoint.get("failed_step_id")
    if failed and failed not in candidates:
        candidates.append(failed)
    result = []
    for step_id in reversed(candidates):
        step = by_id.get(step_id)
        if step and step.get("phase") == "apply":
            result.append(step)
    return result


def rollback_completed(plan, state_dir, locks, checkpoint, runtime_env=None, automatic=False):
    """Roll back only steps explicitly reversible and proven to have a backup."""
    manual = []
    rollback_failed = []
    completed = set(checkpoint.get("completed_step_ids") or [])
    rolled_back = set(checkpoint.get("rolled_back_step_ids") or [])
    append_audit(state_dir, plan["plan_id"], "rollback_started", {"automatic": automatic})
    for step in _rollback_candidates(plan, checkpoint):
        step_id = step["step_id"]
        if step_id in rolled_back:
            continue
        if not step.get("reversible"):
            manual.append({"step_id": step_id, "reason": "step_not_reversible"})
            continue
        backup_id = step.get("backup_step_id")
        if not backup_id or backup_id not in completed:
            manual.append({"step_id": step_id, "reason": "verified_backup_missing"})
            continue
        action = step.get("rollback_action")
        if not action:
            manual.append({"step_id": step_id, "reason": "rollback_action_missing"})
            continue
        rollback_id = "rollback:" + step_id
        idempotency_key = plan["plan_id"] + ":" + rollback_id
        evidence = execute_action(
            plan, state_dir, locks, rollback_id, "rollback", step["component"],
            step["target"], action, idempotency_key, runtime_env,
        )
        checkpoint["step_results"][rollback_id] = _checkpoint_result(evidence)
        if not evidence["ok"]:
            rollback_failed.append({"step_id": step_id, "reason": "rollback_command_failed"})
            write_checkpoint(state_dir, checkpoint)
            continue
        ready_action = step.get("rollback_ready_action")
        if ready_action:
            ready_id = "rollback-ready:" + step_id
            ready_evidence = execute_action(
                plan, state_dir, locks, ready_id, "ready", step["component"],
                step["target"], ready_action,
                plan["plan_id"] + ":" + ready_id, runtime_env,
            )
            checkpoint["step_results"][ready_id] = _checkpoint_result(ready_evidence)
            if not ready_evidence["ok"]:
                rollback_failed.append({"step_id": step_id, "reason": "rollback_readiness_failed"})
                write_checkpoint(state_dir, checkpoint)
                continue
        test_action = step.get("rollback_test_action")
        if test_action:
            test_id = "rollback-test:" + step_id
            test_evidence = execute_action(
                plan, state_dir, locks, test_id, "test", step["component"],
                step["target"], test_action,
                plan["plan_id"] + ":" + test_id, runtime_env,
            )
            checkpoint["step_results"][test_id] = _checkpoint_result(test_evidence)
            if not test_evidence["ok"]:
                rollback_failed.append({"step_id": step_id, "reason": "rollback_test_failed"})
                write_checkpoint(state_dir, checkpoint)
                continue
        checkpoint["rolled_back_step_ids"].append(step_id)
        rolled_back.add(step_id)
        append_audit(state_dir, plan["plan_id"], "step_rolled_back", {"step_id": step_id})
        write_checkpoint(state_dir, checkpoint)
    checkpoint["manual_recovery"] = manual + rollback_failed
    if checkpoint["manual_recovery"]:
        checkpoint["status"] = "failed_manual_recovery" if automatic else "manual_recovery"
    else:
        checkpoint["status"] = "failed_rolled_back" if automatic else "rolled_back"
    append_audit(state_dir, plan["plan_id"], "rollback_finished", {
        "status": checkpoint["status"],
        "manual_recovery": checkpoint["manual_recovery"],
    })
    write_checkpoint(state_dir, checkpoint)
    return checkpoint


def apply_plan(plan, state_dir=None, resume=False, auto_rollback=True, runtime_env=None, archive_terminal=False):
    """Apply a verified plan under environment and target fencing locks."""
    verify_drift_gate(plan)
    state_dir = _plan_state_dir(plan, state_dir)
    ensure_directory(state_dir)
    existing = load_checkpoint(state_dir)
    archived_checkpoint = None
    if existing and existing.get("plan_id") != plan["plan_id"]:
        require(
            archive_terminal,
            "checkpoint belongs to another plan; use --archive-terminal "
            "only for a verified terminal predecessor",
        )
        require(not resume, "a new plan cannot resume its predecessor")
        require(existing.get("status") in ARCHIVABLE_CHECKPOINT_STATUSES,
                "predecessor checkpoint is not safely archivable")
        require(not existing.get("manual_recovery"),
                "predecessor checkpoint still requires manual recovery")
        archived_checkpoint = existing
        existing = None
    if existing:
        require(resume, "checkpoint exists; use --resume for the same plan")
        if existing.get("status") == "complete":
            return existing
        require(existing.get("status") not in (
            "rolled_back", "failed_rolled_back", "manual_recovery", "failed_manual_recovery",
        ), "a rolled-back or manual-recovery plan cannot resume forward")
        owner_id = existing["owner_id"]
        checkpoint = existing
    else:
        owner_id = secrets.token_hex(16)
        checkpoint = _new_checkpoint(plan, owner_id)
    locks = acquire_plan_locks(plan, state_dir, owner_id)
    try:
        if archived_checkpoint:
            current = load_checkpoint(state_dir)
            require(current == archived_checkpoint,
                    "predecessor checkpoint changed before archival")
            _archive_terminal_checkpoint(
                state_dir, archived_checkpoint, plan["plan_id"],
            )
        checkpoint["fencing_tokens"] = {
            metadata["key"]: metadata["fencing_token"] for metadata in locks
        }
        active = checkpoint.get("active_step")
        if active and active.get("step_id") not in checkpoint.get("completed_step_ids", []):
            if not active.get("idempotent"):
                checkpoint["status"] = "failed_manual_recovery"
                checkpoint["manual_recovery"] = [{
                    "step_id": active.get("step_id"),
                    "reason": "non_idempotent_result_indeterminate",
                }]
                write_checkpoint(state_dir, checkpoint)
                append_audit(state_dir, plan["plan_id"], "resume_refused", {
                    "step_id": active.get("step_id"),
                    "reason": "non_idempotent_result_indeterminate",
                })
                return checkpoint
            append_audit(state_dir, plan["plan_id"], "idempotent_step_resumed", active)
            checkpoint["active_step"] = None
        checkpoint["status"] = "applying"
        write_checkpoint(state_dir, checkpoint)
        append_audit(state_dir, plan["plan_id"], "apply_started", {
            "resume": resume,
            "fencing_tokens": checkpoint["fencing_tokens"],
        })
        completed = set(checkpoint.get("completed_step_ids") or [])
        for step in plan["steps"]:
            if step["step_id"] in completed:
                continue
            ok = _execute_planned_step(
                plan, state_dir, locks, checkpoint, step, runtime_env,
            )
            if not ok:
                checkpoint["status"] = "failed"
                write_checkpoint(state_dir, checkpoint)
                if auto_rollback:
                    rollback_completed(
                        plan, state_dir, locks, checkpoint, runtime_env, automatic=True,
                    )
                return checkpoint
            completed.add(step["step_id"])
        checkpoint["status"] = "complete"
        checkpoint["failed_step_id"] = None
        checkpoint["active_step"] = None
        write_checkpoint(state_dir, checkpoint)
        append_audit(state_dir, plan["plan_id"], "apply_complete", {
            "completed_steps": len(checkpoint["completed_step_ids"]),
        })
        return checkpoint
    finally:
        release_plan_locks(state_dir, owner_id, locks)


def run_plan_operation(plan, operation_name, state_dir=None, runtime_env=None,
                       operation_id=None):
    require(operation_name in OPERATION_NAMES, "unknown plan operation: " + operation_name)
    if plan["execution_mode"] == "legacy_full_once":
        require(operation_name in plan["operations"],
                "legacy plan operation is unavailable: " + operation_name)
    if operation_name == "restart":
        require(is_identifier(operation_id),
                "restart requires a stable operation_id")
    elif operation_id is not None:
        require(is_identifier(operation_id), "operation_id is invalid")
    verify_drift_gate(plan)
    state_dir = _plan_state_dir(plan, state_dir)
    ensure_directory(state_dir)
    checkpoint = load_checkpoint(state_dir)
    if operation_name == "test":
        require(checkpoint and checkpoint.get("plan_id") == plan["plan_id"],
                "test requires a checkpoint for this plan")
        require(checkpoint.get("status") == "complete", "test requires a completed apply")
    owner_id = secrets.token_hex(16)
    locks = acquire_plan_locks(plan, state_dir, owner_id)
    try:
        append_audit(state_dir, plan["plan_id"], "operation_started", {
            "operation": operation_name, "operation_id": operation_id,
        })
        if plan["execution_mode"] == "component_adapters":
            results = []
            for step in plan["operation_steps"][operation_name]:
                execution_step_id = step["step_id"]
                idempotency_key = step["idempotency_key"]
                if operation_id is not None:
                    execution_step_id += ":" + operation_id
                    idempotency_key += ":" + operation_id
                evidence = execute_action(
                    plan, state_dir, locks, execution_step_id, operation_name,
                    step["component"], step["target"], step,
                    idempotency_key, runtime_env,
                )
                results.append(evidence)
                if not evidence["ok"]:
                    break
            evidence = {
                "ok": len(results) == len(plan["operation_steps"][operation_name])
                      and all(item["ok"] for item in results),
                "return_code": next(
                    (item["return_code"] for item in results if not item["ok"]), 0,
                ),
                "results": results,
            }
        else:
            action = plan["operations"][operation_name]
            step_id = "operation:" + operation_name
            evidence = execute_action(
                plan, state_dir, locks, step_id, operation_name, "administration",
                {"target_id": "environment"}, action,
                action["idempotency_key"], runtime_env,
            )
        append_audit(state_dir, plan["plan_id"], "operation_finished", {
            "operation": operation_name, "ok": evidence["ok"],
            "operation_id": operation_id, "return_code": evidence["return_code"],
        })
        return evidence
    finally:
        release_plan_locks(state_dir, owner_id, locks)


def rollback_plan(plan, state_dir=None, runtime_env=None):
    verify_drift_gate(plan)
    state_dir = _plan_state_dir(plan, state_dir)
    checkpoint = load_checkpoint(state_dir)
    require(checkpoint is not None, "rollback requires an existing checkpoint")
    require(checkpoint.get("plan_id") == plan["plan_id"], "checkpoint belongs to another plan")
    owner_id = checkpoint.get("owner_id") or secrets.token_hex(16)
    locks = acquire_plan_locks(plan, state_dir, owner_id)
    try:
        return rollback_completed(
            plan, state_dir, locks, checkpoint, runtime_env, automatic=False,
        )
    finally:
        release_plan_locks(state_dir, owner_id, locks)


def plan_status(plan, state_dir=None):
    validate_plan(plan)
    state_dir = _plan_state_dir(plan, state_dir)
    checkpoint = load_checkpoint(state_dir)
    if not checkpoint:
        return {
            "ok": True,
            "plan_id": plan["plan_id"],
            "environment": plan["environment"],
            "status": "not_started",
            "completed_steps": 0,
            "total_steps": len(plan["steps"]),
        }
    require(checkpoint.get("plan_id") == plan["plan_id"], "checkpoint belongs to another plan")
    return {
        "ok": checkpoint.get("status") == "complete",
        "plan_id": plan["plan_id"],
        "environment": plan["environment"],
        "status": checkpoint.get("status"),
        "completed_steps": len(checkpoint.get("completed_step_ids") or []),
        "total_steps": len(plan["steps"]),
        "failed_step_id": checkpoint.get("failed_step_id"),
        "manual_recovery": checkpoint.get("manual_recovery") or [],
        "updated_at": checkpoint.get("updated_at"),
    }


def _print_json(value):
    print(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2))


def _build_parser():
    parser = argparse.ArgumentParser(
        description="Deterministic administration orchestrator above legacy rollout hooks",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    plan_parser = subparsers.add_parser("plan", help="validate inputs and create an immutable plan")
    plan_parser.add_argument("--inventory", required=True)
    plan_parser.add_argument("--registry", required=True)
    plan_parser.add_argument("--manifest", required=True)
    plan_parser.add_argument("--output", required=True)
    plan_parser.add_argument("--component", action="append",
                             help="component name or comma-separated names")
    plan_parser.add_argument("--target", action="append",
                             help="target_id or comma-separated target_ids")
    plan_parser.add_argument("--worker-canary-count", type=int)
    plan_parser.add_argument("--worker-batch-size", type=int)
    plan_parser.add_argument(
        "--legacy-full-once", action="store_true",
        help="use the retained exactly-once legacy backend for a full selection",
    )
    plan_parser.add_argument("--allow-placeholders", action="store_true")
    apply_parser = subparsers.add_parser("apply", help="apply an exact immutable plan")
    apply_parser.add_argument("--plan", required=True)
    apply_parser.add_argument("--state-dir")
    apply_parser.add_argument("--resume", action="store_true")
    apply_parser.add_argument("--no-auto-rollback", action="store_true")
    apply_parser.add_argument(
        "--archive-terminal", action="store_true",
        help="archive a completed or cleanly rolled-back predecessor under __OLD__",
    )
    status_parser = subparsers.add_parser("status", help="read local checkpoint status")
    status_parser.add_argument("--plan", required=True)
    status_parser.add_argument("--state-dir")
    status_parser.add_argument("--live", action="store_true")
    test_parser = subparsers.add_parser("test", help="run the registered post-apply test operation")
    test_parser.add_argument("--plan", required=True)
    test_parser.add_argument("--state-dir")
    restart_parser = subparsers.add_parser(
        "restart", help="restart the selected adapter components and targets",
    )
    restart_parser.add_argument("--plan", required=True)
    restart_parser.add_argument("--state-dir")
    restart_parser.add_argument("--operation-id", required=True)
    rollback_parser = subparsers.add_parser("rollback", help="reverse only backed-up reversible steps")
    rollback_parser.add_argument("--plan", required=True)
    rollback_parser.add_argument("--state-dir")
    return parser


def main(argv=None):
    parser = _build_parser()
    arguments = parser.parse_args(argv)
    try:
        if arguments.command == "plan":
            plan = build_plan(
                arguments.inventory, arguments.registry, arguments.manifest,
                allow_placeholders=arguments.allow_placeholders,
                selected_components=arguments.component,
                selected_targets=arguments.target,
                worker_canary_count=arguments.worker_canary_count,
                worker_batch_size=arguments.worker_batch_size,
                force_legacy_full_once=arguments.legacy_full_once,
            )
            output = os.path.abspath(arguments.output)
            require(not path_is_within(output, plan["release_root"]),
                    "plan output must be outside the immutable release root")
            atomic_write_json(output, plan)
            _print_json({
                "ok": True,
                "plan_id": plan["plan_id"],
                "output": output,
                "steps": len(plan["steps"]),
                "selected_components": plan["selected_components"],
                "selected_targets": [item["target_id"] for item in plan["targets"]],
                "has_placeholders": plan["has_placeholders"],
            })
            return 0
        plan = load_plan(arguments.plan)
        if arguments.command == "apply":
            result = apply_plan(
                plan, arguments.state_dir, resume=arguments.resume,
                auto_rollback=not arguments.no_auto_rollback,
                archive_terminal=arguments.archive_terminal,
            )
            _print_json(plan_status(plan, arguments.state_dir))
            return 0 if result.get("status") == "complete" else 3
        if arguments.command == "status":
            status = plan_status(plan, arguments.state_dir)
            live_ok = True
            if arguments.live:
                live = run_plan_operation(plan, "status", arguments.state_dir)
                status["live"] = {
                    "ok": live["ok"], "return_code": live["return_code"],
                }
                live_ok = live["ok"]
            _print_json(status)
            local_ok = status.get("status") not in (
                "failed", "manual_recovery", "failed_manual_recovery",
            )
            return 0 if local_ok and live_ok else 3
        if arguments.command == "test":
            result = run_plan_operation(plan, "test", arguments.state_dir)
            _print_json({"ok": result["ok"], "return_code": result["return_code"]})
            return 0 if result["ok"] else 3
        if arguments.command == "restart":
            result = run_plan_operation(
                plan, "restart", arguments.state_dir,
                operation_id=arguments.operation_id,
            )
            _print_json({"ok": result["ok"], "return_code": result["return_code"]})
            return 0 if result["ok"] else 3
        if arguments.command == "rollback":
            result = rollback_plan(plan, arguments.state_dir)
            _print_json(plan_status(plan, arguments.state_dir))
            return 0 if result.get("status") == "rolled_back" else 4
    except (ValueError, RuntimeError, OSError, json.JSONDecodeError) as exc:
        _print_json({"ok": False, "error": redact_text(str(exc))})
        return 2
    return 2


if __name__ == "__main__":
    sys.exit(main())

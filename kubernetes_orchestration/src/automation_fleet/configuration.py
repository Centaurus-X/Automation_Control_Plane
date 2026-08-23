"""Configuration loading with release-relative path resolution."""

import copy
import os
import pathlib

from automation_fleet.canonical import read_json
from automation_fleet.validation import require


def _resolve(base, value, label):
    require(isinstance(value, str) and value,
            "config_path", label + " must be a non-empty path")
    path = pathlib.Path(value)
    if not path.is_absolute():
        path = base / path
    return str(path.resolve())


def load_configuration(root, config_path=None):
    root_path = pathlib.Path(root).resolve()
    source = pathlib.Path(config_path).resolve() if config_path else (
        root_path / "config" / "fleet.example.json")
    value = read_json(source)
    require(value.get("schema_version") == 1,
            "config_schema", "fleet config schema_version must be 1")
    config = copy.deepcopy(value)
    base = source.parent
    for key in ("inventory_path", "catalog_path", "desired_state_path", "policy_path"):
        config[key] = _resolve(base, config.get(key), key)
    state_override = os.environ.get("FLEET_STATE_DIR")
    if state_override:
        config["state_dir"] = str(pathlib.Path(state_override).resolve())
    else:
        state = config.get("state_dir")
        if not state:
            xdg = os.environ.get("XDG_STATE_HOME")
            state_root = pathlib.Path(xdg) if xdg else pathlib.Path.home() / ".local" / "state"
            state = state_root / "automation-fleet"
        elif not pathlib.Path(state).is_absolute():
            state = base / state
        config["state_dir"] = str(pathlib.Path(state).resolve())
    token_override = os.environ.get("FLEET_ADMIN_TOKEN_FILE")
    token_value = token_override or config.get("admin_token_file")
    require(isinstance(token_value, str) and token_value,
            "admin_token_file", "admin token file is required")
    token_path = pathlib.Path(token_value)
    if not token_path.is_absolute():
        token_path = base / token_path
    config["admin_token_file"] = str(token_path.resolve())
    for role in ("viewer", "operator", "approver", "auditor"):
        key = role + "_token_file"
        environment_key = "FLEET_" + role.upper() + "_TOKEN_FILE"
        token_value = os.environ.get(environment_key) or config.get(key)
        if token_value:
            token_path = pathlib.Path(token_value)
            if not token_path.is_absolute():
                token_path = base / token_path
            config[key] = str(token_path.resolve())
    registry_value = (os.environ.get("FLEET_AGENT_REGISTRY_FILE")
                      or config.get("agent_registry_path"))
    if registry_value:
        registry_path = pathlib.Path(registry_value)
        if not registry_path.is_absolute():
            registry_path = base / registry_path
        config["agent_registry_path"] = str(registry_path.resolve())
    listen = config.get("listen", {})
    require(isinstance(listen, dict), "listen", "listen must be an object")
    host = os.environ.get("FLEET_LISTEN_HOST", listen.get("host", "127.0.0.1"))
    port_text = os.environ.get("FLEET_LISTEN_PORT", str(listen.get("port", 9080)))
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError("listen_port: invalid port") from exc
    require(isinstance(host, str) and host and 1 <= port <= 65535,
            "listen", "listen host or port is invalid")
    config["listen"] = {"host": host, "port": port}
    config["allow_mutations"] = os.environ.get("FLEET_ALLOW_MUTATIONS", "0") == "1"
    config["cookie_secure"] = os.environ.get(
        "FLEET_COOKIE_SECURE", "1" if host not in {"127.0.0.1", "localhost"} else "0") == "1"
    config["root"] = str(root_path)
    config["config_path"] = str(source)
    return config


def load_documents(config):
    return {
        "inventory": read_json(config["inventory_path"]),
        "catalog": read_json(config["catalog_path"]),
        "desired": read_json(config["desired_state_path"]),
        "policy": read_json(config["policy_path"]),
    }

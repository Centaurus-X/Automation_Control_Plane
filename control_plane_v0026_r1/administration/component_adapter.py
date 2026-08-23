#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Operational, credential-free lifecycle adapter for one selected component.

The orchestrator invokes this program without a shell.  Control-plane actions
delegate to the existing per-component scripts.  Worker actions transfer the
exact planned archive over SSH and reuse the release's verified installer and
remote lifecycle implementation.  Authentication remains an external SSH
concern; no credential is accepted by this adapter.
"""

import argparse
import hashlib
import ipaddress
import json
import os
import re
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import zipfile

from functools import partial


COMPONENTS = ("database", "mqtt", "master", "proxy", "mcp_controller_server", "worker")
COMPONENT_DIRECTORIES = {
    "database": "postgres_system",
    "mqtt": "mqtt_broker",
    "master": "master_node",
    "proxy": "proxy_gateway",
    "mcp_controller_server": "mcp_controller_server",
}
RUNTIME_SERVICES = {
    "database": ("postgres-primary", "postgres-standby"),
    "mqtt": ("mqtt-broker",),
    "master": ("master-node",),
    "proxy": ("proxy-gateway",),
    "mcp_controller_server": ("mcp-controller-server",),
}
RUNTIME_READINESS_TIMEOUT_S = 120.0
RUNTIME_READINESS_POLL_S = 1.0
MUTATING_ACTIONS = {"backup", "apply", "restart", "rollback"}
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
HASH_PATTERN = re.compile(r"^[0-9a-f]{64}$")
SSH_TARGET_PATTERN = re.compile(
    r"^(?:[A-Za-z0-9_.-]+@)?[A-Za-z0-9][A-Za-z0-9_.-]*$",
)


AdapterError = RuntimeError


def require(condition, message):
    if not condition:
        raise AdapterError(message)


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for block in iter(partial(stream.read, 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def atomic_json(path, value):
    directory = os.path.dirname(os.path.abspath(path))
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".adapter-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(value, stream, ensure_ascii=False, sort_keys=True,
                      separators=(",", ":"))
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def run(command, cwd=None, input_text=None, environment=None):
    completed = subprocess.run(
        list(command), cwd=cwd, env=environment, input=input_text,
        text=True, encoding="utf-8", errors="replace", capture_output=True,
        check=False,
    )
    if completed.stdout:
        print(completed.stdout, end="")
    if completed.stderr:
        print(completed.stderr, end="", file=sys.stderr)
    require(completed.returncode == 0,
            "command failed with exit {}: {}".format(
                completed.returncode, command[0],
            ))
    return completed.stdout


def captured(command):
    completed = subprocess.run(
        list(command), text=True, encoding="utf-8", errors="replace",
        capture_output=True, check=False,
    )
    require(
        completed.returncode == 0,
        "command failed with exit {}: {}".format(
            completed.returncode, command[0],
        ),
    )
    return completed.stdout


def parse_options(text):
    try:
        value = json.loads(text or "{}")
    except json.JSONDecodeError as exc:
        raise AdapterError("target options are not valid JSON") from exc
    require(isinstance(value, dict), "target options must be an object")
    require(all(isinstance(key, str) and isinstance(item, str)
                for key, item in value.items()),
            "target options must contain only string values")
    return value


def _adapter_state_path(arguments):
    safe_target = re.sub(r"[^A-Za-z0-9_.-]", "_", arguments.target_id)
    safe_component = re.sub(r"[^A-Za-z0-9_.-]", "_", arguments.component)
    return os.path.join(arguments.state_dir, "adapter-state", safe_target,
                        safe_component + ".json")


def claim_mutation(arguments):
    path = _adapter_state_path(arguments)
    current = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as stream:
            current = json.load(stream)
    previous_token = int(current.get("fencing_token") or 0)
    require(arguments.fencing_token >= previous_token,
            "stale adapter fencing token")
    completed_keys = set(current.get("completed_idempotency_keys") or [])
    if current.get("status") == "complete" and current.get("idempotency_key"):
        completed_keys.add(current["idempotency_key"])
    if arguments.idempotency_key in completed_keys:
        print("adapter idempotency replay: already complete")
        return path, True
    atomic_json(path, {
        "plan_id": arguments.plan_id,
        "step_id": arguments.step_id,
        "idempotency_key": arguments.idempotency_key,
        "fencing_token": arguments.fencing_token,
        "action": arguments.action,
        "status": "running",
        "completed_idempotency_keys": sorted(completed_keys),
    })
    return path, False


def finish_mutation(path, arguments, status):
    current = {}
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as stream:
            current = json.load(stream)
    completed_keys = set(current.get("completed_idempotency_keys") or [])
    if status == "complete":
        completed_keys.add(arguments.idempotency_key)
    atomic_json(path, {
        "plan_id": arguments.plan_id,
        "step_id": arguments.step_id,
        "idempotency_key": arguments.idempotency_key,
        "fencing_token": arguments.fencing_token,
        "action": arguments.action,
        "status": status,
        "completed_idempotency_keys": sorted(completed_keys),
    })


def _component_root(arguments):
    relative = COMPONENT_DIRECTORIES[arguments.component]
    path = os.path.join(arguments.release_root, relative)
    require(os.path.isdir(path), "component directory is missing: " + path)
    return path


def _assert_local_target(host):
    target_host = str(host).rsplit("@", 1)[-1]
    require(target_host and bool(SSH_TARGET_PATTERN.fullmatch(target_host)),
            "control-plane target host is unsafe")
    if target_host in ("localhost", "127.0.0.1", "::1", socket.gethostname(),
                       socket.getfqdn()):
        return
    try:
        targets = {item[4][0] for item in socket.getaddrinfo(target_host, None)}
    except socket.gaierror as exc:
        raise AdapterError("control-plane target host cannot be resolved") from exc
    local = {"127.0.0.1", "::1"}
    for name in (socket.gethostname(), socket.getfqdn()):
        try:
            local.update(item[4][0] for item in socket.getaddrinfo(name, None))
        except socket.gaierror:
            pass
    for address in targets:
        family = socket.AF_INET6 if ":" in address else socket.AF_INET
        probe = socket.socket(family, socket.SOCK_DGRAM)
        try:
            probe.connect((address, 9))
            local.add(probe.getsockname()[0])
        except OSError:
            pass
        finally:
            probe.close()
    require(bool(targets.intersection(local)),
            "control-plane target does not resolve to this host")


def _script(arguments, name):
    path = os.path.join(_component_root(arguments), name)
    require(os.path.isfile(path) and not os.path.islink(path),
            "component script is missing or unsafe: " + path)
    return path


def _docker_compose(arguments, *extra):
    return run(("docker", "compose") + tuple(extra), cwd=_component_root(arguments))


def _check_runtime_services(arguments):
    run((_script(arguments, "status.sh"),), cwd=_component_root(arguments))
    for service in RUNTIME_SERVICES[arguments.component]:
        container_id = _docker_compose(arguments, "ps", "-q", service).strip()
        require(container_id, "runtime service is absent: " + service)
        state = run((
            "docker", "inspect", "--format",
            "{{.State.Status}}|{{if .State.Health}}{{.State.Health.Status}}{{end}}",
            container_id,
        )).strip()
        status, _separator, health = state.partition("|")
        require(status == "running", "runtime service is not running: " + service)
        require(health in ("", "healthy"),
                "runtime service is not healthy: " + service)


def _wait_runtime_services(arguments):
    deadline = time.monotonic() + RUNTIME_READINESS_TIMEOUT_S
    last_error = None
    while True:
        try:
            _check_runtime_services(arguments)
            return
        except AdapterError as exc:
            last_error = exc
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise AdapterError(
                "runtime readiness timed out: " + str(last_error),
            ) from last_error
        time.sleep(min(RUNTIME_READINESS_POLL_S, remaining))


def _validated_host(value, option_name):
    require(value, option_name + " must not be empty")
    try:
        ipaddress.ip_address(value)
        return value
    except ValueError:
        pass
    require(
        len(value) <= 253
        and bool(re.fullmatch(
            r"[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value,
        )),
        option_name + " is not a safe IP address or hostname",
    )
    return value


def _mqtt_handoff_directory(arguments):
    return os.path.join(
        arguments.release_root, "mqtt_broker", "runtime-pki-handoff",
    )


def _mqtt_handoff_file(arguments, name):
    directory = _mqtt_handoff_directory(arguments)
    marker = os.path.join(directory, ".live_mqtt_pki_handoff")
    path = os.path.join(directory, name)
    require(os.path.isfile(marker) and not os.path.islink(marker),
            "verified live MQTT PKI handoff marker is missing")
    require(os.path.isfile(path) and not os.path.islink(path),
            "verified live MQTT PKI handoff file is missing: " + name)
    return path


def _expanded_external_path(value):
    text = str(value or "")
    home = os.path.expanduser("~")
    if text == "$HOME":
        text = home
    elif text.startswith("$HOME/"):
        text = os.path.join(home, text[6:])
    elif text == "~":
        text = home
    elif text.startswith("~/"):
        text = os.path.join(home, text[2:])
    return os.path.abspath(text)


def _private_external_file(arguments, value, option_name):
    path = _expanded_external_path(value)
    require(os.path.isabs(path), option_name + " must resolve to an absolute path")
    require(os.path.commonpath((arguments.release_root, path)) != arguments.release_root,
            option_name + " must stay outside the immutable release root")
    require(os.path.isfile(path) and not os.path.islink(path),
            option_name + " is missing or unsafe")
    metadata = os.stat(path)
    require(metadata.st_size > 0, option_name + " must be non-empty")
    require((metadata.st_mode & 0o077) == 0,
            option_name + " permissions must be 0600 or stricter")
    return path


def _public_external_file(arguments, value, option_name):
    path = _expanded_external_path(value)
    require(os.path.isabs(path), option_name + " must resolve to an absolute path")
    require(os.path.commonpath((arguments.release_root, path)) != arguments.release_root,
            option_name + " must stay outside the immutable release root")
    require(os.path.isfile(path) and not os.path.islink(path),
            option_name + " is missing or unsafe")
    require(os.path.getsize(path) > 0, option_name + " must be non-empty")
    return path


def _validated_port(value, option_name):
    require(bool(re.fullmatch(r"[1-9][0-9]{0,4}", str(value or ""))),
            option_name + " must be a positive TCP port")
    port = int(value)
    require(port <= 65535, option_name + " exceeds the TCP port range")
    return str(port)


def _configure_mcp_environment(arguments, options):
    profile = options.get("deployment_profile", "test")
    require(profile in ("test", "production"),
            "MCP deployment_profile must be test or production")
    site_id = options.get("site_id", "site-main")
    require(bool(IDENTIFIER_PATTERN.fullmatch(site_id)), "site_id is invalid")
    os.environ["AUTOMATION_SITE_ID"] = site_id
    os.environ["MCP_BIND_ADDRESS"] = _validated_host(
        options.get("mcp_bind_address", "127.0.0.1"), "mcp_bind_address",
    )
    os.environ["MCP_HTTP_PORT"] = _validated_port(
        options.get("mcp_http_port", "8080"), "mcp_http_port",
    )
    source_id = options.get("mcp_source_instance_id", "mcp-controller-01")
    require(bool(IDENTIFIER_PATTERN.fullmatch(source_id)),
            "mcp_source_instance_id is invalid")
    os.environ["MCP_MQTT_SOURCE_INSTANCE_ID"] = source_id
    auth_source = options.get(
        "mcp_auth_source", "$HOME/.config/automation-control-plane/mcp_auth.json",
    )
    os.environ["MCP_AUTH_SOURCE"] = _private_external_file(
        arguments, auth_source, "mcp_auth_source",
    )

    if profile == "test":
        pki_root = os.path.join(arguments.release_root, "mqtt_broker", "test-pki")
        os.environ["MCP_MQTT_CA_SOURCE"] = os.path.join(pki_root, "ca", "ca.crt")
        os.environ["MCP_MQTT_CERT_SOURCE"] = os.path.join(
            pki_root, "clients", "mcp-controller", "mqtt.crt",
        )
        os.environ["MCP_MQTT_KEY_SOURCE"] = os.path.join(
            pki_root, "clients", "mcp-controller", "mqtt.key",
        )
        return

    configured = (
        options.get("mcp_mqtt_ca_source"),
        options.get("mcp_mqtt_cert_source"),
        options.get("mcp_mqtt_key_source"),
    )
    require(not any(configured) or all(configured),
            "production MCP MQTT material must be configured as one complete set")
    if all(configured):
        os.environ["MCP_MQTT_CA_SOURCE"] = _public_external_file(
            arguments, configured[0], "mcp_mqtt_ca_source",
        )
        os.environ["MCP_MQTT_CERT_SOURCE"] = _public_external_file(
            arguments, configured[1], "mcp_mqtt_cert_source",
        )
        os.environ["MCP_MQTT_KEY_SOURCE"] = _private_external_file(
            arguments, configured[2], "mcp_mqtt_key_source",
        )
        return

    os.environ["MCP_MQTT_CA_SOURCE"] = _mqtt_handoff_file(
        arguments, "mqtt_ca.crt",
    )
    os.environ["MCP_MQTT_CERT_SOURCE"] = _mqtt_handoff_file(
        arguments, "mcp_mqtt.crt",
    )
    os.environ["MCP_MQTT_KEY_SOURCE"] = _mqtt_handoff_file(
        arguments, "mcp_mqtt.key",
    )


def _configure_control_plane_environment(arguments, options):
    values = (
        ("mqtt_bind_address", "MQTT_BIND_ADDRESS"),
        ("mqtt_test_host", "MQTT_TEST_HOST"),
        ("mqtt_test_broker_ip", "MQTT_TEST_BROKER_IP"),
    )
    for option_name, environment_name in values:
        value = options.get(option_name)
        if value:
            os.environ[environment_name] = _validated_host(value, option_name)

    if arguments.component == "mcp_controller_server":
        _configure_mcp_environment(arguments, options)

    if options.get("deployment_profile", "test") != "production":
        return
    handoff_directory = _mqtt_handoff_directory(arguments)
    marker = os.path.join(
        handoff_directory, ".live_mqtt_pki_handoff",
    )
    if not os.path.isfile(marker):
        return
    if arguments.component == "master":
        os.environ["MQTT_CA_FILE"] = _mqtt_handoff_file(
            arguments, "mqtt_ca.crt",
        )
        os.environ["MASTER_MQTT_CERT_FILE"] = _mqtt_handoff_file(
            arguments, "master_mqtt.crt",
        )
        os.environ["MASTER_MQTT_KEY_FILE"] = _mqtt_handoff_file(
            arguments, "master_mqtt.key",
        )
    elif arguments.component == "proxy":
        os.environ["MQTT_CA_FILE"] = _mqtt_handoff_file(
            arguments, "mqtt_ca.crt",
        )
        os.environ["PROXY_MQTT_CERT_FILE"] = _mqtt_handoff_file(
            arguments, "proxy_mqtt.crt",
        )
        os.environ["PROXY_MQTT_KEY_FILE"] = _mqtt_handoff_file(
            arguments, "proxy_mqtt.key",
        )


def _handoff_live_database_secrets(arguments):
    handoff = os.path.join(
        _component_root(arguments), "scripts", "handoff_live_secrets.sh",
    )
    require(os.path.isfile(handoff) and not os.path.islink(handoff),
            "database runtime-material handoff hook is missing or unsafe")
    run((handoff,), cwd=_component_root(arguments))


def _postgres_tls_handoff_file(arguments, name):
    directory = os.path.join(
        _component_root(arguments), "runtime-tls-handoff",
    )
    path = os.path.join(directory, name)
    require(os.path.isfile(path) and not os.path.islink(path),
            "PostgreSQL TLS handoff file is missing or unsafe: " + name)
    require(os.path.getsize(path) > 0,
            "PostgreSQL TLS handoff file is empty: " + name)
    return path


def _configure_database_tls_handoff_environment(arguments):
    directory = os.path.join(
        _component_root(arguments), "runtime-tls-handoff",
    )
    marker = os.path.join(directory, ".live_postgres_tls_handoff")
    if not os.path.exists(marker):
        return
    require(os.path.isfile(marker) and not os.path.islink(marker),
            "PostgreSQL TLS handoff marker is unsafe")
    mappings = (
        ("POSTGRES_CA_SOURCE", "ca.crt"),
        ("POSTGRES_PRIMARY_CERT_SOURCE", "primary.crt"),
        ("POSTGRES_PRIMARY_KEY_SOURCE", "primary.key"),
        ("POSTGRES_STANDBY_CERT_SOURCE", "standby.crt"),
        ("POSTGRES_STANDBY_KEY_SOURCE", "standby.key"),
    )
    for environment_name, name in mappings:
        os.environ[environment_name] = _postgres_tls_handoff_file(
            arguments, name,
        )


def _handoff_live_database_tls(arguments, profile):
    handoff = os.path.join(
        _component_root(arguments), "scripts", "handoff_live_tls.sh",
    )
    require(os.path.isfile(handoff) and not os.path.islink(handoff),
            "database TLS handoff hook is missing or unsafe")
    require(profile in ("test", "production"),
            "database deployment profile is invalid")
    run((handoff, profile), cwd=_component_root(arguments))
    _configure_database_tls_handoff_environment(arguments)


def _handoff_live_mqtt_pki(arguments, profile):
    handoff = os.path.join(
        _component_root(arguments), "scripts", "handoff_live_pki.sh",
    )
    require(os.path.isfile(handoff) and not os.path.islink(handoff),
            "MQTT runtime-material handoff hook is missing or unsafe")
    require(profile in ("test", "production"),
            "MQTT deployment profile is invalid")
    run((handoff, profile), cwd=_component_root(arguments))


def _backup_manifest_path(arguments):
    return os.path.join(arguments.state_dir, "backups", arguments.plan_id,
                        arguments.target_id, arguments.component + ".json")


def _backup_control_plane(arguments):
    if arguments.component == "database":
        backup = os.path.join(_component_root(arguments), "scripts", "backup.sh")
        require(os.path.isfile(backup), "database backup hook is missing")
        run((backup,), cwd=_component_root(arguments))
        atomic_json(_backup_manifest_path(arguments), {
            "component": "database", "database_backup": "completed",
            "reversible": False,
        })
        return
    application_backup = None
    if arguments.component == "mcp_controller_server":
        run((_script(arguments, "backup.sh"),), cwd=_component_root(arguments))
        application_backup = "completed"
    images = sorted(set(filter(None, _docker_compose(
        arguments, "config", "--images",
    ).splitlines())))
    require(images, "component compose model has no images")
    mappings = []
    plan_slug = re.sub(r"[^a-z0-9_.-]", "-", arguments.plan_id.lower())[:32]
    for index, image in enumerate(images, 1):
        image_id = run(("docker", "image", "inspect", "--format", "{{.Id}}", image)).strip()
        require(image_id, "running image cannot be resolved: " + image)
        backup_tag = "automation-admin-rollback/{}-{}-{}:snapshot".format(
            plan_slug, arguments.component, index,
        )
        run(("docker", "tag", image_id, backup_tag))
        mappings.append({"image": image, "image_id": image_id,
                         "backup_tag": backup_tag})
    atomic_json(_backup_manifest_path(arguments), {
        "component": arguments.component,
        "reversible": True,
        "images": mappings,
        "application_backup": application_backup,
    })


def _rollback_control_plane(arguments):
    require(arguments.component != "database",
            "database rollback is intentionally manual (expand/contract policy)")
    path = _backup_manifest_path(arguments)
    require(os.path.isfile(path), "verified rollback manifest is missing")
    with open(path, "r", encoding="utf-8") as stream:
        manifest = json.load(stream)
    require(manifest.get("reversible") is True and manifest.get("images"),
            "rollback manifest is not reversible")
    for item in manifest["images"]:
        observed = run((
            "docker", "image", "inspect", "--format", "{{.Id}}",
            item["backup_tag"],
        )).strip()
        require(observed == item["image_id"], "rollback image snapshot drifted")
        run(("docker", "tag", item["backup_tag"], item["image"]))
    _docker_compose(
        arguments, "up", "-d", "--no-build", "--force-recreate",
        *RUNTIME_SERVICES[arguments.component],
    )
    _wait_runtime_services(arguments)


def control_plane_action(arguments, options):
    require(options.get("transport", "local") == "local",
            "control-plane component adapter must run locally on its target")
    _assert_local_target(arguments.host)
    _configure_control_plane_environment(arguments, options)
    if arguments.action == "preflight":
        run(("docker", "compose", "version"))
        if arguments.component == "mcp_controller_server":
            run((_script(arguments, "preflight.sh"),), cwd=_component_root(arguments))
        else:
            _docker_compose(arguments, "config", "--quiet")
    elif arguments.action == "backup":
        _backup_control_plane(arguments)
    elif arguments.action == "apply":
        profile = options.get("deployment_profile", "test")
        name = "deploy.sh" if profile == "production" else "deploy_test.sh"
        if arguments.component == "database":
            _handoff_live_database_secrets(arguments)
            _handoff_live_database_tls(arguments, profile)
        elif arguments.component == "mqtt":
            _handoff_live_mqtt_pki(arguments, profile)
            _configure_control_plane_environment(arguments, options)
        run((_script(arguments, name),), cwd=_component_root(arguments))
    elif arguments.action == "ready":
        _wait_runtime_services(arguments)
    elif arguments.action == "status":
        _check_runtime_services(arguments)
    elif arguments.action == "test":
        test_name = "test_running.sh" if arguments.component == "database" else "test.sh"
        if arguments.component == "database":
            require(test_name == "test_running.sh",
                    "database live test must remain non-deploying")
        run((_script(arguments, test_name),), cwd=_component_root(arguments))
    elif arguments.action == "restart":
        _docker_compose(arguments, "restart", *RUNTIME_SERVICES[arguments.component])
        _wait_runtime_services(arguments)
    elif arguments.action == "rollback":
        _rollback_control_plane(arguments)
    else:
        raise AdapterError("unsupported control-plane action: " + arguments.action)


def _ssh_target(arguments, options):
    require(options.get("transport", "ssh") == "ssh",
            "worker component requires SSH transport")
    require(arguments.host and bool(SSH_TARGET_PATTERN.fullmatch(arguments.host)),
            "worker SSH target is unsafe")
    return arguments.host


def _ssh(arguments, options, remote_command, input_text=None):
    target = _ssh_target(arguments, options)
    return run((
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=15",
        target, remote_command,
    ), input_text=input_text)


def _worker_values(arguments, options):
    node_id = arguments.node_id
    require(bool(IDENTIFIER_PATTERN.fullmatch(node_id)), "worker node_id is invalid")
    worker_path = options.get(
        "worker_path", "python-skripte_22/__NEW_FILES__/automation_system_v0101",
    )
    values = {
        "worker_path": worker_path,
        "site_id": options.get("site_id", "site-main"),
        "node_id": node_id,
        "profile": options.get("profile", "worker_simulation"),
        "pki_stage_path": options.get(
            "pki_stage_path", ".cache/automation-control-plane/pki/site-main/" + node_id,
        ),
        "pki_source_path": options.get("pki_source_path", ""),
        "worker_log_path": options.get(
            "worker_log_path",
            ".local/state/automation-worker/site-main/{}/worker.log".format(node_id),
        ),
        "config_drift_policy": options.get("config_drift_policy", "preserve"),
    }
    for name, value in values.items():
        if name == "pki_source_path" and not value:
            continue
        require(value and "\n" not in value and "\r" not in value,
                "unsafe Worker option: " + name)
    require(values["config_drift_policy"] in ("preserve", "strict"),
            "invalid Worker config drift policy")
    return values


def _worker_archive_identity(path):
    require(os.path.isfile(path) and not os.path.islink(path),
            "planned Worker archive is missing or unsafe")
    with zipfile.ZipFile(path, "r") as archive:
        version_names = [name for name in archive.namelist() if name.endswith("/VERSION")]
        require(len(version_names) == 1, "Worker archive VERSION entry is ambiguous")
        version = archive.read(version_names[0]).decode("utf-8").strip()
        require(bool(IDENTIFIER_PATTERN.fullmatch(version)), "Worker archive version is invalid")
        root = version_names[0][:-len("VERSION")]
        sums_name = root + "SHA256SUMS"
        require(sums_name in archive.namelist(), "Worker archive SHA256SUMS is missing")
        manifest_sha256 = hashlib.sha256(archive.read(sums_name)).hexdigest()
    return version, sha256_file(path), manifest_sha256


def _worker_pki_sources(arguments, options):
    values = _worker_values(arguments, options)
    configured = values["pki_source_path"]
    if configured:
        require(os.path.isabs(configured),
                "Worker pki_source_path must be an absolute local path")
        require(os.path.isdir(configured) and not os.path.islink(configured),
                "Worker PKI source directory is missing or unsafe")
        sources = {
            "ca.crt": os.path.join(configured, "ca.crt"),
            "mqtt.crt": os.path.join(configured, "mqtt.crt"),
            "mqtt.key": os.path.join(configured, "mqtt.key"),
        }
    else:
        test_root = os.path.join(
            arguments.release_root, "mqtt_broker", "test-pki",
        )
        marker = os.path.join(test_root, ".test_mqtt_pki")
        require(os.path.isfile(marker) and not os.path.islink(marker),
                "marked test Worker PKI source is unavailable")
        client_root = os.path.join(
            test_root, "clients", "worker-" + values["node_id"],
        )
        sources = {
            "ca.crt": os.path.join(test_root, "ca", "ca.crt"),
            "mqtt.crt": os.path.join(client_root, "mqtt.crt"),
            "mqtt.key": os.path.join(client_root, "mqtt.key"),
        }

    for name, path in sources.items():
        require(os.path.isfile(path) and not os.path.islink(path),
                "Worker PKI source is missing or unsafe: " + name)
        require(os.path.getsize(path) > 0,
                "Worker PKI source is empty: " + name)
    require(os.stat(sources["mqtt.key"]).st_mode & 0o077 == 0,
            "Worker PKI source private-key permissions are too broad")

    run((
        "openssl", "verify", "-purpose", "sslclient",
        "-CAfile", sources["ca.crt"], sources["mqtt.crt"],
    ))
    run((
        "openssl", "x509", "-checkend", "86400", "-noout",
        "-in", sources["mqtt.crt"],
    ))
    expected_identity = "URI:urn:automation:{}:worker:{}".format(
        values["site_id"], values["node_id"],
    )
    subject_alternative_name = captured((
        "openssl", "x509", "-noout", "-ext", "subjectAltName",
        "-in", sources["mqtt.crt"],
    ))
    require(expected_identity in subject_alternative_name,
            "Worker certificate identity does not match the planned target")
    certificate_public_key = captured((
        "openssl", "x509", "-in", sources["mqtt.crt"],
        "-pubkey", "-noout",
    )).strip()
    private_public_key = captured((
        "openssl", "pkey", "-in", sources["mqtt.key"], "-pubout",
    )).strip()
    require(certificate_public_key
            and certificate_public_key == private_public_key,
            "Worker certificate/private-key pair does not match")
    print(
        "Worker PKI source verified: identity={} ca_sha256={} "
        "certificate_sha256={}".format(
            expected_identity, sha256_file(sources["ca.crt"]),
            sha256_file(sources["mqtt.crt"]),
        ),
    )
    return sources


def _stage_worker_pki(arguments, options):
    values = _worker_values(arguments, options)
    sources = _worker_pki_sources(arguments, options)
    require(bool(IDENTIFIER_PATTERN.fullmatch(arguments.target_id)),
            "worker target_id is invalid")
    remote_stage = ".cache/automation-control-plane/admin/{}/{}/worker-pki.incomplete".format(
        arguments.plan_id, arguments.target_id,
    )
    program = r'''
import os, pathlib, shutil, sys
home = pathlib.Path.home().resolve()
def scoped(value):
    candidate = pathlib.Path(value)
    if not candidate.is_absolute():
        candidate = home / candidate
    candidate = pathlib.Path(os.path.abspath(candidate))
    if home not in candidate.parents:
        raise SystemExit("unsafe Worker PKI path")
    current = home
    for part in candidate.relative_to(home).parts[:-1]:
        current = current / part
        if current.exists() and current.is_symlink():
            raise SystemExit("Worker PKI parent path contains a symlink")
    return candidate
stage = scoped(sys.argv[1])
target = scoped(sys.argv[2])
worker = scoped(sys.argv[3])
action = sys.argv[4]
if stage == target or worker == target or worker in target.parents or target in worker.parents:
    raise SystemExit("Worker PKI paths overlap the Worker package")
if action == "prepare":
    if stage.exists():
        if stage.is_symlink() or not stage.is_dir():
            raise SystemExit("unsafe incomplete Worker PKI stage")
        shutil.rmtree(stage)
    stage.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    stage.mkdir(mode=0o700)
elif action == "activate":
    for name in ("ca.crt", "mqtt.crt", "mqtt.key"):
        path = stage / name
        if not path.is_file() or path.is_symlink() or path.stat().st_size == 0:
            raise SystemExit("incomplete transferred Worker PKI")
    os.chmod(stage / "ca.crt", 0o644)
    os.chmod(stage / "mqtt.crt", 0o644)
    os.chmod(stage / "mqtt.key", 0o600)
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if target.exists():
        if target.is_symlink() or not target.is_dir():
            raise SystemExit("unsafe existing Worker PKI stage")
        shutil.rmtree(target)
    os.replace(stage, target)
    os.chmod(target, 0o700)
else:
    raise SystemExit("unsupported Worker PKI staging action")
'''.strip()

    def remote_stage_action(action):
        command = shlex.join((
            "python3", "-c", program, remote_stage,
            values["pki_stage_path"], values["worker_path"], action,
        ))
        _ssh(arguments, options, command)

    remote_stage_action("prepare")
    remote_target = _ssh_target(arguments, options) + ":" + remote_stage + "/"
    for name in ("ca.crt", "mqtt.crt", "mqtt.key"):
        run(("scp", "-q", sources[name], remote_target + name))
    remote_stage_action("activate")
    print("Worker MQTT identity staged for one verified lifecycle apply.")


def _lifecycle(arguments, options, action):
    values = _worker_values(arguments, options)
    version, _archive_sha, manifest_sha = _worker_archive_identity(arguments.worker_archive)
    lifecycle = os.path.join(arguments.release_root, "tools", "worker_remote_lifecycle.sh")
    require(os.path.isfile(lifecycle) and not os.path.islink(lifecycle),
            "Worker lifecycle hook is missing or unsafe")
    with open(lifecycle, "r", encoding="utf-8") as stream:
        program = stream.read()
    command = shlex.join((
        "bash", "-s", "--", action, values["worker_path"], values["site_id"],
        values["node_id"], values["profile"], values["pki_stage_path"],
        values["worker_log_path"], "no", version, manifest_sha,
        values["config_drift_policy"],
    ))
    return _ssh(arguments, options, command, input_text=program)


def _remote_worker_backup(arguments, options, restore=False):
    values = _worker_values(arguments, options)
    program = r'''
import os, pathlib, shutil, sys
home = pathlib.Path.home().resolve()
source = (home / sys.argv[1]).resolve()
backup = (home / ".local/state/automation-control-plane/admin-backups" /
          sys.argv[2] / sys.argv[3] / "worker").resolve()
for path in (source, backup):
    if home not in path.parents:
        raise SystemExit("unsafe Worker backup path")
if sys.argv[4] == "backup":
    if not source.is_dir() or source.is_symlink():
        raise SystemExit("active Worker release is unavailable")
    backup.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    if not backup.exists():
        stage = backup.with_name("worker.incomplete")
        if stage.exists():
            shutil.rmtree(stage)
        shutil.copytree(source, stage, symlinks=False)
        os.replace(stage, backup)
else:
    if not backup.is_dir() or backup.is_symlink():
        raise SystemExit("verified Worker backup is unavailable")
    suffix = sys.argv[2][:12]
    failed = source.with_name(source.name + "_failed_" + suffix)
    stage = source.with_name(source.name + "_restore_" + suffix)
    if stage.exists():
        shutil.rmtree(stage)
    shutil.copytree(backup, stage, symlinks=False)
    if source.exists():
        if failed.exists():
            raise SystemExit("Worker failed-release archive already exists")
        os.replace(source, failed)
    os.replace(stage, source)
'''.strip()
    remote = shlex.join((
        "python3", "-c", program, values["worker_path"], arguments.plan_id,
        arguments.target_id, "restore" if restore else "backup",
    ))
    _ssh(arguments, options, remote)


def _install_worker(arguments, options):
    values = _worker_values(arguments, options)
    version, archive_sha, manifest_sha = _worker_archive_identity(arguments.worker_archive)
    remote_dir = ".cache/automation-control-plane/admin/{}/{}".format(
        arguments.plan_id, arguments.target_id,
    )
    remote_archive = remote_dir + "/" + version + ".zip"
    _ssh(arguments, options, shlex.join(("mkdir", "-p", remote_dir)))
    run((
        "scp", "-q", arguments.worker_archive,
        _ssh_target(arguments, options) + ":" + remote_archive,
    ))
    installer = os.path.join(arguments.release_root, "tools", "install_worker_release.py")
    require(os.path.isfile(installer) and not os.path.islink(installer),
            "Worker installer is missing or unsafe")
    with open(installer, "r", encoding="utf-8") as stream:
        program = stream.read()
    install_root = values["worker_path"].rsplit("/", 1)[0]
    command = shlex.join((
        "python3", "-", "--archive", remote_archive, "--home", ".",
        "--install-root", install_root, "--old-root", "__OLD__",
        "--expected-version", version, "--expected-sha256", archive_sha,
        "--expected-manifest-sha256", manifest_sha,
        "--config-drift-policy", values["config_drift_policy"],
    ))
    _ssh(arguments, options, command, input_text=program)


def _worker_test(arguments):
    admission = os.path.join(arguments.release_root, "test_worker_auto_admission.sh")
    telemetry = os.path.join(arguments.release_root, "test_worker_sensor_telemetry.sh")
    for path in (admission, telemetry):
        require(os.path.isfile(path) and not os.path.islink(path),
                "Worker acceptance hook is missing or unsafe")
        run((path, arguments.node_id, "120"), cwd=arguments.release_root)


def worker_action(arguments, options):
    if arguments.action == "preflight":
        _worker_archive_identity(arguments.worker_archive)
        _worker_pki_sources(arguments, options)
        _ssh(arguments, options, "command -v python3 && command -v bash")
    elif arguments.action == "backup":
        _remote_worker_backup(arguments, options)
    elif arguments.action == "apply":
        _stage_worker_pki(arguments, options)
        _lifecycle(arguments, options, "stop-compatible")
        _install_worker(arguments, options)
        _lifecycle(arguments, options, "install-start")
    elif arguments.action == "ready":
        _lifecycle(arguments, options, "verify-active")
    elif arguments.action == "status":
        _lifecycle(arguments, options, "verify-active")
    elif arguments.action == "test":
        _worker_test(arguments)
    elif arguments.action == "restart":
        _lifecycle(arguments, options, "restart")
        _lifecycle(arguments, options, "verify-active")
        _worker_test(arguments)
    elif arguments.action == "rollback":
        _lifecycle(arguments, options, "stop")
        _remote_worker_backup(arguments, options, restore=True)
        _lifecycle(arguments, options, "restart")
    else:
        raise AdapterError("unsupported Worker action: " + arguments.action)


def parser():
    result = argparse.ArgumentParser(description="Automation component lifecycle adapter")
    result.add_argument("--release-root")
    result.add_argument("--state-dir")
    result.add_argument("--action")
    result.add_argument("--component", choices=COMPONENTS)
    result.add_argument("--target-id")
    result.add_argument("--host", default="")
    result.add_argument("--node-id", default="")
    result.add_argument("--target-options", default="{}")
    result.add_argument("--worker-archive")
    result.add_argument("--plan-id")
    result.add_argument("--step-id")
    result.add_argument("--idempotency-key")
    result.add_argument("--fencing-token", type=int)
    result.add_argument("--self-test", action="store_true")
    return result


def self_test():
    assert parse_options('{"transport":"local"}') == {"transport": "local"}
    try:
        parse_options("[]")
    except AdapterError:
        pass
    else:
        raise AssertionError("non-object target options accepted")
    assert bool(IDENTIFIER_PATTERN.fullmatch("fn-01"))
    assert bool(SSH_TARGET_PATTERN.fullmatch("automation@worker-01.test.invalid"))
    assert not SSH_TARGET_PATTERN.fullmatch("-oProxyCommand=unsafe")
    assert "mcp_controller_server" in COMPONENTS
    assert RUNTIME_SERVICES["mcp_controller_server"] == ("mcp-controller-server",)
    print("PASS component adapter self-test")
    return 0


def main(argv=None):
    arguments = parser().parse_args(argv)
    if arguments.self_test:
        return self_test()
    required = (
        "release_root", "state_dir", "action", "component", "target_id",
        "worker_archive", "plan_id", "step_id", "idempotency_key",
        "fencing_token",
    )
    for name in required:
        require(getattr(arguments, name) not in (None, ""),
                "missing required argument: " + name)
    arguments.release_root = os.path.abspath(arguments.release_root)
    arguments.state_dir = os.path.abspath(arguments.state_dir)
    require(os.path.isdir(arguments.release_root), "release root is unavailable")
    require(not os.path.commonpath((arguments.release_root, arguments.state_dir))
            == arguments.release_root,
            "adapter state must be outside the immutable release root")
    require(bool(IDENTIFIER_PATTERN.fullmatch(arguments.target_id)),
            "target_id is invalid")
    require(bool(HASH_PATTERN.fullmatch(arguments.plan_id)), "plan_id is invalid")
    require(arguments.fencing_token > 0, "fencing token must be positive")
    options = parse_options(arguments.target_options)
    state_path = None
    replay = False
    if arguments.action in MUTATING_ACTIONS:
        state_path, replay = claim_mutation(arguments)
        if replay:
            return 0
    try:
        if arguments.component == "worker":
            worker_action(arguments, options)
        else:
            control_plane_action(arguments, options)
    except Exception:
        if state_path:
            finish_mutation(state_path, arguments, "failed")
        raise
    if state_path:
        finish_mutation(state_path, arguments, "complete")
    print("PASS component adapter {} {} {}".format(
        arguments.action, arguments.component, arguments.target_id,
    ))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (AdapterError, OSError, ValueError, zipfile.BadZipFile) as exc:
        print("BLOCKED: " + str(exc), file=sys.stderr)
        raise SystemExit(2)

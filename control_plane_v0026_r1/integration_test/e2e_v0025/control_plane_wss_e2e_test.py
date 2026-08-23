#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""TLS-verifying WSS-primary E2E for Automation Control Plane v0025."""

import argparse
import asyncio
import base64
import copy
import datetime
import fcntl
import getpass
import hashlib
import http.client
import json
import os
import pathlib
import re
import secrets
import signal
import ssl
import stat
import subprocess
import sys
import tempfile
import time
import traceback
import urllib.parse
import uuid

from http.cookies import SimpleCookie

import websockets
from websockets.exceptions import ConnectionClosed, InvalidStatus


VERSION = "0.25.0-e2e.3"
PROTOCOL = "automation.proxy.websocket.v1"
USER_AGENT = "automation-control-plane-e2e/" + VERSION
STATUS_CODES = {"PASSED": 0, "FAILED": 1, "BLOCKED": 3, "SKIPPED": 4}
ALL_ACTIONS = (
    "users.list", "user.create", "roles.list", "permissions.list",
    "api_tokens.list", "api_tokens.create", "api_tokens.revoke", "sites.list",
    "nodes.list", "node.get", "runtime.status", "config.get", "config.history",
    "config.diff", "config.propose", "config.rollback", "actuator.request",
    "operation.get", "audit.list", "node.drain", "node.state.set",
    "node.admission.approve", "terminal.execute",
)
TERMINAL_STATES = (
    "CONFIRMED", "WRITE_UNVERIFIED", "FAILED", "REJECTED", "UNCERTAIN", "CANCELLED",
)
SENSITIVE_NAMES = (
    "password", "token", "authorization", "cookie", "csrf", "secret", "private_key",
)
MAX_HTTP_BODY = 8 * 1024 * 1024
RECOVERY_FAILURE_MAX_LENGTH = 2048
EXTERNAL_LOG_MAX_START_AGE_S = 300.0
EVENT_UPDATE_PENDING_LIMIT = 256
CONFIG_CAS_ATTEMPTS = 8
CROSS_FIXTURE_SHA256 = "9c6e8b9f4128c4b061fcfb9bd2a5a2d0fc9ae2c2f9cfe7c97155bbc6bc74c2f2"
CORE_REQUIRED_CHECKS = (
    "HTTPS-READY", "TLS-001", "TLS-002", "TLS-003", "AUTH", "SEC-001",
    "SEC-002", "SEC-003", "SEC-006", "WSS-CONTROL", "PATH-ORIGIN",
    "PTH-001", "PTH-002", "PTH-003", "PTH-004", "WSS-EVENTS",
    "CMD-READS", "RD-001", "RD-002", "RD-003", "HTTP-001", "HTTP-002",
    "WORKER-FN01", "EVT-001", "EVT-002", "EVT-003",
    "EVT-001A", "EVT-004", "EVT-004A", "SEC-005", "SEC-007", "SEC-008",
    "CMD-NEGATIVE", "SEC-004", "CFG-001", "CFG-002", "CFG-003",
    "CFG-004", "CFG-005", "CFG-006", "CONFIG-DIFF", "ACT-001", "ACT-002",
    "ACT-003", "ACT-004", "PAR-001", "CROSS-WORKER", "ACT-RESTORE",
    "CFG-RESTORE", "CFG-MATRIX", "SEC-LOG", "SEC-009",
)
FULL_LAB_REQUIRED_CHECKS = (
    "XWK-000", "FN02-REGISTRY-PREFLIGHT", "SIM-001", "FN02-ACTIVATE",
    "SIM-002a", "XWK-001", "SIM-002b", "XWK-002", "XWK-003",
    "XWK-SENSOR-RESTORE", "FN02-MAINTENANCE", "SIM-STOP",
    "XWK-LAUNCHER-RESTORE", "XST-001", "XWK-007",
)


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _sensitive_name(name):
    lowered = str(name or "").lower()
    return any(fragment in lowered for fragment in SENSITIVE_NAMES)


def _sanitize(value, **kwargs):
    key = kwargs.get("key")
    if key is not None and _sensitive_name(key):
        return "[REDACTED]"
    if isinstance(value, dict):
        return {str(name): _sanitize(item, key=name) for name, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_sanitize(item) for item in value]
    if isinstance(value, str) and len(value) > 4096:
        return value[:4096] + "[TRUNCATED]"
    return value


def _log(context, level, message, **kwargs):
    item = {
        "timestamp": _utc_now(),
        "level": str(level),
        "message": str(message),
    }
    if "details" in kwargs:
        item["details"] = _sanitize(kwargs["details"])
    line = json.dumps(item, ensure_ascii=False, sort_keys=True, default=str)
    handle = context.get("log_handle")
    if handle:
        handle.write(line + "\n")
        handle.flush()
    if context.get("verbose") or level in ("INFO", "WARNING", "ERROR"):
        print("{} | {:7s} | {}".format(item["timestamp"], str(level), message))


def _load_json(path, label):
    try:
        value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("BLOCKED: invalid {}: {}".format(label, exc)) from exc
    if not isinstance(value, dict):
        raise RuntimeError("BLOCKED: {} must contain a JSON object".format(label))
    return value


def _file_sha256(path):
    digest = hashlib.sha256()
    with pathlib.Path(path).open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _tail_sha256(path):
    path = pathlib.Path(path)
    with path.open("rb") as handle:
        size = handle.seek(0, os.SEEK_END)
        handle.seek(max(0, size - 65536), os.SEEK_SET)
        return hashlib.sha256(handle.read()).hexdigest()


def _assert_distinct_paths(named_paths):
    resolved = {}
    identities = {}
    for name, value in named_paths.items():
        path = pathlib.Path(value).expanduser().resolve()
        previous = resolved.get(path)
        if previous is not None:
            raise RuntimeError(
                "BLOCKED: file paths alias each other: {} and {}".format(previous, name)
            )
        resolved[path] = name
        try:
            details = path.stat()
        except FileNotFoundError:
            continue
        identity = (details.st_dev, details.st_ino)
        hardlink = identities.get(identity)
        if hardlink is not None:
            raise RuntimeError(
                "BLOCKED: file paths hardlink-alias each other: {} and {}".format(
                    hardlink, name
                )
            )
        identities[identity] = name
    return {name: str(path) for path, name in resolved.items()}


def _capture_external_log(path, label):
    path = pathlib.Path(path).expanduser().resolve()
    captured_ns = time.time_ns()
    try:
        details = path.stat()
    except OSError as exc:
        raise RuntimeError("BLOCKED: {} is unavailable: {}".format(label, path)) from exc
    age_s = (captured_ns - details.st_mtime_ns) / 1_000_000_000.0
    if not stat.S_ISREG(details.st_mode) or details.st_size <= 0:
        raise RuntimeError("BLOCKED: {} must be a non-empty regular file".format(label))
    if age_s < -5.0 or age_s > EXTERNAL_LOG_MAX_START_AGE_S:
        raise RuntimeError("BLOCKED: {} is not a fresh live log".format(label))
    try:
        tail_sha256 = _tail_sha256(path)
    except OSError as exc:
        raise RuntimeError("BLOCKED: {} is not readable".format(label)) from exc
    return {
        "label": label,
        "path": str(path),
        "captured_at_ns": captured_ns,
        "size": details.st_size,
        "mtime_ns": details.st_mtime_ns,
        "device": details.st_dev,
        "inode": details.st_ino,
        "tail_sha256": tail_sha256,
    }


def _validate_scan_file(path, label):
    path = pathlib.Path(path).expanduser().resolve()
    try:
        details = path.stat()
    except OSError as exc:
        raise RuntimeError("BLOCKED: {} is unavailable: {}".format(label, path)) from exc
    if not stat.S_ISREG(details.st_mode):
        raise RuntimeError("BLOCKED: {} must be a readable regular file".format(label))
    try:
        with path.open("rb") as handle:
            handle.read(1)
    except OSError as exc:
        raise RuntimeError("BLOCKED: {} is unreadable: {}".format(label, path)) from exc
    return path


def _verify_signed_broker_proof(public_key_path, proof, expected_signer_sha256=None):
    if proof.get("signature_algorithm") != "ed25519":
        raise RuntimeError("FAILED: Broker proof signature algorithm differs")
    try:
        signature = base64.b64decode(str(proof.get("signature") or ""), validate=True)
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey
        public_bytes = pathlib.Path(public_key_path).read_bytes()
        public_key = serialization.load_pem_public_key(public_bytes)
        if not isinstance(public_key, Ed25519PublicKey):
            raise TypeError("not an Ed25519 key")
        signed = copy.deepcopy(proof)
        signed.pop("signature", None)
        payload = json.dumps(
            signed, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        public_key.verify(signature, payload)
    except Exception as exc:
        raise RuntimeError("FAILED: Broker proof signature verification failed") from exc
    signer_sha256 = hashlib.sha256(public_bytes).hexdigest()
    if expected_signer_sha256 is not None \
            and signer_sha256 != str(expected_signer_sha256):
        raise RuntimeError("FAILED: Broker proof public key differs from the pinned authority")
    return signer_sha256


def _load_schema_validators(schema_directory):
    """Load the shipped Draft 2020-12 contracts with offline reference resolution."""
    try:
        import jsonschema
        import referencing
    except ImportError as exc:
        raise RuntimeError(
            "BLOCKED: jsonschema from requirements-e2e.txt is unavailable"
        ) from exc
    schemas = {}
    resources = []
    for path in sorted(pathlib.Path(schema_directory).glob("*.schema.json")):
        schema = _load_json(path, "JSON schema " + path.name)
        schema_id = schema.get("$id")
        if not isinstance(schema_id, str) or not schema_id:
            raise RuntimeError("BLOCKED: JSON schema has no stable $id: " + path.name)
        try:
            jsonschema.Draft202012Validator.check_schema(schema)
            resource = referencing.Resource.from_contents(schema)
        except Exception as exc:
            raise RuntimeError("BLOCKED: invalid JSON schema: " + path.name) from exc
        name = path.name.removesuffix(".schema.json")
        schemas[name] = schema
        resources.append((schema_id, resource))
    required = {
        "action_payloads", "command_request", "command_response",
        "event_envelope", "event_filter_update", "event_response",
        "event_subscription", "recovery_state",
    }
    if set(schemas) != required:
        raise RuntimeError("BLOCKED: E2E JSON schema set differs from the v0025 contract")
    registry = referencing.Registry().with_resources(resources)
    checker = jsonschema.FormatChecker()
    validators = {}
    for name, schema in schemas.items():
        validators[name] = jsonschema.Draft202012Validator(
            schema, registry=registry, format_checker=checker
        )
    return validators


def _validate_schema(context, name, value):
    validators = context.get("schema_validators") or {}
    validator = validators.get(name)
    if validator is None:
        return True
    try:
        error = next(validator.iter_errors(value), None)
    except Exception as exc:
        raise RuntimeError("FAILED: JSON schema resolution failed for " + str(name)) from exc
    if error is None:
        return True
    location = "/".join(str(item) for item in error.absolute_path) or "<root>"
    keyword = str(error.validator or "contract")
    pending = [error]
    leaves = []
    while pending:
        current = pending.pop(0)
        if current.context:
            pending.extend(current.context)
        else:
            leaves.append(current)
    deepest = max((len(item.absolute_path) for item in leaves), default=0)
    details = []
    for item in leaves:
        if len(item.absolute_path) != deepest:
            continue
        item_location = "/".join(
            str(token) for token in item.absolute_path) or "<root>"
        detail = item_location + ":" + str(item.validator or "contract")
        if detail not in details:
            details.append(detail)
        if len(details) >= 8:
            break
    suffix = "; details=" + ",".join(details) if details else ""
    raise RuntimeError(
        "FAILED: {} JSON contract failed at {} ({}){}".format(
            name, location, keyword, suffix)
    )


def _fsync_directory(path):
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    descriptor = os.open(str(path), flags)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _atomic_json(path, value, **kwargs):
    path = pathlib.Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    if kwargs.get("exclusive"):
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        temporary = None
    else:
        temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
        descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True, default=str)
        os.write(descriptor, payload.encode("utf-8") + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    if temporary:
        os.replace(temporary, path)
    os.chmod(path, 0o600)
    _fsync_directory(path.parent)


def _parse_server_url(value, scheme):
    parsed = urllib.parse.urlsplit(value)
    if parsed.scheme != scheme or not parsed.hostname:
        raise RuntimeError("BLOCKED: URL must be an absolute {} URL".format(scheme))
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise RuntimeError("BLOCKED: URL credentials, query and fragment are forbidden")
    if parsed.path not in ("", "/"):
        raise RuntimeError("BLOCKED: server URL must not contain an endpoint path")
    port = parsed.port or 443
    return {
        "url": "{}://{}".format(scheme, parsed.netloc),
        "host": parsed.hostname,
        "port": port,
        "authority": parsed.netloc,
    }


def _validate_arguments(arguments):
    https = _parse_server_url(arguments.https_url, "https")
    wss = _parse_server_url(arguments.wss_url, "wss")
    if https["host"] == wss["host"] and https["port"] == wss["port"]:
        raise RuntimeError("BLOCKED: direct HTTPS and WSS listeners must use separate ports")
    origin = urllib.parse.urlsplit(arguments.origin)
    if origin.scheme != "https" or not origin.hostname:
        raise RuntimeError("BLOCKED: --origin must be an absolute https Origin")
    if origin.path not in ("", "/") or origin.query or origin.fragment:
        raise RuntimeError("BLOCKED: --origin must not contain path, query or fragment")
    ca_file = pathlib.Path(arguments.ca_file).expanduser().resolve()
    if not ca_file.is_file():
        raise RuntimeError("BLOCKED: trusted Proxy CA is unavailable: " + str(ca_file))
    if arguments.profile not in ("core", "full-lab"):
        raise RuntimeError("BLOCKED: profile must be core or full-lab")
    for name in ("http_timeout", "command_timeout", "event_timeout", "operation_timeout"):
        if float(getattr(arguments, name)) <= 0:
            raise RuntimeError("BLOCKED: --{} must be positive".format(name.replace("_", "-")))
    if not 128 <= int(arguments.parallel_clients) <= 512:
        raise RuntimeError("BLOCKED: --parallel-clients must be between 128 and 512")
    if not 30.0 <= float(arguments.fn02_heartbeat_stale_s) <= 300.0:
        raise RuntimeError(
            "BLOCKED: --fn02-heartbeat-stale-s must be between 30 and 300 seconds"
        )
    if not str(arguments.approved_actuator_host).strip():
        raise RuntimeError("BLOCKED: --approved-actuator-host must not be empty")
    if not 1 <= int(arguments.approved_actuator_port) <= 65535:
        raise RuntimeError("BLOCKED: --approved-actuator-port is invalid")
    cross_fixture = pathlib.Path(arguments.cross_fixture).expanduser().resolve()
    if not cross_fixture.is_file() or _file_sha256(cross_fixture) != CROSS_FIXTURE_SHA256:
        raise RuntimeError("BLOCKED: cross-worker fixture differs from the approved v0025 lab fixture")
    named_paths = {
        "--result-file": arguments.result_file,
        "--log-file": arguments.log_file,
        "--recovery-file": arguments.recovery_file,
    }
    required_logs = (
        ("--proxy-log-source", arguments.proxy_log_source),
        ("--protocol-log-source", arguments.protocol_log_source),
    )
    baselines = []
    for label, value in required_logs:
        if not value:
            raise RuntimeError("BLOCKED: {} is required for SEC-009".format(label))
        named_paths[label] = value
    for index, value in enumerate(arguments.secret_log_source):
        named_paths["--secret-log-source[{}]".format(index)] = value
    _assert_distinct_paths(named_paths)
    for label, value in required_logs:
        baselines.append(_capture_external_log(value, label))
    for index, value in enumerate(arguments.secret_log_source):
        _validate_scan_file(value, "--secret-log-source[{}]".format(index))
    return {
        "https": https,
        "wss": wss,
        "ca_file": ca_file,
        "external_log_baselines": baselines,
    }


def _lock_descriptor(path):
    flags = os.O_RDWR | os.O_CREAT
    flags |= getattr(os, "O_CLOEXEC", 0)
    flags |= getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags, 0o600)
    os.fchmod(descriptor, 0o600)
    return descriptor


def _acquire_run_locks(context):
    recovery = pathlib.Path(context["recovery_path"])
    recovery.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key = "{}\n{}".format(
        context["servers"]["wss"]["url"], context["arguments"].site_id
    ).encode("utf-8")
    global_name = "automation-control-plane-e2e-{}.lock".format(
        hashlib.sha256(key).hexdigest()
    )
    paths = (
        recovery.with_name(recovery.name + ".runner.lock"),
        pathlib.Path("/tmp") / global_name,
    )
    held = []
    try:
        for path in paths:
            descriptor = _lock_descriptor(path)
            try:
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                os.close(descriptor)
                raise RuntimeError(
                    "BLOCKED: another E2E runner owns {}".format(path)
                ) from exc
            metadata = json.dumps({
                "pid": os.getpid(),
                "run_id": context["run_id"],
                "site_id": context["arguments"].site_id,
                "wss_url": context["servers"]["wss"]["url"],
                "acquired_at": _utc_now(),
            }, sort_keys=True).encode("utf-8") + b"\n"
            os.ftruncate(descriptor, 0)
            os.write(descriptor, metadata)
            os.fsync(descriptor)
            held.append({"descriptor": descriptor, "path": path})
    except Exception:
        for item in reversed(held):
            fcntl.flock(item["descriptor"], fcntl.LOCK_UN)
            os.close(item["descriptor"])
        raise
    context["run_locks"] = held
    return [str(item["path"]) for item in held]


def _release_run_locks(context):
    held = list(context.get("run_locks") or ())
    context["run_locks"] = []
    for item in reversed(held):
        try:
            fcntl.flock(item["descriptor"], fcntl.LOCK_UN)
        finally:
            os.close(item["descriptor"])
    return True


def _ssl_context(ca_file):
    context = ssl.create_default_context(cafile=str(ca_file))
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.check_hostname = True
    context.verify_mode = ssl.CERT_REQUIRED
    return context


def _cookie_header(context):
    return "; ".join(
        "{}={}".format(name, value) for name, value in sorted(context["cookies"].items())
    )


def _capture_cookies(context, headers):
    for name, value in headers:
        if name.lower() != "set-cookie":
            continue
        parsed = SimpleCookie()
        parsed.load(value)
        for cookie_name, morsel in parsed.items():
            if morsel.value:
                context["cookies"][cookie_name] = morsel.value
            else:
                context["cookies"].pop(cookie_name, None)


def _http_headers(context, request, encoded):
    headers = {
        "Accept": "application/json",
        "Origin": context["arguments"].origin,
        "User-Agent": USER_AGENT,
    }
    if encoded is not None:
        headers["Content-Type"] = "application/json"
    bearer = context.get("bearer_token")
    if bearer:
        headers["Authorization"] = "Bearer " + bearer
    cookie = _cookie_header(context)
    if cookie:
        headers["Cookie"] = cookie
    if request.get("csrf"):
        csrf = context["cookies"].get("automation_csrf")
        if not csrf:
            raise RuntimeError("FAILED: authenticated session has no CSRF cookie")
        headers["X-CSRF-Token"] = csrf
    idempotency_key = request.get("idempotency_key")
    if idempotency_key is not None:
        if (not isinstance(idempotency_key, str) or not idempotency_key
                or len(idempotency_key) > 128):
            raise RuntimeError("FAILED: HTTPS Idempotency-Key is invalid")
        headers["Idempotency-Key"] = idempotency_key
    return headers


def _http_request_target(request):
    path = str(request.get("path") or "")
    if not path.startswith("/") or "?" in path or "#" in path:
        raise RuntimeError(
            "FAILED: HTTPS API path must be an absolute path without query or fragment"
        )
    query = request.get("query")
    if query is None:
        return path
    if not isinstance(query, dict) or not query:
        raise RuntimeError("FAILED: HTTPS API query must be a non-empty object")
    if any(not isinstance(name, str) or not name for name in query):
        raise RuntimeError("FAILED: HTTPS API query names must be non-empty strings")
    query_items = []
    for name in sorted(query):
        value = query[name]
        values = value if isinstance(value, (list, tuple)) else (value,)
        if not values:
            raise RuntimeError("FAILED: HTTPS API query values must not be empty")
        for item in values:
            if isinstance(item, bool) or not isinstance(item, (str, int)):
                raise RuntimeError(
                    "FAILED: HTTPS API query values must be strings or integers"
                )
            query_items.append((name, str(item)))
    return path + "?" + urllib.parse.urlencode(query_items)


def _http_request(context, request):
    method = str(request.get("method") or "GET").upper()
    path = _http_request_target(request)
    body = request.get("body")
    encoded = None
    if body is not None:
        encoded = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    headers = _http_headers(context, request, encoded)
    connection = http.client.HTTPSConnection(
        context["servers"]["https"]["host"],
        context["servers"]["https"]["port"],
        context=context["ssl_context"],
        timeout=context["arguments"].http_timeout,
    )
    started = time.monotonic()
    try:
        connection.request(method, path, body=encoded, headers=headers)
        response = connection.getresponse()
        raw = response.read(MAX_HTTP_BODY + 1)
        response_headers = response.getheaders()
    except (OSError, ssl.SSLError, http.client.HTTPException) as exc:
        raise RuntimeError("FAILED: HTTPS {} {} failed: {}".format(method, path, exc)) from exc
    finally:
        connection.close()
    if len(raw) > MAX_HTTP_BODY:
        raise RuntimeError("FAILED: HTTPS response exceeds bounded E2E limit")
    _capture_cookies(context, response_headers)
    try:
        result = json.loads(raw.decode("utf-8")) if raw else {}
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("FAILED: HTTPS {} returned invalid JSON".format(path)) from exc
    _log(context, "DEBUG", "HTTPS {} {} -> {}".format(method, path, response.status), details={
        "elapsed_s": round(time.monotonic() - started, 3),
        "response": result,
    })
    if request.get("expected_status") is not None:
        if response.status != int(request["expected_status"]):
            code = result.get("code") if isinstance(result, dict) else "http_error"
            raise RuntimeError(
                "FAILED: HTTPS {} returned unexpected status {} ({})".format(
                    path, response.status, code))
    elif not 200 <= response.status < 300:
        code = result.get("code") if isinstance(result, dict) else "http_error"
        raise RuntimeError("FAILED: HTTPS {} returned {} {}".format(path, response.status, code))
    if not isinstance(result, dict):
        raise RuntimeError("FAILED: HTTPS JSON root must be an object")
    return {"status": response.status, "body": result, "headers": response_headers}


def _authenticate(context):
    environment_name = context["arguments"].bearer_token_env
    bearer = os.environ.get(environment_name) if environment_name else None
    if bearer:
        context["bearer_token"] = bearer
        _log(context, "INFO", "Using bearer authentication from protected environment")
        mode = "bearer"
    else:
        password = os.environ.get(context["arguments"].password_env)
        if password is None and sys.stdin.isatty():
            password = getpass.getpass("Password for Proxy user {}: ".format(
                context["arguments"].username
            ))
        if not password:
            raise RuntimeError(
                "BLOCKED: no bearer token or interactive/environment session password is available"
            )
        context.setdefault("authentication_canaries", []).append(password)
        response = _http_request(context, {
            "method": "POST",
            "path": "/api/v1/auth/login",
            "body": {"username": context["arguments"].username, "password": password},
        })
        password = ""
        if not context["cookies"].get("automation_session"):
            raise RuntimeError("FAILED: login did not issue automation_session")
        if not context["cookies"].get("automation_csrf"):
            raise RuntimeError("FAILED: login did not issue automation_csrf")
        if response["body"].get("ok") is not True:
            raise RuntimeError("FAILED: Proxy login result is not ok")
        mode = "session"
    session = _http_request(context, {"method": "GET", "path": "/api/v1/auth/session"})
    principal = session["body"].get("principal")
    subject = str(principal.get("subject") or "") if isinstance(principal, dict) else ""
    try:
        normalized_subject = str(uuid.UUID(subject))
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("FAILED: authenticated principal has no stable UUID subject") from exc
    context["principal_subject"] = normalized_subject
    return {"mode": mode, "principal_subject": normalized_subject}


def _logout(context):
    if not context["cookies"].get("automation_session"):
        return True
    try:
        _http_request(context, {
            "method": "POST", "path": "/api/v1/auth/logout", "body": {}, "csrf": True,
        })
        return True
    except Exception as exc:
        _log(context, "WARNING", "Session logout failed", details={"error": str(exc)})
        return False
    finally:
        context["cookies"].clear()


def _wss_headers(context):
    if context.get("bearer_token"):
        return {"Authorization": "Bearer " + context["bearer_token"]}
    cookie = _cookie_header(context)
    return {"Cookie": cookie} if cookie else {}


async def _receive_json(context, socket, **kwargs):
    timeout = float(kwargs.get("timeout") or context["arguments"].command_timeout)
    try:
        message = await asyncio.wait_for(socket.recv(), timeout=timeout)
    except TimeoutError as exc:
        raise RuntimeError("FAILED: WSS response timed out") from exc
    if not isinstance(message, str):
        raise RuntimeError("FAILED: WSS server returned a binary frame")
    try:
        value = json.loads(message)
    except json.JSONDecodeError as exc:
        raise RuntimeError("FAILED: WSS server returned invalid JSON") from exc
    if not isinstance(value, dict):
        raise RuntimeError("FAILED: WSS frame root must be an object")
    _log(context, "DEBUG", "WSS frame received", details=value)
    return value


def _pending_event_frames(stream):
    pending = stream.setdefault("pending_events", [])
    if not isinstance(pending, list):
        raise RuntimeError("FAILED: event stream pending-frame state is invalid")
    return pending


def _command_envelope(context, request):
    envelope = {
        "protocol": PROTOCOL,
        "type": "command.request",
        "request_id": request.get("request_id") or "cmd-" + secrets.token_hex(8),
        "action": request["action"],
        "payload": copy.deepcopy(request.get("payload") or {}),
        "ttl_s": float(request.get("ttl_s") or 30.0),
    }
    if request.get("mutation"):
        envelope["idempotency_key"] = request.get("idempotency_key") or (
            context["run_id"] + ":" + request["action"] + ":" + secrets.token_hex(6)
        )
        if not context.get("bearer_token") and not request.get("omit_csrf"):
            envelope["csrf_token"] = request.get(
                "csrf_token", context["cookies"].get("automation_csrf")
            )
    return envelope


async def _ws_command(context, socket, request):
    envelope = _command_envelope(context, request)
    if not request.get("allow_invalid_contract"):
        _validate_schema(context, "command_request", envelope)
    _log(context, "DEBUG", "WSS command send " + request["action"], details=envelope)
    await socket.send(json.dumps(envelope, ensure_ascii=False, separators=(",", ":")))
    accepted = None
    result = None
    deadline = time.monotonic() + context["arguments"].command_timeout
    while time.monotonic() < deadline:
        frame = await _receive_json(context, socket, timeout=deadline - time.monotonic())
        _validate_schema(context, "command_response", frame)
        if frame.get("protocol") != PROTOCOL:
            raise RuntimeError("FAILED: WSS command response protocol differs")
        if frame.get("request_id") != envelope["request_id"]:
            raise RuntimeError("FAILED: WSS command response request_id differs")
        if frame.get("action") not in (None, request["action"]):
            raise RuntimeError("FAILED: WSS command response action differs")
        if frame.get("type") == "command.rejected":
            if request.get("expected_rejection"):
                allowed = tuple(request.get("allowed_codes") or ())
                if allowed and frame.get("code") not in allowed:
                    raise RuntimeError("FAILED: command rejected with unexpected code " + str(
                        frame.get("code")
                    ))
                return {"request": envelope, "rejected": frame}
            retryable = tuple(request.get("retryable_rejection_codes") or ())
            if frame.get("code") in retryable:
                return {"request": envelope, "rejected": frame}
            raise RuntimeError("FAILED: WSS {} rejected: {} {}".format(
                request["action"], frame.get("code"), frame.get("message") or ""
            ))
        if frame.get("type") == "command.accepted":
            accepted = frame
            if request.get("expected_rejection"):
                raise RuntimeError(
                    "FAILED: WSS {} was accepted but a fail-closed rejection was required".format(
                        request["action"]
                    )
                )
            state = str(frame.get("state") or "ACCEPTED").upper()
            if state in ("ACCEPTED", "PENDING"):
                return {"request": envelope, "accepted": accepted, "result": None}
            if state != "COMPLETED" and not request.get("expect_result"):
                return {"request": envelope, "accepted": accepted, "result": None}
        elif frame.get("type") == "command.result":
            result = frame
            if accepted is None:
                raise RuntimeError("FAILED: command.result arrived before command.accepted")
            return {"request": envelope, "accepted": accepted, "result": result["result"]}
        else:
            raise RuntimeError("FAILED: unexpected WSS command response type")
    raise RuntimeError("FAILED: WSS command did not complete")


async def _open_control(context):
    url = context["servers"]["wss"]["url"] + "/ws/v1/control"
    socket = await websockets.connect(
        url,
        ssl=context["ssl_context"],
        origin=context["arguments"].origin,
        additional_headers=_wss_headers(context),
        user_agent_header=USER_AGENT,
        open_timeout=context["arguments"].command_timeout,
        close_timeout=5.0,
        max_size=4 * 1024 * 1024,
        max_queue=16,
        ping_interval=20.0,
        ping_timeout=20.0,
        proxy=None,
    )
    ready = await _receive_json(context, socket)
    _validate_schema(context, "command_response", ready)
    expected = (
        ready.get("protocol") == PROTOCOL
        and ready.get("type") == "connection.ready"
        and ready.get("endpoint") == "control"
    )
    if not expected:
        await socket.close(code=1000)
        raise RuntimeError("FAILED: /ws/v1/control did not send connection.ready")
    return socket


async def _open_events(context, filters, **kwargs):
    url = context["servers"]["wss"]["url"] + "/ws/v1/events"
    socket = await websockets.connect(
        url,
        ssl=context["ssl_context"],
        origin=context["arguments"].origin,
        additional_headers=_wss_headers(context),
        user_agent_header=USER_AGENT,
        open_timeout=context["arguments"].command_timeout,
        close_timeout=5.0,
        max_size=4 * 1024 * 1024,
        max_queue=64,
        ping_interval=20.0,
        ping_timeout=20.0,
        proxy=None,
    )
    request_id = "sub-" + secrets.token_hex(8)
    request = {
        "protocol": PROTOCOL,
        "type": "subscribe",
        "request_id": request_id,
        "filters": copy.deepcopy(filters),
    }
    last_event_id = kwargs.get("last_event_id", context["arguments"].last_event_id)
    if last_event_id is not None:
        request["last_event_id"] = int(last_event_id)
    _validate_schema(context, "event_subscription", request)
    await socket.send(json.dumps(request, ensure_ascii=False, separators=(",", ":")))
    accepted = await _receive_json(context, socket, timeout=context["arguments"].event_timeout)
    _validate_schema(context, "event_response", accepted)
    if (
        accepted.get("protocol") != PROTOCOL
        or accepted.get("type") != "subscription.accepted"
        or accepted.get("request_id") != request_id
        or not isinstance(accepted.get("generation"), int)
    ):
        await socket.close(code=1000)
        raise RuntimeError("FAILED: /ws/v1/events did not accept the exact subscription")
    stream = {
        "socket": socket,
        "accepted": accepted,
        "request": request,
        "pending_events": [],
    }
    if last_event_id is None and kwargs.get("align_to_high_watermark", True):
        high_watermark = accepted.get("replay_high_watermark")
        if isinstance(high_watermark, bool) or not isinstance(high_watermark, int) \
                or high_watermark < 0:
            await socket.close(code=1000)
            raise RuntimeError(
                "FAILED: /ws/v1/events did not expose a valid replay high-water mark"
            )
        await socket.close(code=1000)
        _log(context, "DEBUG", "Event stream aligned to the live high-water mark", details={
            "last_event_id": high_watermark,
        })
        return await _open_events(
            context,
            filters,
            last_event_id=high_watermark,
            align_to_high_watermark=False,
        )
    return stream


async def _update_event_filters(context, stream, filters):
    previous = int(stream["accepted"].get("generation") or 0)
    request = {
        "protocol": PROTOCOL,
        "type": "subscribe",
        "request_id": "sub-update-" + secrets.token_hex(8),
        "filters": copy.deepcopy(filters),
    }
    _validate_schema(context, "event_filter_update", request)
    await stream["socket"].send(json.dumps(
        request, ensure_ascii=False, separators=(",", ":")
    ))
    deadline = time.monotonic() + context["arguments"].event_timeout
    accepted = None
    interleaved = 0
    while time.monotonic() < deadline:
        frame = await _receive_json(
            context, stream["socket"], timeout=deadline - time.monotonic()
        )
        if frame.get("protocol") == PROTOCOL:
            _validate_schema(context, "event_response", frame)
            if (
                frame.get("type") == "subscription.accepted"
                and frame.get("request_id") == request["request_id"]
            ):
                accepted = frame
                break
            if (
                frame.get("type") == "subscription.rejected"
                and frame.get("request_id") == request["request_id"]
            ):
                raise RuntimeError(
                    "FAILED: event filter update was rejected: {}".format(
                        frame.get("code") or "unknown"
                    )
                )
            if frame.get("type") == "replay.resync_required":
                raise RuntimeError(
                    "FAILED: event filter update requires an explicit resynchronisation"
                )
            raise RuntimeError(
                "FAILED: event filter update received an unrelated control frame"
            )
        _validate_schema(context, "event_envelope", frame)
        if frame.get("protocol") != "automation.control.v1":
            raise RuntimeError(
                "FAILED: event filter update received an unsupported interleaved frame"
            )
        if frame.get("site_id") != context["arguments"].site_id:
            raise RuntimeError(
                "FAILED: event escaped the authorized site during filter update"
            )
        pending = _pending_event_frames(stream)
        if interleaved >= EVENT_UPDATE_PENDING_LIMIT:
            raise RuntimeError(
                "FAILED: event filter update interleave buffer exceeded its bound"
            )
        pending.append(frame)
        interleaved += 1
    if accepted is None:
        raise RuntimeError("FAILED: event filter update response timed out")
    if (
        accepted.get("type") != "subscription.accepted"
        or accepted.get("request_id") != request["request_id"]
        or int(accepted.get("generation") or 0) <= previous
        or accepted.get("filters") != request["filters"]
    ):
        raise RuntimeError("FAILED: event filter update was not applied atomically")
    stream["accepted"] = copy.deepcopy(accepted)
    stream["request"]["filters"] = copy.deepcopy(filters)
    return {"previous_generation": previous, "accepted": accepted}


def _event_payload(event):
    payload = event.get("payload")
    if isinstance(payload, dict) and isinstance(payload.get("payload"), dict):
        return payload["payload"]
    return payload if isinstance(payload, dict) else {}


def _event_matches(event, criteria):
    if criteria.get("message_id") and event.get("message_id") != criteria["message_id"]:
        return False
    if criteria.get("event_type") and event.get("event_type") != criteria["event_type"]:
        return False
    payload = _event_payload(event)
    nested = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    for name in (
        "operation_id", "controller_uuid", "actuator_id", "sensor_id",
        "source_node_id", "node_id", "e2e_run_id", "relay_message_id",
        "proposal_id",
    ):
        expected = criteria.get(name)
        actual = payload.get(name, nested.get(name))
        if expected is not None and str(actual) != str(expected):
            return False
    if "value" in criteria and payload.get("value") != criteria["value"]:
        return False
    if criteria.get("simulation") is not None:
        simulation_payload = nested if nested else payload
        if simulation_payload.get("simulation") is not criteria["simulation"]:
            return False
    if criteria.get("terminal") and str(payload.get("terminal_state")) not in TERMINAL_STATES:
        return False
    if criteria.get("after_sequence") is not None:
        sequence = _event_sequence(event)
        if sequence is None or sequence <= int(criteria["after_sequence"]):
            return False
    return True


async def _wait_event(context, stream, criteria):
    deadline = time.monotonic() + float(criteria.get("timeout") or context["arguments"].event_timeout)
    while time.monotonic() < deadline:
        pending = _pending_event_frames(stream)
        if pending:
            event = pending.pop(0)
        else:
            try:
                event = await _receive_json(
                    context, stream["socket"], timeout=deadline - time.monotonic()
                )
            except RuntimeError as exc:
                if str(exc) == "FAILED: WSS response timed out":
                    expected = str(
                        criteria.get("event_type")
                        or criteria.get("message_id")
                        or "event criteria"
                    )
                    raise RuntimeError(
                        "FAILED: matching WSS event timed out: " + expected
                    ) from exc
                raise
        if event.get("protocol") == PROTOCOL:
            _validate_schema(context, "event_response", event)
            if event.get("type") == "replay.resync_required":
                raise RuntimeError("FAILED: event replay requires an explicit resynchronisation")
            raise RuntimeError("FAILED: event endpoint returned an unexpected control frame")
        _validate_schema(context, "event_envelope", event)
        if event.get("protocol") != "automation.control.v1":
            raise RuntimeError("FAILED: event envelope protocol differs")
        if event.get("site_id") != context["arguments"].site_id:
            raise RuntimeError("FAILED: event escaped the authorized site")
        if _event_matches(event, criteria):
            return event
    raise RuntimeError("FAILED: matching WSS event timed out")


def _event_sequence(event):
    extensions = event.get("extensions") if isinstance(event.get("extensions"), dict) else {}
    value = extensions.get("replay_sequence")
    return int(value) if isinstance(value, int) and value > 0 else None


async def _collect_event_window(context, stream, options):
    deadline = time.monotonic() + float(options.get("duration") or 1.0)
    matched = []
    observed = 0
    while time.monotonic() < deadline:
        pending = _pending_event_frames(stream)
        if pending:
            event = pending.pop(0)
        else:
            try:
                message = await asyncio.wait_for(
                    stream["socket"].recv(),
                    timeout=max(0.01, deadline - time.monotonic()),
                )
            except TimeoutError:
                break
            if not isinstance(message, str):
                raise RuntimeError(
                    "FAILED: event stream returned a binary frame while draining"
                )
            try:
                event = json.loads(message)
            except json.JSONDecodeError as exc:
                raise RuntimeError(
                    "FAILED: event stream returned invalid JSON while draining"
                ) from exc
            if not isinstance(event, dict):
                raise RuntimeError(
                    "FAILED: event stream returned a non-object while draining"
                )
        if event.get("protocol") == PROTOCOL:
            _validate_schema(context, "event_response", event)
            raise RuntimeError("FAILED: unexpected event control frame while draining")
        _validate_schema(context, "event_envelope", event)
        if event.get("site_id") != context["arguments"].site_id:
            raise RuntimeError("FAILED: event escaped the authorized site while draining")
        observed += 1
        if _event_matches(event, options.get("criteria") or {}):
            matched.append(event)
    return {"observed": observed, "matched": matched}


async def _filter_update_check(context, stream, baseline):
    payload = _event_payload(baseline)
    exact = [{
        "resource_type": "sensor",
        "resource_pattern": "{}:{}".format(
            payload.get("controller_uuid") or "*", payload.get("sensor_id") or "*"
        ),
    }]
    original = copy.deepcopy(stream["request"]["filters"])
    narrowed = await _update_event_filters(context, stream, exact)
    restored = await _update_event_filters(context, stream, original)
    if int(restored["accepted"]["generation"]) != int(
            narrowed["accepted"]["generation"]) + 1:
        raise RuntimeError("FAILED: event subscription generation is not monotonic")
    return {
        "narrow_generation": narrowed["accepted"]["generation"],
        "restore_generation": restored["accepted"]["generation"],
    }


async def _empty_filter_check(context, baseline):
    extensions = baseline.get("extensions") if isinstance(
        baseline.get("extensions"), dict) else {}
    sequence = extensions.get("replay_sequence")
    if not isinstance(sequence, int) or sequence <= 0:
        raise RuntimeError("FAILED: empty-filter replay requires a durable sequence")
    stream = await _open_events(context, [], last_event_id=sequence - 1)
    try:
        replayed = await _wait_event(context, stream, {
            "message_id": baseline.get("message_id"),
            "event_type": baseline.get("event_type"),
        })
    finally:
        await stream["socket"].close(code=1000)
    return {
        "message_id": replayed.get("message_id"),
        "site_id": replayed.get("site_id"),
        "filters": [],
    }


async def _replay_check(context, baseline):
    extensions = baseline.get("extensions") if isinstance(baseline.get("extensions"), dict) else {}
    sequence = extensions.get("replay_sequence")
    if not isinstance(sequence, int) or sequence <= 0:
        raise RuntimeError("FAILED: live event has no durable replay_sequence")
    payload = _event_payload(baseline)
    filters = [{
        "resource_type": "sensor",
        "resource_pattern": "{}:{}".format(
            payload.get("controller_uuid") or "*", payload.get("sensor_id") or "*"
        ),
    }]
    stream = await _open_events(context, filters, last_event_id=sequence - 1)
    try:
        replayed = await _wait_event(context, stream, {
            "message_id": baseline.get("message_id"),
            "event_type": baseline.get("event_type"),
            "timeout": context["arguments"].event_timeout,
        })
    finally:
        await stream["socket"].close(code=1000)
    return {
        "replay_sequence": sequence,
        "message_id": replayed.get("message_id"),
        "high_watermark": stream["accepted"].get("replay_high_watermark"),
    }


async def _node_filter_check(context):
    stream = await _open_events(context, [{
        "resource_type": "node",
        "resource_pattern": context["arguments"].node_id,
    }])
    try:
        event = await _wait_event(context, stream, {
            "event_type": "node.heartbeat",
            "node_id": context["arguments"].node_id,
            "timeout": context["arguments"].event_timeout,
        })
    finally:
        await stream["socket"].close(code=1000)
    return {
        "event_type": event.get("event_type"),
        "message_id": event.get("message_id"),
        "node_id": _event_payload(event).get("node_id"),
    }


def _record(context, check_id, status, **kwargs):
    if status not in STATUS_CODES:
        raise ValueError("unknown check status")
    item = {"status": status}
    if kwargs.get("detail") is not None:
        item["detail"] = _sanitize(kwargs["detail"])
    context["report"]["checks"][check_id] = item
    _log(context, "INFO" if status in ("PASSED", "SKIPPED") else "WARNING",
         "{} {}".format(check_id, status), details=item)
    return item


def _result_payload(result):
    value = result.get("result")
    return value if isinstance(value, dict) else {}


def _find_key(value, key):
    if isinstance(value, dict):
        if key in value:
            return value[key]
        for item in value.values():
            found = _find_key(item, key)
            if found is not None:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_key(item, key)
            if found is not None:
                return found
    return None


def _nodes_list(result):
    nodes = _find_key(_result_payload(result), "nodes")
    if not isinstance(nodes, list):
        raise RuntimeError("FAILED: nodes.list reply has no nodes array")
    return nodes


def _node(nodes, node_id):
    for value in nodes:
        if isinstance(value, dict) and str(value.get("node_id")) == str(node_id):
            return value
    return None


def _assert_reserved_probe_nodes_absent(live_reads):
    present = {
        str(item.get("node_id"))
        for item in _nodes_list(live_reads["nodes.list"])
        if isinstance(item, dict)
    }
    reserved = {
        "fn-e2e-http-missing", "fn-e2e-missing", "fn-e2e-csrf-missing",
        "fn-e2e-scope-denied",
    }
    collisions = sorted(present.intersection(reserved))
    if collisions:
        raise RuntimeError(
            "BLOCKED: reserved fail-closed probe node exists: " + ", ".join(collisions)
        )
    return True


def _is_non_negative_integer(value):
    return isinstance(value, int) and not isinstance(value, bool) and value >= 0


def _config_snapshot(result):
    reply = _result_payload(result)
    if not reply and isinstance(result, dict) and result.get("ok") is True:
        reply = result
    candidate = reply.get("config") if isinstance(reply.get("config"), dict) else reply
    mirror = candidate.get("mirror") if isinstance(candidate.get("mirror"), dict) else None
    if mirror is not None:
        scope_snapshot = mirror.get("snapshot")
        if not isinstance(scope_snapshot, dict) \
                or not isinstance(scope_snapshot.get("config"), dict):
            raise RuntimeError("FAILED: scoped config.get mirror has no immutable snapshot")
        scope = mirror.get("scope")
        fingerprint = mirror.get("fingerprint")
        integer_identity = {
            "authority_epoch": mirror.get("authority_epoch"),
            "scope_revision": mirror.get("scope_revision"),
            "execution_epoch": mirror.get("execution_epoch"),
        }
        if not isinstance(scope, str) or not scope \
                or any(not _is_non_negative_integer(value)
                       for value in integer_identity.values()) \
                or not isinstance(fingerprint, str) \
                or re.fullmatch(r"[0-9a-f]{64}", fingerprint) is None:
            raise RuntimeError("FAILED: scoped config.get mirror identity is incomplete")
        identity = {
            "scope": scope,
            **integer_identity,
            "fingerprint": fingerprint,
        }
        for name, value in identity.items():
            snapshot_value = scope_snapshot.get(name)
            if name in ("authority_epoch", "scope_revision", "execution_epoch"):
                if not _is_non_negative_integer(snapshot_value):
                    raise RuntimeError(
                        "FAILED: scoped config.get snapshot identity is incomplete")
            if snapshot_value != value:
                raise RuntimeError(
                    "FAILED: scoped config.get mirror identity differs at " + name)
        if _config_fingerprint(scope_snapshot["config"]) != identity["fingerprint"]:
            raise RuntimeError("FAILED: scoped config.get mirror fingerprint differs")
        return {
            "scope": identity["scope"],
            "authority_epoch": identity["authority_epoch"],
            "revision": identity["scope_revision"],
            "scope_revision": identity["scope_revision"],
            "execution_epoch": identity["execution_epoch"],
            "fingerprint": identity["fingerprint"],
            "config_data": copy.deepcopy(scope_snapshot["config"]),
            "contracts_version": mirror.get("contracts_version"),
            "type_schema_version": mirror.get("type_schema_version"),
            "config_norm_version": copy.deepcopy(mirror.get("config_norm_version")),
        }
    if isinstance(candidate.get("config_data"), dict):
        return candidate
    if isinstance(candidate.get("config"), dict):
        return {
            "revision": candidate.get("revision"),
            "fingerprint": candidate.get("fingerprint"),
            "config_data": candidate["config"],
        }
    raise RuntimeError("FAILED: config.get reply has no config snapshot")

def _config_idempotency_identity(command):
    """Return the stable relay identity allowed by the Node-Local contract."""
    if not isinstance(command, dict):
        return None
    accepted = command.get("accepted")
    accepted = accepted if isinstance(accepted, dict) else {}
    operation_id = accepted.get("operation_id")
    if operation_id in (None, ""):
        return None
    return ("operation", str(operation_id))


def _action_fixture(context):
    fixture = _load_json(context["arguments"].action_fixture, "WSS action fixture")
    actions = fixture.get("actions")
    if not isinstance(actions, list):
        raise RuntimeError("BLOCKED: action fixture has no actions array")
    names = tuple(item.get("action") for item in actions if isinstance(item, dict))
    if len(names) != len(set(names)) or set(names) != set(ALL_ACTIONS):
        raise RuntimeError("BLOCKED: action fixture does not cover the exact 23-action registry")
    return fixture


def _new_recovery(context):
    return {
        "version": VERSION,
        "run_id": context["run_id"],
        "profile": context["arguments"].profile,
        "site_id": context["arguments"].site_id,
        "node_id": context["arguments"].node_id,
        "principal_subject": context["principal_subject"],
        "cross_fixture_sha256": _file_sha256(context["arguments"].cross_fixture),
        "status": "OPEN",
        "started_at": _utc_now(),
        "updated_at": _utc_now(),
        "secret_material_stored": False,
        "config": {
            "mutated": False,
            "original_revision": None,
            "latest_revision": None,
            "original_fingerprint": None,
            "latest_fingerprint": None,
            "mutation_path": None,
            "original_value": None,
            "applied_value": None,
            "owned_changes": [],
            "restore_confirmed": False,
            "original_node_replicas": {},
        },
        "actuator": {
            "mutated": False,
            "node_id": None,
            "controller_uuid": None,
            "actuator_id": None,
            "original_value": None,
            "target_descriptor": None,
            "restore_confirmed": False,
        },
        "cross_worker": {
            "enabled": context["arguments"].profile == "full-lab",
            "cross_site": False,
            "sensor_mutated": False,
            "original_sensor_value": None,
            "sensor_restore_confirmed": False,
        },
        "api_token": {
            "created": False,
            "token_id": None,
            "revoked": False,
            "pending_names": [],
            "token_ids": [],
            "revoked_ids": [],
            "token_names": {},
        },
        "worker_launcher": {
            "started": False,
            "restore_confirmed": False,
            "archive_sha256": None,
        },
        "fn02_simulator": {
            "started": False,
            "stopped": False,
            "activated": False,
            "lock_id": None,
            "reservation_id": None,
            "reservation_source": None,
            "reservation_expires_at": None,
            "proof_signer_sha256": None,
            "previous_desired_state": None,
            "fixture_bootstrap_performed": False,
            "expected_residual": True,
        },
    }


def _archive_recovery(context):
    path = context["recovery_path"]
    if not path.exists():
        return None
    run_id = str((context.get("recovery") or {}).get("run_id") or "old")
    archived = path.with_name(
        path.name + ".completed-" + run_id + "-" + secrets.token_hex(4)
    )
    os.replace(path, archived)
    os.chmod(archived, 0o600)
    _fsync_directory(archived.parent)
    return archived


def _create_recovery(context):
    path = context["recovery_path"]
    context["recovery"] = _new_recovery(context)
    _validate_schema(context, "recovery_state", context["recovery"])
    _atomic_json(path, context["recovery"], exclusive=True)
    return context["recovery"]


def _begin_recovery(context):
    path = context["recovery_path"]
    if not path.exists():
        _create_recovery(context)
        return {"recovering": False}
    previous = _load_json(path, "E2E recovery journal")
    # R4 journals predate the immutable Modbus recovery descriptor. Preserve
    # their recoverability through the existing authority reconstruction path;
    # a successfully reconstructed target is pinned before restoration below.
    actuator_state = previous.get("actuator") if isinstance(previous, dict) else None
    if isinstance(actuator_state, dict) and "target_descriptor" not in actuator_state:
        actuator_state["target_descriptor"] = None
    _validate_schema(context, "recovery_state", previous)
    if previous.get("status") == "COMPLETED":
        context["recovery"] = previous
        _archive_recovery(context)
        _create_recovery(context)
        return {"recovering": False}
    if previous.get("profile") != context["arguments"].profile:
        raise RuntimeError(
            "BLOCKED: rerun the E2E with profile {} to recover the open journal".format(
                previous.get("profile")
            )
        )
    expected_binding = {
        "site_id": context["arguments"].site_id,
        "node_id": context["arguments"].node_id,
        "principal_subject": context["principal_subject"],
        "cross_fixture_sha256": _file_sha256(context["arguments"].cross_fixture),
    }
    for name, expected in expected_binding.items():
        if previous.get(name) != expected:
            raise RuntimeError(
                "BLOCKED: open recovery journal binding differs for " + name
            )
    context["requested_run_id"] = context["run_id"]
    context["run_id"] = str(previous["run_id"])
    context["recovery"] = previous
    context["recovery"]["status"] = "RECOVERING"
    _update_recovery(context)
    return {"recovering": True, "run_id": context["run_id"]}


def _update_recovery(context):
    context["recovery"]["updated_at"] = _utc_now()
    _validate_schema(context, "recovery_state", context["recovery"])
    _atomic_json(context["recovery_path"], context["recovery"])


def _complete_recovery(context, success):
    if not context.get("recovery"):
        return
    context["recovery"]["status"] = "COMPLETED" if success else "FAILED"
    if success:
        context["recovery"].pop("failure", None)
    else:
        failure = context["recovery"].get("failure")
        if isinstance(failure, str):
            context["recovery"]["failure"] = failure[:RECOVERY_FAILURE_MAX_LENGTH]
    context["recovery"]["updated_at"] = _utc_now()
    _validate_schema(context, "recovery_state", context["recovery"])
    _atomic_json(context["recovery_path"], context["recovery"])


def _config_data_equal(left, right):
    encoded_left = json.dumps(left, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    encoded_right = json.dumps(right, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return encoded_left == encoded_right


def _config_fingerprint(value):
    master_root = pathlib.Path(__file__).resolve().parents[2] \
        / "master_node" / "master_node"
    source_text = str(master_root)
    if not (master_root / "src" / "library" / "_generic_type_library.py").is_file():
        raise RuntimeError("BLOCKED: canonical Type Authority is unavailable")
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    try:
        from src.library import _generic_type_library as type_authority
    except ImportError as exc:
        raise RuntimeError("BLOCKED: canonical Type Authority cannot be imported") from exc
    return type_authority.config_fingerprint(copy.deepcopy(value))


def _require_v9_storage(config_data):
    meta = config_data.get("meta") if isinstance(config_data, dict) else None
    if not isinstance(meta, list) or len(meta) != 1 \
            or not isinstance(meta[0], dict) or meta[0].get("version") != 9:
        raise RuntimeError("FAILED: authority config is not canonical V9 storage")
    for section in ("sensors", "actuators"):
        rows = config_data.get(section)
        if not isinstance(rows, list) or any(
                not isinstance(row, dict) or "rows" in row for row in rows):
            raise RuntimeError("FAILED: authority V9 {} table is not flat".format(section))
    return config_data


def _scope_fingerprints(config_data, node_id):
    release_root = pathlib.Path(__file__).resolve().parents[2]
    master_root = release_root / "master_node" / "master_node"
    contracts_source = master_root / "src" / "library" \
        / "_generic_system_contract_library"
    if not contracts_source.is_dir():
        raise RuntimeError("BLOCKED: canonical system contracts are unavailable")
    source_text = str(master_root)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    try:
        from src.library._generic_system_contract_library import (
            config_graph,
            shard_library,
        )
    except ImportError as exc:
        raise RuntimeError("BLOCKED: canonical system contracts cannot be imported") from exc
    projected = config_graph.active_projection(copy.deepcopy(config_data))
    local = shard_library.build_local_execution_shard(projected, str(node_id))
    cross = shard_library.build_cross_execution_shard(projected, str(node_id))
    return {
        "node": shard_library.canonical_fingerprint(local),
        "cross": shard_library.canonical_fingerprint(cross)
        if isinstance(cross, dict) and cross.get("rules") else None,
    }


def _canonical_scope_operations(before, after):
    release_root = pathlib.Path(__file__).resolve().parents[2]
    master_root = release_root / "master_node" / "master_node"
    source_text = str(master_root)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    try:
        from src.library._generic_system_contract_library import (
            config_sharding_contracts,
        )
    except ImportError as exc:
        raise RuntimeError("BLOCKED: canonical system contracts cannot be imported") from exc
    operations = config_sharding_contracts.diff_configs(
        copy.deepcopy(before), copy.deepcopy(after))
    validated = config_sharding_contracts.validate_scope_operations(
        operations,
        operation_format=config_sharding_contracts.OPERATION_FORMAT,
    )
    return {
        "operation_format": config_sharding_contracts.OPERATION_FORMAT,
        "operations": [copy.deepcopy(item) for item in validated],
    }


def _node_scope(node_id):
    return "node:" + str(node_id)


def _cross_scope(node_id):
    return "cross:" + str(node_id) + ".default"

def _expected_cross_fingerprint(config_data, node_id):
    fingerprint = _scope_fingerprints(config_data, node_id)["cross"]
    if not fingerprint:
        raise RuntimeError("BLOCKED: current authority config has no expected Cross shard")
    return fingerprint


def _owner_configuration(config, node_id, preferred_uuids):
    options = preferred_uuids if isinstance(preferred_uuids, dict) else {
        "preferred": preferred_uuids,
    }
    owners = {
        str(route.get("controller_uuid"))
        for route in config.get("site_controller_map") or []
        if isinstance(route, dict)
        and route.get("enabled") is not False
        and str(route.get("routing_role") or "owner") == "owner"
        and str(route.get("node_id")) == str(node_id)
        and route.get("controller_uuid")
    }
    preferred = {
        str(value) for value in options.get("preferred") or [] if value
    }
    candidates = []
    for index, controller in enumerate(config.get("controllers") or []):
        if not isinstance(controller, dict):
            continue
        controller_uuid = str(controller.get("controller_uuid") or "")
        tags = controller.get("tags")
        if controller_uuid not in owners or not isinstance(tags, list) \
                or any(not isinstance(tag, str) for tag in tags):
            continue
        candidates.append({
            "index": index,
            "controller": controller,
            "controller_uuid": controller_uuid,
            "preferred": controller_uuid in preferred,
        })

    def candidate_key(item):
        return (not item["preferred"], item["index"])

    candidates.sort(key=candidate_key)
    if options.get("runtime_matrix"):
        compatible = []
        for item in candidates:
            try:
                _group_for_controller(
                    config, "sensors", item["controller_uuid"])
                _group_for_controller(
                    config, "actuators", item["controller_uuid"])
                _pid_state_for_controller(config, item["controller_uuid"])
                _numeric_rule_threshold(config, item["controller_uuid"])
            except RuntimeError:
                continue
            compatible.append(item)
        candidates = compatible
    if not candidates:
        raise RuntimeError(
            "FAILED: config has no safely mutable controller for node " + str(node_id)
        )
    return candidates[0]


def _next_unused(used, start, maximum):
    candidate = int(start)
    while candidate in used and candidate <= int(maximum):
        candidate += 1
    if candidate > int(maximum):
        raise RuntimeError("FAILED: no bounded E2E identifier/address is available")
    return candidate


def _group_for_controller(config, section, controller_uuid):
    flat = [
        (index, row)
        for index, row in enumerate(config.get(section) or [])
        if isinstance(row, dict) and "rows" not in row
        and str(row.get("controller_uuid") or "") == str(controller_uuid)
    ]
    if flat:
        return {
            "layout": "flat",
            "indexes": [item[0] for item in flat],
            "rows": [item[1] for item in flat],
        }
    raise RuntimeError(
        "FAILED: owned controller has no flat canonical V9 {} entities".format(section)
    )


def _rule_target_state_ids(config):
    result = set()
    for rule in config.get("rules") or []:
        if not isinstance(rule, dict) or rule.get("enabled") is False \
                or rule.get("activation_state") == "inactive":
            continue
        for section in ("do", "else_do"):
            for action in rule.get(section) or []:
                if not isinstance(action, dict):
                    continue
                result.update(str(value) for value in action.get("state_ids") or [])
    return result


def _pid_state_for_controller(config, controller_uuid):
    targeted = _rule_target_state_ids(config)
    for index, state in enumerate(config.get("process_states") or []):
        if not isinstance(state, dict) or str(state.get("state_id")) in targeted:
            continue
        strategy = state.get("control_strategy")
        parameters = state.get("parameters")
        refs = state.get("io_refs")
        if not isinstance(strategy, dict) or str(strategy.get("type")) != "PID" \
                or not isinstance(parameters, dict) \
                or not isinstance(parameters.get("target_temp_c"), (int, float)) \
                or isinstance(parameters.get("target_temp_c"), bool) \
                or not isinstance(refs, dict):
            continue
        identities = {
            str(item.get("controller_uuid") or "")
            for section in ("sensors", "actuators")
            for item in refs.get(section) or []
            if isinstance(item, dict)
        }
        if identities == {str(controller_uuid)}:
            return {"index": index, "state": state}
    raise RuntimeError(
        "FAILED: owned controller has no stable PID state for the E2E matrix"
    )


def _process_for_state(config, state):
    process_id = state.get("p_id") if isinstance(state, dict) else None
    for index, process in enumerate(config.get("processes") or []):
        if isinstance(process, dict) and str(process.get("p_id")) == str(process_id):
            return {"index": index, "process": process}
    raise RuntimeError("FAILED: PID state has no process_data owner")


def _numeric_rule_threshold(config, controller_uuid):
    for rule_index, rule in enumerate(config.get("rules") or []):
        if not isinstance(rule, dict) or rule.get("enabled") is False \
                or str(rule.get("execution_scope") or "") != "node:fn-01":
            continue
        for condition_index, condition in enumerate(rule.get("when") or []):
            value = condition.get("value") if isinstance(condition, dict) else None
            if isinstance(condition, dict) \
                    and str(condition.get("controller_uuid") or "") == str(controller_uuid) \
                    and isinstance(value, (int, float)) and not isinstance(value, bool):
                return {
                    "rule_index": rule_index,
                    "condition_index": condition_index,
                    "value": value,
                }
    raise RuntimeError("FAILED: no bounded numeric rule threshold exists")


def _config_change(snapshot, node_id, **kwargs):
    config = snapshot["config_data"]
    owner = _owner_configuration(
        config, node_id, {
            "preferred": kwargs.get("controller_uuids") or [],
            "runtime_matrix": bool(kwargs.get("runtime_matrix")),
        })
    controller = owner["controller"]
    controller_uuid = owner["controller_uuid"]
    marker = "wss-v0025-" + secrets.token_hex(4)
    tag_change = {
        "operation": "set",
        "path": "controllers.{}.tags".format(owner["index"]),
        "value": list(controller.get("tags") or []) + [marker],
    }
    if not kwargs.get("runtime_matrix"):
        return {
            "changes": [tag_change],
            "proof": {"controller_uuid": controller_uuid, "marker": marker},
        }

    sensors = _group_for_controller(config, "sensors", controller_uuid)
    actuators = _group_for_controller(config, "actuators", controller_uuid)
    pid = _pid_state_for_controller(config, controller_uuid)
    process = _process_for_state(config, pid["state"])
    threshold = _numeric_rule_threshold(config, controller_uuid)
    state = pid["state"]
    mode_ids = [
        row.get("mode_id") for row in config.get("process_modes") or []
        if isinstance(row, dict) and row.get("mode_id") is not None
    ]
    value_ids = [
        row.get("value_id") for row in config.get("process_values") or []
        if isinstance(row, dict) and row.get("value_id") is not None
    ]
    new_mode = next(
        (value for value in mode_ids if str(value) != str(state.get("mode_id"))), None)
    new_value = next(
        (value for value in value_ids if str(value) != str(state.get("value_id"))), None)
    if new_mode is None or new_value is None:
        raise RuntimeError("FAILED: PID state has no alternate mode/value for E2E")

    sensor_ids = {int(row.get("sensor_id")) for row in sensors["rows"]}
    sensor_addresses = {int(row.get("address")) for row in sensors["rows"]}
    actuator_ids = {int(row.get("actuator_id")) for row in actuators["rows"]}
    actuator_addresses = {int(row.get("address")) for row in actuators["rows"]}
    sensor_id = _next_unused(sensor_ids, 60000, 65535)
    sensor_address = _next_unused(sensor_addresses, 14, 63)
    actuator_id = _next_unused(actuator_ids, 60000, 65535)
    actuator_address = _next_unused(actuator_addresses, 547, 599)
    sensor_row = copy.deepcopy(sensors["rows"][-1])
    sensor_row.update({
        "sensor_id": sensor_id,
        "address": sensor_address,
        "sensor_group": "E2E_" + marker,
        "bus_type": "modbus",
        "function_type": "read_holding_registers",
    })
    actuator_row = copy.deepcopy(actuators["rows"][-1])
    actuator_row.update({
        "actuator_id": actuator_id,
        "address": actuator_address,
        "actuator_group": "E2E_" + marker,
        "bus_type": "modbus",
        "function_type": "write_register",
    })
    threshold_value = float(threshold["value"]) + 0.5
    target_value = float(state["parameters"]["target_temp_c"]) + 1.25
    sensor_change = {
        "operation": "append", "path": "sensors", "value": sensor_row,
    }
    actuator_change = {
        "operation": "append", "path": "actuators", "value": actuator_row,
    }
    process_location = str(process["process"].get("process_location") or "")
    process_location_after = process_location + "|" + marker
    changes = [
        tag_change,
        sensor_change,
        actuator_change,
        {
            "operation": "set",
            "path": "rules.{}.when.{}.value".format(
                threshold["rule_index"], threshold["condition_index"]),
            "value": threshold_value,
        },
        {
            "operation": "set",
            "path": "process_states.{}.mode_id".format(pid["index"]),
            "value": new_mode,
        },
        {
            "operation": "set",
            "path": "process_states.{}.value_id".format(pid["index"]),
            "value": new_value,
        },
        {
            "operation": "set",
            "path": "process_states.{}.parameters.target_temp_c".format(pid["index"]),
            "value": target_value,
        },
        {
            "operation": "set",
            "path": "processes.{}.process_location".format(process["index"]),
            "value": process_location_after,
        },
    ]
    return {
        "changes": changes,
        "proof": {
            "controller_uuid": controller_uuid,
            "marker": marker,
            "sensor_id": sensor_id,
            "sensor_address": sensor_address,
            "actuator_id": actuator_id,
            "actuator_address": actuator_address,
            "pid_state_id": state.get("state_id"),
            "mode_id": {"before": state.get("mode_id"), "after": new_mode},
            "value_id": {"before": state.get("value_id"), "after": new_value},
            "pid_target_temp_c": {
                "before": state["parameters"]["target_temp_c"],
                "after": target_value,
            },
            "rule_threshold": {
                "before": threshold["value"], "after": threshold_value,
            },
            "process_data": {
                "p_id": process["process"].get("p_id"),
                "process_location": {
                    "before": process_location,
                    "after": process_location_after,
                },
            },
            "storage_contract": {"version": 9, "io_layout": "flat"},
        },
    }


def _config_path_tokens(path):
    parts = str(path or "").split(".")
    if any(not part for part in parts):
        raise RuntimeError("FAILED: E2E-owned config path is invalid")
    safe = (
        len(parts) == 1 and parts[0] in ("sensors", "actuators")
    ) or (
        len(parts) == 3 and parts[0] == "controllers"
        and parts[1].isdigit() and parts[2] == "tags"
    ) or (
        len(parts) == 3 and parts[0] in ("sensors", "actuators")
        and parts[1].isdigit() and parts[2] == "rows"
    ) or (
        len(parts) == 3 and parts[0] == "process_states"
        and parts[1].isdigit() and parts[2] in ("mode_id", "value_id")
    ) or (
        len(parts) == 4 and parts[0] == "process_states"
        and parts[1].isdigit() and parts[2:] == ["parameters", "target_temp_c"]
    ) or (
        len(parts) == 3 and parts[0] == "processes"
        and parts[1].isdigit() and parts[2] == "process_location"
    ) or (
        len(parts) == 5 and parts[0] == "rules" and parts[1].isdigit()
        and parts[2] == "when" and parts[3].isdigit() and parts[4] == "value"
    )
    if not safe:
        raise RuntimeError("FAILED: E2E-owned config path is not safely bounded")
    return tuple(int(part) if part.isdigit() else part for part in parts)


def _config_path_value(config_data, path):
    value = config_data
    for token in _config_path_tokens(path):
        if isinstance(token, int) and isinstance(value, list) \
                and 0 <= token < len(value):
            value = value[token]
        elif isinstance(token, str) and isinstance(value, dict) and token in value:
            value = value[token]
        else:
            raise RuntimeError("FAILED: E2E-owned config path is absent")
    return copy.deepcopy(value)


def _config_candidate(snapshot, changes):
    candidate = copy.deepcopy(snapshot["config_data"])
    if isinstance(changes, dict) and isinstance(changes.get("changes"), list):
        changes = changes["changes"]
    elif isinstance(changes, dict):
        changes = [changes]
    if not isinstance(changes, list) or not changes:
        raise RuntimeError("FAILED: E2E config change set is empty")
    for change in changes:
        if not isinstance(change, dict) \
                or change.get("operation") not in ("set", "append"):
            raise RuntimeError("FAILED: E2E config operation is not safely bounded")
        tokens = _config_path_tokens(change.get("path"))
        value = copy.deepcopy(change.get("value"))
        if tokens[-1] == "tags" and (
                not isinstance(value, list)
                or any(not isinstance(tag, str) or not tag for tag in value)):
            raise RuntimeError("FAILED: E2E controller tags candidate is invalid")
        if change.get("operation") == "append" and (
                len(tokens) != 1 or tokens[0] not in ("sensors", "actuators")
                or not isinstance(value, dict) or "rows" in value):
            raise RuntimeError("FAILED: E2E V9 I/O append candidate is invalid")
        if change.get("operation") == "set" and len(tokens) == 1 \
                and tokens[0] in ("sensors", "actuators") and (
                    not isinstance(value, list)
                    or any(not isinstance(row, dict) or "rows" in row for row in value)):
            raise RuntimeError("FAILED: E2E V9 I/O restore table is invalid")
        if tokens[-1] == "rows" and (
                not isinstance(value, list) or not value or len(value) > 4096
                or any(not isinstance(row, dict) for row in value)):
            raise RuntimeError("FAILED: E2E I/O rows candidate is invalid")
        if change.get("operation") == "append":
            target = candidate.get(tokens[0])
            if not isinstance(target, list):
                raise RuntimeError("FAILED: E2E V9 I/O table is absent")
            controller_uuid = str(value.get("controller_uuid") or "")
            same_controller = [
                index for index, row in enumerate(target)
                if isinstance(row, dict)
                and str(row.get("controller_uuid") or "") == controller_uuid
            ]
            if not controller_uuid:
                raise RuntimeError("FAILED: E2E V9 I/O entity has no controller_uuid")
            insert_at = same_controller[-1] + 1 if same_controller else len(target)
            target.insert(insert_at, value)
            continue
        parent = candidate
        for token in tokens[:-1]:
            if isinstance(token, int) and isinstance(parent, list) \
                    and 0 <= token < len(parent):
                parent = parent[token]
            elif isinstance(token, str) and isinstance(parent, dict) and token in parent:
                parent = parent[token]
            else:
                raise RuntimeError("FAILED: E2E config candidate path is absent")
        target = tokens[-1]
        if isinstance(target, int) and isinstance(parent, list) \
                and 0 <= target < len(parent):
            parent[target] = value
        elif isinstance(target, str) and isinstance(parent, dict) and target in parent:
            parent[target] = value
        else:
            raise RuntimeError("FAILED: E2E config candidate target is absent")
    _config_fingerprint(candidate)
    return candidate


async def _read_scope_snapshot(context, control, scope):
    result = await _ws_command(context, control, {
        "action": "config.get",
        "payload": {"scope": str(scope)},
        "expect_result": True,
    })
    snapshot = _config_snapshot(result)
    if snapshot.get("scope") != str(scope):
        raise RuntimeError("FAILED: config.get returned another Mirror scope")
    if not _is_non_negative_integer(snapshot.get("authority_epoch")) \
            or not _is_non_negative_integer(snapshot.get("revision")) \
            or not _is_non_negative_integer(snapshot.get("execution_epoch")) \
            or re.fullmatch(r"[0-9a-f]{64}", str(snapshot.get("fingerprint") or "")) is None:
        raise RuntimeError("FAILED: config.get Mirror head is incomplete")
    if str(scope).startswith("node:"):
        _require_v9_storage(snapshot["config_data"])
    else:
        meta = snapshot["config_data"].get("meta") \
            if isinstance(snapshot.get("config_data"), dict) else None
        if not isinstance(meta, list) or len(meta) != 1 \
                or not isinstance(meta[0], dict) or meta[0].get("version") != 8:
            raise RuntimeError("FAILED: Cross Mirror is not canonical V8 storage")
    return snapshot


async def _read_config_snapshot(context, control, revision=None):
    if revision is not None:
        raise RuntimeError(
            "FAILED: node_local config history is immutable Mirror history, not global revisions")
    node_id = str(
        context.get("active_config_node_id")
        or context["arguments"].node_id)
    return await _read_scope_snapshot(context, control, _node_scope(node_id))


async def _wait_scope_snapshot(context, control, expectation):
    scope = str(expectation["scope"])
    base_revision = int(expectation["base_revision"])
    authority_epoch = int(expectation["authority_epoch"])
    expected_fingerprint = str(expectation["fingerprint"])
    expected_config = copy.deepcopy(expectation["config_data"])
    deadline = time.monotonic() + context["arguments"].operation_timeout
    last = None
    while time.monotonic() < deadline:
        current = await _read_scope_snapshot(context, control, scope)
        last = current
        current_epoch = int(current["authority_epoch"])
        current_revision = int(current["revision"])
        if current_epoch != authority_epoch:
            raise RuntimeError(
                "FAILED: scope authority epoch changed during the E2E-owned mutation")
        if current_revision < base_revision:
            raise RuntimeError("FAILED: Mirror scope revision regressed")
        if current_revision == base_revision:
            await asyncio.sleep(0.25)
            continue
        if current_revision != base_revision + 1:
            raise RuntimeError("FAILED: a concurrent scope commit advanced the Mirror head")
        if str(current["fingerprint"]) != expected_fingerprint \
                or not _config_data_equal(current["config_data"], expected_config):
            raise RuntimeError(
                "FAILED: a concurrent commit changed the E2E-owned Mirror scope")
        return current
    raise RuntimeError(
        "FAILED: Worker Mirror did not publish the accepted scope revision: "
        + str(_sanitize(last)))


def _node_convergence_state(node, node_id, expectation):
    value = node if isinstance(node, dict) else {}
    expected = expectation if isinstance(expectation, dict) else {}
    replica_revision = int(value.get("node_replica_revision") or 0)
    replica_fingerprint = str(value.get("node_replica_fingerprint") or "")
    observed_global_revision = int(value.get("observed_global_revision") or 0)
    live_config_revision = int(value.get("live_config_revision") or 0)
    expected_fingerprint = expected.get("node_replica_fingerprint")
    previous_fingerprint = str(expected.get("previous_fingerprint") or "")
    minimum_replica_revision = expected.get("minimum_node_replica_revision")
    minimum_replica_revision = 0 if minimum_replica_revision is None \
        else int(minimum_replica_revision)
    fingerprint_ok = len(replica_fingerprint) == 64
    if expected_fingerprint:
        fingerprint_ok = replica_fingerprint == str(expected_fingerprint)
    elif previous_fingerprint:
        fingerprint_ok = fingerprint_ok and replica_fingerprint != previous_fingerprint
    checks = {
        "admission": str(value.get("admission_state") or "") == "APPROVED",
        "observed_state": str(value.get("observed_state") or "") == "ACTIVE",
        "online": value.get("online") is True,
        "scope": str(value.get("node_replica_scope") or "") == _node_scope(node_id),
        "replica_revision": replica_revision >= minimum_replica_revision,
        "replica_fingerprint": fingerprint_ok,
        "observed_global_revision": observed_global_revision >= int(
            expected.get("minimum_observed_global_revision") or 0
        ),
        "live_config_revision": live_config_revision >= int(
            expected.get("minimum_live_config_revision") or 0
        ),
    }
    return {
        "ready": all(checks.values()),
        "checks": checks,
        "observed": {
            "node_id": value.get("node_id"),
            "node_replica_revision": replica_revision,
            "node_replica_fingerprint": replica_fingerprint,
            "observed_global_revision": observed_global_revision,
            "live_config_revision": live_config_revision,
        },
        "expected": copy.deepcopy(expected),
    }


async def _wait_node_ready(context, control, node_id):
    deadline = time.monotonic() + context["arguments"].operation_timeout
    last = None
    state = None
    while time.monotonic() < deadline:
        result = await _ws_command(context, control, {
            "action": "nodes.list", "payload": {}, "expect_result": True,
        })
        last = _node(_nodes_list(result), node_id)
        expectation = (context.get("node_expectations") or {}).get(str(node_id)) or {}
        state = _node_convergence_state(last, node_id, expectation)
        if state["ready"]:
            return last
        await asyncio.sleep(1.0)
    raise RuntimeError(
        "FAILED: Worker {} did not become approved, active and converged: {}".format(
            node_id, _sanitize(state or {"observed": last})
        )
    )


def _node_config_timeout_detail(snapshot, state, values):
    return {
        "scope": snapshot.get("scope"),
        "scope_revision": int(snapshot.get("revision") or 0),
        "scope_fingerprint": str(snapshot.get("fingerprint") or ""),
        "node_convergence": copy.deepcopy(state),
        "verified_paths": [str(item.get("path") or "") for item in values],
    }


async def _wait_node_config_values(context, control, expected):
    node_id = str(expected["node_id"])
    values = copy.deepcopy(expected.get("values") or [])
    if not values:
        raise RuntimeError("FAILED: no E2E-owned config values were supplied")
    deadline = time.monotonic() + context["arguments"].operation_timeout
    last = None
    while time.monotonic() < deadline:
        snapshot = await _read_scope_snapshot(
            context, control, _node_scope(node_id))
        values_match = all(
            _config_data_equal(
                _config_path_value(snapshot["config_data"], item["path"]),
                item["value"],
            )
            for item in values
        )
        result = await _ws_command(context, control, {
            "action": "nodes.list", "payload": {}, "expect_result": True,
        })
        node = _node(_nodes_list(result), node_id)
        expectation = {
            "minimum_observed_global_revision": 0,
            "minimum_live_config_revision": 1,
            "minimum_node_replica_revision": int(snapshot["revision"]),
            "node_replica_fingerprint": str(snapshot["fingerprint"]),
        }
        state = _node_convergence_state(node, node_id, expectation)
        last = _node_config_timeout_detail(snapshot, state, values)
        if values_match and state["ready"]:
            context.setdefault("node_expectations", {})[node_id] = expectation
            return {"snapshot": snapshot, "node": node}
        await asyncio.sleep(0.5)
    raise RuntimeError(
        "FAILED: Worker-local config did not converge on the E2E-owned fields: "
        + str(_sanitize(last))
    )


async def _wait_cross_replica(context, control):
    node_id = context["arguments"].node_id
    scope = _cross_scope(node_id)
    deadline = time.monotonic() + context["arguments"].operation_timeout
    last = None
    while time.monotonic() < deadline:
        try:
            snapshot = await _read_scope_snapshot(context, control, scope)
        except RuntimeError as exc:
            if "not_found" not in str(exc):
                raise
            await asyncio.sleep(0.5)
            continue
        result = await _ws_command(context, control, {
            "action": "nodes.list", "payload": {}, "expect_result": True,
        })
        last = _node(_nodes_list(result), node_id)
        if last and (
                str(last.get("cross_replica_scope") or "") == scope
                and int(last.get("cross_replica_revision") or 0)
                >= int(snapshot["revision"])
                and str(last.get("cross_replica_fingerprint") or "")
                == str(snapshot["fingerprint"])):
            return {
                "scope": scope,
                "authority_epoch": int(snapshot["authority_epoch"]),
                "revision": int(snapshot["revision"]),
                "execution_epoch": int(snapshot["execution_epoch"]),
                "fingerprint": str(snapshot["fingerprint"]),
                "master_materialized": False,
                "mirror_source": "worker",
            }
        await asyncio.sleep(0.5)
    raise RuntimeError("FAILED: fn-01 Cross Mirror did not converge: {}".format(
        _sanitize(last)))


async def _wait_proposal_result(context, stream, options):
    event = await _wait_event(context, stream, {
        "event_type": "config.proposal.result",
        "node_id": options["node_id"],
        "relay_message_id": options["relay_message_id"],
    })
    payload = _event_payload(event)
    accepted = payload.get("accepted") is True
    expected_accepted = options.get("accepted")
    if expected_accepted is not None and accepted is not bool(expected_accepted):
        raise RuntimeError("FAILED: Worker proposal ACK decision differs")
    expected_code = options.get("code")
    if expected_code is not None and str(payload.get("code") or "") != str(expected_code):
        raise RuntimeError("FAILED: Worker proposal ACK code differs")
    return {"event": event, "payload": payload}

def _validate_fn02_capabilities(node):
    protocols = set(node.get("protocol_versions") or ())
    if node.get("protocol_version") != "automation.distributed.v2":
        raise RuntimeError("FAILED: fn-02 did not negotiate distributed v2")
    if protocols != {"automation.distributed.v1", "automation.distributed.v2"}:
        raise RuntimeError("FAILED: fn-02 protocol fallback set differs")
    contracts_version = str(node.get("contracts_version") or "")
    if contracts_version != "2.0":
        raise RuntimeError("FAILED: fn-02 contracts_version differs")
    capabilities = node.get("capabilities") \
        if isinstance(node.get("capabilities"), dict) else {}
    features = capabilities.get("features") \
        if isinstance(capabilities.get("features"), dict) else {}
    required = (
        "config_scope_operations_v1", "authority_epoch_v1", "canonical_config_v9",
        "config_proposals", "local_automation_authority", "mirror_v1", "node_full",
        "node_patch", "cross_runtime", "cross_owner_accept_v1",
        "cross_fact_consumer", "remote_actuator",
    )
    missing = [name for name in required if features.get(name) is not True]
    if missing or features.get("chunk_reassembly") is not False:
        raise RuntimeError("FAILED: fn-02 capability set differs: " + ",".join(missing))
    if features.get("master_automation_execution") is not False:
        raise RuntimeError("FAILED: fn-02 advertises forbidden Master execution")
    return {
        "protocols": sorted(protocols),
        "contracts_version": contracts_version,
        "features": required,
    }
async def _mutate_config(context, control, node_id, **kwargs):
    recovery = context["recovery"]["config"]
    if recovery["mutated"]:
        raise RuntimeError("BLOCKED: a previous E2E config mutation is not restored")
    event_stream = kwargs.get("event_stream")
    if not isinstance(event_stream, dict):
        raise RuntimeError("BLOCKED: config mutation requires a correlated Event stream")
    context["config_event_stream"] = event_stream
    node_id = str(node_id)
    context["active_config_node_id"] = node_id
    before_node = await _wait_node_ready(context, control, node_id)
    capabilities = before_node.get("capabilities") \
        if isinstance(before_node.get("capabilities"), dict) else {}
    features = capabilities.get("features") \
        if isinstance(capabilities.get("features"), dict) else {}
    required_features = (
        "config_scope_operations_v1", "authority_epoch_v1",
        "canonical_config_v9", "config_proposals",
        "local_automation_authority", "mirror_v1",
    )
    missing = [name for name in required_features if features.get(name) is not True]
    if str(before_node.get("contracts_version") or "") != "2.0" or missing:
        raise RuntimeError(
            "FAILED: Worker does not advertise the complete Node-Local contract: "
            + ",".join(missing))
    scope = _node_scope(node_id)
    for attempt in range(1, CONFIG_CAS_ATTEMPTS + 1):
        snapshot = await _read_scope_snapshot(context, control, scope)
        revision = int(snapshot["revision"])
        authority_epoch = int(snapshot["authority_epoch"])
        descriptor = _config_change(
            snapshot, node_id,
            controller_uuids=capabilities.get("controller_uuids"),
            runtime_matrix=bool(kwargs.get("runtime_matrix")),
        )
        changes = descriptor["changes"]
        candidate = _config_candidate(snapshot, changes)
        owned_changes = [
            {
                "path": change["path"],
                "original_value": _config_path_value(
                    snapshot["config_data"], change["path"]),
                "applied_value": _config_path_value(candidate, change["path"]),
            }
            for change in changes
        ]
        expected_fingerprint = _config_fingerprint(candidate)
        if expected_fingerprint == str(snapshot["fingerprint"]):
            raise RuntimeError("FAILED: E2E config candidate does not change the Node scope")
        canonical = _canonical_scope_operations(snapshot["config_data"], candidate)
        before_replica = {
            "revision": int(before_node.get("node_replica_revision") or revision),
            "fingerprint": str(snapshot["fingerprint"]),
            "observed_global_revision": int(
                before_node.get("observed_global_revision") or 0
            ),
            "live_config_revision": int(before_node.get("live_config_revision") or 0),
        }
        context["original_node_replicas"] = {
            node_id: copy.deepcopy(before_replica),
        }
        context["original_config"] = copy.deepcopy(snapshot)
        recovery.update({
            "mutated": True,
            "original_revision": revision,
            "latest_revision": revision + 1,
            "original_fingerprint": str(snapshot["fingerprint"]),
            "latest_fingerprint": expected_fingerprint,
            "mutation_path": owned_changes[0]["path"],
            "original_value": copy.deepcopy(owned_changes[0]["original_value"]),
            "applied_value": copy.deepcopy(owned_changes[0]["applied_value"]),
            "owned_changes": copy.deepcopy(owned_changes),
            "restore_confirmed": False,
            "original_node_replicas": {
                node_id: {
                    "revision": revision,
                    "fingerprint": str(snapshot["fingerprint"]),
                },
            },
        })
        _update_recovery(context)
        command_request = {
            "action": "config.propose",
            "payload": {
                "target_node_id": node_id,
                "operation": "scope_operations",
                "operation_format": canonical["operation_format"],
                "scope": scope,
                "base_authority_epoch": authority_epoch,
                "base_scope_revision": revision,
                "base_scope_fingerprint": str(snapshot["fingerprint"]),
                "reason": "WSS v0025 Node-Local E2E " + context["run_id"],
                "operations": copy.deepcopy(canonical["operations"]),
            },
            "mutation": True,
            "expect_result": False,
            "retryable_rejection_codes": ("stale_revision",),
            "idempotency_key": "{}:config:{}:{}".format(
                context["run_id"], node_id, attempt
            ),
        }
        result = await _ws_command(context, control, command_request)
        rejected = result.get("rejected") if isinstance(result, dict) else None
        if isinstance(rejected, dict):
            recovery["mutated"] = False
            recovery["restore_confirmed"] = True
            latest = await _read_scope_snapshot(context, control, scope)
            recovery["latest_revision"] = int(latest["revision"])
            recovery["latest_fingerprint"] = str(latest["fingerprint"])
            _update_recovery(context)
            await asyncio.sleep(0.25)
            before_node = await _wait_node_ready(context, control, node_id)
            continue
        first_identity = _config_idempotency_identity(result)
        if first_identity is None or first_identity[0] != "operation":
            raise RuntimeError("FAILED: Node-Local relay has no stable operation identity")
        relay_message_id = "relay:" + str(first_identity[1])
        proposal_ack = await _wait_proposal_result(context, event_stream, {
            "node_id": node_id,
            "relay_message_id": relay_message_id,
            "accepted": True,
        })
        changed = await _wait_scope_snapshot(context, control, {
            "scope": scope,
            "base_revision": revision,
            "authority_epoch": authority_epoch,
            "fingerprint": expected_fingerprint,
            "config_data": candidate,
        })
        converged = await _wait_node_config_values(context, control, {
            "node_id": node_id,
            "values": [
                {"path": item["path"], "value": item["applied_value"]}
                for item in owned_changes
            ],
        })
        converged_node = converged["node"]
        retry = await _ws_command(context, control, command_request)
        retry_identity = _config_idempotency_identity(retry)
        if retry_identity != first_identity:
            raise RuntimeError(
                "FAILED: idempotent Node-Local retry did not preserve its operation identity")
        conflict_request = copy.deepcopy(command_request)
        conflict_request["payload"]["reason"] += " conflict-probe"
        conflict_request.update({
            "expected_rejection": True,
            "allowed_codes": ("conflict",),
        })
        conflict = await _ws_command(context, control, conflict_request)
        stale_request = copy.deepcopy(command_request)
        stale_request["idempotency_key"] = (
            context["run_id"] + ":config-stale:" + node_id)
        stale = await _ws_command(context, control, stale_request)
        stale_identity = _config_idempotency_identity(stale)
        if stale_identity is None or stale_identity[0] != "operation":
            raise RuntimeError("FAILED: stale proposal relay has no operation identity")
        stale_ack = await _wait_proposal_result(context, event_stream, {
            "node_id": node_id,
            "relay_message_id": "relay:" + str(stale_identity[1]),
            "accepted": False,
            "code": "stale_revision",
        })
        stable = await _read_scope_snapshot(context, control, scope)
        if int(stable["revision"]) != int(changed["revision"]) \
                or str(stable["fingerprint"]) != str(changed["fingerprint"]) \
                or not _config_data_equal(stable["config_data"], changed["config_data"]):
            raise RuntimeError("FAILED: rejected stale proposal changed the Mirror head")
        node_replica_revision = int(
            converged_node.get("node_replica_revision") or 0)
        node_replica_fingerprint = str(
            converged_node.get("node_replica_fingerprint") or "")
        return {
            "command": result,
            "retry": retry,
            "conflict": conflict,
            "stale": stale,
            "proposal_ack": proposal_ack,
            "stale_ack": stale_ack,
            "before": snapshot,
            "after": changed,
            "node": converged_node,
            "node_id": node_id,
            "cas_retries": attempt - 1,
            "matrix": copy.deepcopy(descriptor["proof"]),
            "owned_paths": [item["path"] for item in owned_changes],
            "commit_identity": first_identity,
            "commit_origin": "canonical_client_proxy_master_relay_worker_local",
            "file_proposal": False,
            "transport_expectation": "distributed.config.propose",
            "node_patch_capability": True,
            "node_local_proposal_proof": {
                "relay_message_id": relay_message_id,
                "authority_epoch": authority_epoch,
                "base_scope_revision": revision,
                "target_scope_revision": int(changed["revision"]),
                "target_scope_fingerprint": str(changed["fingerprint"]),
                "node_replica_revision": node_replica_revision,
                "node_replica_fingerprint": node_replica_fingerprint,
                "operation_format": canonical["operation_format"],
                "operation_count": len(canonical["operations"]),
            },
        }
    raise RuntimeError("FAILED: config mutation exhausted bounded scope-CAS retries")

def _owned_restore_changes(recovery):
    changes = recovery.get("owned_changes") or []
    if not changes and recovery.get("mutation_path"):
        changes = [{
            "path": recovery.get("mutation_path"),
            "original_value": copy.deepcopy(recovery.get("original_value")),
            "applied_value": copy.deepcopy(recovery.get("applied_value")),
        }]
    normalized = []
    for item in changes:
        if not isinstance(item, dict) or not item.get("path") \
                or "original_value" not in item or "applied_value" not in item:
            raise RuntimeError("FAILED: config recovery ownership proof is incomplete")
        normalized.append({
            "path": str(item["path"]),
            "original_value": copy.deepcopy(item["original_value"]),
            "applied_value": copy.deepcopy(item["applied_value"]),
        })
    return normalized


def _persisted_restore_revalidation(recovery):
    changes = _owned_restore_changes(recovery)
    replicas = recovery.get("original_node_replicas") or {}
    original_revision = int(recovery.get("original_revision") or 0)
    latest_revision = int(recovery.get("latest_revision") or 0)
    latest_fingerprint = str(recovery.get("latest_fingerprint") or "")
    original_fingerprint = str(recovery.get("original_fingerprint") or "")
    intent_not_applied = bool(
        latest_revision == original_revision
        and latest_fingerprint
        and latest_fingerprint == original_fingerprint
    )
    if not recovery.get("restore_confirmed") \
            or latest_revision < original_revision \
            or (latest_revision == original_revision and not intent_not_applied) \
            or not changes or len(replicas) != 1:
        return None
    return {
        "node_id": str(next(iter(replicas))),
        "values": [
            {
                "path": item["path"],
                "value": copy.deepcopy(item["original_value"]),
            }
            for item in changes
        ],
        "mode": "intent_not_applied" if intent_not_applied
        else "persisted_restore_revalidation",
    }


def _restore_proposal_payload(context, node_id, options):
    snapshot = options["snapshot"]
    canonical = options["canonical"]
    return {
        "target_node_id": str(node_id),
        "operation": "scope_operations",
        "operation_format": canonical["operation_format"],
        "scope": str(snapshot["scope"]),
        "base_authority_epoch": int(snapshot["authority_epoch"]),
        "base_scope_revision": int(snapshot["revision"]),
        "base_scope_fingerprint": str(snapshot["fingerprint"]),
        "reason": "WSS v0025 Node-Local compensating restore " + context["run_id"],
        "operations": copy.deepcopy(canonical["operations"]),
    }


def _validate_restore_proposal_ack(ack, snapshot, expected_fingerprint):
    payload = ack.get("payload") if isinstance(ack, dict) else None
    payload = payload if isinstance(payload, dict) else {}
    expected = {
        "scope": str(snapshot["scope"]),
        "authority_epoch": int(snapshot["authority_epoch"]),
        "scope_revision": int(snapshot["revision"]) + 1,
        "fingerprint": str(expected_fingerprint),
    }
    observed = {
        "scope": str(payload.get("scope") or ""),
        "authority_epoch": int(payload.get("authority_epoch") or 0),
        "scope_revision": int(payload.get("scope_revision") or 0),
        "fingerprint": str(payload.get("fingerprint") or ""),
    }
    if observed != expected:
        raise RuntimeError("FAILED: accepted Worker restore ACK identity differs")
    return observed


async def _restore_config(context, control):
    recovery = context["recovery"]["config"]
    if not recovery["mutated"]:
        recovery["restore_confirmed"] = True
        _update_recovery(context)
        revalidation = _persisted_restore_revalidation(recovery)
        if revalidation is None:
            return {"skipped": True, "restore_confirmed": True}
        converged = await _wait_node_config_values(
            context, control, revalidation)
        latest = converged["snapshot"]
        recovery["latest_revision"] = int(latest["revision"])
        recovery["latest_fingerprint"] = str(latest["fingerprint"])
        _update_recovery(context)
        return {
            "command": None,
            "proposal_ack": None,
            "snapshot": latest,
            "converged_snapshot": latest,
            "restore_confirmed": True,
            "mode": revalidation["mode"],
            "cas_retries": 0,
            "node": converged["node"],
        }
    owned_changes = _owned_restore_changes(recovery)
    replicas = recovery.get("original_node_replicas") or {}
    if not owned_changes or len(replicas) != 1:
        raise RuntimeError(
            "FAILED: node-local config recovery requires one immutable Worker baseline")
    event_stream = context.get("config_event_stream")
    if not isinstance(event_stream, dict):
        raise RuntimeError(
            "BLOCKED: node-local config restore requires a correlated Event stream")
    node_id = str(next(iter(replicas)))
    scope = _node_scope(node_id)
    command = None
    proposal_ack = None
    restored = None
    mode = None
    retries = 0
    for attempt in range(1, CONFIG_CAS_ATTEMPTS + 1):
        snapshot = await _read_scope_snapshot(context, control, scope)
        pending = []
        for item in owned_changes:
            value = _config_path_value(snapshot["config_data"], item["path"])
            if _config_data_equal(value, item["original_value"]):
                continue
            if not _config_data_equal(value, item["applied_value"]):
                raise RuntimeError(
                    "FAILED: a concurrent owner changed an E2E-owned config field; "
                    "automatic node-local compensation is refused")
            pending.append({
                "operation": "set",
                "path": item["path"],
                "value": copy.deepcopy(item["original_value"]),
            })
        if not pending:
            exact_baseline = bool(
                int(snapshot["revision"])
                == int(recovery.get("original_revision") or 0)
                and str(snapshot["fingerprint"])
                == str(recovery.get("original_fingerprint") or "")
            )
            restored = snapshot
            mode = "intent_not_applied" if exact_baseline else "already_restored"
            retries = attempt - 1
            break
        candidate = _config_candidate(snapshot, pending)
        expected_fingerprint = _config_fingerprint(candidate)
        canonical = _canonical_scope_operations(
            snapshot["config_data"], candidate)
        if not canonical["operations"]:
            raise RuntimeError("FAILED: node-local restore produced no canonical operations")
        request = {
            "action": "config.propose",
            "payload": _restore_proposal_payload(context, node_id, {
                "snapshot": snapshot,
                "canonical": canonical,
            }),
            "mutation": True,
            "expect_result": False,
            "retryable_rejection_codes": (
                "stale_revision", "authority_epoch_stale"),
            "idempotency_key": "{}:config:restore:{}:{}".format(
                context["run_id"], node_id, attempt),
        }
        command = await _ws_command(context, control, request)
        rejected = command.get("rejected") \
            if isinstance(command, dict) else None
        if isinstance(rejected, dict):
            await asyncio.sleep(0.25)
            continue
        identity = _config_idempotency_identity(command)
        if identity is None or identity[0] != "operation":
            raise RuntimeError(
                "FAILED: node-local restore relay has no stable operation identity")
        relay_message_id = "relay:" + str(identity[1])
        proposal_ack = await _wait_proposal_result(context, event_stream, {
            "node_id": node_id,
            "relay_message_id": relay_message_id,
            "accepted": None,
        })
        ack_payload = proposal_ack["payload"]
        if ack_payload.get("accepted") is not True:
            code = str(ack_payload.get("code") or "proposal_rejected")
            if code in ("stale_revision", "authority_epoch_stale"):
                await asyncio.sleep(0.25)
                continue
            raise RuntimeError(
                "FAILED: Worker rejected node-local restore proposal: " + code)
        _validate_restore_proposal_ack(
            proposal_ack, snapshot, expected_fingerprint)
        restored = await _wait_scope_snapshot(context, control, {
            "scope": scope,
            "base_revision": int(snapshot["revision"]),
            "authority_epoch": int(snapshot["authority_epoch"]),
            "fingerprint": expected_fingerprint,
            "config_data": candidate,
        })
        mode = "compensating_proposal"
        retries = attempt - 1
        break
    if restored is None:
        raise RuntimeError(
            "FAILED: node-local config restore exhausted bounded scope-CAS retries")
    converged = await _wait_node_config_values(context, control, {
        "node_id": node_id,
        "values": [
            {
                "path": item["path"],
                "value": copy.deepcopy(item["original_value"]),
            }
            for item in owned_changes
        ],
    })
    latest = converged["snapshot"]
    recovery["latest_revision"] = int(latest["revision"])
    recovery["latest_fingerprint"] = str(latest["fingerprint"])
    recovery["mutated"] = False
    recovery["restore_confirmed"] = True
    _update_recovery(context)
    return {
        "command": command,
        "proposal_ack": proposal_ack,
        "snapshot": restored,
        "converged_snapshot": latest,
        "restore_confirmed": True,
        "mode": mode,
        "cas_retries": retries,
        "node": converged["node"],
    }


def _config_restore_evidence(context, restored, options):
    actuator_restored_before_config = bool(
        options.get("actuator_restored_before_config"))
    before_parallel_load = bool(options.get("before_parallel_load"))
    if restored.get("skipped"):
        checks = context["report"].get("checks") or {}
        for name in ("CFG-RESTORE", "CFG-006"):
            detail = (checks.get(name) or {}).get("detail")
            if isinstance(detail, dict) \
                    and isinstance(detail.get("worker_scope"), dict):
                return copy.deepcopy(detail)
        recovery = context["recovery"]["config"]
        if not recovery.get("mutated") \
                and recovery.get("original_revision") is None \
                and not recovery.get("owned_changes") \
                and not recovery.get("original_node_replicas"):
            return {
                "restore_confirmed": True,
                "actuator_restored_before_config": bool(
                    actuator_restored_before_config),
                "before_parallel_load": bool(before_parallel_load),
                "mode": "not_mutated",
                "cas_retries": 0,
                "not_mutated": True,
            }
        raise RuntimeError("FAILED: skipped config restore has no durable proof")
    recovery = context["recovery"]["config"]
    snapshot = restored.get("converged_snapshot") or restored.get("snapshot")
    node = restored.get("node")
    changes = _owned_restore_changes(recovery)
    replicas = recovery.get("original_node_replicas") or {}
    if not isinstance(snapshot, dict) or not isinstance(
            snapshot.get("config_data"), dict) \
            or not isinstance(node, dict) or not changes or len(replicas) != 1:
        raise RuntimeError("FAILED: config restore proof inputs are incomplete")
    node_id = str(next(iter(replicas)))
    baseline_scope = replicas[node_id]
    authority_revision = int(snapshot.get("revision") or 0)
    authority_fingerprint = str(snapshot.get("fingerprint") or "")
    worker_revision = int(node.get("node_replica_revision") or 0)
    worker_fingerprint = str(node.get("node_replica_fingerprint") or "")
    observed_global = int(node.get("observed_global_revision") or 0)
    values_restored = all(
        _config_data_equal(
            _config_path_value(snapshot["config_data"], item["path"]),
            item["original_value"],
        )
        for item in changes
    )
    original_fingerprint = str(recovery.get("original_fingerprint") or "")
    baseline_fingerprint = str(baseline_scope.get("fingerprint") or "")
    fingerprints = (
        original_fingerprint,
        authority_fingerprint,
        baseline_fingerprint,
        worker_fingerprint,
    )
    mode = str(restored.get("mode") or "")
    original_revision = int(recovery.get("original_revision") or 0)
    baseline_scope_revision = int(baseline_scope.get("revision") or 0)
    common_invalid = bool(
        any(re.fullmatch(r"[0-9a-f]{64}", item) is None
            for item in fingerprints)
        or original_fingerprint != baseline_fingerprint
        or worker_fingerprint != authority_fingerprint
        or str(snapshot.get("scope") or "") != _node_scope(node_id)
        or str(node.get("node_replica_scope") or "") != _node_scope(node_id)
        or not values_restored
    )
    if mode == "intent_not_applied":
        progress_invalid = bool(
            authority_revision != original_revision
            or authority_fingerprint != original_fingerprint
            or worker_revision < baseline_scope_revision
            or worker_fingerprint != baseline_fingerprint
        )
    else:
        progress_invalid = bool(
            authority_revision <= original_revision
            or authority_revision <= baseline_scope_revision
            or worker_revision < authority_revision
        )
    if common_invalid or progress_invalid:
        raise RuntimeError("FAILED: config restore convergence proof differs")
    return {
        "restore_confirmed": True,
        "actuator_restored_before_config": bool(
            actuator_restored_before_config),
        "before_parallel_load": bool(before_parallel_load),
        "mode": mode,
        "authority_mode": "node_local",
        "cas_retries": int(restored.get("cas_retries") or 0),
        "baseline": {
            "scope_revision": int(recovery["original_revision"]),
            "scope_fingerprint": original_fingerprint,
            "node_scope_revision": baseline_scope_revision,
            "node_scope_fingerprint": baseline_fingerprint,
        },
        "scope_revision": authority_revision,
        "scope_fingerprint": authority_fingerprint,
        "owned_paths": [str(item["path"]) for item in changes],
        "owned_values_restored": True,
        "worker_scope": {
            "node_id": node_id,
            "scope": _node_scope(node_id),
            "revision": worker_revision,
            "fingerprint": worker_fingerprint,
            "expected_fingerprint": authority_fingerprint,
            "observed_global_revision": observed_global,
        },
    }


def _terminal_config_restore_detail(context):
    checks = context["report"].get("checks") or {}
    details = []
    for name in ("CFG-RESTORE", "CFG-006"):
        item = checks.get(name)
        detail = item.get("detail") if isinstance(item, dict) else None
        if not isinstance(item, dict) or item.get("status") != "PASSED" \
                or not isinstance(detail, dict):
            return None
        details.append(detail)
    detail = details[0]
    if details[1] != detail \
            or detail.get("mode") not in (
                "compensating_proposal", "already_restored",
                "intent_not_applied", "persisted_restore_revalidation") \
            or detail.get("authority_mode") != "node_local" \
            or detail.get("restore_confirmed") is not True \
            or detail.get("owned_values_restored") is not True \
            or not isinstance(detail.get("baseline"), dict) \
            or not isinstance(detail.get("worker_scope"), dict):
        return None
    return copy.deepcopy(detail)


def _record_config_restore_evidence(context, detail):
    if str(detail.get("mode") or "") == "persisted_restore_revalidation":
        terminal = _terminal_config_restore_detail(context)
        if terminal is None:
            raise RuntimeError(
                "FAILED: restore revalidation has no immutable terminal proof")
        _record(
            context, "CFG-RESTORE-REVALIDATION", "PASSED", detail=detail)
        return terminal
    _record(context, "CFG-RESTORE", "PASSED", detail=detail)
    _record(context, "CFG-006", "PASSED", detail=detail)
    return copy.deepcopy(detail)

def _select_target(snapshot, node_id, **kwargs):
    config = snapshot["config_data"]
    owners = config.get("site_controller_map") or []
    owner_controllers = {
        str(item.get("controller_uuid"))
        for item in owners
        if isinstance(item, dict)
        and str(item.get("node_id")) == str(node_id)
        and str(item.get("routing_role")) == "owner"
        and item.get("enabled", True)
    }
    if not owner_controllers:
        raise RuntimeError(
            "BLOCKED: refusing actuation because the selected node has no explicit owner mapping"
        )
    controllers = {
        str(item.get("controller_uuid")): item
        for item in config.get("virtual_controllers") or []
        if isinstance(item, dict) and item.get("virt_active", True)
    }
    candidates = []
    requested_controller = kwargs.get("controller_uuid")
    requested_actuator = kwargs.get("actuator_id")
    for group in config.get("actuators") or []:
        if not isinstance(group, dict) or str(group.get("node_id")) != str(node_id):
            continue
        controller_uuid = str(group.get("controller_uuid") or "")
        if requested_controller and controller_uuid != str(requested_controller):
            continue
        if controller_uuid not in owner_controllers:
            continue
        rows = group.get("rows") if isinstance(group.get("rows"), list) else [group]
        for actuator in rows:
            if not isinstance(actuator, dict) or actuator.get("enabled", True) is False:
                continue
            if requested_actuator is not None and str(
                    actuator.get("actuator_id")) != str(requested_actuator):
                continue
            candidates.append((controller_uuid, group, actuator))
    if not candidates:
        raise RuntimeError("FAILED: no enabled owner actuator exists for " + str(node_id))
    candidates.sort(key=_target_sort_key)
    controller_uuid, group, actuator = candidates[0]
    controller = controllers.get(controller_uuid)
    if not isinstance(controller, dict):
        raise RuntimeError("FAILED: selected actuator has no virtual controller endpoint")
    host = str(
        controller.get("host") or controller.get("modbus_host") or controller.get("ip") or ""
    )
    port = int(controller.get("port") or controller.get("modbus_port") or 0)
    if not host or not 1 <= port <= 65535:
        raise RuntimeError("FAILED: selected Modbus endpoint is invalid")
    expected_host = str(kwargs.get("approved_host") or "").strip()
    expected_port = int(kwargs.get("approved_port") or 0)
    if not expected_host or not 1 <= expected_port <= 65535:
        raise RuntimeError("BLOCKED: approved Modbus actuator fixture is not configured")
    if host != expected_host or port != expected_port:
        raise RuntimeError(
            "BLOCKED: refusing actuation on unapproved Modbus endpoint {}:{}; "
            "approved fixture is {}:{}".format(host, port, expected_host, expected_port)
        )
    sensors = []
    requested_sensor = kwargs.get("sensor_id")
    for sensor_group in config.get("sensors") or []:
        if not isinstance(sensor_group, dict) or sensor_group.get("enabled", True) is False:
            continue
        if str(sensor_group.get("node_id") or node_id) != str(node_id):
            continue
        if str(sensor_group.get("controller_uuid") or "") != controller_uuid:
            continue
        rows = sensor_group.get("rows") \
            if isinstance(sensor_group.get("rows"), list) else [sensor_group]
        for sensor in rows:
            if not isinstance(sensor, dict) or sensor.get("enabled", True) is False:
                continue
            if requested_sensor is not None and str(
                    sensor.get("sensor_id")) != str(requested_sensor):
                continue
            sensors.append(copy.deepcopy(sensor))
    if not sensors:
        raise RuntimeError("BLOCKED: selected owner controller has no approved live sensor")
    sensors.sort(key=_sensor_sort_key)
    return {
        "controller_uuid": controller_uuid,
        "actuator": copy.deepcopy(actuator),
        "actuator_group": copy.deepcopy(group),
        "host": host,
        "port": port,
        "device_id": int(controller.get("unit_id") or controller.get("device_id") or 1),
        "sensor": sensors[0],
        "owner_node_id": str(node_id),
    }


def _target_sort_key(value):
    return (str(value[0]), str(value[2].get("actuator_id")))


def _sensor_sort_key(value):
    return str(value.get("sensor_id"))


def _sanitized_modbus_target_descriptor(target, approved_host, approved_port):
    if not isinstance(target, dict) or not isinstance(target.get("actuator"), dict):
        raise RuntimeError("BLOCKED: actuator recovery target descriptor is invalid")
    expected_host = str(approved_host or "").strip()
    try:
        expected_port = int(approved_port)
    except (TypeError, ValueError) as exc:
        raise RuntimeError("BLOCKED: approved Modbus recovery port is invalid") from exc
    if not expected_host or not 1 <= expected_port <= 65535:
        raise RuntimeError("BLOCKED: approved Modbus recovery endpoint is invalid")
    host = str(target.get("host") or "")
    try:
        port = int(target.get("port"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("BLOCKED: actuator recovery endpoint is invalid") from exc
    if host != expected_host or port != expected_port:
        raise RuntimeError(
            "BLOCKED: persisted actuator endpoint {}:{} differs from approved "
            "fixture {}:{}".format(host, port, expected_host, expected_port)
        )
    controller_uuid = str(target.get("controller_uuid") or "")
    try:
        uuid.UUID(controller_uuid)
    except (AttributeError, TypeError, ValueError) as exc:
        raise RuntimeError("BLOCKED: actuator recovery controller UUID is invalid") from exc
    owner_node_id = str(target.get("owner_node_id") or "")
    if re.fullmatch(r"[a-z0-9][a-z0-9._-]{0,127}", owner_node_id) is None:
        raise RuntimeError("BLOCKED: actuator recovery owner node is invalid")
    actuator = target["actuator"]
    actuator_id = actuator.get("actuator_id")
    if isinstance(actuator_id, bool) or not isinstance(actuator_id, (int, str)) \
            or not str(actuator_id) or len(str(actuator_id)) > 128 \
            or re.fullmatch(r"[A-Za-z0-9._:-]+", str(actuator_id)) is None:
        raise RuntimeError("BLOCKED: actuator recovery identifier is invalid")
    function_type = str(actuator.get("function_type") or "")
    if function_type not in ("write_coil", "write_register"):
        raise RuntimeError("BLOCKED: actuator recovery function is not reversible")
    bus_type = str(
        actuator.get("bus_type")
        or (target.get("actuator_group") or {}).get("bus_type")
        or "modbus"
    ).lower()
    if bus_type != "modbus":
        raise RuntimeError("BLOCKED: actuator recovery target is not Modbus")
    try:
        address = int(actuator.get("address"))
        device_id = int(target.get("device_id"))
    except (TypeError, ValueError) as exc:
        raise RuntimeError("BLOCKED: actuator recovery Modbus coordinates are invalid") from exc
    if not 0 <= address <= 65535 or not 0 <= device_id <= 247:
        raise RuntimeError("BLOCKED: actuator recovery Modbus coordinates are unsafe")
    return {
        "bus_type": "modbus",
        "host": host,
        "port": port,
        "device_id": device_id,
        "owner_node_id": owner_node_id,
        "controller_uuid": controller_uuid,
        "actuator_id": actuator_id,
        "address": address,
        "function_type": function_type,
    }


def _target_from_modbus_descriptor(descriptor, approved_host, approved_port):
    if not isinstance(descriptor, dict):
        raise RuntimeError("BLOCKED: persisted actuator target descriptor is unavailable")
    target = {
        "controller_uuid": descriptor.get("controller_uuid"),
        "actuator": {
            "actuator_id": descriptor.get("actuator_id"),
            "address": descriptor.get("address"),
            "function_type": descriptor.get("function_type"),
            "bus_type": descriptor.get("bus_type"),
        },
        "host": descriptor.get("host"),
        "port": descriptor.get("port"),
        "device_id": descriptor.get("device_id"),
        "owner_node_id": descriptor.get("owner_node_id"),
    }
    sanitized = _sanitized_modbus_target_descriptor(
        target, approved_host, approved_port)
    if sanitized != descriptor:
        raise RuntimeError("BLOCKED: persisted actuator target descriptor is not canonical")
    return target


def _pin_actuator_target_descriptor(context, target):
    recovery = context["recovery"]["actuator"]
    descriptor = _sanitized_modbus_target_descriptor(
        target,
        context["arguments"].approved_actuator_host,
        context["arguments"].approved_actuator_port,
    )
    existing = recovery.get("target_descriptor")
    if existing is not None and existing != descriptor:
        raise RuntimeError("BLOCKED: immutable actuator recovery target changed")
    recovery["target_descriptor"] = copy.deepcopy(descriptor)
    return descriptor


def _modbus_client(target):
    try:
        from pymodbus.client import ModbusTcpClient
    except ImportError as exc:
        raise RuntimeError("BLOCKED: pymodbus from requirements-e2e.txt is unavailable") from exc
    client = ModbusTcpClient(target["host"], port=target["port"], timeout=5.0)
    if not client.connect():
        client.close()
        raise RuntimeError("BLOCKED: approved Modbus simulator endpoint is unreachable")
    return client


def _read_actuator(target):
    function = str(target["actuator"].get("function_type") or "")
    address = int(target["actuator"].get("address"))
    client = _modbus_client(target)
    try:
        if function == "write_coil":
            response = _modbus_call(client.read_coils, {
                "address": address, "count": 1, "device_id": target["device_id"],
            })
            values = getattr(response, "bits", None)
            value = bool(values[0]) if values else None
        elif function == "write_register":
            response = _modbus_call(client.read_holding_registers, {
                "address": address, "count": 1, "device_id": target["device_id"],
            })
            values = getattr(response, "registers", None)
            value = int(values[0]) if values else None
        else:
            raise RuntimeError("BLOCKED: selected actuator function is not reversible")
        if getattr(response, "isError", _false_function)() or value is None:
            raise RuntimeError("FAILED: actuator baseline Modbus read failed")
        return value
    finally:
        client.close()


def _write_actuator_direct(target, value):
    function = str(target["actuator"].get("function_type") or "")
    address = int(target["actuator"].get("address"))
    if function == "write_coil":
        if not isinstance(value, bool):
            raise RuntimeError("BLOCKED: persisted coil restore value is not boolean")
        method_name = "write_coil"
    elif function == "write_register":
        if isinstance(value, bool) or not isinstance(value, int) \
                or not 0 <= value <= 65535:
            raise RuntimeError("BLOCKED: persisted register restore value is invalid")
        method_name = "write_register"
    else:
        raise RuntimeError("BLOCKED: persisted actuator function is not reversible")
    client = _modbus_client(target)
    try:
        response = _modbus_call(getattr(client, method_name), {
            "address": address,
            "value": value,
            "device_id": target["device_id"],
        })
        if getattr(response, "isError", _false_function)():
            raise RuntimeError("FAILED: direct persisted-descriptor actuator restore failed")
    finally:
        client.close()
    return {
        "success": True,
        "write_succeeded": True,
        "controller_uuid": target["controller_uuid"],
        "actuator_id": target["actuator"]["actuator_id"],
        "address": address,
        "function_type": function,
        "command_value": value,
    }


def _false_function():
    return False


def _inverse_value(value):
    if isinstance(value, bool):
        return not value
    if isinstance(value, int) and 0 <= value <= 65535:
        return 0 if value else 1
    raise RuntimeError("BLOCKED: actuator baseline cannot be safely inverted")


def _operation(result):
    reply = _result_payload(result)
    candidate = reply.get("operation") if isinstance(reply.get("operation"), dict) else reply
    return candidate if isinstance(candidate, dict) else {}


async def _wait_operation(context, control, operation_id):
    deadline = time.monotonic() + context["arguments"].operation_timeout
    latest = None
    while time.monotonic() < deadline:
        result = await _ws_command(context, control, {
            "action": "operation.get",
            "payload": {"operation_id": operation_id},
            "expect_result": True,
        })
        latest = _operation(result)
        state = str(latest.get("state") or latest.get("terminal_state") or "")
        if state in TERMINAL_STATES:
            return latest
        await asyncio.sleep(1.0)
    raise RuntimeError("FAILED: operation {} did not become terminal: {}".format(
        operation_id, _sanitize(latest)
    ))


def _verify_operation(operation, request):
    state = str(operation.get("state") or operation.get("terminal_state") or "")
    result = operation.get("result") if isinstance(operation.get("result"), dict) else {}
    if state != "CONFIRMED":
        raise RuntimeError("FAILED: actuator operation terminal state is " + state)
    for name in ("success", "write_succeeded", "readback_checked", "readback_verified"):
        if result.get(name) is not True:
            raise RuntimeError("FAILED: actuator operation lacks " + name)
    target = request["target"]
    expected = {
        "controller_uuid": target["controller_uuid"],
        "actuator_id": target["actuator"]["actuator_id"],
        "address": target["actuator"].get("address"),
        "function_type": target["actuator"].get("function_type"),
        "command_value": request["value"],
        "readback": request["value"],
    }
    for name, value in expected.items():
        if str(result.get(name)) != str(value):
            raise RuntimeError("FAILED: actuator result differs for " + name)
    return result


def _actuator_readiness_rejection(rejected):
    code = str(rejected.get("code") or "")
    message = str(rejected.get("message") or "")
    return code == "worker_not_synchronised" or (
        code == "not_found" and message == "controller has no active owner lease"
    )


async def _request_actuator(context, control, request):
    target = request["target"]
    value = request["value"]
    retry_window_s = min(30.0, float(context["arguments"].operation_timeout))
    deadline = time.monotonic() + retry_window_s
    readiness_retries = 0
    last_rejection = None
    while True:
        response = await _ws_command(context, control, {
            "action": "actuator.request",
            "payload": {
                "controller_uuid": target["controller_uuid"],
                "actuator_id": target["actuator"]["actuator_id"],
                "value": value,
                "idempotent": False,
                "device_command_id_supported": False,
            },
            "mutation": True,
            "expect_result": False,
            # Keep one key across all readiness attempts. A late response cannot
            # create a second actuator operation after an accepted request.
            "idempotency_key": request["idempotency_key"],
            "retryable_rejection_codes": (
                "not_found", "worker_not_synchronised",
            ),
        })
        last_rejection = response.get("rejected")
        if last_rejection is None:
            break
        if not _actuator_readiness_rejection(last_rejection):
            raise RuntimeError("FAILED: actuator.request rejected: {} {}".format(
                last_rejection.get("code"), last_rejection.get("message") or ""
            ))
        readiness_retries += 1
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise RuntimeError(
                "FAILED: actuator owner lease did not become ready within bounded "
                "window ({:.1f}s): {} {}".format(
                    retry_window_s,
                    last_rejection.get("code"),
                    last_rejection.get("message") or "",
                )
            )
        await asyncio.sleep(min(0.5, remaining))
    operation_id = response["accepted"].get("operation_id")
    if not operation_id:
        raise RuntimeError("FAILED: actuator.request has no operation_id")
    operation = await _wait_operation(context, control, operation_id)
    result = _verify_operation(operation, request)
    return {
        "operation_id": operation_id,
        "operation": operation,
        "result": result,
        "lease_readiness_retries": readiness_retries,
    }


async def _actuator_cycle(context, control, streams):
    events = streams["all"]
    operation_events = streams["operation"]
    selector = streams.get("selector") \
        if isinstance(streams.get("selector"), dict) else {}
    config = await _ws_command(context, control, {
        "action": "config.get",
        "payload": {"scope": _node_scope(context["arguments"].node_id)},
        "expect_result": True,
    })
    target = _select_target(
        _config_snapshot(config),
        context["arguments"].node_id,
        controller_uuid=selector.get("controller_uuid")
        or context["arguments"].controller_uuid,
        actuator_id=selector.get("actuator_id")
        if selector.get("actuator_id") is not None
        else context["arguments"].actuator_id,
        sensor_id=selector.get("sensor_id")
        if selector.get("sensor_id") is not None
        else context["arguments"].sensor_id,
        approved_host=context["arguments"].approved_actuator_host,
        approved_port=context["arguments"].approved_actuator_port,
    )
    original = _read_actuator(target)
    changed = _inverse_value(original)
    baseline_sensor = await _wait_event(context, events, {
        "event_type": "sensor.value",
        "controller_uuid": target["controller_uuid"],
        "sensor_id": target["sensor"]["sensor_id"],
    })
    context["actuator_target"] = target
    _pin_actuator_target_descriptor(context, target)
    context["recovery"]["actuator"].update({
        "mutated": True,
        "node_id": context["arguments"].node_id,
        "controller_uuid": target["controller_uuid"],
        "actuator_id": target["actuator"]["actuator_id"],
        "original_value": original,
    })
    _update_recovery(context)
    primary = await _request_actuator(context, control, {
        "target": target,
        "value": changed,
        "idempotency_key": context["run_id"] + ":actuator:primary",
    })
    event = await _wait_event(context, operation_events, {
        "event_type": "operation.result",
        "operation_id": primary["operation_id"],
        "terminal": True,
    })
    post_sensor = await _wait_event(context, events, {
        "event_type": "sensor.value",
        "controller_uuid": target["controller_uuid"],
        "sensor_id": target["sensor"]["sensor_id"],
    })
    observed = _read_actuator(target)
    if observed != changed:
        raise RuntimeError("FAILED: direct Modbus readback differs after actuator.request")
    return {
        "target": target,
        "original": original,
        "changed": changed,
        "primary": primary,
        "event": event,
        "baseline_sensor": baseline_sensor,
        "post_sensor": post_sensor,
    }


async def _restore_actuator(context, control):
    recovery = context["recovery"]["actuator"]
    if recovery["restore_confirmed"]:
        return {"skipped": True, "already_restored": True, "restore_confirmed": True}
    if not recovery["mutated"]:
        recovery["restore_confirmed"] = True
        _update_recovery(context)
        return {"skipped": True, "restore_confirmed": True}
    target = context.get("actuator_target")
    if not target:
        raise RuntimeError("FAILED: actuator recovery target is unavailable")
    persisted_target = _target_from_modbus_descriptor(
        recovery.get("target_descriptor"),
        context["arguments"].approved_actuator_host,
        context["arguments"].approved_actuator_port,
    )
    target_descriptor = _sanitized_modbus_target_descriptor(
        target,
        context["arguments"].approved_actuator_host,
        context["arguments"].approved_actuator_port,
    )
    if target_descriptor != recovery["target_descriptor"]:
        raise RuntimeError("BLOCKED: actuator recovery target differs from immutable journal")
    route = str(context.get("actuator_recovery_route") or "authority")
    if route == "persisted_descriptor":
        target = persisted_target
    observed_before_restore = _read_actuator(target)
    if observed_before_restore == recovery["original_value"]:
        restored = {
            "verified_noop": True,
            "write_performed": False,
            "readback": observed_before_restore,
        }
        restore_route = "verified_noop"
    elif route == "persisted_descriptor":
        restored = {
            "direct_modbus_restore": _write_actuator_direct(
                target, recovery["original_value"]),
        }
        restore_route = route
    else:
        restored = await _request_actuator(context, control, {
            "target": target,
            "value": recovery["original_value"],
            "idempotency_key": context["run_id"] + ":actuator:restore",
        })
        restore_route = route
    observed = _read_actuator(target)
    if observed != recovery["original_value"]:
        raise RuntimeError("FAILED: actuator restore direct Modbus readback differs")
    recovery["restore_confirmed"] = True
    recovery["mutated"] = False
    _update_recovery(context)
    return {
        "operation": restored,
        "restore_route": restore_route,
        "recovery_route": route,
        "readback_before_restore": observed_before_restore,
        "final_readback": observed,
        "restore_confirmed": True,
    }


async def _live_read_actions(context, control):
    requests = (
        {"action": "users.list", "payload": {"limit": 100}},
        {"action": "roles.list", "payload": {}},
        {"action": "permissions.list", "payload": {}},
        {"action": "api_tokens.list", "payload": {"limit": 100}},
        {"action": "sites.list", "payload": {}},
        {"action": "nodes.list", "payload": {}},
        {"action": "node.get", "payload": {"node_id": context["arguments"].node_id}},
        {"action": "runtime.status", "payload": {}},
        {"action": "config.get", "payload": {
            "scope": _node_scope(context["arguments"].node_id),
        }},
        {"action": "config.history", "payload": {"limit": 20}},
        {"action": "audit.list", "payload": {"limit": 100}},
    )
    results = {}
    for request in requests:
        prepared = dict(request, expect_result=True)
        results[request["action"]] = await _ws_command(context, control, prepared)
    node = _node(_nodes_list(results["nodes.list"]), context["arguments"].node_id)
    if not node:
        raise RuntimeError("FAILED: nodes.list does not contain the configured fn-01")
    _config_snapshot(results["config.get"])
    return results


def _fn02_registry_precondition(context, live_reads):
    node = _node(_nodes_list(live_reads["nodes.list"]), "fn-02")
    if node is None:
        context["report"]["worker_fn02"]["fixture_bootstrap_performed"] = True
        context["recovery"]["fn02_simulator"]["fixture_bootstrap_performed"] = True
        _update_recovery(context)
        return {"fixture_bootstrap_performed": True, "preexisting": False}
    if node.get("online") is True or str(node.get("observed_state") or "") == "ACTIVE":
        raise RuntimeError("BLOCKED: Master registry shows an active fn-02 before simulation")
    desired = str(node.get("desired_state") or "")
    if desired != "MAINTENANCE":
        raise RuntimeError("BLOCKED: preexisting fn-02 is not in a safe offline desired state")
    context["report"]["worker_fn02"]["fixture_bootstrap_performed"] = False
    context["recovery"]["fn02_simulator"]["previous_desired_state"] = desired
    _update_recovery(context)
    return {
        "fixture_bootstrap_performed": False,
        "preexisting": True,
        "desired_state": desired,
        "admission_state": node.get("admission_state"),
    }


async def _http_read_parity(context, live_reads):
    nodes_http = _http_request(context, {"method": "GET", "path": "/api/v1/nodes"})
    config_http = _http_request(context, {
        "method": "GET",
        "path": "/api/v1/config",
        "query": {"scope": _node_scope(context["arguments"].node_id)},
    })
    audit_http = _http_request(context, {"method": "GET", "path": "/api/v1/audit"})
    http_nodes = _find_key(nodes_http["body"], "nodes")
    if not isinstance(http_nodes, list):
        raise RuntimeError("FAILED: HTTPS nodes response has no nodes array")
    wss_ids = sorted(str(item.get("node_id")) for item in _nodes_list(
        live_reads["nodes.list"]
    ) if isinstance(item, dict))
    http_ids = sorted(str(item.get("node_id")) for item in http_nodes if isinstance(item, dict))
    if wss_ids != http_ids:
        raise RuntimeError("FAILED: HTTPS/WSS nodes.list semantic parity differs")
    wss_snapshot = _config_snapshot(live_reads["config.get"])
    http_snapshot = _config_snapshot({"result": config_http["body"]})
    http_revision = http_snapshot.get("revision")
    if isinstance(http_revision, bool) or not isinstance(http_revision, int) \
            or http_revision < 0:
        raise RuntimeError(
            "FAILED: HTTPS scoped config revision is not a non-negative integer"
        )
    identities = ("scope", "authority_epoch", "revision", "fingerprint")
    if any(http_snapshot.get(name) != wss_snapshot.get(name)
           for name in identities) or not _config_data_equal(
               http_snapshot["config_data"], wss_snapshot["config_data"]):
        raise RuntimeError("FAILED: HTTPS/WSS scoped config parity differs")
    wss_audit = _find_key(_result_payload(live_reads["audit.list"]), "audit")
    http_audit = _find_key(audit_http["body"], "audit")
    if not isinstance(wss_audit, list) or not isinstance(http_audit, list):
        raise RuntimeError("FAILED: HTTPS/WSS audit responses have no audit array")
    if not wss_audit or not http_audit:
        raise RuntimeError("FAILED: HTTPS/WSS audit parity has no decision entries")
    for transport, entries in (("WSS", wss_audit), ("HTTPS", http_audit)):
        invalid = [
            item.get("source_ip")
            for item in entries
            if isinstance(item, dict)
            and item.get("source_ip") is not None
            and not isinstance(item.get("source_ip"), str)
        ]
        if invalid:
            raise RuntimeError(
                "FAILED: {} audit source_ip is not canonical JSON text".format(
                    transport))
    return {
        "node_ids": wss_ids,
        "config_revision": int(http_revision),
        "wss_audit_rows": len(wss_audit),
        "https_audit_rows": len(http_audit),
    }


def _http_mutation_parity(context):
    response = _http_request(context, {
        "method": "PATCH",
        "path": "/api/v1/nodes/fn-e2e-http-missing",
        "body": {
            "action": "drain",
            "reason": "v0025 secondary REST fail-closed parity probe",
            "deadline_s": 30,
        },
        "csrf": not bool(context.get("bearer_token")),
        "idempotency_key": context["run_id"] + ":http:missing-node-drain",
        "expected_status": 404,
    })
    if response["body"].get("code") != "not_found":
        raise RuntimeError("FAILED: secondary REST mutation did not preserve not_found policy")
    return {"status": response["status"], "code": response["body"].get("code")}


async def _issue_test_token(context, control, spec):
    token_state = context["recovery"]["api_token"]
    existing = await _ws_command(context, control, {
        "action": "api_tokens.list", "payload": {"limit": 200}, "expect_result": True,
    })
    rows = _find_key(_result_payload(existing), "tokens")
    rows = rows if isinstance(rows, list) else []
    if len(rows) >= 190:
        raise RuntimeError("BLOCKED: token recovery requires fewer than 190 principal tokens")
    if any(isinstance(row, dict) and row.get("name") == spec["name"] for row in rows):
        raise RuntimeError("BLOCKED: exact E2E token name already exists")
    if spec["name"] not in token_state["pending_names"]:
        token_state["pending_names"].append(spec["name"])
    token_state["created"] = True
    token_state["revoked"] = False
    _update_recovery(context)
    created = await _ws_command(context, control, {
        "action": "api_tokens.create",
        "payload": {
            "name": spec["name"],
            "permissions": copy.deepcopy(spec["permissions"]),
            "site_scopes": copy.deepcopy(spec["site_scopes"]),
        },
        "mutation": True,
        "expect_result": True,
        "idempotency_key": spec["idempotency_key"],
    })
    token_id = _find_key(_result_payload(created), "token_id")
    raw_token = _find_key(_result_payload(created), "token")
    if not token_id or not isinstance(raw_token, str) or len(raw_token) < 32:
        raise RuntimeError("FAILED: api_tokens.create returned no one-time token material")
    token_state["token_id"] = str(token_id)
    token_state["token_names"][spec["name"]] = str(token_id)
    if str(token_id) not in token_state["token_ids"]:
        token_state["token_ids"].append(str(token_id))
    _update_recovery(context)
    context["secret_canaries"] = tuple(context.get("secret_canaries") or ()) + (raw_token,)
    return {"created": created, "token_id": str(token_id), "raw_token": raw_token}


async def _revoke_test_token(context, control, token_id):
    revoked = await _ws_command(context, control, {
        "action": "api_tokens.revoke",
        "payload": {"token_id": str(token_id)},
        "mutation": True,
        "expect_result": True,
        "idempotency_key": context["run_id"] + ":api-token:revoke:" + str(token_id),
    })
    token = context["recovery"]["api_token"]
    if str(token_id) not in token["revoked_ids"]:
        token["revoked_ids"].append(str(token_id))
    token["revoked"] = set(token["token_ids"]).issubset(set(token["revoked_ids"]))
    _update_recovery(context)
    return revoked


async def _token_cycle(context, control):
    empty = await _issue_test_token(context, control, {
        "name": "e2e-empty-v0025-" + context["run_id"],
        "permissions": [],
        "site_scopes": [],
        "idempotency_key": context["run_id"] + ":api-token:empty:create",
    })
    token_id = empty["token_id"]
    raw_token = empty["raw_token"]
    listed = await _ws_command(context, control, {
        "action": "api_tokens.list", "payload": {"limit": 200}, "expect_result": True,
    })
    rows = _find_key(_result_payload(listed), "tokens")
    row = None
    for candidate in rows if isinstance(rows, list) else []:
        if isinstance(candidate, dict) and str(candidate.get("token_id")) == str(token_id):
            row = candidate
            break
    if not row or row.get("permissions") != [] or row.get("site_scopes") != []:
        raise RuntimeError("FAILED: explicit empty API-token scopes were not persisted exactly")
    denied = await _expect_upgrade_rejection(context, {
        "url": context["servers"]["wss"]["url"] + "/ws/v1/control",
        "origin": context["arguments"].origin,
        "headers": {"Authorization": "Bearer " + raw_token},
        "statuses": (403,),
    })
    empty_revoked = await _revoke_test_token(context, control, token_id)
    raw_token = ""
    empty["raw_token"] = ""

    limited = await _issue_test_token(context, control, {
        "name": "e2e-limited-v0025-" + context["run_id"],
        "permissions": ["node.read"],
        "site_scopes": [context["arguments"].site_id],
        "idempotency_key": context["run_id"] + ":api-token:limited:create",
    })
    limited_socket = None
    revoked_result = None
    revocation_observed = False
    try:
        limited_socket = await websockets.connect(
            context["servers"]["wss"]["url"] + "/ws/v1/control",
            ssl=context["ssl_context"],
            origin=context["arguments"].origin,
            additional_headers={"Authorization": "Bearer " + limited["raw_token"]},
            user_agent_header=USER_AGENT,
            open_timeout=context["arguments"].command_timeout,
            close_timeout=5.0,
            proxy=None,
        )
        ready = await _receive_json(context, limited_socket)
        _validate_schema(context, "command_response", ready)
        if ready.get("type") != "connection.ready":
            raise RuntimeError("FAILED: limited-scope API token did not open control WSS")
        scoped_denial = await _ws_command(context, limited_socket, {
            "action": "node.drain",
            "payload": {
                "node_id": "fn-e2e-scope-denied",
                "reason": "permission scope probe",
                "deadline_s": 30,
            },
            "mutation": True,
            "expected_rejection": True,
            "allowed_codes": ("authorization_denied",),
            "idempotency_key": context["run_id"] + ":token-scope-denied",
        })
        revoked_result = await _revoke_test_token(context, control, limited["token_id"])
        try:
            await _ws_command(context, limited_socket, {
                "action": "nodes.list",
                "payload": {},
                "expected_rejection": True,
                "allowed_codes": (
                    "authentication_required", "authentication_expired", "authorization_denied",
                ),
            })
            await asyncio.wait_for(
                limited_socket.wait_closed(), timeout=context["arguments"].event_timeout
            )
            revocation_observed = limited_socket.close_code == 1008
        except ConnectionClosed as exc:
            revocation_observed = exc.code == 1008
        if not revocation_observed:
            raise RuntimeError("FAILED: revoked API-token WSS did not close with 1008")
    finally:
        if limited_socket:
            await limited_socket.close(code=1000)
        limited["raw_token"] = ""
    return {
        "empty_created": True,
        "empty_scope_denied": denied,
        "empty_revoked": bool(empty_revoked),
        "limited_scope_denied": bool(scoped_denial),
        "revocation_observed": revocation_observed,
        "limited_revoked": bool(revoked_result),
        "token_ids": [str(token_id), limited["token_id"]],
    }


async def _revoke_recovery_token(context, control):
    token = context["recovery"]["api_token"]
    if not token["created"] and not token["pending_names"]:
        return True
    listed = await _ws_command(context, control, {
        "action": "api_tokens.list", "payload": {"limit": 200}, "expect_result": True,
    })
    rows = _find_key(_result_payload(listed), "tokens")
    rows = [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []
    by_id = {str(row.get("token_id") or ""): row for row in rows if row.get("token_id")}
    for name in token["pending_names"]:
        candidates = [row for row in rows if str(row.get("name") or "") == str(name)]
        expected_id = str(token["token_names"].get(name) or "")
        if expected_id:
            candidates = [row for row in candidates if str(row.get("token_id")) == expected_id]
        if len(candidates) != 1:
            raise RuntimeError(
                "FAILED: test-token recovery cannot prove exactly one token for " + str(name)
            )
        token_id = str(candidates[0]["token_id"])
        token["token_names"][name] = token_id
        if token_id not in token["token_ids"]:
            token["token_ids"].append(token_id)
        if candidates[0].get("revoked_at") is not None:
            if token_id not in token["revoked_ids"]:
                token["revoked_ids"].append(token_id)
            _update_recovery(context)
            continue
        if token_id in token["revoked_ids"]:
            raise RuntimeError("FAILED: token journal claims revoke but store is still active")
        _update_recovery(context)
        await _ws_command(context, control, {
            "action": "api_tokens.revoke",
            "payload": {"token_id": token_id},
            "mutation": True,
            "expect_result": True,
            "idempotency_key": context["run_id"] + ":api-token:cleanup:" + token_id,
        })
        token["revoked_ids"].append(token_id)
        _update_recovery(context)
    expected_ids = set(token["token_names"].values())
    if not expected_ids or not expected_ids.issubset(set(token["revoked_ids"])):
        raise RuntimeError("FAILED: test-token recovery did not revoke every known token")
    if any(token_id not in by_id for token_id in expected_ids):
        raise RuntimeError("FAILED: test-token recovery list was truncated or principal-mismatched")
    token["revoked"] = True
    _update_recovery(context)
    return True


async def _safe_negative_actions(context, control):
    requests = (
        {"action": "user.create", "payload": {}},
        {"action": "node.drain", "payload": {
            "node_id": "fn-e2e-missing", "reason": "negative fixture", "deadline_s": 30,
        }},
        {"action": "node.state.set", "payload": {
            "node_id": "fn-e2e-missing", "desired_state": "ACTIVE", "reason": "negative fixture",
        }},
        {"action": "node.admission.approve", "payload": {
            "node_id": "fn-e2e-missing", "reason": "negative fixture",
        }},
        {"action": "terminal.execute", "payload": {"command": "__e2e_forbidden__"}},
        {"action": "automation.unknown", "payload": {}},
    )
    results = {}
    for index, request in enumerate(requests):
        prepared = dict(request)
        prepared.update({
            "mutation": True,
            "expect_result": False,
            "expected_rejection": True,
            "allowed_codes": (
                "invalid_payload", "not_found", "authorization_denied", "conflict",
            ),
            "idempotency_key": context["run_id"] + ":negative:" + str(index),
            "allow_invalid_contract": True,
        })
        results[request["action"]] = await _ws_command(context, control, prepared)
    return results


async def _csrf_checks(context, control):
    if context.get("bearer_token"):
        return {"skipped": True, "reason": "bearer authentication has no CSRF contract"}
    results = {}
    variants = (
        {"name": "missing", "omit_csrf": True},
        {"name": "invalid", "csrf_token": "e2e-invalid-csrf-" + secrets.token_hex(16)},
    )
    for index, variant in enumerate(variants):
        request = {
            "action": "node.drain",
            "payload": {
                "node_id": "fn-e2e-csrf-missing",
                "reason": "CSRF fail-closed probe",
                "deadline_s": 30,
            },
            "mutation": True,
            "expected_rejection": True,
            "allowed_codes": ("authorization_denied",),
            "idempotency_key": context["run_id"] + ":csrf:" + str(index),
        }
        request.update(variant)
        if variant.get("csrf_token"):
            context["secret_canaries"] = tuple(context.get("secret_canaries") or ()) + (
                variant["csrf_token"],
            )
        results[variant["name"]] = await _ws_command(context, control, request)
    return results


def _node_local_config_diff_payload(mutation):
    from_revision = max(1, int(mutation["before"]["revision"]))
    to_revision = max(from_revision + 1, int(mutation["after"]["revision"]))
    return {"from_revision": from_revision, "to_revision": to_revision}


async def _config_diff_check(context, control, mutation):
    canonical = _canonical_scope_operations(
        mutation["before"]["config_data"],
        mutation["after"]["config_data"],
    )
    if not canonical["operations"]:
        raise RuntimeError("FAILED: canonical scope diff returned no operations")
    diff_payload = _node_local_config_diff_payload(mutation)
    rejected = await _ws_command(context, control, {
        "action": "config.diff",
        "payload": diff_payload,
        "expected_rejection": True,
        "allowed_codes": ("unsupported_operation_format",),
    })
    rejection = rejected.get("rejected") \
        if isinstance(rejected, dict) else None
    if not isinstance(rejection, dict):
        raise RuntimeError("FAILED: node_local config.diff was not rejected")
    return {
        "result": {
            "operation_format": canonical["operation_format"],
            "operation_count": len(canonical["operations"]),
            "legacy_diff_rejected": True,
            "rejection_code": str(rejection.get("code") or ""),
        },
    }


async def _expect_upgrade_rejection(context, request):
    try:
        socket = await websockets.connect(
            request["url"],
            ssl=request.get("ssl_context", context["ssl_context"]),
            origin=request.get("origin"),
            additional_headers=request.get("headers"),
            user_agent_header=USER_AGENT,
            open_timeout=context["arguments"].command_timeout,
            proxy=None,
            server_hostname=request.get("server_hostname"),
        )
    except Exception as exc:
        if request.get("tls_failure"):
            if isinstance(exc, (ssl.SSLError, OSError)) and not isinstance(exc, InvalidStatus):
                return {"tls_rejected": True}
            raise RuntimeError("FAILED: invalid TLS identity reached HTTP Upgrade") from exc
        if not isinstance(exc, InvalidStatus):
            raise RuntimeError("FAILED: WSS rejection did not return a bounded HTTP status") from exc
        status = int(exc.response.status_code)
        allowed = tuple(int(value) for value in request.get("statuses") or ())
        if allowed and status not in allowed:
            raise RuntimeError("FAILED: WSS rejection returned unexpected status " + str(status))
        return {"status": status}
    await socket.close()
    raise RuntimeError("FAILED: forbidden WebSocket Upgrade was accepted")


async def _path_and_origin_checks(context):
    headers = _wss_headers(context)
    wss = context["servers"]["wss"]["url"]
    https = context["servers"]["https"]["url"].replace("https://", "wss://", 1)
    valid = wss + "/ws/v1/control"
    invalid_token = "e2e-invalid-bearer-" + secrets.token_hex(16)
    query_canary = "e2e-query-secret-" + secrets.token_hex(16)
    context["secret_canaries"] = tuple(context.get("secret_canaries") or ()) + (
        invalid_token, query_canary,
    )
    checks = {}
    checks["https_upgrade"] = await _expect_upgrade_rejection(context, {
        "url": https + "/ws/v1/control", "origin": context["arguments"].origin,
        "headers": headers, "statuses": (400, 404, 426),
    })
    checks["unknown_path"] = await _expect_upgrade_rejection(context, {
        "url": wss + "/ws/v1/unknown", "origin": context["arguments"].origin,
        "headers": headers, "statuses": (404,),
    })
    checks["query_secret"] = await _expect_upgrade_rejection(context, {
        "url": valid + "?token=" + query_canary, "origin": context["arguments"].origin,
        "headers": headers, "statuses": (400,),
    })
    checks["foreign_origin"] = await _expect_upgrade_rejection(context, {
        "url": valid, "origin": "https://origin-denied.invalid",
        "headers": headers, "statuses": (403,),
    })
    checks["missing_origin"] = await _expect_upgrade_rejection(context, {
        "url": valid, "origin": None, "headers": headers, "statuses": (403,),
    })
    checks["missing_auth"] = await _expect_upgrade_rejection(context, {
        "url": valid, "origin": context["arguments"].origin,
        "headers": {}, "statuses": (401,),
    })
    checks["invalid_auth"] = await _expect_upgrade_rejection(context, {
        "url": valid, "origin": context["arguments"].origin,
        "headers": {"Authorization": "Bearer " + invalid_token}, "statuses": (401, 403),
    })
    checks["wrong_hostname"] = await _expect_upgrade_rejection(context, {
        "url": valid, "origin": context["arguments"].origin, "headers": headers,
        "server_hostname": "wrong-hostname.invalid", "tls_failure": True,
    })
    untrusted = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    untrusted.minimum_version = ssl.TLSVersion.TLSv1_2
    untrusted.check_hostname = True
    untrusted.verify_mode = ssl.CERT_REQUIRED
    checks["wrong_ca"] = await _expect_upgrade_rejection(context, {
        "url": valid, "origin": context["arguments"].origin, "headers": headers,
        "ssl_context": untrusted, "tls_failure": True,
    })
    invalid_token = ""
    query_canary = ""
    return checks


def _parallel_read_request(index, node_id):
    requests = (
        {"action": "users.list", "payload": {"limit": 1}},
        {"action": "roles.list", "payload": {}},
        {"action": "permissions.list", "payload": {}},
        {"action": "api_tokens.list", "payload": {"limit": 1}},
        {"action": "sites.list", "payload": {}},
        {"action": "nodes.list", "payload": {}},
        {"action": "runtime.status", "payload": {}},
        {"action": "config.get", "payload": {"scope": _node_scope(node_id)}},
        {"action": "config.history", "payload": {"limit": 1}},
        {"action": "audit.list", "payload": {"limit": 1}},
    )
    return copy.deepcopy(requests[int(index) % len(requests)])


async def _open_parallel_client(context, index):
    async with context["parallel_handshake_semaphore"]:
        if int(index) % 2:
            stream = await _open_events(
                context,
                [],
                last_event_id=int(context["parallel_event_last_event_id"]),
                align_to_high_watermark=False,
            )
            return {"kind": "event", "socket": stream["socket"], "stream": stream}
        socket = await _open_control(context)
        return {
            "kind": "control",
            "socket": socket,
            "request": _parallel_read_request(
                index, context["arguments"].node_id),
        }


async def _parallel_client_read(context, client):
    if client["kind"] != "control":
        return {"event_subscription": True}
    request = copy.deepcopy(client["request"])
    request["expect_result"] = True
    result = await _ws_command(context, client["socket"], request)
    return {"action": request["action"], "result": bool(_result_payload(result))}


async def _parallel_event_cursor(context):
    # Align once while the server is far below its principal limit. Reusing
    # this cursor prevents every Event client from opening a disposable
    # alignment socket immediately before its retained parallel-test socket.
    stream = await _open_events(context, [])
    try:
        cursor = stream["accepted"].get("replay_high_watermark")
        if isinstance(cursor, bool) or not isinstance(cursor, int) or cursor < 0:
            raise RuntimeError("FAILED: parallel Event cursor is invalid")
        return cursor
    finally:
        await stream["socket"].close(code=1000)


def _parallel_failure_summary(failures):
    summary = {}
    for failure in failures:
        response = getattr(failure, "response", None)
        status = getattr(response, "status_code", None)
        label = type(failure).__name__
        if isinstance(status, int):
            label += ":http-" + str(status)
        summary[label] = summary.get(label, 0) + 1
    return summary


def _parallel_failure_is_transient(failure):
    response = getattr(failure, "response", None)
    status = getattr(response, "status_code", None)
    return status in (429, 503) or isinstance(failure, (TimeoutError, OSError))


def _parallel_wss_is_stable(wss, expected_clients):
    return (
        isinstance(wss, dict)
        and int(wss.get("clients") or 0) == int(expected_clients)
        and int(wss.get("reserved_connections") or 0) == 0
        and "pending_principal_connections" in wss
        and int(wss.get("pending_principal_connections") or 0) == 0
        and int(wss.get("response_queue_depth") or 0) == 0
    )


async def _wait_parallel_wss_state(context, expected_clients, deadline=None):
    local_deadline = time.monotonic() + min(
        30.0, float(context["arguments"].operation_timeout)
    )
    deadline = local_deadline if deadline is None else min(
        float(deadline), local_deadline
    )
    last = None
    stable_samples = 0
    while time.monotonic() < deadline:
        status = await _ws_command(context, context["primary_control"], {
            "action": "runtime.status", "payload": {}, "expect_result": True,
        })
        proxy = _result_payload(status).get("proxy")
        last = proxy.get("wss") if isinstance(proxy, dict) else None
        if _parallel_wss_is_stable(last, expected_clients):
            stable_samples += 1
            if stable_samples >= 5:
                return last
        else:
            stable_samples = 0
        await asyncio.sleep(0.1)
    observed = last if isinstance(last, dict) else {}
    raise RuntimeError(
        "FAILED: Proxy WSS state did not drain to the parallel-test baseline "
        "(expected_clients={}, clients={}, reservations={}, pending={}, "
        "responses={})".format(
            int(expected_clients),
            int(observed.get("clients") or 0),
            int(observed.get("reserved_connections") or 0),
            int(observed.get("pending_principal_connections") or 0),
            int(observed.get("response_queue_depth") or 0),
        )
    )


async def _parallel_smoke(context, retained_clients):
    requested_total = int(context["arguments"].parallel_clients)
    additional = max(0, requested_total - int(retained_clients))
    parallel_event_cursor = await _parallel_event_cursor(context)
    context["parallel_event_last_event_id"] = parallel_event_cursor
    baseline = await _wait_parallel_wss_state(context, retained_clients)
    context["parallel_handshake_semaphore"] = asyncio.Semaphore(16)
    opened = await asyncio.gather(*(
        _open_parallel_client(context, index) for index in range(additional)
    ), return_exceptions=True)
    clients = []
    failures = []
    failed_indexes = []
    for index, value in enumerate(opened):
        if isinstance(value, dict):
            clients.append(value)
        else:
            failed_indexes.append(index)
            failures.append(
                value if isinstance(value, BaseException)
                else RuntimeError("parallel client returned an invalid result")
            )
    initial_failures = len(failures)
    retry_attempts = 0
    retry_window_s = min(30.0, float(context["arguments"].operation_timeout))
    try:
        if failed_indexes:
            retry_failures = []
            pending_indexes = list(failed_indexes)
            last_failures = dict(zip(failed_indexes, failures))
            retry_deadline = time.monotonic() + retry_window_s
            await _wait_parallel_wss_state(
                context, int(retained_clients) + len(clients), retry_deadline
            )
            # Retry transiently failed path identities one at a time inside a
            # shared recovery window. A quiet server-side snapshot is required
            # before every attempt so a late teardown cannot consume the exact
            # principal-limit slot indefinitely. Permanent authentication or
            # protocol failures remain fail-closed.
            while pending_indexes:
                index = pending_indexes.pop(0)
                if time.monotonic() >= retry_deadline:
                    retry_failures.extend(
                        last_failures[item] for item in [index] + pending_indexes
                    )
                    break
                await _wait_parallel_wss_state(
                    context, int(retained_clients) + len(clients), retry_deadline
                )
                retried = await asyncio.gather(
                    _open_parallel_client(context, index),
                    return_exceptions=True,
                )
                retry_attempts += 1
                value = retried[0]
                if isinstance(value, dict):
                    clients.append(value)
                    last_failures.pop(index, None)
                    # The shared deadline limits opening attempts. The next
                    # iteration confirms this accepted client before another
                    # attempt; the final accepted client is confirmed below
                    # with its own bounded state-drain window.
                else:
                    failure = (
                        value if isinstance(value, BaseException)
                        else RuntimeError("parallel retry returned an invalid result")
                    )
                    last_failures[index] = failure
                    if _parallel_failure_is_transient(failure) and \
                            time.monotonic() < retry_deadline:
                        pending_indexes.append(index)
                        await asyncio.sleep(min(
                            0.25, max(0.0, retry_deadline - time.monotonic())
                        ))
                    else:
                        retry_failures.append(failure)
                        last_failures.pop(index, None)
            failures = retry_failures
        if failures or len(clients) != additional:
            raise RuntimeError(
                "FAILED: parallel WSS handshakes did not reach the required "
                "client count (opened={}/{}, initial_failures={}, "
                "retry_attempts={}, remaining_failures={})".format(
                    len(clients), additional, initial_failures,
                    retry_attempts,
                    _parallel_failure_summary(failures),
                )
            )
        await _wait_parallel_wss_state(context, requested_total)
        reads = await asyncio.gather(*(
            _parallel_client_read(context, client) for client in clients
        ), return_exceptions=True)
        read_failures = [value for value in reads if isinstance(value, BaseException)]
        if read_failures:
            raise RuntimeError("FAILED: parallel WSS read smoke failed") from read_failures[0]
    finally:
        await asyncio.gather(*(
            client["socket"].close(code=1000) for client in clients
        ), return_exceptions=True)
        context.pop("parallel_event_last_event_id", None)
    wss = await _wait_parallel_wss_state(context, retained_clients)
    if (
        # Each retained direct-WSS connection owns reader, writer,
        # authentication-watchdog and disconnect-watcher tasks.  The
        # runtime.status command itself may be the one additional in-flight
        # task represented by the snapshot.
        int(wss.get("task_count") or 0) > int(retained_clients) * 4 + 1
    ):
        raise RuntimeError("FAILED: parallel WSS smoke left clients, reservations or responses")
    return {
        "simultaneous_clients": requested_total,
        "baseline_clients": int(baseline.get("clients") or 0),
        "initial_handshake_failures": initial_failures,
        "handshake_retries": retry_attempts,
        "retry_mode": "bounded_sequential_state_synchronized",
        "retry_window_seconds": retry_window_s,
        "event_cursor_alignment": "single_shared_preflight",
        "event_cursor": parallel_event_cursor,
        "stable_samples_required": 5,
        "control_clients_exercised": sum(1 for item in clients if item["kind"] == "control"),
        "event_clients_exercised": sum(1 for item in clients if item["kind"] == "event"),
        "remaining_clients": int(wss.get("clients") or 0),
        "remaining_task_count": int(wss.get("task_count") or 0),
    }


async def _pagination_producer(context, options):
    socket = await _open_control(context)
    produced = 0
    requests = (
        {"action": "nodes.list", "payload": {}},
        {"action": "node.get", "payload": {
            "node_id": context["arguments"].node_id,
        }},
        {"action": "runtime.status", "payload": {}},
        {"action": "config.get", "payload": {
            "scope": _node_scope(context["arguments"].node_id),
        }},
        {"action": "config.history", "payload": {"limit": 1}},
    )
    try:
        for index in range(int(options["count"])):
            action_index = (int(options["offset"]) + index) % len(requests)
            request = copy.deepcopy(requests[action_index])
            request.update({
                "expect_result": True,
                "request_id": "page-{}-{}-{}".format(
                    hashlib.sha256(context["run_id"].encode("utf-8")).hexdigest()[:16],
                    options["worker"], index
                ),
            })
            result = await _ws_command(context, socket, request)
            if result.get("result") is None:
                raise RuntimeError(
                    "FAILED: replay pagination producer did not receive a durable result"
                )
            produced += 1
    finally:
        await socket.close(code=1000)
    return produced


def _pagination_retained_filters(filters):
    return [
        copy.deepcopy(item)
        for item in filters or []
        if isinstance(item, dict) and item.get("resource_type") != "command"
    ]


async def _replay_pagination_check(context, control, retained_stream):
    retained_filters = _pagination_retained_filters(
        retained_stream["request"].get("filters")
    )
    if retained_filters != retained_stream["request"].get("filters"):
        retained_transition = await _update_event_filters(
            context, retained_stream, retained_filters
        )
    else:
        retained_transition = None
    status = await _ws_command(context, control, {
        "action": "runtime.status", "payload": {}, "expect_result": True,
    })
    proxy = _result_payload(status).get("proxy")
    wss = proxy.get("wss") if isinstance(proxy, dict) else None
    page_size = wss.get("replay_page_size") if isinstance(wss, dict) else None
    if isinstance(page_size, bool) or not isinstance(page_size, int) \
            or not 1 <= page_size <= 500:
        raise RuntimeError(
            "FAILED: runtime.status does not expose the bounded WSS replay page size"
        )

    filters = [{"resource_type": "command", "resource_pattern": "*"}]
    marker = await _open_events(context, filters)
    after_sequence = marker["accepted"].get("replay_high_watermark")
    await marker["socket"].close(code=1000)
    if not isinstance(after_sequence, int) or after_sequence < 0:
        raise RuntimeError("FAILED: replay pagination marker is invalid")
    target_count = page_size + 17
    worker_count = min(64, max(4, (target_count + 8) // 9))
    base = target_count // worker_count
    remainder = target_count % worker_count
    jobs = []
    offset = 0
    for worker in range(worker_count):
        count = base + int(worker < remainder)
        jobs.append(_pagination_producer(context, {
            "worker": worker,
            "count": count,
            "offset": offset,
        }))
        offset += count
    results = await asyncio.gather(*jobs, return_exceptions=True)
    failures = [item for item in results if isinstance(item, BaseException)]
    if failures or sum(item for item in results if isinstance(item, int)) != target_count:
        raise RuntimeError("FAILED: replay pagination event production failed") from (
            failures[0] if failures else None
        )
    replay = await _open_events(context, filters, last_event_id=after_sequence)
    high_watermark = replay["accepted"].get("replay_high_watermark")
    if not isinstance(high_watermark, int) or high_watermark <= after_sequence:
        await replay["socket"].close(code=1000)
        raise RuntimeError("FAILED: replay pagination high-watermark did not advance")
    marker_id = "page-live-marker-" + secrets.token_hex(8)
    replayed = []
    seen_ids = set()
    live_marker = None
    deadline = time.monotonic() + context["arguments"].operation_timeout
    try:
        marker_result = await _ws_command(context, control, {
            "action": "nodes.list",
            "payload": {},
            "expect_result": True,
            "request_id": marker_id,
        })
        if marker_result.get("result") is None:
            raise RuntimeError(
                "FAILED: replay pagination live marker did not receive a durable result"
            )
        while time.monotonic() < deadline:
            event = await _receive_json(
                context, replay["socket"], timeout=deadline - time.monotonic()
            )
            _validate_schema(context, "event_envelope", event)
            if event.get("site_id") != context["arguments"].site_id \
                    or event.get("event_type") != "command.ack":
                raise RuntimeError("FAILED: command replay filter leaked another event")
            extensions = event.get("extensions") \
                if isinstance(event.get("extensions"), dict) else {}
            sequence = extensions.get("replay_sequence")
            if isinstance(sequence, int) and sequence <= high_watermark:
                message_id = str(event.get("message_id") or "")
                if not message_id or message_id in seen_ids:
                    raise RuntimeError("FAILED: replay pagination duplicated an event")
                seen_ids.add(message_id)
                replayed.append(sequence)
            live_sequence = extensions.get("live_sequence")
            if isinstance(live_sequence, int) and live_sequence > high_watermark:
                live_marker = event
                break
    finally:
        await replay["socket"].close(code=1000)
    live_extensions = live_marker.get("extensions") \
        if isinstance(live_marker, dict) \
        and isinstance(live_marker.get("extensions"), dict) else {}
    if live_marker is None or len(replayed) < target_count:
        raise RuntimeError(
            "FAILED: multi-page replay did not reach the live handoff marker "
            "(replayed={}, target={}, after_sequence={}, high_watermark={}, "
            "live_marker_sequence={})".format(
                len(replayed), target_count, after_sequence, high_watermark,
                live_extensions.get("live_sequence"),
            )
        )
    if replayed != sorted(replayed) or len(set(replayed)) != len(replayed):
        raise RuntimeError("FAILED: multi-page replay sequence is not strictly monotonic")
    if int(live_extensions.get("live_sequence") or 0) <= high_watermark:
        raise RuntimeError("FAILED: replay/live handoff marker is not after the high-watermark")
    return {
        "page_size": page_size,
        "records_produced": target_count,
        "records_replayed": len(replayed),
        "pages_minimum": 2,
        "replay_high_watermark": high_watermark,
        "live_marker_sequence": live_extensions.get("live_sequence"),
        "retained_stream_generation": (
            retained_transition["accepted"].get("generation")
            if retained_transition else retained_stream["accepted"].get("generation")
        ),
        "retained_stream_command_filter_removed": True,
    }


def _read_argv(path, label):
    try:
        value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("BLOCKED: invalid {}: {}".format(label, exc)) from exc
    if not isinstance(value, list) or not value:
        raise RuntimeError("BLOCKED: {} must be a non-empty argv array".format(label))
    if any(not isinstance(item, str) or not item for item in value):
        raise RuntimeError("BLOCKED: {} contains an invalid argv item".format(label))
    return value


def _subprocess_json(command, label):
    completed = subprocess.run(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=120,
    )
    lines = [line for line in completed.stdout.splitlines() if line.strip()]
    try:
        result = json.loads(lines[-1]) if lines else {}
    except json.JSONDecodeError as exc:
        raise RuntimeError("FAILED: {} returned no machine-readable result".format(label)) from exc
    status = result.get("status")
    if completed.returncode == STATUS_CODES["BLOCKED"] or status == "BLOCKED":
        raise RuntimeError("BLOCKED: {}: {}".format(label, result.get("detail") or "blocked"))
    if completed.returncode != 0 or status != "PASSED":
        raise RuntimeError("FAILED: {}: {}".format(label, result.get("detail") or "failed"))
    return result


def _launcher_command(context, action):
    prefix = context["worker_launcher_prefix"]
    return list(prefix) + ["--action", action, "--run-id", context["run_id"]]


async def _launcher_call(context, action):
    return await asyncio.to_thread(
        _subprocess_json,
        _launcher_command(context, action),
        "Worker launcher " + action,
    )


def _simulator_check_command(context):
    script = pathlib.Path(__file__).with_name("fn02_protocol_simulator.py")
    return [
        sys.executable,
        str(script),
        "--config", context["arguments"].fn02_runtime_config,
        "--broker-introspection-proof", context["arguments"].broker_introspection_proof,
        "--broker-proof-public-key", context["arguments"].broker_proof_public_key,
        "--broker-proof-key-sha256", context["arguments"].broker_proof_key_sha256,
        "--fixture", context["arguments"].cross_fixture,
        "--run-id", context["run_id"],
        "--check-only",
    ]


async def _full_lab_preflight(context):
    arguments = context["arguments"]
    required = {
        "--broker-introspection-proof": arguments.broker_introspection_proof,
        "--broker-proof-public-key": arguments.broker_proof_public_key,
        "--broker-proof-key-sha256": arguments.broker_proof_key_sha256,
        "--fn02-runtime-config": arguments.fn02_runtime_config,
        "--worker-launcher-command-file": arguments.worker_launcher_command_file,
    }
    missing = [name for name, value in required.items() if not value]
    if not arguments.broker_release_proof:
        missing.append("--broker-release-proof")
    if missing:
        raise RuntimeError("BLOCKED: full-lab misses " + ", ".join(missing))
    pinned_hash = str(arguments.broker_proof_key_sha256 or "")
    if len(pinned_hash) != 64 or any(
            character not in "0123456789abcdef" for character in pinned_hash):
        raise RuntimeError(
            "BLOCKED: --broker-proof-key-sha256 must be 64 lowercase hex characters"
        )
    for label, value in required.items():
        if not pathlib.Path(value).expanduser().is_file():
            raise RuntimeError("BLOCKED: full-lab prerequisite unavailable: " + label)
    release_parent = pathlib.Path(
        arguments.broker_release_proof
    ).expanduser().resolve().parent
    if not release_parent.is_dir():
        raise RuntimeError("BLOCKED: Broker release proof directory is unavailable")
    context["worker_launcher_prefix"] = _read_argv(
        arguments.worker_launcher_command_file, "Worker launcher command file"
    )
    launcher = await _launcher_call(context, "preflight")
    expected = "c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467"
    if launcher.get("worker_archive_sha256") != expected:
        raise RuntimeError("BLOCKED: external Worker launcher did not prove the v0101 archive hash")
    simulator = await asyncio.to_thread(
        _subprocess_json, _simulator_check_command(context), "fn-02 simulator preflight"
    )
    broker = simulator.get("broker_proof") \
        if isinstance(simulator.get("broker_proof"), dict) else {}
    reservation_id = str(broker.get("reservation_id") or "")
    signer_sha256 = str(broker.get("signer_sha256") or "")
    reservation_source = str(broker.get("reservation_source") or "")
    reservation_expires_at = str(broker.get("reservation_expires_at") or "")
    if not reservation_id or not reservation_source or not reservation_expires_at \
            or len(signer_sha256) != 64:
        raise RuntimeError("BLOCKED: authenticated Broker reservation proof is incomplete")
    context["broker_reservation_id"] = reservation_id
    context["broker_proof_signer_sha256"] = signer_sha256
    if signer_sha256 != pinned_hash:
        raise RuntimeError("BLOCKED: Broker proof signer differs from the pinned authority")
    context["broker_reservation_source"] = reservation_source
    context["broker_reservation_expires_at"] = reservation_expires_at
    context["recovery"]["fn02_simulator"]["reservation_id"] = reservation_id
    context["recovery"]["fn02_simulator"]["reservation_source"] = reservation_source
    context["recovery"]["fn02_simulator"][
        "reservation_expires_at"
    ] = reservation_expires_at
    context["recovery"]["fn02_simulator"]["proof_signer_sha256"] = signer_sha256
    _update_recovery(context)
    return {"simulator": simulator, "launcher": launcher}


async def _start_full_lab(context):
    ready_file = context["result_dir"] / ("fn02-ready-" + context["run_id"] + ".json")
    result_file = context["result_dir"] / ("fn02-result-" + context["run_id"] + ".json")
    if ready_file.exists() or result_file.exists():
        raise RuntimeError("BLOCKED: fn-02 run files already exist for this run_id")
    command = _simulator_check_command(context)[:-1]
    command.extend(["--ready-file", str(ready_file), "--result-file", str(result_file)])
    process = await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    context["fn02_process"] = process
    context["fn02_ready_file"] = ready_file
    context["fn02_result_file"] = result_file
    context["recovery"]["fn02_simulator"].update({
        "started": True,
        "stopped": False,
        "lock_id": "pid:" + str(process.pid),
    })
    _update_recovery(context)
    deadline = time.monotonic() + 30.0
    while time.monotonic() < deadline:
        if ready_file.is_file():
            ready = _load_json(ready_file, "fn-02 ready file")
            if (
                ready.get("status") != "PASSED"
                or ready.get("broker_session_exclusive") is not True
                or str(ready.get("broker_reservation_id") or "")
                != str(context.get("broker_reservation_id") or "")
                or str(ready.get("broker_proof_signer_sha256") or "")
                != str(context.get("broker_proof_signer_sha256") or "")
                or str(ready.get("broker_reservation_source") or "")
                != str(context.get("broker_reservation_source") or "")
                or str(ready.get("broker_reservation_expires_at") or "")
                != str(context.get("broker_reservation_expires_at") or "")
            ):
                raise RuntimeError("FAILED: fn-02 ready file lacks broker exclusivity")
            # A lost launcher response can occur after the remote Worker was replaced.
            # Record the conservative recovery obligation before invoking it.
            context["recovery"]["worker_launcher"]["started"] = True
            _update_recovery(context)
            launcher = await _launcher_call(context, "start")
            context["recovery"]["worker_launcher"].update({
                "archive_sha256": launcher.get("worker_archive_sha256"),
            })
            _update_recovery(context)
            return {"launcher": launcher, "simulator": ready}
        if process.returncode is not None:
            output = await process.stdout.read()
            decoded = output.decode("utf-8", errors="replace")[-4096:]
            lines = [line for line in decoded.splitlines() if line.strip()]
            result = {}
            try:
                result = json.loads(lines[-1]) if lines else {}
            except json.JSONDecodeError:
                result = {}
            prefix = "BLOCKED: " if result.get("status") == "BLOCKED" else "FAILED: "
            raise RuntimeError(prefix + "fn-02 simulator exited before ready: " + decoded[-2048:])
        await asyncio.sleep(0.25)
    raise RuntimeError("FAILED: fn-02 simulator readiness timed out")


async def _activate_fn02_fixture(context, control):
    state = context["recovery"]["fn02_simulator"]
    if state.get("previous_desired_state") != "MAINTENANCE":
        return {"skipped": True, "fresh_fixture": True}
    result = await _ws_command(context, control, {
        "action": "node.state.set",
        "payload": {
            "node_id": "fn-02",
            "desired_state": "ACTIVE",
            "reason": "fn-02 E2E simulator activation",
        },
        "mutation": True,
        "expect_result": False,
        "idempotency_key": context["run_id"] + ":fn02:activate",
    })
    state["activated"] = True
    _update_recovery(context)
    return result


def _recovery_actuator_target(snapshot, recovery, **kwargs):
    config = snapshot["config_data"]
    for entry in config.get("actuators") or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("controller_uuid") or "") != str(
                recovery.get("controller_uuid") or ""):
            continue
        rows = entry.get("rows") if isinstance(entry.get("rows"), list) \
            else [entry]
        for actuator in rows:
            if isinstance(actuator, dict) and str(
                    actuator.get("actuator_id")) == str(recovery.get("actuator_id")):
                return _select_target(
                    snapshot,
                    str(entry.get("node_id") or ""),
                    controller_uuid=recovery["controller_uuid"],
                    actuator_id=recovery["actuator_id"],
                    sensor_id=None,
                    approved_host=kwargs.get("approved_host"),
                    approved_port=kwargs.get("approved_port"),
                )
    raise RuntimeError("FAILED: interrupted actuator recovery target no longer exists")


def _resolve_interrupted_actuator_target(snapshot, recovery, **kwargs):
    approved_host = kwargs.get("approved_host")
    approved_port = kwargs.get("approved_port")
    persisted_descriptor = recovery.get("target_descriptor")
    persisted_target = None
    if persisted_descriptor is not None:
        persisted_target = _target_from_modbus_descriptor(
            persisted_descriptor, approved_host, approved_port)
        if str(persisted_descriptor.get("owner_node_id")) != str(
                recovery.get("node_id") or "") \
                or str(persisted_descriptor.get("controller_uuid")) != str(
                    recovery.get("controller_uuid") or "") \
                or str(persisted_descriptor.get("actuator_id")) != str(
                    recovery.get("actuator_id")):
            raise RuntimeError(
                "BLOCKED: persisted actuator descriptor differs from recovery identity"
            )
    try:
        authority_target = _recovery_actuator_target(
            snapshot,
            recovery,
            approved_host=approved_host,
            approved_port=approved_port,
        )
    except RuntimeError as exc:
        if str(exc) != "FAILED: interrupted actuator recovery target no longer exists" \
                or persisted_target is None:
            raise
        return {
            "target": persisted_target,
            "route": "persisted_descriptor",
            "descriptor": copy.deepcopy(persisted_descriptor),
            "authority_entity_present": False,
        }
    authority_descriptor = _sanitized_modbus_target_descriptor(
        authority_target, approved_host, approved_port)
    if persisted_descriptor is not None \
            and authority_descriptor != persisted_descriptor:
        raise RuntimeError(
            "BLOCKED: current authority actuator target differs from immutable journal"
        )
    return {
        "target": authority_target,
        "route": "authority",
        "descriptor": authority_descriptor,
        "authority_entity_present": True,
        "legacy_descriptor_reconstructed": persisted_descriptor is None,
    }


async def _prepare_interrupted_recovery(context, control):
    recovery = context["recovery"]
    replicas = copy.deepcopy(
        recovery["config"].get("original_node_replicas") or {})
    context["original_node_replicas"] = replicas
    if recovery["config"].get("mutated"):
        if len(replicas) != 1:
            raise RuntimeError(
                "FAILED: interrupted node-local recovery has no unique Worker scope")
        context["active_config_node_id"] = str(next(iter(replicas)))
    if recovery["actuator"].get("mutated") \
            and not recovery["actuator"].get("restore_confirmed"):
        actuator_node_id = str(
            recovery["actuator"].get("node_id")
            or context["arguments"].node_id)
        current = await _read_scope_snapshot(
            context, control, _node_scope(actuator_node_id))
        resolved_target = _resolve_interrupted_actuator_target(
            current,
            recovery["actuator"],
            approved_host=context["arguments"].approved_actuator_host,
            approved_port=context["arguments"].approved_actuator_port,
        )
        context["actuator_target"] = resolved_target["target"]
        context["actuator_recovery_route"] = resolved_target["route"]
        if recovery["actuator"].get("target_descriptor") is None:
            # Additive compatibility for a pre-descriptor R4 journal. The
            # authority-derived descriptor is pinned once and then immutable.
            recovery["actuator"]["target_descriptor"] = copy.deepcopy(
                resolved_target["descriptor"])
            _update_recovery(context)
    if recovery["cross_worker"].get("sensor_mutated"):
        fixture = _load_json(
            context["arguments"].cross_fixture, "cross-worker fixture")
        context["cross_target"] = _cross_target(fixture)
    if recovery["profile"] == "full-lab":
        context["broker_reservation_id"] = str(
            recovery["fn02_simulator"].get("reservation_id") or ""
        )
        context["broker_proof_signer_sha256"] = str(
            recovery["fn02_simulator"].get("proof_signer_sha256") or ""
        )
        context["broker_reservation_source"] = str(
            recovery["fn02_simulator"].get("reservation_source") or ""
        )
        context["broker_reservation_expires_at"] = str(
            recovery["fn02_simulator"].get("reservation_expires_at") or ""
        )
        command_file = context["arguments"].worker_launcher_command_file
        if not command_file:
            raise RuntimeError(
                "BLOCKED: full-lab recovery requires --worker-launcher-command-file"
            )
        context["worker_launcher_prefix"] = _read_argv(
            command_file, "Worker launcher command file"
        )
        result_dir = context["recovery_path"].parent
        context["fn02_ready_file"] = result_dir / (
            "fn02-ready-" + context["run_id"] + ".json"
        )
        context["fn02_result_file"] = result_dir / (
            "fn02-result-" + context["run_id"] + ".json"
        )
    if recovery["config"].get("mutated"):
        node_id = str(next(iter(replicas)))
        event_stream = await _open_events(context, [
            {"resource_type": "node", "resource_pattern": node_id},
            {"resource_type": "config", "resource_pattern": "*"},
        ])
        context["config_event_stream"] = event_stream
        context["owned_config_event_stream"] = event_stream
    return {
        "run_id": context["run_id"],
        "profile": recovery["profile"],
        "config_scope": _node_scope(str(next(iter(replicas))))
        if recovery["config"].get("mutated") else None,
    }


def _recovered_fn02_process(context):
    lock_id = str(context["recovery"]["fn02_simulator"].get("lock_id") or "")
    if not lock_id.startswith("pid:") or not lock_id[4:].isdigit():
        return None
    pid = int(lock_id[4:])
    if pid <= 1:
        raise RuntimeError("FAILED: interrupted fn-02 recovery PID is unsafe")
    command_path = pathlib.Path("/proc") / str(pid) / "cmdline"
    if not command_path.exists():
        return None
    parts = [
        item.decode("utf-8", errors="replace")
        for item in command_path.read_bytes().split(b"\x00") if item
    ]
    expected = str(pathlib.Path(__file__).with_name("fn02_protocol_simulator.py"))
    if expected not in parts or "--run-id" not in parts:
        raise RuntimeError("FAILED: interrupted fn-02 PID belongs to another process")
    run_index = parts.index("--run-id")
    if run_index + 1 >= len(parts) or parts[run_index + 1] != context["run_id"]:
        raise RuntimeError("FAILED: interrupted fn-02 PID has another run identity")
    return {"pid": pid, "command_path": command_path}


async def _stop_recovered_fn02(context):
    recovered = _recovered_fn02_process(context)
    if not recovered:
        return {"process_absent": True}
    os.kill(recovered["pid"], signal.SIGTERM)
    deadline = time.monotonic() + 20.0
    while time.monotonic() < deadline and recovered["command_path"].exists():
        await asyncio.sleep(0.25)
    if recovered["command_path"].exists():
        checked = _recovered_fn02_process(context)
        if checked:
            os.kill(checked["pid"], signal.SIGKILL)
            kill_deadline = time.monotonic() + 5.0
            while time.monotonic() < kill_deadline and checked["command_path"].exists():
                await asyncio.sleep(0.1)
            if checked["command_path"].exists() and _recovered_fn02_process(context):
                raise RuntimeError("FAILED: interrupted fn-02 process survived SIGKILL")
    return {"process_pid": recovered["pid"], "stopped": True}


def _trace_window(trace, phase):
    start = int((phase or {}).get("started_monotonic_ns") or 0)
    finish = int((phase or {}).get("finished_monotonic_ns") or 0)
    if start <= 0 or finish < start:
        return []
    return [
        item for item in trace
        if isinstance(item, dict)
        and start <= int(item.get("monotonic_ns") or -1) <= finish
    ]


async def _stop_fn02(context):
    process = context.get("fn02_process")
    if not process:
        stopped = await _stop_recovered_fn02(context)
        context["fn02_stopped_epoch_s"] = time.time()
        context["recovery"]["fn02_simulator"]["stopped"] = True
        _update_recovery(context)
        result_file = context.get("fn02_result_file")
        if result_file and result_file.is_file():
            return _load_json(result_file, "recovered fn-02 result")
        return stopped
    if process.returncode is None:
        process.terminate()
        try:
            await asyncio.wait_for(process.wait(), timeout=20.0)
        except TimeoutError:
            process.kill()
            await process.wait()
    context["fn02_stopped_epoch_s"] = time.time()
    output = await process.stdout.read()
    result = None
    if context["fn02_result_file"].is_file():
        result = _load_json(context["fn02_result_file"], "fn-02 result")
    if process.returncode != 0 or not result or result.get("status") != "PASSED":
        raise RuntimeError("FAILED: fn-02 simulator shutdown/result failed: " + output.decode(
            "utf-8", errors="replace"
        )[-2048:])
    statistics = result.get("statistics") if isinstance(result.get("statistics"), dict) else {}
    replica = (result.get("state") or {}).get("node_replica") \
        if isinstance(result.get("state"), dict) else {}
    measurement = context.get("cross_measurement") or {}
    trace = result.get("actuator_trace") if isinstance(result.get("actuator_trace"), list) else []
    in_window = _trace_window(trace, measurement)
    if not context.get("primary_failed") and (
        int(statistics.get("mirror_requests") or 0) < 1
        or int(statistics.get("mirror_snapshots") or 0) < 1
        or int(statistics.get("actuator_results") or 0) < 1
        or int((replica or {}).get("revision") or 0) <= 0
        or len(str((replica or {}).get("fingerprint") or "")) != 64
    ):
        raise RuntimeError(
            "FAILED: fn-02 result lacks Node-Authority Mirror/Actuator convergence proof")
    if not context.get("primary_failed"):
        if len(in_window) != 1:
            raise RuntimeError(
                "FAILED: fn-02 executed {} actuator commands in the Cross measurement window"
                .format(len(in_window))
            )
        executed = in_window[0]
        expected = {
            "message_id": ((measurement.get("identity_chain") or {}).get(
                "remote_intent") or {}).get("observed_message_id"),
            "operation_id": measurement.get("operation_id"),
            "controller_uuid": measurement.get("controller_uuid"),
            "actuator_id": measurement.get("actuator_id"),
            "command_value": measurement.get("command_value"),
            "coordinator_term": ((measurement.get("identity_chain") or {}).get(
                "remote_intent") or {}).get("authority_term"),
            "authority_revision": ((measurement.get("identity_chain") or {}).get(
                "remote_intent") or {}).get("authority_revision"),
            "cross_execution_scope": "cross:fn-01.default",
            "cross_execution_epoch": ((measurement.get("identity_chain") or {}).get(
                "remote_intent") or {}).get("execution_epoch"),
            "target_execution_scope": "node:fn-02",
        }
        if any(value is None or str(executed.get(name)) != str(value)
               for name, value in expected.items()):
            raise RuntimeError("FAILED: fn-02 measurement trace differs from the exact Cross intent")
        if (
            int(executed.get("coordinator_term") or 0) <= 0
            or int(executed.get("authority_revision") or 0) <= 0
            or int(executed.get("cross_execution_epoch") or 0) <= 0
            or int(executed.get("target_execution_epoch") or 0) <= 0
            or int(executed.get("target_scope_revision") or 0) <= 0
            or len(str(executed.get("target_scope_fingerprint") or "")) != 64
        ):
            raise RuntimeError("FAILED: fn-02 measurement trace lacks exact fencing metadata")
        identity_chain = copy.deepcopy(measurement.get("identity_chain") or {})
        fn02_result = identity_chain.get("fn02_result") \
            if isinstance(identity_chain.get("fn02_result"), dict) else {}
        result_message_id = str(executed.get("result_message_id") or "")
        if not result_message_id:
            raise RuntimeError("FAILED: fn-02 trace lacks the published result message identity")
        fn02_result.update({
            "observed_broker_message_id": result_message_id,
            "execution_trace_message_id": str(executed.get("message_id") or ""),
        })
        identity_chain["fn02_result"] = fn02_result
        identity_chain["trace_confirmed"] = True
        measurement["identity_chain"] = copy.deepcopy(identity_chain)
        context["report"]["cross_worker"].update({
            "measurement_trace": copy.deepcopy(executed),
            "identity_chain": copy.deepcopy(identity_chain),
        })
        for check_id in ("XWK-002", "XWK-003"):
            check = context["report"]["checks"].get(check_id)
            if isinstance(check, dict) and isinstance(check.get("detail"), dict):
                check["detail"]["identity_chain"] = _sanitize(identity_chain)
        baseline_phase = context.get("cross_baseline_phase") or {}
        restore_phase = context.get("cross_restore_phase") or {}
        baseline_trace = _trace_window(trace, baseline_phase)
        restore_trace = _trace_window(trace, restore_phase)
        if len(baseline_trace) != int(baseline_phase.get("expected_count") or 0):
            raise RuntimeError("FAILED: fn-02 baseline trace count differs")
        if len(restore_trace) != int(restore_phase.get("expected_count") or 0):
            raise RuntimeError("FAILED: fn-02 restore trace count differs")
        for item in baseline_trace + restore_trace:
            if str(item.get("command_value")) != str(
                    (baseline_phase if item in baseline_trace else restore_phase).get(
                        "command_value"
                    )):
                raise RuntimeError("FAILED: fn-02 baseline/restore trace value differs")
        accounted = baseline_trace + in_window + restore_trace
        if len(accounted) != len(trace) or len({
                str(item.get("message_id") or "") for item in accounted}) != len(accounted):
            raise RuntimeError("FAILED: fn-02 trace contains an unaccounted or duplicate intent")
    context["recovery"]["fn02_simulator"]["stopped"] = True
    _update_recovery(context)
    return result


async def _restore_launcher(context):
    if not context["recovery"]["worker_launcher"]["started"]:
        context["recovery"]["worker_launcher"]["restore_confirmed"] = True
        _update_recovery(context)
        return {"skipped": True}
    result = await _launcher_call(context, "restore")
    if result.get("restore_confirmed") is not True or result.get("worker_package_modified") is not False:
        raise RuntimeError("FAILED: Worker launcher restore proof is incomplete")
    context["recovery"]["worker_launcher"]["restore_confirmed"] = True
    _update_recovery(context)
    return result


def _cross_target(fixture):
    source = fixture["source"]
    return {
        "host": source["modbus_host"],
        "port": int(source["modbus_port"]),
        "device_id": int(source["device_id"]),
        "address": int(source["address"]),
    }


def _read_register(target):
    client = _modbus_client(target)
    try:
        response = _modbus_call(client.read_holding_registers, {
            "address": target["address"], "count": 1, "device_id": target["device_id"],
        })
        registers = getattr(response, "registers", None)
        if getattr(response, "isError", _false_function)() or not registers:
            raise RuntimeError("FAILED: cross-trigger holding-register read failed")
        return int(registers[0])
    finally:
        client.close()


def _write_register(target, value):
    client = _modbus_client(target)
    try:
        response = _modbus_call(client.write_register, {
            "address": target["address"], "count": None,
            "device_id": target["device_id"], "value": int(value),
        })
        if getattr(response, "isError", _false_function)():
            raise RuntimeError("FAILED: cross-trigger holding-register write failed")
    finally:
        client.close()
    observed = _read_register(target)
    if observed != int(value):
        raise RuntimeError("FAILED: cross-trigger Modbus readback differs")
    return observed


def _modbus_call(method, request):
    errors = []
    for keyword in ("device_id", "slave", "unit"):
        options = {keyword: request["device_id"]}
        if request.get("count") is not None:
            options["count"] = request["count"]
        try:
            if "value" in request:
                return method(request["address"], request["value"], **options)
            return method(request["address"], **options)
        except TypeError as exc:
            errors.append(str(exc))
    raise RuntimeError("BLOCKED: installed pymodbus has no supported unit keyword: " + "; ".join(errors))


def _nested_value(value, path):
    current = value
    for name in path:
        if not isinstance(current, dict) or name not in current:
            return None
        current = current[name]
    return current


def _runtime_idle_snapshot(result):
    payload = _result_payload(result)
    runtime = payload.get("runtime") if isinstance(payload.get("runtime"), dict) else {}
    proxy = payload.get("proxy") if isinstance(payload.get("proxy"), dict) else {}
    if runtime.get("ready") is not True \
            or _nested_value(runtime, ("orchestrator", "role")) != "leader":
        raise RuntimeError("FAILED: Master is not ready leader during Cross drain")
    paths = {
        "master_legacy_command": (runtime, ("mqtt", "legacy", "command_queue_depth")),
        "master_legacy_outbound": (
            runtime, ("mqtt", "legacy", "transport", "outbound_queue_depth")
        ),
        "master_legacy_qos1": (
            runtime, ("mqtt", "legacy", "transport", "pending_qos1")
        ),
        "master_control_ingress": (runtime, ("mqtt", "control", "queue_depth")),
        "master_control_outbound": (
            runtime, ("mqtt", "control", "transport", "outbound_queue_depth")
        ),
        "master_control_qos1": (
            runtime, ("mqtt", "control", "transport", "pending_qos1")
        ),
        "proxy_mqtt_ingress": (proxy, ("mqtt", "queue_depth")),
        "proxy_mqtt_waiters": (proxy, ("mqtt", "waiters")),
        "proxy_mqtt_outbound": (
            proxy, ("mqtt", "transport", "outbound_queue_depth")
        ),
        "proxy_mqtt_qos1": (proxy, ("mqtt", "transport", "pending_qos1")),
    }
    queues = {}
    for name, target in paths.items():
        value = _nested_value(target[0], target[1])
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise RuntimeError(
                "FAILED: runtime.status lacks authoritative Cross drain counter " + name
            )
        queues[name] = value
    published = _nested_value(runtime, ("orchestrator", "statistics", "outbox_published"))
    if isinstance(published, bool) or not isinstance(published, int) or published < 0:
        raise RuntimeError("FAILED: runtime.status lacks Master outbox publication counter")
    return {"queues": queues, "outbox_published": published}


async def _fn02_drain_marker(context, control):
    listed = await _ws_command(context, control, {
        "action": "nodes.list", "payload": {}, "expect_result": True,
    })
    node = _node(_nodes_list(listed), "fn-02")
    health = node.get("health") if isinstance(node, dict) \
        and isinstance(node.get("health"), dict) else {}
    marker = health.get("drain_sequence")
    if (
        health.get("simulation") is not True
        or str(health.get("e2e_run_id") or "") != context["run_id"]
        or isinstance(marker, bool)
        or not isinstance(marker, int)
        or marker < 0
    ):
        raise RuntimeError("FAILED: fn-02 heartbeat lacks authoritative drain marker")
    return {
        "sequence": marker,
        "queue_idle": health.get("command_queue_idle") is True,
        "last_operation_id": str(health.get("last_operation_id") or ""),
        "last_command_message_id": str(health.get("last_command_message_id") or ""),
    }


async def _wait_cross_phase_idle(context, control, phase):
    deadline = time.monotonic() + context["arguments"].operation_timeout
    stable = 0
    previous_outbox = None
    latest = None
    while time.monotonic() < deadline:
        operation = None
        operation_id = str(phase.get("operation_id") or "")
        if operation_id:
            operation_result = await _ws_command(context, control, {
                "action": "operation.get",
                "payload": {"operation_id": operation_id},
                "expect_result": True,
            })
            operation = _operation(operation_result)
            if str(operation.get("state") or "") != "CONFIRMED":
                stable = 0
                await asyncio.sleep(1.0)
                continue
        status = await _ws_command(context, control, {
            "action": "runtime.status", "payload": {}, "expect_result": True,
        })
        runtime = _runtime_idle_snapshot(status)
        marker = await _fn02_drain_marker(context, control)
        expected_last = operation_id or str(phase.get("before_operation_id") or "")
        marker_ready = bool(
            marker["sequence"] > int(phase["after_sequence"])
            and marker["queue_idle"]
            and marker["last_operation_id"] == expected_last
        )
        zero = all(value == 0 for value in runtime["queues"].values())
        outbox_stable = previous_outbox == runtime["outbox_published"]
        stable = stable + 1 if marker_ready and zero and outbox_stable else 0
        previous_outbox = runtime["outbox_published"]
        latest = {
            "phase": phase["name"],
            "stable_samples": stable,
            "marker": marker,
            "runtime": runtime,
            "operation": operation,
            "relevant_outbox_terminal": bool(
                not operation_id or (operation and operation.get("state") == "CONFIRMED")
            ),
            "outbox_disposition": (
                "ACKED_BEFORE_CONFIRMED_EVENT" if operation_id
                else "NOT_APPLICABLE_NO_OPERATION"
            ),
        }
        if stable >= 3:
            return latest
        await asyncio.sleep(1.0)
    raise RuntimeError(
        "FAILED: {} Cross phase did not reach terminal/outbox/queue idle: {}".format(
            phase["name"], _sanitize(latest)
        )
    )


async def _cross_intent_proof(context, control, evidence):
    fixture = evidence["fixture"]
    source_event = evidence["source_event"]
    result_event = evidence["result_event"]
    cross_replica = context.get("cross_replica_proof") or {}
    result_payload = _event_payload(result_event)
    operation_id = str(result_payload.get("operation_id") or "")
    if not operation_id:
        raise RuntimeError("FAILED: Cross WSS result lacks operation identity")
    operation_result = await _ws_command(context, control, {
        "action": "operation.get",
        "payload": {"operation_id": operation_id},
        "expect_result": True,
    })
    operation = _operation(operation_result)
    request_payload = operation.get("request_payload") \
        if isinstance(operation.get("request_payload"), dict) else {}
    expected = {
        "operation_id": operation_id,
        "state": "CONFIRMED",
        "source_service": "worker",
        "source_instance_id": fixture["cross"]["owner_node_id"],
        "principal_subject": "worker:" + fixture["cross"]["owner_node_id"],
        "owner_node_id": fixture["target"]["node_id"],
        "controller_uuid": fixture["target"]["controller_uuid"],
        "actuator_id": fixture["target"]["actuator_id"],
        "requested_value": fixture["target"]["trigger_command_value"],
        "execution_scope": fixture["cross"]["scope"],
    }
    for name, value in expected.items():
        if str(operation.get(name)) != str(value):
            raise RuntimeError("FAILED: Cross remote intent differs for " + name)
    execution_epoch = operation.get("execution_epoch")
    if isinstance(execution_epoch, bool) or not isinstance(execution_epoch, int) \
            or execution_epoch <= 0:
        raise RuntimeError("FAILED: Cross remote intent lacks exact execution epoch")
    request_expected = {
        "execution_scope": fixture["cross"]["scope"],
        "execution_epoch": execution_epoch,
        "cross_revision": cross_replica.get("revision"),
        "cross_fingerprint": cross_replica.get("fingerprint"),
        "distributed_protocol": "automation.distributed.v2",
    }
    for name, value in request_expected.items():
        if value is None or str(request_payload.get(name)) != str(value):
            raise RuntimeError("FAILED: Cross fenced intent payload differs for " + name)
    source_payload = _event_payload(source_event)
    source_expected = {
        "controller_uuid": fixture["source"]["controller_uuid"],
        "sensor_id": fixture["source"]["sensor_id"],
        "source_node_id": fixture["cross"]["owner_node_id"],
        "value": fixture["source"]["trigger_value"],
    }
    for name, value in source_expected.items():
        if str(source_payload.get(name)) != str(value):
            raise RuntimeError("FAILED: Cross source fact differs for " + name)
    nested = result_payload.get("result") \
        if isinstance(result_payload.get("result"), dict) else {}
    stored_result = operation.get("result") if isinstance(operation.get("result"), dict) else {}
    if nested != stored_result:
        raise RuntimeError("FAILED: fn-02 WSS result differs from the terminal Master journal")
    authority_term = operation.get("authority_term")
    config_revision = operation.get("config_revision")
    if any(isinstance(value, bool) or not isinstance(value, int) or value <= 0 for value in (
            authority_term, config_revision)):
        raise RuntimeError("FAILED: Cross remote intent lacks authority fences")
    source_id = str(source_event.get("message_id") or "")
    result_id = str(result_event.get("message_id") or "")
    if not source_id or not result_id or source_id == result_id:
        raise RuntimeError("FAILED: Cross source/result event identities are incomplete")
    source_identity = source_event.get("source") \
        if isinstance(source_event.get("source"), dict) else {}
    source_principal = source_event.get("principal") \
        if isinstance(source_event.get("principal"), dict) else {}
    expected_worker_subject = "worker:" + fixture["cross"]["owner_node_id"]
    if (
        source_identity != {"service": "master", "instance_id": "master-01"}
        or str(source_principal.get("subject") or "") != expected_worker_subject
    ):
        raise RuntimeError(
            "FAILED: Cross source WSS event lacks the Master/Worker provenance chain"
        )
    source_correlation_id = str(source_event.get("correlation_id") or "")
    idempotency_key = str(operation.get("idempotency_key") or "")
    try:
        uuid.UUID(source_id)
        uuid.UUID(source_correlation_id)
        uuid.UUID(result_id)
        uuid.UUID(operation_id)
        uuid.UUID(idempotency_key)
    except (ValueError, AttributeError) as exc:
        raise RuntimeError("FAILED: Cross identity chain contains a non-UUID identity") from exc
    expected_command_id = "actuator-execute:{}:{}".format(
        authority_term, operation_id
    )
    marker = (evidence.get("measurement_idle") or {}).get("marker")
    marker = marker if isinstance(marker, dict) else {}
    observed_command_id = str(marker.get("last_command_message_id") or "")
    if observed_command_id != expected_command_id \
            or str(marker.get("last_operation_id") or "") != operation_id:
        raise RuntimeError(
            "FAILED: fn-02 drain heartbeat is not bound to the exact remote intent"
        )
    return {
        "source_event": {
            "message_id": source_id,
            "sequence": _event_sequence(source_event),
            "source_node_id": source_expected["source_node_id"],
            "envelope_source": copy.deepcopy(source_identity),
            "principal_subject": expected_worker_subject,
            "controller_uuid": source_expected["controller_uuid"],
            "sensor_id": source_expected["sensor_id"],
            "value": source_expected["value"],
        },
        "cross_fact": {
            "source_wss_message_id": source_id,
            "source_wss_correlation_id": source_correlation_id,
            "identity_observation": "wss_payload_and_fence_correlation",
            "scope": fixture["cross"]["scope"],
            "rule_id": fixture["cross"]["rule_id"],
            "scope_revision": cross_replica.get("revision"),
            "scope_fingerprint": cross_replica.get("fingerprint"),
            "execution_epoch": execution_epoch,
            "owner_node_id": fixture["cross"]["owner_node_id"],
        },
        "remote_intent": {
            "operation_id": operation_id,
            "idempotency_key": idempotency_key,
            "observed_message_id": observed_command_id,
            "derived_correlation_key": expected_command_id,
            "observation": "fn02_post_command_heartbeat",
            "source_node_id": operation.get("source_instance_id"),
            "target_node_id": operation.get("owner_node_id"),
            "authority_term": authority_term,
            "authority_revision": config_revision,
            "execution_scope": operation.get("execution_scope"),
            "execution_epoch": execution_epoch,
        },
        "fn02_result": {
            "wss_message_id": result_id,
            "operation_id": operation_id,
            "terminal_state": result_payload.get("terminal_state"),
            "simulation": nested.get("simulation"),
            "readback": nested.get("readback"),
        },
    }


async def _cross_trigger(context, events):
    fixture = _load_json(context["arguments"].cross_fixture, "cross-worker fixture")
    if fixture.get("site_id") != context["arguments"].site_id or fixture.get("cross_site") is not False:
        raise RuntimeError("BLOCKED: cross fixture must be same-site and cross_site=false")
    target = _cross_target(fixture)
    original = _read_register(target)
    context["cross_target"] = target
    context["recovery"]["cross_worker"].update({
        "original_sensor_value": original,
        "sensor_mutated": True,
    })
    _update_recovery(context)
    phase_filters = [
        {
            "resource_type": "sensor",
            "resource_pattern": "{}:{}".format(
                fixture["source"]["controller_uuid"], fixture["source"]["sensor_id"]
            ),
        },
        {"resource_type": "operation", "resource_pattern": "*"},
    ]
    baseline_stream = await _open_events(context, phase_filters)
    baseline_hwm = baseline_stream["accepted"].get("replay_high_watermark")
    if not isinstance(baseline_hwm, int) or baseline_hwm < 0:
        await baseline_stream["socket"].close(code=1000)
        raise RuntimeError("FAILED: Cross baseline has no fresh high-watermark")
    baseline_expected = int(original) >= int(fixture["source"]["threshold"])
    baseline_before = await _fn02_drain_marker(context, context["primary_control"])
    baseline_started_ns = time.monotonic_ns()
    try:
        _write_register(target, fixture["source"]["baseline_value"])
        await _wait_event(context, baseline_stream, {
            "event_type": "sensor.value",
            "controller_uuid": fixture["source"]["controller_uuid"],
            "sensor_id": fixture["source"]["sensor_id"],
            "value": fixture["source"]["baseline_value"],
            "after_sequence": baseline_hwm,
        })
        baseline_result = None
        if baseline_expected:
            baseline_result = await _wait_event(context, baseline_stream, {
                "event_type": "operation.result",
                "controller_uuid": fixture["target"]["controller_uuid"],
                "actuator_id": fixture["target"]["actuator_id"],
                "e2e_run_id": context["run_id"],
                "simulation": True,
                "terminal": True,
                "after_sequence": baseline_hwm,
                "timeout": context["arguments"].operation_timeout,
            })
            baseline_nested = _event_payload(baseline_result).get("result")
            baseline_nested = baseline_nested if isinstance(baseline_nested, dict) else {}
            if str(baseline_nested.get("command_value")) != str(
                    fixture["target"]["baseline_command_value"]):
                raise RuntimeError("FAILED: Cross baseline operation value differs")
        baseline_window = await _collect_event_window(context, baseline_stream, {
            "duration": 2.0,
            "criteria": {
                "event_type": "operation.result",
                "controller_uuid": fixture["target"]["controller_uuid"],
                "actuator_id": fixture["target"]["actuator_id"],
                "e2e_run_id": context["run_id"],
                "simulation": True,
                "terminal": True,
            },
        })
        if baseline_window["matched"]:
            raise RuntimeError("FAILED: Cross baseline produced duplicate or unexpected intents")
        baseline_operation_id = str(
            _event_payload(baseline_result).get("operation_id") if baseline_result else ""
        )
        baseline_idle = await _wait_cross_phase_idle(
            context, context["primary_control"], {
                "name": "baseline",
                "after_sequence": baseline_before["sequence"],
                "before_operation_id": baseline_before["last_operation_id"],
                "operation_id": baseline_operation_id,
            }
        )
    finally:
        baseline_finished_ns = time.monotonic_ns()
        await baseline_stream["socket"].close(code=1000)
    context["cross_baseline_phase"] = {
        "started_monotonic_ns": baseline_started_ns,
        "finished_monotonic_ns": baseline_finished_ns,
        "expected_count": int(baseline_expected),
        "command_value": fixture["target"]["baseline_command_value"],
        "idle": baseline_idle,
    }
    marker = await _open_events(context, [])
    marker_hwm = marker["accepted"].get("replay_high_watermark")
    await marker["socket"].close(code=1000)
    if not isinstance(marker_hwm, int) or marker_hwm < 0:
        raise RuntimeError("FAILED: cross-worker marker has no high-watermark")
    measurement = await _open_events(context, [
        {
            "resource_type": "sensor",
            "resource_pattern": "{}:{}".format(
                fixture["source"]["controller_uuid"], fixture["source"]["sensor_id"]
            ),
        },
        {"resource_type": "operation", "resource_pattern": "*"},
    ], last_event_id=marker_hwm)
    measurement_before = await _fn02_drain_marker(context, context["primary_control"])
    try:
        measurement_hwm = measurement["accepted"].get("replay_high_watermark")
        if not isinstance(measurement_hwm, int) or measurement_hwm < 0:
            raise RuntimeError("FAILED: cross-worker measurement has no fresh high-watermark")
        measurement_started_ns = time.monotonic_ns()
        _write_register(target, fixture["source"]["trigger_value"])
        source_event = await _wait_event(context, measurement, {
            "event_type": "sensor.value",
            "controller_uuid": fixture["source"]["controller_uuid"],
            "sensor_id": fixture["source"]["sensor_id"],
            "value": fixture["source"]["trigger_value"],
            "after_sequence": measurement_hwm,
        })
        result_event = await _wait_event(context, measurement, {
            "event_type": "operation.result",
            "controller_uuid": fixture["target"]["controller_uuid"],
            "actuator_id": fixture["target"]["actuator_id"],
            "e2e_run_id": context["run_id"],
            "simulation": True,
            "terminal": True,
            "after_sequence": measurement_hwm,
            "timeout": context["arguments"].operation_timeout,
        })
        duplicate_window = await _collect_event_window(context, measurement, {
            "duration": 2.0,
            "criteria": {
                "event_type": "operation.result",
                "controller_uuid": fixture["target"]["controller_uuid"],
                "actuator_id": fixture["target"]["actuator_id"],
                "e2e_run_id": context["run_id"],
                "simulation": True,
                "terminal": True,
            },
        })
        measurement_operation_id = str(_event_payload(result_event).get("operation_id") or "")
        measurement_idle = await _wait_cross_phase_idle(
            context, context["primary_control"], {
                "name": "measurement",
                "after_sequence": measurement_before["sequence"],
                "before_operation_id": measurement_before["last_operation_id"],
                "operation_id": measurement_operation_id,
            }
        )
        measurement_finished_ns = time.monotonic_ns()
    finally:
        await measurement["socket"].close(code=1000)
    if duplicate_window["matched"]:
        raise RuntimeError("FAILED: cross-worker measurement produced duplicate terminal intents")
    payload = _event_payload(result_event)
    nested = payload.get("result") if isinstance(payload.get("result"), dict) else {}
    if payload.get("terminal_state") != "CONFIRMED" or nested.get("simulation") is not True:
        raise RuntimeError("FAILED: cross-worker operation is not a confirmed simulation result")
    expected_result = {
        "controller_uuid": fixture["target"]["controller_uuid"],
        "actuator_id": fixture["target"]["actuator_id"],
        "address": fixture["target"]["address"],
        "function_type": fixture["target"]["function_type"],
        "e2e_run_id": context["run_id"],
        "command_value": fixture["target"]["trigger_command_value"],
        "readback": fixture["target"]["trigger_command_value"],
    }
    for name, value in expected_result.items():
        if str(nested.get(name)) != str(value):
            raise RuntimeError("FAILED: cross-worker result differs for " + name)
    source_sequence = _event_sequence(source_event)
    result_sequence = _event_sequence(result_event)
    if source_sequence is None or result_sequence is None or result_sequence <= source_sequence:
        raise RuntimeError("FAILED: cross-worker result is not ordered after the source event")
    identity_chain = await _cross_intent_proof(
        context, context["primary_control"], {
            "fixture": fixture,
            "source_event": source_event,
            "result_event": result_event,
            "measurement_idle": measurement_idle,
        }
    )
    context["cross_measurement"] = {
        "started_monotonic_ns": measurement_started_ns,
        "finished_monotonic_ns": measurement_finished_ns,
        "operation_id": str(payload.get("operation_id") or ""),
        "controller_uuid": fixture["target"]["controller_uuid"],
        "actuator_id": fixture["target"]["actuator_id"],
        "command_value": fixture["target"]["trigger_command_value"],
        "identity_chain": copy.deepcopy(identity_chain),
    }
    return {
        "source_event": source_event,
        "operation_event": result_event,
        "original_sensor_value": original,
        "cross_site": False,
        "source_sequence": source_sequence,
        "result_sequence": result_sequence,
        "measurement_high_watermark": measurement_hwm,
        "measurement_started_monotonic_ns": measurement_started_ns,
        "measurement_finished_monotonic_ns": measurement_finished_ns,
        "baseline_drain": {
            "observed": baseline_window["observed"],
            "expected_operation": baseline_expected,
            "idle": baseline_idle,
        },
        "duplicate_window": {
            "observed": duplicate_window["observed"],
            "matched": len(duplicate_window["matched"]),
        },
        "measurement_drain": measurement_idle,
        "identity_chain": identity_chain,
    }


async def _restore_cross_sensor(context):
    recovery = context["recovery"]["cross_worker"]
    if not recovery["sensor_mutated"]:
        recovery["sensor_restore_confirmed"] = True
        _update_recovery(context)
        return {"skipped": True}
    target = context.get("cross_target")
    if not target:
        raise RuntimeError("FAILED: cross sensor recovery target is unavailable")
    fixture = _load_json(context["arguments"].cross_fixture, "cross-worker fixture")
    filters = [
        {
            "resource_type": "sensor",
            "resource_pattern": "{}:{}".format(
                fixture["source"]["controller_uuid"], fixture["source"]["sensor_id"]
            ),
        },
        {"resource_type": "operation", "resource_pattern": "*"},
    ]
    stream = await _open_events(context, filters)
    high_watermark = stream["accepted"].get("replay_high_watermark")
    if not isinstance(high_watermark, int) or high_watermark < 0:
        await stream["socket"].close(code=1000)
        raise RuntimeError("FAILED: Cross restore has no fresh high-watermark")
    original = int(recovery["original_sensor_value"])
    restore_expected = original < int(fixture["source"]["threshold"])
    restore_before = await _fn02_drain_marker(context, context["primary_control"])
    restore_started_ns = time.monotonic_ns()
    try:
        observed = _write_register(target, original)
        await _wait_event(context, stream, {
            "event_type": "sensor.value",
            "controller_uuid": fixture["source"]["controller_uuid"],
            "sensor_id": fixture["source"]["sensor_id"],
            "value": original,
            "after_sequence": high_watermark,
        })
        if restore_expected:
            result_event = await _wait_event(context, stream, {
                "event_type": "operation.result",
                "controller_uuid": fixture["target"]["controller_uuid"],
                "actuator_id": fixture["target"]["actuator_id"],
                "e2e_run_id": context["run_id"],
                "simulation": True,
                "terminal": True,
                "after_sequence": high_watermark,
                "timeout": context["arguments"].operation_timeout,
            })
            nested = _event_payload(result_event).get("result")
            nested = nested if isinstance(nested, dict) else {}
            if str(nested.get("command_value")) != str(
                    fixture["target"]["baseline_command_value"]):
                raise RuntimeError("FAILED: Cross restore operation value differs")
        quiet = await _collect_event_window(context, stream, {
            "duration": 2.0,
            "criteria": {
                "event_type": "operation.result",
                "controller_uuid": fixture["target"]["controller_uuid"],
                "actuator_id": fixture["target"]["actuator_id"],
                "e2e_run_id": context["run_id"],
                "simulation": True,
                "terminal": True,
            },
        })
        if quiet["matched"]:
            raise RuntimeError("FAILED: Cross restore produced duplicate or unexpected intents")
        restore_operation_id = str(
            _event_payload(result_event).get("operation_id") if restore_expected else ""
        )
        restore_idle = await _wait_cross_phase_idle(
            context, context["primary_control"], {
                "name": "restore",
                "after_sequence": restore_before["sequence"],
                "before_operation_id": restore_before["last_operation_id"],
                "operation_id": restore_operation_id,
            }
        )
    finally:
        restore_finished_ns = time.monotonic_ns()
        await stream["socket"].close(code=1000)
    context["cross_restore_phase"] = {
        "started_monotonic_ns": restore_started_ns,
        "finished_monotonic_ns": restore_finished_ns,
        "expected_count": int(restore_expected),
        "command_value": fixture["target"]["baseline_command_value"],
        "idle": restore_idle,
    }
    recovery["sensor_restore_confirmed"] = observed == recovery["original_sensor_value"]
    _update_recovery(context)
    if not recovery["sensor_restore_confirmed"]:
        raise RuntimeError("FAILED: cross sensor restore was not confirmed")
    return {
        "restore_confirmed": True,
        "value": observed,
        "expected_operation": restore_expected,
        "quiet_events": quiet["observed"],
        "idle": restore_idle,
    }


def _initial_report(arguments, run_id):
    cross_enabled = arguments.profile == "full-lab"
    return {
        "version": VERSION,
        "run_id": run_id,
        "profile": arguments.profile,
        "status": "BLOCKED",
        "started_at": _utc_now(),
        "completed_at": None,
        "tls_verification": True,
        "transport": "wss-primary",
        "wss_server": "websockets",
        "https_websocket_enabled": False,
        "worker_fn01": {"real": True, "converged": False},
        "worker_fn02": {
            "simulation": True,
            "enabled": cross_enabled,
            "executed": False,
            "converged": False,
            "exclusive_lock_acquired": False,
            "preexisting_active_session": None,
            "broker_session_exclusive": False,
            "exclusive_lock_released": False,
            "fixture_bootstrap_performed": False,
            "expected_residual": cross_enabled,
        },
        "cross_worker": {
            "enabled": cross_enabled,
            "executed": False,
            "skipped": not cross_enabled,
            "tested": False,
            "cross_site": False,
        },
        "config_restore_confirmed": False,
        "actuator_restore_confirmed": False,
        "checks": {},
    }


def _secret_canaries(context):
    values = list(context.get("authentication_canaries") or ())
    if context.get("bearer_token"):
        values.append(context["bearer_token"])
    for name in ("automation_session", "automation_csrf"):
        if context["cookies"].get(name):
            values.append(context["cookies"][name])
    return tuple(value for value in values if value and len(value) >= 8)


def _file_canary_matches(path, canaries):
    path = pathlib.Path(path).expanduser().resolve()
    if not path.is_file():
        raise RuntimeError("BLOCKED: secret-log source is unavailable: " + str(path))
    needles = tuple(str(value).encode("utf-8") for value in canaries)
    overlap = max((len(value) for value in needles), default=1) - 1
    matches = []
    tail = b""
    with path.open("rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            haystack = tail + block
            for index, needle in enumerate(needles):
                if index not in matches and needle in haystack:
                    matches.append(index)
            tail = haystack[-overlap:] if overlap > 0 else b""
    return {"path": str(path), "matches": matches}


def _external_log_progress(context):
    evidence = []
    for baseline in context.get("external_log_baselines") or ():
        path = pathlib.Path(baseline["path"])
        try:
            current = path.stat()
        except OSError as exc:
            raise RuntimeError(
                "FAILED: live log disappeared during E2E: " + str(path)
            ) from exc
        if not stat.S_ISREG(current.st_mode) or current.st_size <= 0:
            raise RuntimeError("FAILED: live log is no longer a non-empty regular file")
        try:
            current_tail = _tail_sha256(path)
        except OSError as exc:
            raise RuntimeError("FAILED: live log is no longer readable") from exc
        grew = current.st_size > int(baseline["size"])
        same_file = (current.st_dev, current.st_ino) == (
            int(baseline["device"]), int(baseline["inode"])
        )
        if not same_file:
            raise RuntimeError("FAILED: live log inode changed during E2E")
        rewritten = bool(
            current.st_mtime_ns > int(baseline["mtime_ns"])
            and current_tail != baseline["tail_sha256"]
        )
        evidence.append({
            "label": baseline["label"],
            "path": str(path),
            "start_size": baseline["size"],
            "end_size": current.st_size,
            "grew": grew,
            "same_inode": same_file,
            "rewritten": rewritten,
            "changed_after_start": grew or rewritten,
        })
    return evidence


def _assert_no_secret_leak(context):
    context["log_handle"].flush()
    progress = _external_log_progress(context)
    canaries = tuple(context.get("secret_canaries") or ())
    sources = [
        context["log_path"],
        context["arguments"].proxy_log_source,
        context["arguments"].protocol_log_source,
    ]
    sources.extend(context["arguments"].secret_log_source)
    results = [_file_canary_matches(path, canaries) for path in sources]
    if any(item["matches"] for item in results):
        raise RuntimeError("FAILED: authentication canary appeared in a scanned log source")
    return {
        "sources_scanned": [item["path"] for item in results],
        "canaries": len(canaries),
        "external_log_captures": progress,
    }


async def _fn02_maintenance(context, control):
    simulator = context["recovery"]["fn02_simulator"]
    if not simulator["started"] and not simulator["activated"]:
        return {"skipped": True}
    result = await _ws_command(context, control, {
        "action": "node.state.set",
        "payload": {
            "node_id": "fn-02",
            "desired_state": "MAINTENANCE",
            "reason": "fn-02 E2E simulator cleanup",
        },
        "mutation": True,
        "expect_result": False,
        "idempotency_key": context["run_id"] + ":fn02:maintenance",
    })
    deadline = time.monotonic() + context["arguments"].operation_timeout
    last = None
    process = context.get("fn02_process")
    process_alive = bool(process and process.returncode is None)
    if not process_alive:
        process_alive = _recovered_fn02_process(context) is not None
    while time.monotonic() < deadline:
        nodes = await _ws_command(context, control, {
            "action": "nodes.list", "payload": {}, "expect_result": True,
        })
        last = _node(_nodes_list(nodes), "fn-02")
        acknowledged = bool(
            last and str(last.get("observed_state") or "") == "MAINTENANCE"
        )
        stale_offline = bool(last and last.get("online") is False and not process_alive)
        if last and str(last.get("desired_state") or "") == "MAINTENANCE" and (
                acknowledged or stale_offline):
            return {"command": result, "node": last}
        await asyncio.sleep(1.0)
    raise RuntimeError("FAILED: fn-02 did not acknowledge MAINTENANCE before disconnect: {}".format(
        _sanitize(last)
    ))


def _timestamp_epoch(value, label):
    text = str(value or "").replace("Z", "+00:00")
    try:
        return datetime.datetime.fromisoformat(text).timestamp()
    except ValueError as exc:
        raise RuntimeError("FAILED: {} timestamp is invalid".format(label)) from exc


def _broker_release_proof(context, node):
    path = pathlib.Path(context["arguments"].broker_release_proof).expanduser().resolve()
    proof = _load_json(path, "Broker release proof")
    signer_sha256 = _verify_signed_broker_proof(
        context["arguments"].broker_proof_public_key, proof,
        context["arguments"].broker_proof_key_sha256,
    )
    expected_signer = str(context.get("broker_proof_signer_sha256") or "")
    if not expected_signer:
        expected_signer = str(
            context["recovery"]["fn02_simulator"].get("proof_signer_sha256") or ""
        )
    if signer_sha256 != expected_signer:
        raise RuntimeError("FAILED: Broker release proof signer differs from reservation proof")
    reservation_id = str(context.get("broker_reservation_id") or "")
    if not reservation_id:
        reservation_id = str(
            context["recovery"]["fn02_simulator"].get("reservation_id") or ""
        )
    reservation_source = str(context.get("broker_reservation_source") or "")
    if not reservation_source:
        reservation_source = str(
            context["recovery"]["fn02_simulator"].get("reservation_source") or ""
        )
    reservation_expires_at = str(context.get("broker_reservation_expires_at") or "")
    if not reservation_expires_at:
        reservation_expires_at = str(
            context["recovery"]["fn02_simulator"].get("reservation_expires_at") or ""
        )
    expected = {
        "proof_kind": "broker-session-release",
        "site_id": context["arguments"].site_id,
        "node_id": "fn-02",
        "client_id": "automation-e2e-{}-fn-02".format(context["arguments"].site_id),
        "certificate_identity": "urn:automation:{}:worker:fn-02".format(
            context["arguments"].site_id
        ),
        "reservation_run_id": context["run_id"],
        "reservation_id": reservation_id,
        "source": reservation_source,
        "reservation_expires_at": reservation_expires_at,
    }
    if proof.get("authoritative") is not True or any(
            str(proof.get(name) or "") != str(value) for name, value in expected.items()):
        raise RuntimeError("FAILED: Broker release proof identity differs")
    if proof.get("active_client_id") is not False \
            or proof.get("active_certificate_identity") is not False \
            or proof.get("reservation_released") is not True:
        raise RuntimeError("FAILED: Broker release proof still reports fn-02 ownership")
    source = str(proof.get("source") or "")
    captured = _timestamp_epoch(proof.get("captured_at"), "Broker release proof")
    stopped = float(context.get("fn02_stopped_epoch_s") or 0.0)
    last_seen = None
    stale_after = stopped
    if isinstance(node, dict):
        last_seen = _timestamp_epoch(node.get("last_seen_at"), "fn-02 last heartbeat")
        stale_after = last_seen + float(context["arguments"].fn02_heartbeat_stale_s)
    age = time.time() - captured
    if (
        not source
        or stopped <= 0
        or captured < max(stopped, stale_after)
        or age < -5.0
        or age > 60.0
    ):
        raise RuntimeError(
            "FAILED: Broker release proof is stale or predates simulator stop/heartbeat staleness"
        )
    return {
        "source": source,
        "captured_at": proof.get("captured_at"),
        "reservation_id": reservation_id,
        "reservation_source": reservation_source,
        "reservation_expires_at": reservation_expires_at,
        "signer_sha256": signer_sha256,
        "reservation_released": True,
        "active_client_id": False,
        "active_certificate_identity": False,
        "registry_absent": node is None,
        "heartbeat_stale_after": (
            datetime.datetime.fromtimestamp(
                stale_after, datetime.timezone.utc
            ).isoformat() if last_seen is not None else None
        ),
    }


async def _wait_fn02_released(context, control):
    deadline = time.monotonic() + context["arguments"].operation_timeout
    last_node = None
    last_error = None
    while time.monotonic() < deadline:
        nodes = await _ws_command(context, control, {
            "action": "nodes.list", "payload": {}, "expect_result": True,
        })
        last_node = _node(_nodes_list(nodes), "fn-02")
        last_seen = None
        if last_node is not None:
            try:
                last_seen = _timestamp_epoch(
                    last_node.get("last_seen_at"), "fn-02 last heartbeat"
                )
            except Exception as exc:
                last_error = str(exc)
        stale_age = time.time() - last_seen if last_seen is not None else -1.0
        registry_released = last_node is None or bool(
            last_node
            and last_node.get("online") is False
            and str(last_node.get("desired_state") or "") == "MAINTENANCE"
            and str(last_node.get("observed_state") or "") == "MAINTENANCE"
            and stale_age >= float(context["arguments"].fn02_heartbeat_stale_s)
        )
        try:
            proof = _broker_release_proof(context, last_node) if registry_released else None
        except Exception as exc:
            proof = None
            last_error = str(exc)
        if registry_released and proof:
            return {
                "node": last_node,
                "broker": proof,
                "heartbeat_stale": True if last_node is not None else None,
                "heartbeat_age_s": round(stale_age, 3) if last_node is not None else None,
                "observed_offline": True if last_node is not None else None,
                "registry_absent": last_node is None,
            }
        await asyncio.sleep(1.0)
    raise RuntimeError(
        "FAILED: fn-02 release was not proven by registry and Broker: {} / {}".format(
            _sanitize(last_node), last_error
        )
    )


async def _live_body(context, control, events):
    if context["arguments"].profile == "full-lab":
        registry = await _ws_command(context, control, {
            "action": "nodes.list", "payload": {}, "expect_result": True,
        })
        fn02_precondition = _fn02_registry_precondition(
            context, {"nodes.list": registry}
        )
        _record(context, "FN02-REGISTRY-PREFLIGHT", "PASSED", detail=fn02_precondition)
        started = await _start_full_lab(context)
        context["report"]["worker_fn02"].update({
            "executed": True,
            "exclusive_lock_acquired": True,
            "preexisting_active_session": False,
            "broker_session_exclusive": True,
        })
        _record(context, "SIM-001", "PASSED", detail=started["simulator"])
        activation = await _activate_fn02_fixture(context, control)
        _record(context, "FN02-ACTIVATE", "PASSED", detail=activation)
        fn02 = await _wait_node_ready(context, control, "fn-02")
        capabilities = _validate_fn02_capabilities(fn02)
        context["report"]["worker_fn02"]["converged"] = True
        _record(context, "SIM-002a", "PASSED", detail={
            "node_id": fn02.get("node_id"), "capabilities": capabilities,
        })
    live_reads = await _live_read_actions(context, control)
    _assert_reserved_probe_nodes_absent(live_reads)
    _record(context, "CMD-READS", "PASSED", detail={"actions": list(live_reads)})
    _record(context, "RD-001", "PASSED", detail={"action": "nodes.list"})
    _record(context, "RD-002", "PASSED", detail={"action": "config.get"})
    _record(context, "RD-003", "PASSED", detail={"action": "runtime.status"})
    parity = await _http_read_parity(context, live_reads)
    _record(context, "HTTP-001", "PASSED", detail=parity)
    http_mutation = _http_mutation_parity(context)
    _record(context, "HTTP-002", "PASSED", detail=http_mutation)
    worker = await _wait_node_ready(context, control, context["arguments"].node_id)
    context["report"]["worker_fn01"]["converged"] = True
    _record(context, "WORKER-FN01", "PASSED", detail={
        "node_id": worker.get("node_id"),
        "admission_state": worker.get("admission_state"),
        "observed_state": worker.get("observed_state"),
    })
    baseline = await _wait_event(context, events, {
        "event_type": "sensor.value", "timeout": context["arguments"].event_timeout,
    })
    _record(context, "EVT-001", "PASSED", detail={
        "event_type": baseline.get("event_type"),
        "replay_high_watermark": events["accepted"].get("replay_high_watermark"),
        "generation": events["accepted"].get("generation"),
    })
    updated = await _filter_update_check(context, events, baseline)
    _record(context, "EVT-003", "PASSED", detail=updated)
    empty_filter = await _empty_filter_check(context, baseline)
    _record(context, "EVT-001A", "PASSED", detail=empty_filter)
    replay = await _replay_check(context, baseline)
    _record(context, "EVT-004", "PASSED", detail=replay)
    node_filter = await _node_filter_check(context)
    pagination = await _replay_pagination_check(context, control, events)
    _record(context, "EVT-004A", "PASSED", detail=pagination)
    token = await _token_cycle(context, control)
    _record(context, "SEC-005", "PASSED", detail={"limited_scope_denied": True})
    _record(context, "SEC-007", "PASSED", detail={"revocation_close_code": 1008})
    _record(context, "SEC-008", "PASSED", detail={
        "token_ids": token["token_ids"], "empty_scopes_preserved": True,
    })
    negatives = await _safe_negative_actions(context, control)
    _record(context, "CMD-NEGATIVE", "PASSED", detail={"actions": list(negatives)})
    csrf = await _csrf_checks(context, control)
    _record(
        context,
        "SEC-004",
        "SKIPPED" if csrf.get("skipped") else "PASSED",
        detail=csrf,
    )
    mutation = await _mutate_config(
        context, control, context["arguments"].node_id,
        runtime_matrix=True, event_stream=events)
    _record(context, "CFG-001", "PASSED", detail={
        "from_revision": mutation["before"]["revision"],
        "to_revision": mutation["after"]["revision"],
    })
    _record(context, "CFG-002", "PASSED", detail={
        "node_id": mutation["node_id"],
        "node_replica_revision": mutation["node"].get("node_replica_revision"),
        "node_replica_fingerprint": mutation["node"].get("node_replica_fingerprint"),
    })
    _record(context, "CFG-003", "PASSED", detail={"idempotent_retry": True})
    _record(context, "CFG-004", "PASSED", detail={"conflict": True})
    _record(context, "CFG-005", "PASSED", detail={"stale_revision": True})
    _record(context, "CFG-MATRIX", "PASSED", detail={
        "commit_path": "client_wss_proxy_master_mqtt_worker",
        "commit_origin": mutation["commit_origin"],
        "commit_identity": mutation["commit_identity"],
        "file_proposal": mutation["file_proposal"],
        "transport_expectation": mutation["transport_expectation"],
        "node_patch_capability": mutation["node_patch_capability"],
        "node_local_proposal_proof": mutation["node_local_proposal_proof"],
        "storage_contract": {"version": 9, "io_layout": "flat"},
        "owned_paths": mutation["owned_paths"],
        "matrix": mutation["matrix"],
        "worker_live_ack": True,
    })
    diff = await _config_diff_check(context, control, mutation)
    _record(context, "CONFIG-DIFF", "PASSED", detail={"result": bool(_result_payload(diff))})
    operation_events = await _open_events(context, [
        {"resource_type": "operation", "resource_pattern": "*"},
    ])
    try:
        actuator = await _actuator_cycle(context, control, {
            "all": events, "operation": operation_events,
            "selector": mutation["matrix"],
        })
    finally:
        await operation_events["socket"].close(code=1000)
    _record(context, "EVT-002", "PASSED", detail={
        "node_filter": node_filter,
        "operation_id": actuator["primary"]["operation_id"],
        "operation_event": actuator["event"].get("message_id"),
    })
    _record(context, "ACT-001", "PASSED", detail={
        "operation_id": actuator["primary"]["operation_id"],
        "lease_readiness_retries": actuator["primary"].get(
            "lease_readiness_retries", 0
        ),
        "controller_uuid": actuator["target"]["controller_uuid"],
        "actuator_id": actuator["target"]["actuator"]["actuator_id"],
        "owner_node_id": actuator["target"]["owner_node_id"],
        "approved_endpoint": "{}:{}".format(
            actuator["target"]["host"], actuator["target"]["port"]
        ),
    })
    _record(context, "ACT-002", "PASSED", detail={
        "operation_id": actuator["primary"]["operation_id"],
        "event_message_id": actuator["event"].get("message_id"),
    })
    _record(context, "ACT-003", "PASSED", detail={
        "controller_uuid": actuator["target"]["controller_uuid"],
        "sensor_id": actuator["target"]["sensor"]["sensor_id"],
        "baseline_message_id": actuator["baseline_sensor"].get("message_id"),
        "post_message_id": actuator["post_sensor"].get("message_id"),
        "owner_node_id": actuator["target"]["owner_node_id"],
    })
    restored_actuator = await _restore_actuator(context, control)
    context["report"]["actuator_restore_confirmed"] = True
    _record(context, "ACT-004", "PASSED", detail=restored_actuator)
    if str(actuator["target"]["actuator"].get("actuator_id")) \
            != str(mutation["matrix"]["actuator_id"]) \
            or str(actuator["target"]["sensor"].get("sensor_id")) \
            != str(mutation["matrix"]["sensor_id"]):
        raise RuntimeError("FAILED: runtime actuation did not use CFG-MATRIX entities")
    # The dynamically added actuator must be restored while it is still part of
    # the current V9 authority. Then restore only E2E-owned config paths; never
    # overwrite concurrent runtime-originated fields wholesale.
    restored_config = await _restore_config(context, control)
    context["report"]["config_restore_confirmed"] = True
    restore_detail = _config_restore_evidence(
        context, restored_config, {
            "actuator_restored_before_config": True,
            "before_parallel_load": True,
        })
    _record_config_restore_evidence(context, restore_detail)
    parallel = await _parallel_smoke(context, 2)
    _record(context, "PAR-001", "PASSED", detail=parallel)
    if context["arguments"].profile == "core":
        _record(context, "CROSS-WORKER", "SKIPPED", detail={
            "reason": "core profile", "cross_site": False,
        })
        return
    cross_replica = await _wait_cross_replica(context, control)
    context["cross_replica_proof"] = copy.deepcopy(cross_replica)
    _record(context, "XWK-001", "PASSED", detail=cross_replica)
    patch = await _mutate_config(
        context, control, "fn-02", event_stream=events)
    _record(context, "SIM-002b", "PASSED", detail={
        "from_revision": patch["before"]["revision"],
        "to_revision": patch["after"]["revision"],
    })
    cross = await _cross_trigger(context, events)
    context["report"]["cross_worker"].update({
        "executed": True, "skipped": False, "tested": True, "cross_site": False,
        "identity_chain": copy.deepcopy(cross["identity_chain"]),
    })
    _record(context, "XWK-002", "PASSED", detail={
        "source_sequence": cross["source_sequence"],
        "result_sequence": cross["result_sequence"],
        "baseline_events_drained": cross["baseline_drain"]["observed"],
        "duplicate_terminal_intents": cross["duplicate_window"]["matched"],
        "identity_chain": cross["identity_chain"],
    })
    _record(context, "XWK-003", "PASSED", detail={
        "cross_site": False,
        "operation_id": _event_payload(cross["operation_event"]).get("operation_id"),
        "simulation": True,
        "identity_chain": cross["identity_chain"],
    })
    _record(context, "CROSS-WORKER", "PASSED", detail={
        "profile": "full-lab", "cross_site": False, "tested": True,
    })


async def _cleanup_live(context, control):
    failures = []
    actuator_ready_for_config = True
    if not context.get("recovery"):
        return failures
    if control:
        try:
            restored = await _restore_actuator(context, control)
            context["report"]["actuator_restore_confirmed"] = True
            _record(context, "ACT-RESTORE", "PASSED", detail=restored)
        except Exception as exc:
            actuator_ready_for_config = False
            failures.append("actuator restore: " + str(exc))
            _record(context, "ACT-RESTORE", "FAILED", detail=str(exc))
    if context["arguments"].profile == "full-lab":
        try:
            restored = await _restore_cross_sensor(context)
            _record(context, "XWK-SENSOR-RESTORE", "PASSED", detail=restored)
        except Exception as exc:
            failures.append("cross sensor restore: " + str(exc))
            _record(context, "XWK-SENSOR-RESTORE", "FAILED", detail=str(exc))
    if control:
        if actuator_ready_for_config:
            try:
                restored = await _restore_config(context, control)
                context["report"]["config_restore_confirmed"] = True
                detail = _config_restore_evidence(
                    context, restored, {
                        "actuator_restored_before_config": True,
                        "before_parallel_load": True,
                    })
                _record_config_restore_evidence(context, detail)
                if context["arguments"].profile == "full-lab" \
                        and context["recovery"]["fn02_simulator"]["started"]:
                    process = context.get("fn02_process")
                    process_alive = bool(process and process.returncode is None)
                    if not process_alive:
                        process_alive = _recovered_fn02_process(context) is not None
                    if process_alive:
                        await _wait_node_ready(context, control, "fn-02")
            except Exception as exc:
                failures.append("config restore: " + str(exc))
                _record(context, "CFG-RESTORE", "FAILED", detail=str(exc))
        else:
            detail = (
                "FAILED: config restore deferred because actuator restore is "
                "not proven; recovery journal remains open"
            )
            failures.append("config restore: " + detail)
            _record(context, "CFG-RESTORE", "FAILED", detail=detail)
        try:
            await _revoke_recovery_token(context, control)
        except Exception as exc:
            failures.append("API token revoke: " + str(exc))
            _record(context, "TOKEN-RESTORE", "FAILED", detail=str(exc))
        if context["arguments"].profile == "full-lab":
            try:
                await _fn02_maintenance(context, control)
                _record(context, "FN02-MAINTENANCE", "PASSED")
            except Exception as exc:
                failures.append("fn-02 maintenance: " + str(exc))
                _record(context, "FN02-MAINTENANCE", "FAILED", detail=str(exc))
    if context["arguments"].profile == "full-lab":
        simulator_state = context["recovery"]["fn02_simulator"]
        cleanup_obligation = bool(
            simulator_state.get("started") or simulator_state.get("reservation_id")
        )
        if cleanup_obligation:
            try:
                result = await _stop_fn02(context)
                released = await _wait_fn02_released(context, control)
                context["report"]["worker_fn02"]["exclusive_lock_released"] = True
                _record(context, "SIM-STOP", "PASSED", detail={
                    "status": result.get("status", "PASSED")
                    if isinstance(result, dict) else "PASSED",
                })
                _record(context, "XWK-007", "PASSED", detail=released)
            except Exception as exc:
                failures.append("fn-02 stop: " + str(exc))
                _record(context, "SIM-STOP", "FAILED", detail=str(exc))
                _record(context, "XWK-007", "FAILED", detail=str(exc))
        else:
            detail = {"reason": "simulator and Broker reservation were never acquired"}
            _record(context, "SIM-STOP", "SKIPPED", detail=detail)
            _record(context, "XWK-007", "SKIPPED", detail=detail)
        try:
            result = await _restore_launcher(context)
            _record(context, "XWK-LAUNCHER-RESTORE", "PASSED", detail=result)
        except Exception as exc:
            failures.append("Worker launcher restore: " + str(exc))
            _record(context, "XWK-LAUNCHER-RESTORE", "FAILED", detail=str(exc))
    owned_event_stream = context.pop("owned_config_event_stream", None)
    if isinstance(owned_event_stream, dict):
        try:
            await owned_event_stream["socket"].close(code=1000)
        except Exception as exc:
            failures.append("config recovery event stream close: " + str(exc))
        if context.get("config_event_stream") is owned_event_stream:
            context.pop("config_event_stream", None)
    return failures


async def _run_live(context):
    control = None
    events = None
    primary = None
    cleanup_failures = []
    health = _http_request(context, {"method": "GET", "path": "/api/v1/health/ready"})
    if health["body"].get("ok") is not True:
        raise RuntimeError("BLOCKED: Proxy HTTPS readiness is not ok")
    _record(context, "HTTPS-READY", "PASSED")
    _record(context, "TLS-002", "PASSED", detail={"listener": "https"})
    if not context["arguments"].confirm_mutations:
        raise RuntimeError(
            "BLOCKED: release E2E requires --confirm-mutations for reversible config/Modbus tests"
        )
    authentication = _authenticate(context)
    context["secret_canaries"] = _secret_canaries(context)
    _record(context, "AUTH", "PASSED", detail=authentication)
    try:
        recovery_mode = _begin_recovery(context)
    except Exception:
        _logout(context)
        raise
    try:
        control = await _open_control(context)
    except Exception:
        _logout(context)
        raise
    context["primary_control"] = control
    _record(context, "WSS-CONTROL", "PASSED")
    _record(context, "TLS-001", "PASSED", detail={"listener": "wss"})
    _record(context, "PTH-002", "PASSED", detail={"endpoint": "/ws/v1/control"})
    if recovery_mode.get("recovering"):
        recovered_run_id = context["run_id"]
        try:
            prepared = await _prepare_interrupted_recovery(context, control)
            # Recovery cleanup must not reinterpret an interrupted, incomplete
            # measurement as a successful release measurement.
            context["primary_failed"] = True
            recovery_failures = await _cleanup_live(context, control)
        except Exception as exc:
            context["recovery"]["failure"] = str(exc)[:2048]
            _complete_recovery(context, False)
            await control.close(code=1000)
            _logout(context)
            raise
        if recovery_failures:
            context["recovery"]["failure"] = "; ".join(recovery_failures)[:2048]
            _complete_recovery(context, False)
            await control.close(code=1000)
            _logout(context)
            raise RuntimeError(
                "FAILED: interrupted E2E recovery failed: " + "; ".join(recovery_failures)
            )
        _complete_recovery(context, True)
        archived = _archive_recovery(context)
        context["run_id"] = context.pop("requested_run_id")
        _create_recovery(context)
        context["primary_failed"] = False
        _record(context, "RECOVERY", "PASSED", detail={
            "recovered_run_id": recovered_run_id,
            "archive": archived.name if archived else None,
            "prepared": prepared,
        })
    try:
        path_checks = await _path_and_origin_checks(context)
        _record(context, "PATH-ORIGIN", "PASSED", detail=path_checks)
        _record(context, "TLS-003", "PASSED", detail={
            "wrong_ca": True, "wrong_hostname": True,
        })
        _record(context, "SEC-001", "PASSED", detail=path_checks["missing_auth"])
        _record(context, "SEC-002", "PASSED", detail=path_checks["invalid_auth"])
        _record(context, "SEC-003", "PASSED", detail={
            "missing": path_checks["missing_origin"],
            "foreign": path_checks["foreign_origin"],
        })
        _record(context, "SEC-006", "PASSED", detail=path_checks["query_secret"])
        _record(context, "PTH-003", "PASSED", detail={
            "unknown": path_checks["unknown_path"],
            "query": path_checks["query_secret"],
        })
        _record(context, "PTH-004", "PASSED", detail=path_checks["https_upgrade"])
        events = await _open_events(context, [
            {"resource_type": "sensor", "resource_pattern": "*"},
            {"resource_type": "operation", "resource_pattern": "*"},
            {"resource_type": "node", "resource_pattern": "fn-*"},
            {"resource_type": "config", "resource_pattern": "*"},
            {"resource_type": "command", "resource_pattern": "*"},
        ])
        _record(context, "WSS-EVENTS", "PASSED", detail=events["accepted"])
        _record(context, "PTH-001", "PASSED", detail={"endpoint": "/ws/v1/events"})
        if context["arguments"].profile == "full-lab":
            preflight = await _full_lab_preflight(context)
            _record(context, "XWK-000", "PASSED", detail=preflight)
            isolation = (preflight.get("simulator") or {}).get("site_isolation")
            if not isinstance(isolation, dict) or isolation.get("contract_only") is not False \
                    or isolation.get("broker_authoritative") is not True \
                    or isolation.get("broker_proof_authenticated") is not True \
                    or isolation.get("broker_proof_signer_sha256") \
                    != context.get("broker_proof_signer_sha256"):
                raise RuntimeError("BLOCKED: fn-02 preflight lacks authoritative Cross-Site ACL proof")
            _record(context, "XST-001", "PASSED", detail=isolation)
        await _live_body(context, control, events)
    except Exception as exc:
        primary = exc
        context["primary_failed"] = True
    finally:
        try:
            cleanup_failures = await _cleanup_live(context, control)
        except Exception as exc:
            cleanup_failures = ["unhandled cleanup error: " + str(exc)]
        if events:
            try:
                await events["socket"].close(code=1000)
            except Exception as exc:
                cleanup_failures.append("event socket close: " + str(exc))
        if control:
            try:
                await control.close(code=1000)
            except Exception as exc:
                cleanup_failures.append("control socket close: " + str(exc))
        _logout(context)
    cleanup_error = None
    if cleanup_failures:
        context["recovery"]["failure"] = "; ".join(cleanup_failures)
        _complete_recovery(context, False)
        cleanup_error = RuntimeError("FAILED: cleanup failed: " + "; ".join(cleanup_failures))
    else:
        _complete_recovery(context, True)
    secret_error = None
    try:
        secret_result = _assert_no_secret_leak(context)
        _record(context, "SEC-LOG", "PASSED", detail=secret_result)
        _record(context, "SEC-009", "PASSED", detail=secret_result)
    except Exception as exc:
        secret_error = exc
        _record(context, "SEC-LOG", "FAILED", detail=str(exc))
        _record(context, "SEC-009", "FAILED", detail=str(exc))
    if cleanup_error and primary:
        raise RuntimeError(
            "FAILED: primary E2E failure: {}; cleanup failure: {}".format(
                str(primary), str(cleanup_error)
            )
        ) from primary
    if cleanup_error:
        raise cleanup_error
    if primary and secret_error:
        raise RuntimeError(
            "FAILED: primary E2E failure: {}; secret-log failure: {}".format(
                str(primary), str(secret_error)
            )
        ) from primary
    if primary:
        raise primary
    if secret_error:
        raise secret_error
    _validate_report(context)
    return context["report"]


async def _async_client_self_test(arguments):
    async def handler(connection):
        request = json.loads(await connection.recv())
        if request.get("protocol") != PROTOCOL or request.get("action") != "nodes.list":
            raise AssertionError("real websockets client sent an invalid envelope")
        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "command.accepted",
            "request_id": request["request_id"],
            "action": request["action"],
            "operation_id": None,
            "state": "COMPLETED",
        }))
        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "command.result",
            "request_id": request["request_id"],
            "action": request["action"],
            "result": {"nodes": []},
        }))

    context = {
        "run_id": "self-test",
        "cookies": {},
        "bearer_token": "self-test-bearer",
        "arguments": arguments,
        "verbose": False,
    }
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect("ws://127.0.0.1:{}".format(port)) as socket:
            result = await _ws_command(context, socket, {
                "action": "nodes.list", "payload": {}, "expect_result": True,
            })
    if _result_payload(result).get("nodes") != []:
        raise AssertionError("real websockets response correlation self-test failed")


async def _async_event_update_interleave_self_test(arguments):
    event = {
        "protocol": "automation.control.v1",
        "schema_version": 1,
        "message_id": "00000000-0000-0000-0000-000000000101",
        "correlation_id": "00000000-0000-0000-0000-000000000102",
        "event_type": "sensor.value",
        "source": {"service": "worker", "instance_id": "fn-01"},
        "target": {"service": "proxy", "instance_id": "proxy-01"},
        "site_id": arguments.site_id,
        "issued_at": "2026-07-19T00:00:00+00:00",
        "expires_at": "2026-07-19T00:01:00+00:00",
        "authority_term": 1,
        "config_revision": 1,
        "payload": {"node_id": "fn-01", "sensor_id": "1", "value": 1},
    }

    async def handler(connection):
        request = json.loads(await connection.recv())
        if request.get("type") != "subscribe":
            raise AssertionError("event update self-test received an invalid request")
        await connection.send(json.dumps(event))
        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "subscription.accepted",
            "request_id": request["request_id"],
            "filters": copy.deepcopy(request["filters"]),
            "generation": 2,
        }))

    context = {
        "run_id": "self-test-event-update",
        "schema_validators": {},
        "arguments": arguments,
        "verbose": False,
    }
    initial_filters = [{"resource_type": "sensor", "resource_pattern": "*"}]
    updated_filters = [{"resource_type": "sensor", "resource_pattern": "fn-01:*"}]
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        async with websockets.connect("ws://127.0.0.1:{}".format(port)) as socket:
            stream = {
                "socket": socket,
                "accepted": {"generation": 1},
                "request": {"filters": copy.deepcopy(initial_filters)},
                "pending_events": [],
            }
            updated = await _update_event_filters(context, stream, updated_filters)
            observed = await _wait_event(context, stream, {
                "event_type": "sensor.value",
                "node_id": "fn-01",
            })
        async with websockets.connect("ws://127.0.0.1:{}".format(port)) as socket:
            stream_with_backlog = {
                "socket": socket,
                "accepted": {"generation": 1},
                "request": {"filters": copy.deepcopy(initial_filters)},
                "pending_events": [
                    copy.deepcopy(event) for _ in range(EVENT_UPDATE_PENDING_LIMIT)
                ],
            }
            await _update_event_filters(context, stream_with_backlog, updated_filters)
    if updated["accepted"].get("generation") != 2:
        raise AssertionError("interleaved event update acknowledgement was not correlated")
    if observed.get("message_id") != event["message_id"]:
        raise AssertionError("interleaved live event was not preserved")
    if _pending_event_frames(stream):
        raise AssertionError("interleaved event buffer was not drained")
    backlog = _pending_event_frames(stream_with_backlog)
    if len(backlog) != EVENT_UPDATE_PENDING_LIMIT + 1:
        raise AssertionError("pre-existing event backlog counted against update bound")
    if backlog[-1].get("message_id") != event["message_id"]:
        raise AssertionError("interleaved event was not appended after the existing backlog")


async def _async_event_open_alignment_self_test(arguments):
    requests = []
    high_watermark = 513

    async def handler(connection):
        request = json.loads(await connection.recv())
        requests.append(copy.deepcopy(request))
        await connection.send(json.dumps({
            "protocol": PROTOCOL,
            "type": "subscription.accepted",
            "request_id": request["request_id"],
            "filters": copy.deepcopy(request["filters"]),
            "generation": 1,
            "replay_high_watermark": high_watermark,
        }))
        if request.get("last_event_id") == high_watermark:
            await connection.send(json.dumps({
                "protocol": "automation.control.v1",
                "schema_version": 1,
                "event_type": "sensor.value",
                "message_id": "00000000-0000-0000-0000-000000000201",
                "correlation_id": "00000000-0000-0000-0000-000000000202",
                "source": {"service": "master", "instance_id": "master-01"},
                "target": {"service": "proxy", "instance_id": "broadcast"},
                "site_id": arguments.site_id,
                "issued_at": "2026-07-19T00:00:00+00:00",
                "expires_at": "2026-07-19T00:01:00+00:00",
                "authority_term": 1,
                "config_revision": 1,
                "payload": {
                    "controller_uuid": "controller-1",
                    "sensor_id": 1,
                    "value": 42,
                },
                "extensions": {
                    "replay_sequence": high_watermark + 1,
                },
            }))
        await connection.wait_closed()

    context = {
        "run_id": "self-test-event-alignment",
        "schema_validators": {},
        "arguments": arguments,
        "verbose": False,
        "cookies": {},
        "bearer_token": "self-test-bearer",
        "ssl_context": None,
    }
    async with websockets.serve(handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        context["servers"] = {"wss": {"url": "ws://127.0.0.1:{}".format(port)}}
        stream = await _open_events(context, [])
        live_event = await _wait_event(context, stream, {
            "event_type": "sensor.value",
            "message_id": "00000000-0000-0000-0000-000000000201",
        })
        await stream["socket"].close(code=1000)
    if live_event.get("message_id") != "00000000-0000-0000-0000-000000000201":
        raise AssertionError("event live-edge alignment did not preserve live delivery")
    if len(requests) != 2:
        raise AssertionError("event live-edge alignment did not reconnect exactly once")
    if "last_event_id" in requests[0]:
        raise AssertionError("initial event subscription unexpectedly supplied a replay cursor")
    if requests[1].get("last_event_id") != high_watermark:
        raise AssertionError("event stream did not align to the accepted high-water mark")


async def _async_verified_noop_actuator_restore_self_test(
        arguments, target, target_descriptor):
    global _read_actuator, _request_actuator, _update_recovery
    global _write_actuator_direct

    original_functions = (
        _read_actuator, _request_actuator, _update_recovery,
        _write_actuator_direct,
    )
    test_arguments = copy.copy(arguments)
    test_arguments.approved_actuator_host = target_descriptor["host"]
    test_arguments.approved_actuator_port = target_descriptor["port"]
    read_values = []
    counters = {"reads": 0, "journal_updates": 0}

    def fake_read(_target):
        counters["reads"] += 1
        return read_values.pop(0) if read_values else False

    async def forbidden_request(_context, _control, _request):
        raise AssertionError("verified no-op recovery attempted an authority write")

    def forbidden_direct_write(_target, _value):
        raise AssertionError("verified no-op recovery attempted a direct write")

    def fake_update(_context):
        counters["journal_updates"] += 1

    _read_actuator = fake_read
    _request_actuator = forbidden_request
    _update_recovery = fake_update
    _write_actuator_direct = forbidden_direct_write
    try:
        for route in ("authority", "persisted_descriptor"):
            recovery = {
                "mutated": True,
                "restore_confirmed": False,
                "original_value": False,
                "target_descriptor": copy.deepcopy(target_descriptor),
            }
            context = {
                "arguments": test_arguments,
                "run_id": "self-test-actuator-" + route,
                "actuator_target": copy.deepcopy(target),
                "actuator_recovery_route": route,
                "recovery": {"actuator": recovery},
            }
            result = await _restore_actuator(context, object())
            if result.get("restore_route") != "verified_noop" \
                    or result.get("recovery_route") != route \
                    or result.get("operation", {}).get("verified_noop") is not True \
                    or result.get("operation", {}).get("write_performed") is not False \
                    or recovery.get("restore_confirmed") is not True \
                    or recovery.get("mutated") is not False:
                raise AssertionError("verified no-op actuator restore self-test failed")

        recovery = {
            "mutated": True,
            "restore_confirmed": False,
            "original_value": False,
            "target_descriptor": copy.deepcopy(target_descriptor),
        }
        context = {
            "arguments": test_arguments,
            "run_id": "self-test-actuator-final-readback",
            "actuator_target": copy.deepcopy(target),
            "actuator_recovery_route": "authority",
            "recovery": {"actuator": recovery},
        }
        read_values.extend((False, True))
        try:
            await _restore_actuator(context, object())
        except RuntimeError as exc:
            if "direct Modbus readback differs" not in str(exc):
                raise
        else:
            raise AssertionError("changed final actuator readback was accepted")
        if recovery.get("restore_confirmed") is not False \
                or recovery.get("mutated") is not True:
            raise AssertionError("failed final readback closed the recovery journal")
        if counters != {"reads": 6, "journal_updates": 2}:
            raise AssertionError("verified no-op actuator restore call boundary changed")
    finally:
        (
            _read_actuator, _request_actuator, _update_recovery,
            _write_actuator_direct,
        ) = original_functions


async def _async_config_restore_race_self_test(arguments):
    global _canonical_scope_operations, _read_scope_snapshot
    global _update_recovery, _wait_node_config_values
    global _wait_proposal_result, _wait_scope_snapshot, _ws_command

    original_functions = (
        _canonical_scope_operations, _read_scope_snapshot,
        _update_recovery, _wait_node_config_values,
        _wait_proposal_result, _wait_scope_snapshot, _ws_command,
    )
    owned_path = "controllers.0.tags"
    original_value = ["baseline"]
    applied_value = ["baseline", "e2e-owned"]
    foreign_value = ["baseline", "foreign-owner"]
    test_arguments = copy.copy(arguments)
    test_arguments.operation_timeout = 0.1

    def snapshot(revision, config_data, **kwargs):
        return {
            "scope": "node:fn-01",
            "authority_epoch": int(kwargs.get("authority_epoch", 4)),
            "execution_epoch": 1,
            "revision": int(revision),
            "fingerprint": _config_fingerprint(config_data),
            "config_data": copy.deepcopy(config_data),
        }

    def recovery_record(config_data, revision, fingerprint):
        return {
            "mutated": True,
            "restore_confirmed": False,
            "original_revision": int(revision),
            "original_fingerprint": str(fingerprint),
            "latest_revision": int(revision) + 1,
            "latest_fingerprint": "d" * 64,
            "original_node_replicas": {
                "fn-01": {
                    "revision": int(revision),
                    "fingerprint": str(fingerprint),
                },
            },
            "owned_changes": [{
                "path": owned_path,
                "original_value": copy.deepcopy(original_value),
                "applied_value": copy.deepcopy(applied_value),
            }],
            "original_value": copy.deepcopy(config_data),
        }

    try:
        baseline = {
            "controllers": [{"tags": copy.deepcopy(original_value)}],
            "unowned": {"owner": "baseline"},
        }
        foreign = copy.deepcopy(baseline)
        foreign["controllers"][0]["tags"] = copy.deepcopy(foreign_value)
        foreign_commands = []
        foreign_journal_updates = []

        async def read_foreign(_context, _control, scope):
            assert scope == "node:fn-01"
            return snapshot(22, foreign)

        async def command_foreign(_context, _control, request):
            foreign_commands.append(copy.deepcopy(request))
            raise AssertionError("foreign owner race attempted a restore command")

        def update_foreign(_context):
            foreign_journal_updates.append(True)

        _read_scope_snapshot = read_foreign
        _ws_command = command_foreign
        _update_recovery = update_foreign
        foreign_recovery = recovery_record(
            baseline, 20, _config_fingerprint(baseline))
        foreign_context = {
            "arguments": test_arguments,
            "run_id": "self-test-config-foreign-owner",
            "config_event_stream": {"socket": object()},
            "recovery": {"config": foreign_recovery},
        }
        try:
            await _restore_config(foreign_context, object())
        except RuntimeError as exc:
            if "concurrent owner changed an E2E-owned config field" not in str(exc):
                raise
        else:
            raise AssertionError("foreign owner restore race was accepted")
        if foreign_commands or foreign_journal_updates \
                or foreign_recovery["mutated"] is not True \
                or foreign_recovery["restore_confirmed"] is not False:
            raise AssertionError("foreign owner race closed the recovery journal")

        applied = copy.deepcopy(baseline)
        applied["controllers"][0]["tags"] = copy.deepcopy(applied_value)
        applied["unowned"]["owner"] = "before-stale"
        rebased = copy.deepcopy(applied)
        rebased["unowned"]["owner"] = "after-stale"
        compensated = copy.deepcopy(rebased)
        compensated["controllers"][0]["tags"] = copy.deepcopy(original_value)
        reads = [snapshot(30, applied), snapshot(31, rebased)]
        proposals = []
        acknowledgements = []
        scope_waits = []
        journal_updates = []
        operation = {
            "op": "replace_field",
            "section": "controllers",
            "entity_key": "controller:self-test",
            "json_pointer": "/tags",
            "expected_value_hash": "c" * 64,
            "value": copy.deepcopy(original_value),
        }

        def canonical(_before, _after):
            return {
                "operation_format": "config_scope_operations_v1",
                "operations": [copy.deepcopy(operation)],
            }

        async def read_rebased(_context, _control, scope):
            assert scope == "node:fn-01" and reads
            return copy.deepcopy(reads.pop(0))

        async def command_rebased(_context, _control, request):
            proposals.append(copy.deepcopy(request))
            return {
                "accepted": {
                    "operation_id": "restore-" + str(len(proposals)),
                },
            }

        async def proposal_result(_context, _stream, options):
            acknowledgements.append(copy.deepcopy(options))
            if len(acknowledgements) == 1:
                return {
                    "event": {"message_id": "event-stale"},
                    "payload": {
                        "accepted": False,
                        "code": "stale_revision",
                    },
                }
            return {
                "event": {"message_id": "event-accepted"},
                "payload": {
                    "accepted": True,
                    "code": "accepted",
                    "scope": "node:fn-01",
                    "authority_epoch": 4,
                    "scope_revision": 32,
                    "fingerprint": _config_fingerprint(compensated),
                },
            }

        async def wait_scope(_context, _control, expectation):
            scope_waits.append(copy.deepcopy(expectation))
            return snapshot(32, compensated)

        async def wait_values(_context, _control, expected):
            assert expected == {
                "node_id": "fn-01",
                "values": [{
                    "path": owned_path,
                    "value": copy.deepcopy(original_value),
                }],
            }
            current = snapshot(32, compensated)
            return {
                "snapshot": current,
                "node": {
                    "node_id": "fn-01",
                    "node_replica_scope": "node:fn-01",
                    "node_replica_revision": 32,
                    "node_replica_fingerprint": current["fingerprint"],
                    "observed_global_revision": 1,
                    "live_config_revision": 32,
                    "admission_state": "APPROVED",
                    "observed_state": "ACTIVE",
                    "online": True,
                },
            }

        def update_rebased(_context):
            journal_updates.append(True)

        _canonical_scope_operations = canonical
        _read_scope_snapshot = read_rebased
        _ws_command = command_rebased
        _wait_proposal_result = proposal_result
        _wait_scope_snapshot = wait_scope
        _wait_node_config_values = wait_values
        _update_recovery = update_rebased
        rebased_recovery = recovery_record(
            baseline, 29, _config_fingerprint(baseline))
        context = {
            "arguments": test_arguments,
            "run_id": "self-test-config-stale-rebase",
            "config_event_stream": {"socket": object()},
            "recovery": {"config": rebased_recovery},
        }
        restored = await _restore_config(context, object())
        payloads = [item["payload"] for item in proposals]
        allowed = {
            "target_node_id", "operation", "operation_format", "scope",
            "base_authority_epoch", "base_scope_revision",
            "base_scope_fingerprint", "reason", "operations",
        }
        if reads or len(proposals) != 2 \
                or [item["base_scope_revision"] for item in payloads] != [30, 31] \
                or any(set(item) != allowed for item in payloads) \
                or any(item["operations"] != [operation] for item in payloads) \
                or any("base_revision" in item or "changes" in item
                       for item in payloads) \
                or [item["relay_message_id"] for item in acknowledgements] \
                != ["relay:restore-1", "relay:restore-2"] \
                or len(scope_waits) != 1 \
                or scope_waits[0]["base_revision"] != 31 \
                or scope_waits[0]["fingerprint"] \
                != _config_fingerprint(compensated) \
                or restored.get("mode") != "compensating_proposal" \
                or restored.get("cas_retries") != 1 \
                or rebased_recovery["mutated"] is not False \
                or rebased_recovery["restore_confirmed"] is not True \
                or journal_updates != [True]:
            raise AssertionError(
                "node-local stale compensation was not correlated and field-bounded")
    finally:
        (
            _canonical_scope_operations, _read_scope_snapshot,
            _update_recovery, _wait_node_config_values,
            _wait_proposal_result, _wait_scope_snapshot, _ws_command,
        ) = original_functions


def _broker_signature_self_test():
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey

    def signed_proof(private_key):
        proof = {
            "proof_kind": "offline-signature-regression",
            "reservation_id": "self-test-reservation",
            "active_client_id": False,
            "signature_algorithm": "ed25519",
        }
        payload = json.dumps(
            proof, ensure_ascii=False, allow_nan=False,
            sort_keys=True, separators=(",", ":"),
        ).encode("utf-8")
        proof["signature"] = base64.b64encode(private_key.sign(payload)).decode("ascii")
        return proof

    with tempfile.TemporaryDirectory(prefix="e2e-v0025-signature-") as directory:
        private_key = Ed25519PrivateKey.generate()
        public_path = pathlib.Path(directory) / "broker-proof-public.pem"
        public_bytes = private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
        public_path.write_bytes(public_bytes)
        valid = signed_proof(private_key)
        expected_signer = hashlib.sha256(public_bytes).hexdigest()
        if _verify_signed_broker_proof(
                public_path, valid, expected_signer) != expected_signer:
            raise AssertionError("valid canonical Ed25519 Broker proof was rejected")
        tampered = copy.deepcopy(valid)
        tampered["active_client_id"] = True
        wrong_signer = signed_proof(Ed25519PrivateKey.generate())
        for label, proof in (("tampered", tampered), ("wrong signer", wrong_signer)):
            try:
                _verify_signed_broker_proof(public_path, proof)
            except RuntimeError:
                continue
            raise AssertionError(label + " Ed25519 Broker proof was accepted")
        try:
            _verify_signed_broker_proof(public_path, valid, "0" * 64)
        except RuntimeError:
            pass
        else:
            raise AssertionError("wrong pinned Broker proof key hash was accepted")


def _self_test(arguments):
    cross_fixture = pathlib.Path(arguments.cross_fixture).expanduser().resolve()
    if not cross_fixture.is_file() \
            or _file_sha256(cross_fixture) != CROSS_FIXTURE_SHA256:
        raise AssertionError("cross-worker fixture pin self-test failed")
    fixture = _load_json(arguments.action_fixture, "WSS action fixture")
    names = tuple(item.get("action") for item in fixture.get("actions") or ())
    if set(names) != set(ALL_ACTIONS) or len(names) != 23:
        raise AssertionError("action fixture does not cover 23 exact actions")
    https = _parse_server_url("https://127.0.0.1:8444", "https")
    wss = _parse_server_url("wss://127.0.0.1:8443", "wss")
    if https["port"] != 8444 or wss["port"] != 8443:
        raise AssertionError("separate listener parsing failed")
    request_context = {
        "run_id": "self-test",
        "cookies": {"automation_csrf": "csrf-canary"},
        "bearer_token": None,
        "arguments": arguments,
        "schema_validators": _load_schema_validators(
            pathlib.Path(__file__).resolve().with_name("schemas")),
    }
    envelope = _command_envelope(request_context, {
        "action": "config.propose", "payload": {}, "mutation": True,
    })
    if envelope.get("protocol") != PROTOCOL or not envelope.get("idempotency_key"):
        raise AssertionError("command envelope self-test failed")
    if envelope.get("csrf_token") != "csrf-canary":
        raise AssertionError("session mutation CSRF self-test failed")
    diff_payload = _node_local_config_diff_payload({
        "before": {"revision": 0},
        "after": {"revision": 1},
    })
    if diff_payload != {"from_revision": 1, "to_revision": 2}:
        raise AssertionError("node-local revision-zero config.diff self-test failed")
    diff_envelope = _command_envelope(request_context, {
        "action": "config.diff", "payload": diff_payload,
    })
    _validate_schema(request_context, "command_request", diff_envelope)
    operation_commit = {
        "accepted": {"operation_id": "operation-1", "state": "RELAYED"},
    }
    operation_retry = copy.deepcopy(operation_commit)
    operation_retry["accepted"]["duplicate"] = True
    if _config_idempotency_identity(operation_commit) != \
            _config_idempotency_identity(operation_retry):
        raise AssertionError(
            "node-local operation retry changed its idempotency identity")
    if _config_idempotency_identity(operation_commit) != \
            ("operation", "operation-1"):
        raise AssertionError("node-local operation identity self-test failed")
    operation_conflict = copy.deepcopy(operation_retry)
    operation_conflict["accepted"]["operation_id"] = "operation-2"
    if _config_idempotency_identity(operation_commit) == \
            _config_idempotency_identity(operation_conflict):
        raise AssertionError("node-local operation identity collision self-test failed")
    if not _parallel_wss_is_stable({
        "clients": 2,
        "reserved_connections": 0,
        "pending_principal_connections": 0,
        "response_queue_depth": 0,
    }, 2):
        raise AssertionError("parallel WSS stable-baseline self-test failed")
    if _parallel_wss_is_stable({
        "clients": 3,
        "reserved_connections": 0,
        "pending_principal_connections": 0,
        "response_queue_depth": 0,
    }, 2):
        raise AssertionError("parallel WSS stale-client self-test failed")
    if _parallel_wss_is_stable({
        "clients": 2,
        "reserved_connections": 0,
        "pending_principal_connections": 1,
        "response_queue_depth": 0,
    }, 2):
        raise AssertionError("parallel WSS pending-principal self-test failed")
    if _parallel_failure_summary([TimeoutError(), TimeoutError()]) != \
            {"TimeoutError": 2}:
        raise AssertionError("parallel WSS failure summary self-test failed")
    if not _parallel_failure_is_transient(TimeoutError()) or \
            _parallel_failure_is_transient(ValueError("permanent")):
        raise AssertionError("parallel WSS transient failure self-test failed")
    if not _actuator_readiness_rejection({
        "code": "not_found", "message": "controller has no active owner lease",
    }):
        raise AssertionError("actuator owner lease readiness self-test failed")
    if not _actuator_readiness_rejection({
        "code": "worker_not_synchronised", "message": "worker is converging",
    }):
        raise AssertionError("actuator worker readiness self-test failed")
    if _actuator_readiness_rejection({
        "code": "not_found", "message": "actuator does not exist",
    }):
        raise AssertionError("unrelated actuator rejection became retryable")
    release_root = pathlib.Path(__file__).resolve().parents[2]
    initial_config_path = release_root / "master_node" / "master_node" \
        / "_config" / "application" / "initial_config_v9.json"
    mutation_config = json.loads(initial_config_path.read_text(encoding="utf-8"))
    mutation_snapshot = {"config_data": mutation_config}
    mutation_descriptor = _config_change(
        mutation_snapshot, "fn-01", runtime_matrix=True)
    mutation_changes = mutation_descriptor["changes"]
    mutation_candidate = _config_candidate(
        mutation_snapshot, mutation_changes)
    restore_changes = [
        {
            "operation": "set",
            "path": change["path"],
            "value": _config_path_value(mutation_config, change["path"]),
        }
        for change in mutation_changes
    ]
    restored_candidate = _config_candidate({
        "config_data": mutation_candidate,
    }, restore_changes)
    master_root = release_root / "master_node" / "master_node"
    source_text = str(master_root)
    if source_text not in sys.path:
        sys.path.insert(0, source_text)
    from src.core import config_authority as master_config_authority
    master_config = _load_json(
        release_root / "master_node" / "master_node" / "_config"
        / "application" / "master_config.json",
        "Master application config",
    )
    applied = master_config_authority.apply_changes(
        mutation_config, mutation_changes,
        **(master_config.get("config_policy") or {})
    )
    verified = master_config_authority.validate_worker_config(
        mutation_candidate, profile="lab_v2", require_v2=True)
    before_scopes = _scope_fingerprints(mutation_config, "fn-01")
    candidate_scopes = _scope_fingerprints(mutation_candidate, "fn-01")
    from src.library._generic_system_contract_library import (
        config_graph,
        shard_library,
    )
    projected_config = config_graph.active_projection(
        copy.deepcopy(mutation_config))
    node_restore_config = shard_library.build_local_execution_shard(
        projected_config, "fn-01")
    node_restore_fingerprint = _config_fingerprint(node_restore_config)
    node_restore_descriptor = _config_change(
        {"config_data": node_restore_config}, "fn-01", runtime_matrix=True)
    node_restore_candidate = _config_candidate(
        {"config_data": node_restore_config},
        node_restore_descriptor["changes"],
    )
    canonical_node_change = _canonical_scope_operations(
        node_restore_config, node_restore_candidate)
    nullable_operation_index = None
    for index, operation in enumerate(canonical_node_change["operations"]):
        if operation.get("expected_value_hash") is None:
            nullable_operation_index = index
            break
    if nullable_operation_index is None:
        raise AssertionError("canonical new-entity CAS self-test has no null hash")
    canonical_proposal = _command_envelope(request_context, {
        "action": "config.propose",
        "payload": {
            "target_node_id": "fn-01",
            "operation": "scope_operations",
            "operation_format": canonical_node_change["operation_format"],
            "scope": "node:fn-01",
            "base_authority_epoch": 0,
            "base_scope_revision": 0,
            "base_scope_fingerprint": node_restore_fingerprint,
            "reason": "canonical Node-Local proposal self-test",
            "operations": copy.deepcopy(canonical_node_change["operations"]),
        },
        "mutation": True,
        "idempotency_key": "self-test:canonical-node-proposal",
    })
    _validate_schema(request_context, "command_request", canonical_proposal)
    invalid_cas_proposal = copy.deepcopy(canonical_proposal)
    invalid_cas_operation = invalid_cas_proposal["payload"]["operations"][
        nullable_operation_index
    ]
    invalid_cas_operation["expected_value_hash"] = True
    try:
        _validate_schema(request_context, "command_request", invalid_cas_proposal)
    except RuntimeError as exc:
        if "expected_value_hash:type" not in str(exc):
            raise
    else:
        raise AssertionError("boolean expected_value_hash was accepted")
    restore_path = node_restore_descriptor["changes"][0]["path"]
    original_restore_value = _config_path_value(
        node_restore_config, restore_path)
    applied_restore_value = _config_path_value(
        node_restore_candidate, restore_path)
    restore_context = {"recovery": {"config": {
        "mutated": False,
        "original_revision": 1,
        "latest_revision": 2,
        "original_fingerprint": node_restore_fingerprint,
        "latest_fingerprint": node_restore_fingerprint,
        "restore_confirmed": True,
        "original_node_replicas": {"fn-01": {
            "revision": 1,
            "fingerprint": node_restore_fingerprint,
        }},
        "owned_changes": [{
            "path": restore_path,
            "original_value": copy.deepcopy(original_restore_value),
            "applied_value": copy.deepcopy(applied_restore_value),
        }],
    }}}
    restored_proof = {
        "converged_snapshot": {
            "scope": "node:fn-01",
            "authority_epoch": 1,
            "execution_epoch": 1,
            "revision": 2,
            "fingerprint": node_restore_fingerprint,
            "config_data": node_restore_config,
        },
        "node": {
            "node_id": "fn-01",
            "node_replica_scope": "node:fn-01",
            "node_replica_revision": 2,
            "node_replica_fingerprint": node_restore_fingerprint,
            "observed_global_revision": 1,
        },
        "mode": "compensating_proposal",
        "cas_retries": 0,
    }
    revalidation = _persisted_restore_revalidation(
        restore_context["recovery"]["config"])
    if revalidation != {
        "node_id": "fn-01",
        "values": [{
            "path": restore_path,
            "value": copy.deepcopy(original_restore_value),
        }],
        "mode": "persisted_restore_revalidation",
    }:
        raise AssertionError("persisted restore revalidation self-test failed")
    incomplete_restore = copy.deepcopy(restore_context["recovery"]["config"])
    incomplete_restore["latest_revision"] = 1
    incomplete_restore["latest_fingerprint"] = ""
    if _persisted_restore_revalidation(incomplete_restore) is not None:
        raise AssertionError("unproven persisted restore became recoverable")
    intent_recovery = copy.deepcopy(restore_context["recovery"]["config"])
    intent_recovery["latest_revision"] = 1
    intent_recovery["latest_fingerprint"] = node_restore_fingerprint
    intent_revalidation = _persisted_restore_revalidation(intent_recovery)
    if not isinstance(intent_revalidation, dict) \
            or intent_revalidation.get("mode") != "intent_not_applied":
        raise AssertionError("persisted unapplied intent is not recoverable")
    restore_context["report"] = {"checks": {}}
    restore_evidence_options = {
        "actuator_restored_before_config": True,
        "before_parallel_load": True,
    }
    restore_evidence = _config_restore_evidence(
        restore_context, restored_proof, restore_evidence_options)
    if restore_evidence.get("restore_confirmed") is not True \
            or restore_evidence.get("authority_mode") != "node_local" \
            or restore_evidence.get("scope_revision") != 2:
        raise AssertionError("config restore evidence self-test failed")
    _record_config_restore_evidence(restore_context, restore_evidence)
    terminal_evidence = copy.deepcopy(restore_evidence)
    revalidation_evidence = copy.deepcopy(restore_evidence)
    revalidation_evidence["mode"] = "persisted_restore_revalidation"
    _record_config_restore_evidence(restore_context, revalidation_evidence)
    _record_config_restore_evidence(restore_context, revalidation_evidence)
    recorded_checks = restore_context["report"]["checks"]
    if recorded_checks["CFG-RESTORE"]["detail"] != terminal_evidence \
            or recorded_checks["CFG-006"]["detail"] != terminal_evidence \
            or recorded_checks["CFG-RESTORE-REVALIDATION"]["detail"] \
            != revalidation_evidence:
        raise AssertionError("double cleanup overwrote terminal restore proof")
    interrupted_context = {"report": {"checks": {}}}
    try:
        _record_config_restore_evidence(
            interrupted_context, revalidation_evidence)
    except RuntimeError as exc:
        if "immutable terminal proof" not in str(exc):
            raise
    else:
        raise AssertionError("orphan restore revalidation was accepted")
    invalid_restore = copy.deepcopy(restored_proof)
    invalid_restore["node"]["node_replica_fingerprint"] = "invalid"
    try:
        _config_restore_evidence(
            restore_context, invalid_restore, restore_evidence_options)
    except RuntimeError:
        pass
    else:
        raise AssertionError("invalid restore fingerprint self-test failed")
    intent_context = {
        "report": {"checks": {}},
        "recovery": {"config": intent_recovery},
    }
    intent_proof = {
        "converged_snapshot": {
            "scope": "node:fn-01",
            "authority_epoch": 1,
            "execution_epoch": 1,
            "revision": 1,
            "fingerprint": node_restore_fingerprint,
            "config_data": node_restore_config,
        },
        "node": {
            "node_id": "fn-01",
            "node_replica_scope": "node:fn-01",
            "node_replica_revision": 1,
            "node_replica_fingerprint": node_restore_fingerprint,
            "observed_global_revision": 1,
        },
        "mode": "intent_not_applied",
        "cas_retries": 0,
    }
    intent_evidence = _config_restore_evidence(
        intent_context, intent_proof, restore_evidence_options)
    if intent_evidence.get("mode") != "intent_not_applied" \
            or intent_evidence.get("scope_revision") != 1:
        raise AssertionError("unapplied intent restore evidence self-test failed")
    invalid_intent = copy.deepcopy(intent_proof)
    invalid_intent["node"]["node_replica_fingerprint"] = "f" * 64
    try:
        _config_restore_evidence(
            intent_context, invalid_intent, restore_evidence_options)
    except RuntimeError:
        pass
    else:
        raise AssertionError("unapplied intent accepted mismatched Worker scope")
    no_mutation_context = {
        "report": {"checks": {}},
        "recovery": {"config": {
            "mutated": False,
            "original_revision": None,
            "owned_changes": [],
            "original_node_replicas": {},
        }},
    }
    no_mutation_evidence = _config_restore_evidence(
        no_mutation_context,
        {"skipped": True, "restore_confirmed": True},
        restore_evidence_options,
    )
    if no_mutation_evidence.get("mode") != "not_mutated" \
            or no_mutation_evidence.get("not_mutated") is not True:
        raise AssertionError("no-mutation recovery evidence self-test failed")
    proof = mutation_descriptor["proof"]
    roots = {str(change.get("path") or "").split(".")[0]
             for change in mutation_changes}
    if len(mutation_changes) != 8 \
            or roots != {"controllers", "sensors", "actuators", "rules",
                          "processes", "process_states"} \
            or applied.get("ok") is not True \
            or verified.get("ok") is not True \
            or not _config_data_equal(applied.get("config"), mutation_candidate) \
            or not _config_data_equal(restored_candidate, mutation_config) \
            or proof.get("sensor_id") is None \
            or proof.get("actuator_id") is None \
            or proof.get("pid_state_id") is None \
            or proof.get("mode_id", {}).get("before") \
            == proof.get("mode_id", {}).get("after") \
            or proof.get("value_id", {}).get("before") \
            == proof.get("value_id", {}).get("after") \
            or proof.get("rule_threshold", {}).get("before") \
            == proof.get("rule_threshold", {}).get("after") \
            or candidate_scopes["node"] == before_scopes["node"] \
            or candidate_scopes["cross"] != before_scopes["cross"]:
        raise AssertionError("scope-safe Master config runtime matrix self-test failed")
    quiet_filters = _pagination_retained_filters([
        {"resource_type": "sensor", "resource_pattern": "*"},
        {"resource_type": "command", "resource_pattern": "*"},
    ])
    if quiet_filters != [{"resource_type": "sensor", "resource_pattern": "*"}]:
        raise AssertionError("pagination retained-stream filter self-test failed")
    legacy_fingerprint = "a" * 64
    legacy_state = _node_convergence_state({
        "node_id": "fn-01",
        "admission_state": "APPROVED",
        "observed_state": "ACTIVE",
        "online": True,
        "node_replica_scope": "node:fn-01",
        "node_replica_revision": 1,
        "node_replica_fingerprint": legacy_fingerprint,
        "observed_global_revision": 1,
        "live_config_revision": 2,
    }, "fn-01", {
        "minimum_node_replica_revision": 1,
        "node_replica_fingerprint": legacy_fingerprint,
        "minimum_observed_global_revision": 1,
        "minimum_live_config_revision": 2,
    })
    if legacy_state.get("ready") is not True:
        raise AssertionError("independent Worker revision readiness self-test failed")
    fresh_fingerprint = "b" * 64
    fresh_state = _node_convergence_state({
        "node_id": "fn-01",
        "admission_state": "APPROVED",
        "observed_state": "ACTIVE",
        "online": True,
        "node_replica_scope": "node:fn-01",
        "node_replica_revision": 0,
        "node_replica_fingerprint": fresh_fingerprint,
        "observed_global_revision": 0,
        "live_config_revision": 1,
    }, "fn-01", {})
    if fresh_state.get("ready") is not True:
        raise AssertionError("fresh revision-zero Worker readiness self-test failed")
    fresh_mirror_fingerprint = _config_fingerprint(node_restore_config)
    fresh_mirror_result = {"result": {"config": {"mirror": {
        "scope": "node:fn-01",
        "authority_epoch": 0,
        "scope_revision": 0,
        "execution_epoch": 0,
        "fingerprint": fresh_mirror_fingerprint,
        "snapshot": {
            "scope": "node:fn-01",
            "authority_epoch": 0,
            "scope_revision": 0,
            "execution_epoch": 0,
            "fingerprint": fresh_mirror_fingerprint,
            "config": copy.deepcopy(node_restore_config),
        },
    }}}}
    fresh_mirror_snapshot = _config_snapshot(fresh_mirror_result)
    if fresh_mirror_snapshot.get("authority_epoch") != 0 \
            or fresh_mirror_snapshot.get("revision") != 0 \
            or fresh_mirror_snapshot.get("execution_epoch") != 0:
        raise AssertionError("fresh revision-zero Mirror self-test failed")
    invalid_fresh_mirror = copy.deepcopy(fresh_mirror_result)
    invalid_fresh_mirror["result"]["config"]["mirror"]["scope_revision"] = True
    try:
        _config_snapshot(invalid_fresh_mirror)
    except RuntimeError:
        pass
    else:
        raise AssertionError("boolean Mirror revision was accepted")
    headers = _http_headers(request_context, {
        "csrf": True,
        "idempotency_key": "self-test:http:mutation",
    }, b"{}")
    if headers.get("Idempotency-Key") != "self-test:http:mutation":
        raise AssertionError("HTTPS mutation idempotency header self-test failed")
    if headers.get("X-CSRF-Token") != "csrf-canary":
        raise AssertionError("HTTPS mutation CSRF header self-test failed")
    query_target = _http_request_target({
        "path": "/api/v1/config",
        "query": {"scope": "node:fn-01"},
    })
    if query_target != "/api/v1/config?scope=node%3Afn-01":
        raise AssertionError("HTTPS structured query encoding self-test failed")
    invalid_http_requests = (
        {"path": "/api/v1/config?scope=node%3Afn-01"},
        {"path": "/api/v1/config", "query": "scope=node%3Afn-01"},
        {"path": "/api/v1/config", "query": {"scope": {"unsafe": True}}},
    )
    for invalid_request in invalid_http_requests:
        try:
            _http_request_target(invalid_request)
        except RuntimeError:
            continue
        raise AssertionError("unsafe HTTPS query request self-test failed")
    sanitized = _sanitize({"bearer_token": "canary", "nested": {"password": "canary"}})
    if "canary" in json.dumps(sanitized):
        raise AssertionError("recursive secret redaction self-test failed")
    safety_controller_uuid = "00000000-0000-4000-8000-000000000101"
    safety_snapshot = {"config_data": {
        "site_controller_map": [{
            "node_id": "fn-01", "controller_uuid": safety_controller_uuid,
            "routing_role": "owner", "enabled": True,
        }],
        "virtual_controllers": [{
            "controller_uuid": safety_controller_uuid, "virt_active": True,
            "host": "192.168.0.3", "port": 5020,
        }],
        "actuators": [{
            "node_id": "fn-01", "controller_uuid": safety_controller_uuid,
            "actuator_id": 1, "address": 1, "function_type": "write_coil",
        }],
        "sensors": [{
            "node_id": "fn-01", "controller_uuid": safety_controller_uuid,
            "sensor_id": 1, "address": 1,
            "function_type": "read_discrete_inputs",
        }],
    }}
    selected = _select_target(
        safety_snapshot, "fn-01", actuator_id="1", sensor_id="1",
        approved_host="192.168.0.3", approved_port=5020,
    )
    if selected["owner_node_id"] != "fn-01":
        raise AssertionError("actuator owner fixture self-test failed")
    recovered_target = _recovery_actuator_target(
        safety_snapshot,
        {"controller_uuid": safety_controller_uuid, "actuator_id": 1},
        approved_host="192.168.0.3", approved_port=5020,
    )
    if recovered_target["actuator"].get("actuator_id") != 1:
        raise AssertionError("canonical V9 actuator recovery self-test failed")
    recovery_descriptor = _sanitized_modbus_target_descriptor(
        selected, "192.168.0.3", 5020)
    asyncio.run(_async_verified_noop_actuator_restore_self_test(
        arguments, selected, recovery_descriptor))
    interrupted_state = {
        "node_id": "fn-01",
        "controller_uuid": safety_controller_uuid,
        "actuator_id": 1,
        "target_descriptor": recovery_descriptor,
    }
    removed_snapshot = copy.deepcopy(safety_snapshot)
    removed_snapshot["config_data"]["actuators"] = []
    descriptor_recovery = _resolve_interrupted_actuator_target(
        removed_snapshot,
        interrupted_state,
        approved_host="192.168.0.3",
        approved_port=5020,
    )
    if descriptor_recovery.get("route") != "persisted_descriptor" \
            or descriptor_recovery.get("authority_entity_present") is not False \
            or _sanitized_modbus_target_descriptor(
                descriptor_recovery["target"], "192.168.0.3", 5020
            ) != recovery_descriptor:
        raise AssertionError("removed-entity descriptor recovery self-test failed")
    legacy_recovery = _resolve_interrupted_actuator_target(
        safety_snapshot,
        dict(interrupted_state, target_descriptor=None),
        approved_host="192.168.0.3",
        approved_port=5020,
    )
    if legacy_recovery.get("route") != "authority" \
            or legacy_recovery.get("legacy_descriptor_reconstructed") is not True:
        raise AssertionError("legacy authority recovery fallback self-test failed")
    tampered_descriptor = copy.deepcopy(recovery_descriptor)
    tampered_descriptor["host"] = "127.0.0.1"
    try:
        _resolve_interrupted_actuator_target(
            removed_snapshot,
            dict(interrupted_state, target_descriptor=tampered_descriptor),
            approved_host="192.168.0.3",
            approved_port=5020,
        )
    except RuntimeError as exc:
        if not str(exc).startswith("BLOCKED:"):
            raise
    else:
        raise AssertionError("unapproved persisted recovery endpoint was accepted")
    changed_authority = copy.deepcopy(safety_snapshot)
    changed_authority["config_data"]["actuators"][0]["address"] = 2
    try:
        _resolve_interrupted_actuator_target(
            changed_authority,
            interrupted_state,
            approved_host="192.168.0.3",
            approved_port=5020,
        )
    except RuntimeError as exc:
        if not str(exc).startswith("BLOCKED:"):
            raise
    else:
        raise AssertionError("authority/descriptor recovery mismatch was accepted")
    unsafe_snapshot = copy.deepcopy(safety_snapshot)
    unsafe_snapshot["config_data"]["site_controller_map"] = []
    try:
        _select_target(
            unsafe_snapshot, "fn-01", actuator_id="1", sensor_id="1",
            approved_host="192.168.0.3", approved_port=5020,
        )
    except RuntimeError as exc:
        if not str(exc).startswith("BLOCKED:"):
            raise
    else:
        raise AssertionError("owner-less actuator fixture was accepted")
    report = _initial_report(arguments, "self-test")
    if report["cross_worker"]["cross_site"] is not False:
        raise AssertionError("report must never claim positive Cross-Site coverage")
    try:
        _validate_report({
            "arguments": arguments,
            "report": report,
            "bearer_token": "self-test-bearer",
        })
    except RuntimeError as exc:
        if "misses required checks" not in str(exc):
            raise
    else:
        raise AssertionError("incomplete E2E report was accepted")
    timeout_detail = _node_config_timeout_detail(
        {
            "revision": 9,
            "fingerprint": "a" * 64,
            "config_data": {"oversized": "x" * 8192},
        },
        {"ready": False, "checks": {"replica_fingerprint": False}},
        [
            {"path": "sensors", "value": "x" * 8192},
            {"path": "actuators", "value": "y" * 8192},
        ],
    )
    timeout_payload = json.dumps(timeout_detail, sort_keys=True)
    if len(timeout_payload) > 1024 or "oversized" in timeout_payload:
        raise AssertionError("Worker config timeout detail is not compact")
    with tempfile.TemporaryDirectory(prefix="e2e-v0025-files-") as directory:
        directory = pathlib.Path(directory)
        first = directory / "first.json"
        second = directory / "second.json"
        first.write_text("{}\n", encoding="utf-8")
        second.write_text("{}\n", encoding="utf-8")
        _assert_distinct_paths({"first": first, "second": second})
        try:
            _assert_distinct_paths({"result": first, "recovery": first})
        except RuntimeError:
            pass
        else:
            raise AssertionError("aliased E2E output paths were accepted")
        hardlink = directory / "hardlink.json"
        os.link(first, hardlink)
        try:
            _assert_distinct_paths({"result": first, "log": hardlink})
        except RuntimeError:
            pass
        else:
            raise AssertionError("hardlink-aliased E2E files were accepted")
        recovery_context = {
            "arguments": arguments,
            "run_id": "self-test-recovery",
            "principal_subject": "00000000-0000-4000-8000-000000000001",
            "schema_validators": _load_schema_validators(
                pathlib.Path(__file__).resolve().with_name("schemas")
            ),
            "recovery_path": directory / "recovery.json",
        }
        recovery_context["recovery"] = _new_recovery(recovery_context)
        recovery_context["recovery"]["config"]["original_node_replicas"] = {
            "fn-01": {
                "revision": 0,
                "fingerprint": fresh_mirror_fingerprint,
            },
        }
        _validate_schema(
            recovery_context, "recovery_state", recovery_context["recovery"])
        invalid_recovery_revision = copy.deepcopy(recovery_context["recovery"])
        invalid_recovery_revision["config"]["original_node_replicas"] \
            ["fn-01"]["revision"] = -1
        try:
            _validate_schema(
                recovery_context, "recovery_state", invalid_recovery_revision)
        except RuntimeError:
            pass
        else:
            raise AssertionError(
                "negative Worker recovery revision was accepted")
        recovery_context["recovery"]["actuator"].update({
            "node_id": "fn-01",
            "controller_uuid": safety_controller_uuid,
            "actuator_id": 1,
            "target_descriptor": recovery_descriptor,
        })
        recovery_context["recovery"]["failure"] = (
            "failure:" + "x" * (RECOVERY_FAILURE_MAX_LENGTH * 2)
        )
        _complete_recovery(recovery_context, False)
        persisted_recovery = _load_json(
            recovery_context["recovery_path"], "self-test recovery journal"
        )
        if (
            persisted_recovery.get("status") != "FAILED"
            or len(persisted_recovery.get("failure") or "")
            != RECOVERY_FAILURE_MAX_LENGTH
        ):
            raise AssertionError("bounded recovery failure persistence self-test failed")
        _validate_schema(recovery_context, "recovery_state", persisted_recovery)
        live_log = directory / "live.log"
        live_log.write_text("preflight\n", encoding="utf-8")
        baseline = _capture_external_log(live_log, "self-test-live-log")
        unchanged = _external_log_progress({"external_log_baselines": [baseline]})
        if not unchanged or unchanged[0].get("changed_after_start") is not False:
            raise AssertionError("quiet live-log capture was rejected")
        with live_log.open("a", encoding="utf-8") as handle:
            handle.write("after-start\n")
        progress = _external_log_progress({"external_log_baselines": [baseline]})
        if not progress or progress[0].get("grew") is not True:
            raise AssertionError("live-log growth self-test failed")
        empty_log = directory / "empty.log"
        empty_log.touch()
        stale_log = directory / "stale.log"
        stale_log.write_text("old capture\n", encoding="utf-8")
        stale_time = time.time() - EXTERNAL_LOG_MAX_START_AGE_S - 10.0
        os.utime(stale_log, (stale_time, stale_time))
        for label, path in (("empty", empty_log), ("stale", stale_log)):
            try:
                _capture_external_log(path, "self-test-" + label)
            except RuntimeError:
                continue
            raise AssertionError(label + " external log was accepted")
    _broker_signature_self_test()
    asyncio.run(_async_config_restore_race_self_test(arguments))
    asyncio.run(_async_client_self_test(arguments))
    asyncio.run(_async_event_update_interleave_self_test(arguments))
    asyncio.run(_async_event_open_alignment_self_test(arguments))
    print("PASS Control Plane WSS E2E self-test")
    return 0


def _parser():
    root = pathlib.Path(__file__).resolve().parent
    result_dir = root / "results"
    default_ca = root.parents[1] / "proxy_gateway" / "test-pki" / "wss" / "ca.crt"
    parser = argparse.ArgumentParser(description="Automation Control Plane v0025 WSS-primary E2E")
    parser.add_argument("--profile", choices=("core", "full-lab"), default="core")
    parser.add_argument("--https-url", default="https://192.168.0.82:8444")
    parser.add_argument("--wss-url", default="wss://192.168.0.82:8443")
    parser.add_argument("--origin", default="https://192.168.0.82:8444")
    parser.add_argument("--ca-file", default=str(default_ca))
    parser.add_argument("--username", default="admin")
    parser.add_argument("--password-env", default="AUTOMATION_ADMIN_PASSWORD")
    parser.add_argument("--bearer-token-env", default="E2E_BEARER_TOKEN")
    parser.add_argument("--site-id", default="site-main")
    parser.add_argument("--node-id", default="fn-01")
    parser.add_argument("--controller-uuid")
    parser.add_argument("--actuator-id", default="1")
    parser.add_argument("--sensor-id", default="1")
    parser.add_argument("--approved-actuator-host", default="192.168.0.3")
    parser.add_argument("--approved-actuator-port", type=int, default=5020)
    parser.add_argument("--confirm-mutations", action="store_true")
    parser.add_argument("--last-event-id", type=int)
    parser.add_argument("--http-timeout", type=float, default=10.0)
    parser.add_argument("--command-timeout", type=float, default=15.0)
    parser.add_argument("--event-timeout", type=float, default=45.0)
    parser.add_argument("--operation-timeout", type=float, default=90.0)
    parser.add_argument("--parallel-clients", type=int, default=128)
    parser.add_argument("--broker-introspection-proof")
    parser.add_argument("--broker-proof-public-key")
    parser.add_argument("--broker-proof-key-sha256")
    parser.add_argument("--broker-release-proof")
    parser.add_argument("--fn02-heartbeat-stale-s", type=float, default=45.0)
    parser.add_argument("--fn02-runtime-config")
    parser.add_argument("--worker-launcher-command-file")
    parser.add_argument("--proxy-log-source")
    parser.add_argument("--protocol-log-source")
    parser.add_argument("--secret-log-source", action="append", default=[])
    parser.add_argument(
        "--action-fixture", default=str(root / "fixtures" / "action_matrix_v0025.json")
    )
    parser.add_argument(
        "--cross-fixture", default=str(root / "fixtures" / "cross_worker_site-main.json")
    )
    parser.add_argument("--result-file", default=str(result_dir / "e2e-v0025-result.json"))
    parser.add_argument("--log-file", default=str(result_dir / "e2e-v0025.jsonl"))
    parser.add_argument("--recovery-file", default=str(result_dir / "e2e-v0025-recovery.json"))
    parser.add_argument("--run-id", default="e2e-" + str(int(time.time())))
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def _open_log(path):
    path = pathlib.Path(path).expanduser().resolve()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
    return path, os.fdopen(descriptor, "a", encoding="utf-8")


def _validate_report(context):
    report = context["report"]
    required = list(CORE_REQUIRED_CHECKS)
    if context["arguments"].profile == "full-lab":
        required.extend(FULL_LAB_REQUIRED_CHECKS)
    missing = [name for name in required if name not in report["checks"]]
    if missing:
        raise RuntimeError("FAILED: E2E report misses required checks: " + ", ".join(missing))
    for name, item in report["checks"].items():
        status = item.get("status") if isinstance(item, dict) else None
        allowed_skip = (
            name == "SEC-004" and bool(context.get("bearer_token"))
        ) or (
            name == "CROSS-WORKER" and context["arguments"].profile == "core"
        )
        if status == "SKIPPED" and allowed_skip:
            continue
        if status != "PASSED":
            raise RuntimeError("FAILED: E2E report check is not PASSED: " + str(name))
    if report.get("tls_verification") is not True:
        raise RuntimeError("FAILED: report does not prove TLS verification")
    if report.get("config_restore_confirmed") is not True:
        raise RuntimeError("FAILED: report does not prove config restore")
    if report.get("actuator_restore_confirmed") is not True:
        raise RuntimeError("FAILED: report does not prove actuator restore")
    cross = report.get("cross_worker") if isinstance(report.get("cross_worker"), dict) else {}
    if context["arguments"].profile == "full-lab":
        worker = report.get("worker_fn02") if isinstance(report.get("worker_fn02"), dict) else {}
        identity = cross.get("identity_chain") \
            if isinstance(cross.get("identity_chain"), dict) else {}
        source = identity.get("source_event") \
            if isinstance(identity.get("source_event"), dict) else {}
        fact = identity.get("cross_fact") \
            if isinstance(identity.get("cross_fact"), dict) else {}
        intent = identity.get("remote_intent") \
            if isinstance(identity.get("remote_intent"), dict) else {}
        result = identity.get("fn02_result") \
            if isinstance(identity.get("fn02_result"), dict) else {}
        exact_identity_chain = bool(
            identity.get("trace_confirmed") is True
            and source.get("message_id")
            and fact.get("source_wss_message_id") == source.get("message_id")
            and fact.get("scope") == intent.get("execution_scope")
            and intent.get("observed_message_id") == intent.get("derived_correlation_key")
            and intent.get("operation_id") == result.get("operation_id")
            and intent.get("observed_message_id")
            == result.get("execution_trace_message_id")
            and result.get("observed_broker_message_id")
            and result.get("wss_message_id")
        )
        if not (
            cross.get("executed") is True
            and cross.get("tested") is True
            and cross.get("cross_site") is False
            and exact_identity_chain
            and worker.get("executed") is True
            and worker.get("converged") is True
            and worker.get("exclusive_lock_acquired") is True
            and worker.get("broker_session_exclusive") is True
            and worker.get("exclusive_lock_released") is True
        ):
            raise RuntimeError("FAILED: full-lab report lacks cross-worker invariants")
    elif not (
        cross.get("executed") is False
        and cross.get("tested") is False
        and cross.get("skipped") is True
        and cross.get("cross_site") is False
    ):
        raise RuntimeError("FAILED: core report overclaims cross-worker coverage")
    return True


def _final_status(context, error):
    if error is None:
        return "PASSED"
    return "BLOCKED" if str(error).startswith("BLOCKED:") else "FAILED"


def main():
    arguments = _parser().parse_args()
    if arguments.self_test:
        return _self_test(arguments)
    context = None
    error = None
    try:
        servers = _validate_arguments(arguments)
        schema_directory = pathlib.Path(__file__).resolve().with_name("schemas")
        schema_validators = _load_schema_validators(schema_directory)
        log_path, log_handle = _open_log(arguments.log_file)
        result_path = pathlib.Path(arguments.result_file).expanduser().resolve()
        recovery_path = pathlib.Path(arguments.recovery_file).expanduser().resolve()
        context = {
            "arguments": arguments,
            "servers": servers,
            "external_log_baselines": copy.deepcopy(
                servers.get("external_log_baselines") or []
            ),
            "ssl_context": _ssl_context(servers["ca_file"]),
            "schema_validators": schema_validators,
            "cookies": {},
            "bearer_token": None,
            "run_id": arguments.run_id,
            "report": _initial_report(arguments, arguments.run_id),
            "result_path": result_path,
            "result_dir": result_path.parent,
            "recovery_path": recovery_path,
            "recovery": None,
            "log_path": log_path,
            "log_handle": log_handle,
            "verbose": arguments.verbose,
            "run_locks": [],
        }
        locks = _acquire_run_locks(context)
        _action_fixture(context)
        _log(context, "INFO", "Automation Control Plane WSS E2E " + VERSION, details={
            "runner_locks": locks,
        })
        asyncio.run(_run_live(context))
    except KeyboardInterrupt as exc:
        error = exc
    except Exception as exc:
        error = exc
        if context:
            _log(context, "ERROR", "E2E execution failed", details={
                "error": str(exc), "traceback": traceback.format_exc(),
            })
    if context is None:
        status = "BLOCKED" if error and str(error).startswith("BLOCKED:") else "FAILED"
        result = {
            "version": VERSION,
            "profile": arguments.profile,
            "status": status,
            "error": str(error) if error else "initialization failed",
            "cross_site": False,
            "completed_at": _utc_now(),
        }
        try:
            _atomic_json(arguments.result_file, result)
        except Exception:
            pass
        print(json.dumps(result, ensure_ascii=False, indent=2), file=sys.stderr)
        return STATUS_CODES[status]
    status = _final_status(context, error)
    context["report"]["status"] = status
    context["report"]["completed_at"] = _utc_now()
    if error:
        context["report"]["error"] = str(error)
    try:
        _atomic_json(context["result_path"], context["report"])
        _log(context, "INFO" if status == "PASSED" else "ERROR", "E2E " + status, details={
            "result_file": str(context["result_path"]), "status": status,
        })
    finally:
        if context.get("log_handle") and not context["log_handle"].closed:
            context["log_handle"].close()
        _release_run_locks(context)
    print(json.dumps(context["report"], ensure_ascii=False, indent=2, default=str))
    return STATUS_CODES[status]


if __name__ == "__main__":
    raise SystemExit(main())

# -*- coding: utf-8 -*-

"""Semantic Master readiness for Worker-authority and Master-authority modes."""

import argparse
import json
import sys
import urllib.error
import urllib.request


def _valid_fingerprint(value):
    fingerprint = str(value or "").strip().lower()
    return bool(
        len(fingerprint) == 64
        and set(fingerprint) != {"0"}
        and all(character in "0123456789abcdef" for character in fingerprint)
    )


def _missing_scope_errors(scopes, expectations, noun):
    errors = []
    available = set(scopes)
    missing_nodes = tuple(sorted(
        set(expectations.get("nodes") or ()) - available))
    if missing_nodes:
        errors.append("missing Node {}: ".format(noun) + ",".join(missing_nodes))
    missing_cross = tuple(sorted(
        set(expectations.get("cross") or ()) - available))
    if missing_cross:
        errors.append(
            "missing explicitly required Cross {}: ".format(noun)
            + ",".join(missing_cross))
    return errors


def _evaluate_node_local(runtime, expectations):
    registry = runtime.get("registry") \
        if isinstance(runtime.get("registry"), dict) else {}
    bootstrap = runtime.get("bootstrap") \
        if isinstance(runtime.get("bootstrap"), dict) else {}
    errors = []
    if runtime.get("config_authority") != "worker":
        errors.append("node_local mode does not identify Worker config authority")
    if runtime.get("master_automation_execution") is not False:
        errors.append("Master automation execution is not disabled")
    if registry.get("assigns_worker_revision") is not False:
        errors.append("Registry may assign Worker revisions in node_local mode")
    if not _valid_fingerprint(registry.get("static_topology_fingerprint")):
        errors.append("Registry static topology fingerprint is missing")

    role = str(registry.get("role") or "")
    expected_bootstrap = {
        "leader": ("ready", "registry_ready"),
        "standby": ("registry_standby", "standby"),
    }.get(role)
    observed_bootstrap = (
        bootstrap.get("state"), bootstrap.get("last_status"))
    if expected_bootstrap is None:
        errors.append("node_local Registry role is neither leader nor standby")
    elif observed_bootstrap != expected_bootstrap:
        errors.append("node_local Registry bootstrap is not ready for its role")

    mirror_scopes = set(registry.get("mirror_scopes") or ())
    try:
        mirror_count = int(registry.get("mirror_count") or 0)
    except (TypeError, ValueError):
        mirror_count = -1
    if mirror_count != len(mirror_scopes):
        errors.append("Registry mirror count does not match its scope inventory")
    errors.extend(_missing_scope_errors(
        mirror_scopes, expectations, "mirrors"))
    return errors


def _evaluate_master_global(runtime, expectations):
    heap = runtime.get("heap") if isinstance(runtime.get("heap"), dict) else {}
    errors = []
    if int(heap.get("global_revision") or 0) < 1:
        errors.append("Master global revision is below one")
    if not _valid_fingerprint(heap.get("global_fingerprint")):
        errors.append("Master global fingerprint is missing")
    errors.extend(_missing_scope_errors(
        set(heap.get("scopes") or ()), expectations, "scopes"))
    return errors


def evaluate_runtime_state(payload, **kwargs):
    runtime = payload.get("runtime") if isinstance(payload, dict) else None
    runtime = runtime if isinstance(runtime, dict) else {}
    errors = []
    if not isinstance(payload, dict) or payload.get("ok") is not True \
            or runtime.get("ready") is not True:
        errors.append("Master readiness endpoint is not ready")
    if runtime.get("config_ready") is not True:
        errors.append("Master authority configuration is not ready")

    expectations = {
        "nodes": tuple(kwargs.get("expected_node_scopes") or ()),
        "cross": tuple(kwargs.get("expected_cross_scopes") or ()),
    }
    authority_mode = str(runtime.get("authority_mode") or "node_local")
    if authority_mode == "node_local":
        errors.extend(_evaluate_node_local(runtime, expectations))
    elif authority_mode == "master_global":
        errors.extend(_evaluate_master_global(runtime, expectations))
    else:
        errors.append("unsupported Master authority mode: " + authority_mode)
    return tuple(errors)


def _arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:9090/health/ready")
    parser.add_argument("--timeout", type=float, default=5.0)
    parser.add_argument("--expected-node-scope", action="append", default=[])
    parser.add_argument("--expected-cross-scope", action="append", default=[])
    return parser.parse_args()


def _read(url, timeout):
    request = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as error:
        try:
            return json.loads(error.read().decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise RuntimeError(
                "Master readiness returned HTTP {}".format(error.code)) from error


def main():
    arguments = _arguments()
    try:
        payload = _read(arguments.url, max(0.1, arguments.timeout))
    except (OSError, RuntimeError, json.JSONDecodeError) as error:
        print(
            "FAIL Master semantic readiness probe: {}".format(error),
            file=sys.stderr,
        )
        return 1
    errors = evaluate_runtime_state(
        payload,
        expected_node_scopes=arguments.expected_node_scope,
        expected_cross_scopes=arguments.expected_cross_scope,
    )
    if errors:
        for error in errors:
            print("FAIL Master semantic readiness: " + error, file=sys.stderr)
        runtime = payload.get("runtime") if isinstance(payload, dict) else {}
        diagnostic = {
            "authority_mode": runtime.get("authority_mode"),
            "bootstrap": runtime.get("bootstrap"),
            "config_authority": runtime.get("config_authority"),
            "config_ready": runtime.get("config_ready"),
            "heap": runtime.get("heap"),
            "last_error": runtime.get("last_error"),
            "master_automation_execution": runtime.get(
                "master_automation_execution"),
            "registry": runtime.get("registry"),
        }
        print(json.dumps(diagnostic, sort_keys=True), file=sys.stderr)
        return 1
    runtime = payload.get("runtime") if isinstance(payload, dict) else {}
    authority_mode = str(runtime.get("authority_mode") or "node_local")
    print("PASS Master {} semantic readiness".format(authority_mode))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

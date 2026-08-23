#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Validate the concrete, credential-free administration release set."""

import argparse
import hashlib
import json
import re
import sys
import zipfile

from functools import partial
from pathlib import Path


sys.dont_write_bytecode = True
RELEASE_ROOT = Path(__file__).resolve().parent.parent
ADMIN_ROOT = RELEASE_ROOT / "administration"
if str(ADMIN_ROOT) not in sys.path:
    sys.path.insert(0, str(ADMIN_ROOT))

import orchestrate  # noqa: E402
import component_adapter  # noqa: E402


def _load(name):
    with (ADMIN_ROOT / name).open("r", encoding="utf-8") as stream:
        value = json.load(stream)
    if not isinstance(value, dict):
        raise RuntimeError(name + " must contain one JSON object")
    return value


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(partial(stream.read, 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _regular_file(path, label):
    if path.is_symlink() or not path.is_file():
        raise RuntimeError(label + " is not a regular non-symlink file")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--require-worker-artifact", action="store_true")
    arguments = parser.parse_args()

    inventory_path = ADMIN_ROOT / "inventory.test-lab.json"
    registry_path = ADMIN_ROOT / "component_registry.test-lab.json"
    manifest_path = ADMIN_ROOT / "release_manifest.json"
    inventory = _load(inventory_path.name)
    registry = _load(registry_path.name)
    manifest = _load(manifest_path.name)
    orchestrate.validate_inventory(inventory)
    orchestrate.validate_registry(registry)
    orchestrate.validate_manifest(manifest)
    orchestrate.validate_inventory(_load("inventory.example.json"))
    orchestrate.validate_registry(_load("component_registry.example.json"))
    orchestrate.validate_registry(_load("component_registry.legacy.example.json"))
    orchestrate.validate_manifest(_load("release_manifest.example.json"))
    if registry.get("execution_mode") != "component_adapters":
        raise RuntimeError("concrete administration registry is not operational adapter mode")

    release = manifest.get("release") or {}
    worker = manifest.get("worker") or {}
    compatibility = manifest.get("compatibility") or {}
    if release.get("version") != "0.26.0" \
            or release.get("revision") != "v0026-r1" \
            or worker.get("revision") != "v0101" \
            or worker.get("version") != "0.101.0" \
            or compatibility != {
                "node_storage": [9],
                "legacy_import": [5],
                "cross_storage": [8],
            }:
        raise RuntimeError("administration release identities or contracts differ")
    expected_order = [
        "database", "mqtt", "master", "proxy", "mcp_controller_server", "worker",
    ]
    if registry.get("required_component_order") != expected_order \
            or (manifest.get("legacy_backend") or {}).get("component_order") != expected_order:
        raise RuntimeError("administration component order does not include MCP correctly")
    control_targets = [
        target for target in inventory.get("targets") or ()
        if target.get("role") == "control_plane"
    ]
    if len(control_targets) != 1 \
            or "mcp_controller_server" not in control_targets[0].get("components", ()):
        raise RuntimeError("administration inventory does not assign the MCP component")
    adapter = control_targets[0].get("adapter") or {}
    if adapter.get("mcp_source_instance_id") != "mcp-controller-01" \
            or not adapter.get("mcp_auth_source"):
        raise RuntimeError("administration MCP target configuration is incomplete")
    release_root = (ADMIN_ROOT / str(inventory.get("release_root"))).resolve()
    if release_root != RELEASE_ROOT or release_root.is_symlink():
        raise RuntimeError("administration inventory targets another release root")
    state_dir = (ADMIN_ROOT / str(inventory.get("state_dir"))).resolve()
    if state_dir == release_root or release_root in state_dir.parents:
        raise RuntimeError("administration state is inside the immutable release")
    adapter_path = ADMIN_ROOT / "component_adapter.py"
    _regular_file(adapter_path, "administration component adapter")
    if component_adapter.self_test() != 0:
        raise RuntimeError("administration component adapter self-test failed")
    for config in inventory.get("config_files") or ():
        path = (release_root / str(config.get("path") or "")).resolve()
        if release_root not in path.parents:
            raise RuntimeError("administration config path escapes release root")
        _regular_file(path, "administration config")
        if _sha256(path) != str(config.get("sha256") or "").lower():
            raise RuntimeError("administration config SHA-256 differs: " + str(path))

    worker_artifact = None
    artifact_ids = set()
    for artifact in manifest.get("artifacts") or ():
        artifact_ids.add(artifact.get("artifact_id"))
        path = (release_root / str(artifact.get("path") or "")).resolve()
        expected_hash = str(artifact.get("sha256") or "").lower()
        if re.fullmatch(r"[0-9a-f]{64}", expected_hash) is None:
            raise RuntimeError("administration artifact SHA-256 is invalid")
        if artifact.get("artifact_id") == "automation-system-v0101":
            worker_artifact = path
            if path != release_root.parent / "automation_system_v0101.zip":
                raise RuntimeError("administration Worker artifact path differs")
            if path.exists():
                _regular_file(path, "administration Worker artifact")
                if _sha256(path) != expected_hash:
                    raise RuntimeError("administration Worker artifact SHA-256 differs")
                expected_manifest = str(artifact.get("manifest_sha256") or "").lower()
                if re.fullmatch(r"[0-9a-f]{64}", expected_manifest) is None:
                    raise RuntimeError("Worker manifest SHA-256 anchor is missing")
                with zipfile.ZipFile(path, "r") as archive:
                    sums = [name for name in archive.namelist()
                            if name.endswith("/SHA256SUMS")]
                    if len(sums) != 1:
                        raise RuntimeError("Worker archive SHA256SUMS is ambiguous")
                    observed_manifest = hashlib.sha256(archive.read(sums[0])).hexdigest()
                if observed_manifest != expected_manifest:
                    raise RuntimeError("Worker archive manifest SHA-256 differs")
            elif arguments.require_worker_artifact:
                raise RuntimeError("administration Worker artifact is required")
        else:
            if release_root not in path.parents:
                raise RuntimeError("administration artifact path escapes release root")
            _regular_file(path, "administration artifact")
            if _sha256(path) != expected_hash:
                raise RuntimeError("administration artifact SHA-256 differs")
    if worker_artifact is None:
        raise RuntimeError("administration Worker artifact declaration is missing")
    required_mcp_artifacts = {
        "mcp-release-metadata", "mcp-component-manifest", "mcp-static-test-hook",
    }
    if not required_mcp_artifacts.issubset(artifact_ids):
        raise RuntimeError("administration MCP artifact bindings are incomplete")
    with (RELEASE_ROOT / "mcp_controller_server" / "RELEASE_METADATA.json").open(
            "r", encoding="utf-8") as stream:
        mcp_metadata = json.load(stream)
    integration = mcp_metadata.get("integration") or {}
    if mcp_metadata.get("version") != "0.1.0" \
            or mcp_metadata.get("release_revision") != "v0001-r1" \
            or integration.get("bundle_revision") != "v0026-r1" \
            or integration.get("orchestration_component") != "mcp_controller_server":
        raise RuntimeError("MCP release metadata is not bound to the v0026 bundle")
    required_tests = set(manifest.get("required_tests") or ())
    if not {
            "mcp-static-contract", "mcp-mqtt-boundary", "mcp-readiness",
    }.issubset(required_tests):
        raise RuntimeError("administration MCP acceptance gates are incomplete")

    backend = manifest.get("legacy_backend") or {}
    backend_path = (release_root / str(backend.get("path") or "")).resolve()
    _regular_file(backend_path, "administration legacy backend")
    if _sha256(backend_path) != str(backend.get("sha256") or "").lower():
        raise RuntimeError("administration legacy backend SHA-256 differs")

    plan_id = None
    if worker_artifact.is_file():
        first = orchestrate.build_plan(
            str(inventory_path), str(registry_path), str(manifest_path))
        second = orchestrate.build_plan(
            str(inventory_path), str(registry_path), str(manifest_path))
        if orchestrate.canonical_bytes(first) != orchestrate.canonical_bytes(second) \
                or first.get("plan_id") != second.get("plan_id") \
                or first.get("has_placeholders") is not False:
            raise RuntimeError("administration plan is not deterministic and pinned")
        plan_id = first["plan_id"]
        if first.get("execution_mode") != "component_adapters" \
                or set(first.get("operation_steps") or {}) != set(orchestrate.OPERATION_NAMES):
            raise RuntimeError("administration adapter operation plan is incomplete")
        integrity = first.get("release_integrity") or {}
        if integrity.get("root_sha256sums_sha256") != _sha256(RELEASE_ROOT / "SHA256SUMS") \
                or not integrity.get("full_release_digest"):
            raise RuntimeError("administration plan does not bind root SHA256SUMS")
        partial = orchestrate.build_plan(
            str(inventory_path), str(registry_path), str(manifest_path),
            selected_components=["worker"], selected_targets=["worker-fn-01"],
        )
        if partial.get("selected_components") != ["worker"] \
                or partial.get("lock_targets") != ["worker-fn-01"] \
                or partial.get("worker_rollout", {}).get("canary") != ["worker-fn-01"]:
            raise RuntimeError("administration partial Worker plan is not target-exact")
        legacy = orchestrate.build_plan(
            str(inventory_path), str(registry_path), str(manifest_path),
            force_legacy_full_once=True,
        )
        legacy_apply = [step for step in legacy.get("steps") or ()
                        if step.get("component") == "legacy_full"
                        and step.get("phase") == "apply"]
        if legacy.get("execution_mode") != "legacy_full_once" or len(legacy_apply) != 1:
            raise RuntimeError("administration legacy exactly-once fallback is unavailable")

    print("PASS concrete administration release set: six-component operational adapters, "
          "isolated MCP identity, partial targets, root digest, V9 primary, V8 cross, "
          "V5 legacy import; deterministic_plan={}".format(
              plan_id or "external-artifact-absent"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

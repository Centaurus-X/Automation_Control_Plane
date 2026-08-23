"""Shared deterministic fixtures for fleet tests."""

import copy
import pathlib

from automation_fleet.canonical import read_json


IMAGE_DIGEST = "sha256:" + ("1" * 64)
RUNTIME_DIGEST = "sha256:c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467"


def release_root():
    return pathlib.Path(__file__).resolve().parents[1]


def catalog():
    return read_json(release_root() / "config" / "release_catalog.json")


def inventory():
    return {
        "schema_version": 2,
        "environment_id": "unit-test",
        "targets": [
            {
                "target_id": "cluster-b",
                "target_kind": "kubernetes",
                "site_id": "site-main",
                "cluster_id": "cluster-b",
                "failure_domain": "zone-b",
                "context": "context-b",
                "namespace": "automation-system",
                "write_owner": "helm",
                "execution_mode": "direct",
                "wave": 2,
                "capabilities": ["persistent-storage", "network-policy", "soft-rt"],
            },
            {
                "target_id": "cluster-a",
                "target_kind": "kubernetes",
                "site_id": "site-main",
                "cluster_id": "cluster-a",
                "failure_domain": "zone-a",
                "context": "context-a",
                "namespace": "automation-system",
                "write_owner": "helm",
                "execution_mode": "direct",
                "wave": 1,
                "capabilities": ["persistent-storage", "network-policy", "soft-rt"],
            },
        ],
    }


def _image_values():
    return {"image": {"repository": "registry.example.invalid/automation/test",
                      "digest": IMAGE_DIGEST, "pullPolicy": "IfNotPresent"}}


def desired_state(all_components=True):
    components = {
        "postgresql": {
            "enabled": True,
            "values": {"mode": "external", "external": {
                "host": "postgresql.example.invalid", "port": 5432,
                "database": "automation_platform"}}},
        "mqtt": {"enabled": all_components, "values": _image_values()},
        "master": {"enabled": all_components, "values": _image_values()},
        "proxy": {"enabled": all_components, "values": _image_values()},
        "mcp-controller": {"enabled": all_components, "values": _image_values()},
        "automation-fleet": {"enabled": all_components, "values": _image_values()},
        "observability": {"enabled": all_components, "values": _image_values()},
        "automation-system": {
            "enabled": all_components,
            "values": {
                "image": {"repository": "registry.example.invalid/automation/system",
                          "digest": IMAGE_DIGEST, "pullPolicy": "IfNotPresent"},
                "runtimeArchiveDigest": RUNTIME_DIGEST,
                "runtimeProfile": "container_simulation",
            },
        },
    }
    return {
        "schema_version": 1,
        "deployment_id": "unit-test-deployment",
        "release_ref": "automation-control-plane-v0028-r1",
        "placement": {"target_ids": ["cluster-b", "cluster-a"]},
        "components": components,
        "rollout": {"strategy": "waves", "max_parallel": 1,
                    "stop_on_failure": True, "approval_per_wave": True,
                    "canary_per_failure_domain": True},
    }


def policy():
    return {
        "schema_version": 1,
        "require_approval": True,
        "require_distinct_approver": True,
        "allow_database_automatic_rollback": False,
        "allow_kubernetes_hard_rt": False,
        "allowed_write_owners": ["helm", "gitops_argocd", "gitops_flux"],
        "max_agent_task_wait_s": 1800,
        "field_manager": "unit-test",
    }


def clone(value):
    return copy.deepcopy(value)

# -*- coding: utf-8 -*-

"""Readiness separates Master-global config from node-local Registry state."""

from src.runtime_state_probe import evaluate_runtime_state


def _master_global_payload(scopes, **kwargs):
    return {
        "ok": kwargs.get("ok", True),
        "runtime": {
            "ready": kwargs.get("ready", True),
            "config_ready": kwargs.get("config_ready", True),
            "authority_mode": "master_global",
            "heap": {
                "global_revision": kwargs.get("revision", 1),
                "global_fingerprint": kwargs.get("fingerprint", "a" * 64),
                "scopes": scopes,
            },
        },
    }


def _node_local_payload(mirror_scopes, **kwargs):
    role = kwargs.get("role", "leader")
    bootstrap_defaults = {
        "leader": ("ready", "registry_ready"),
        "standby": ("registry_standby", "standby"),
    }.get(role, ("ready", "registry_ready"))
    return {
        "ok": kwargs.get("ok", True),
        "runtime": {
            "ready": kwargs.get("ready", True),
            "config_ready": kwargs.get("config_ready", True),
            "authority_mode": "node_local",
            "config_authority": kwargs.get("config_authority", "worker"),
            "master_automation_execution": kwargs.get("execution", False),
            "bootstrap": {
                "state": kwargs.get("bootstrap_state", bootstrap_defaults[0]),
                "last_status": kwargs.get(
                    "bootstrap_status", bootstrap_defaults[1]),
            },
            "registry": {
                "role": role,
                "assigns_worker_revision": kwargs.get("assigns_revision", False),
                "static_topology_fingerprint": kwargs.get(
                    "topology_fingerprint", "b" * 64),
                "mirror_count": kwargs.get("mirror_count", len(mirror_scopes)),
                "mirror_scopes": mirror_scopes,
            },
            "heap": {
                "global_revision": 0,
                "global_fingerprint": None,
                "scopes": (),
            },
        },
    }


def main():
    empty_errors = evaluate_runtime_state(
        _master_global_payload(
            (), config_ready=False, revision=0, fingerprint=None),
        expected_node_scopes=("node:fn-01",),
    )
    assert empty_errors
    node_only = _master_global_payload(("node:fn-01", "node:fn-02"))
    assert not evaluate_runtime_state(
        node_only,
        expected_node_scopes=("node:fn-01", "node:fn-02"),
    )
    cross_errors = evaluate_runtime_state(
        node_only,
        expected_node_scopes=("node:fn-01",),
        expected_cross_scopes=("cross:fn-01.default",),
    )
    assert cross_errors == (
        "missing explicitly required Cross scopes: cross:fn-01.default",
    )

    empty_registry = _node_local_payload(())
    assert not evaluate_runtime_state(empty_registry)
    assert not evaluate_runtime_state(_node_local_payload((), role="standby"))
    mirrored_registry = _node_local_payload(("node:fn-01", "node:fn-02"))
    assert not evaluate_runtime_state(
        mirrored_registry,
        expected_node_scopes=("node:fn-01", "node:fn-02"),
    )
    missing_mirror_errors = evaluate_runtime_state(
        empty_registry,
        expected_node_scopes=("node:fn-01",),
    )
    assert missing_mirror_errors == ("missing Node mirrors: node:fn-01",)
    assert evaluate_runtime_state(
        _node_local_payload((), assigns_revision=True)) == (
            "Registry may assign Worker revisions in node_local mode",
        )
    assert evaluate_runtime_state(
        _node_local_payload((), execution=True)) == (
            "Master automation execution is not disabled",
        )
    assert evaluate_runtime_state(
        _node_local_payload((), topology_fingerprint=None)) == (
            "Registry static topology fingerprint is missing",
        )
    assert evaluate_runtime_state(
        _node_local_payload((), bootstrap_state="registry_standby")) == (
            "node_local Registry bootstrap is not ready for its role",
        )
    assert evaluate_runtime_state(
        _node_local_payload((), mirror_count=1)) == (
            "Registry mirror count does not match its scope inventory",
        )
    print("PASS authority-mode-sensitive Master semantic readiness")


if __name__ == "__main__":
    main()

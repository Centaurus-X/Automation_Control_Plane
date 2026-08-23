#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Validate release structure, configuration and MQTT-only boundaries."""

import ast
import json
import os
import sys


REQUIRED_FILES = (
    "README.md", "VERSION", "RELEASE_REVISION", "Dockerfile", "compose.yaml",
    "pyproject.toml", "requirements.txt", "test_all.sh",
    "config/controller_config.json", "docs/ARCHITECTURE_ANALYSIS_DE.md",
    "docs/MCP_INTERFACE_DE.md", "docs/MQTT_CONTRACT_DE.md",
    "docs/DEPLOYMENT_DE.md", "docs/SECURITY_DE.md",
    "docs/OPERATIONS_DE.md", "docs/TESTING_DE.md",
    "control_plane/component_registry.fragment.json",
)
FORBIDDEN_DATABASE_ROOTS = {
    "psycopg", "psycopg2", "sqlalchemy", "asyncpg", "mysql", "pymysql",
}


def _all_files(root):
    result = []
    for directory, names, files in os.walk(root):
        names[:] = [
            name for name in names
            if name not in (".git", ".test-tmp", "runtime-secrets")
        ]
        for name in files:
            result.append(os.path.join(directory, name))
    return sorted(result)


def _check_python_boundary(path, relative, issues):
    with open(path, "r", encoding="utf-8-sig") as handle:
        tree = ast.parse(handle.read(), filename=relative)
    for node in ast.walk(tree):
        roots = set()
        if isinstance(node, ast.Import):
            roots = {item.name.split(".", 1)[0] for item in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            roots = {node.module.split(".", 1)[0]}
        forbidden = sorted(roots.intersection(FORBIDDEN_DATABASE_ROOTS))
        if forbidden:
            issues.append(relative + " imports platform DB client " + forbidden[0])


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    issues = []
    for transient in ("build", "dist"):
        if os.path.lexists(os.path.join(root, transient)):
            issues.append("generated build directory is present: " + transient)
    for directory, names, _files in os.walk(root):
        for name in names:
            if name.endswith(".egg-info"):
                relative = os.path.relpath(os.path.join(directory, name), root)
                issues.append("generated package metadata is present: " + relative)
    for relative in REQUIRED_FILES:
        if not os.path.isfile(os.path.join(root, relative)):
            issues.append("missing required file: " + relative)
    for path in _all_files(root):
        relative = os.path.relpath(path, root).replace(os.sep, "/")
        if os.path.islink(path):
            issues.append("symbolic links are forbidden: " + relative)
        if relative.endswith(".py"):
            _check_python_boundary(path, relative, issues)
        if relative.endswith(".json"):
            try:
                with open(path, "r", encoding="utf-8") as handle:
                    json.load(handle)
            except (OSError, json.JSONDecodeError) as exc:
                issues.append("invalid JSON {}: {}".format(relative, exc))
        if relative.startswith("secrets/") and relative != "secrets/README.md":
            issues.append("secret material is present in release: " + relative)
        if relative.endswith((".pyc", ".pyo")) or "__pycache__" in relative:
            issues.append("compiled Python artifact is present: " + relative)
    config_path = os.path.join(root, "config", "controller_config.json")
    if os.path.isfile(config_path):
        with open(config_path, "r", encoding="utf-8") as handle:
            config = json.load(handle)
        mqtt = config.get("mqtt") or {}
        policy = config.get("policy") or {}
        if mqtt.get("source_service") != "proxy":
            issues.append("MQTT source service must use the Master proxy boundary")
        if not (mqtt.get("tls") or {}).get("enabled"):
            issues.append("MQTT mTLS is not enabled by default")
        if mqtt.get("allow_insecure_transport"):
            issues.append("insecure MQTT is enabled by default")
        if policy.get("approval_mode") != "external":
            issues.append("external mutation approval is not the default")
        if "terminal.execute" in set(policy.get("mutation_actions") or ()):
            issues.append("terminal execution is exposed as a mutation")
    requirements = os.path.join(root, "requirements.txt")
    if os.path.isfile(requirements):
        with open(requirements, "r", encoding="utf-8") as handle:
            for line in handle:
                value = line.strip()
                if value and not value.startswith("#") and "==" not in value:
                    issues.append("dependency is not exactly pinned: " + value)
    if issues:
        for issue in issues:
            print("FAILED " + issue, file=sys.stderr)
        return 1
    print("PASS release structure and MQTT-only platform boundary")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

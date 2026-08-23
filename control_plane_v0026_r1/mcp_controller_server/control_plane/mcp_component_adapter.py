#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Credential-free lifecycle adapter for future generic Control Plane registries."""

import argparse
import json
import os
import re
import subprocess
import tempfile


ACTIONS = {
    "preflight": "preflight.sh",
    "backup": "backup.sh",
    "apply": "deploy.sh",
    "ready": "ready.sh",
    "status": "status.sh",
    "test": "test.sh",
    "restart": "restart.sh",
    "rollback": "rollback.sh",
}
MUTATING_ACTIONS = {"backup", "apply", "restart", "rollback"}
IDENTIFIER_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,255}$")


def _arguments():
    parser = argparse.ArgumentParser(description="MCP component lifecycle adapter")
    parser.add_argument("--release-root", required=True)
    parser.add_argument("--state-dir", required=True)
    parser.add_argument("--action", required=True, choices=sorted(ACTIONS))
    parser.add_argument("--component", required=True)
    parser.add_argument("--target-id", required=True)
    parser.add_argument("--plan-id", default="manual")
    parser.add_argument("--step-id", default="manual")
    parser.add_argument("--idempotency-key")
    parser.add_argument("--fencing-token", type=int, default=0)
    return parser.parse_args()


def _atomic_json(path, value):
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    descriptor, temporary = tempfile.mkstemp(prefix=".mcp-adapter-", dir=directory)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            json.dump(value, handle, sort_keys=True, separators=(",", ":"))
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _read_state(path):
    if not os.path.isfile(path) or os.path.islink(path):
        return {}
    with open(path, "r", encoding="utf-8") as handle:
        value = json.load(handle)
    return value if isinstance(value, dict) else {}


def _claim(arguments, state_path):
    current = _read_state(state_path)
    if arguments.fencing_token < int(current.get("fencing_token") or 0):
        raise RuntimeError("stale adapter fencing token")
    completed = set(current.get("completed_keys") or ())
    if arguments.idempotency_key in completed:
        return False
    _atomic_json(state_path, {
        "fencing_token": arguments.fencing_token,
        "idempotency_key": arguments.idempotency_key,
        "plan_id": arguments.plan_id,
        "step_id": arguments.step_id,
        "action": arguments.action,
        "status": "running",
        "completed_keys": sorted(completed),
    })
    return True


def _finish(arguments, state_path):
    current = _read_state(state_path)
    completed = set(current.get("completed_keys") or ())
    completed.add(arguments.idempotency_key)
    current["completed_keys"] = sorted(completed)
    current["status"] = "complete"
    _atomic_json(state_path, current)


def _component_root(release_root):
    candidates = (
        os.path.join(release_root, "mcp_controller_server"),
        release_root,
    )
    for candidate in candidates:
        if os.path.isfile(os.path.join(candidate, "VERSION")) \
                and os.path.isdir(os.path.join(candidate, "scripts")):
            return os.path.abspath(candidate)
    raise RuntimeError("mcp_controller_server component root is missing")


def main():
    arguments = _arguments()
    if arguments.component != "mcp_controller_server":
        raise RuntimeError("adapter component mismatch")
    if arguments.action in MUTATING_ACTIONS and not arguments.idempotency_key:
        raise RuntimeError("mutating lifecycle action requires an idempotency key")
    for value in (
            arguments.target_id, arguments.plan_id, arguments.step_id,
            arguments.idempotency_key or "read-only"):
        if not IDENTIFIER_PATTERN.fullmatch(value):
            raise RuntimeError("unsafe lifecycle identifier")
    component_root = _component_root(arguments.release_root)
    script = os.path.join(component_root, "scripts", ACTIONS[arguments.action])
    if not os.path.isfile(script) or os.path.islink(script):
        raise RuntimeError("lifecycle script is missing or unsafe")
    safe_target = re.sub(r"[^A-Za-z0-9_.-]", "_", arguments.target_id)
    state_path = os.path.join(
        os.path.abspath(arguments.state_dir), "adapter-state",
        safe_target, "mcp_controller_server.json",
    )
    claimed = True
    if arguments.action in MUTATING_ACTIONS:
        claimed = _claim(arguments, state_path)
    if not claimed:
        print("adapter idempotency replay: already complete")
        return 0
    completed = subprocess.run(
        [script], cwd=component_root, check=False,
    )
    if completed.returncode != 0:
        raise RuntimeError("lifecycle action failed")
    if arguments.action in MUTATING_ACTIONS:
        _finish(arguments, state_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

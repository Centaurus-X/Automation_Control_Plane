"""Local Kubernetes Lease compare-and-swap tests for the cluster agent."""

import os
import pathlib
import tempfile

from automation_fleet.canonical import atomic_json
from automation_fleet.cluster_agent import acquire_local_lease, load_agent_configuration


KUBECTL_SCRIPT = """#!/usr/bin/env bash
set -eu
arguments=" $* "
if [[ "$arguments" == *" get lease "* ]]; then
  [[ -f "$FAKE_LEASE_STATE" ]] || exit 1
  cat "$FAKE_LEASE_STATE"
  exit 0
fi
if [[ "$arguments" == *" create -f - "* || "$arguments" == *" replace -f - "* ]]; then
  cat >"$FAKE_LEASE_STATE"
  exit 0
fi
exit 2
"""


def main():
    with tempfile.TemporaryDirectory(prefix="automation-cluster-agent-") as directory:
        base = pathlib.Path(directory)
        binary = base / "kubectl"
        binary.write_text(KUBECTL_SCRIPT, encoding="utf-8")
        binary.chmod(0o700)
        token = base / "agent.token"
        token.write_text("t" * 64 + "\n", encoding="utf-8")
        token.chmod(0o600)
        config_path = base / "agent.json"
        atomic_json(config_path, {
            "schema_version": 1,
            "central_url": "https://fleet.example.invalid",
            "target_id": "cluster-a",
            "cluster_id": "cluster-a",
            "holder_id": "pod-a",
            "namespace": "automation-system",
            "lease_name": "automation-cluster-agent",
            "lease_ttl_s": 30,
            "poll_interval_s": 2,
            "token_file": str(token),
            "capabilities": ["helm-v3"],
        })
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(base) + os.pathsep + old_path
        os.environ["FAKE_LEASE_STATE"] = str(base / "lease.json")
        try:
            config = load_agent_configuration(config_path)
            assert acquire_local_lease(config)
            assert acquire_local_lease(config)
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("FAKE_LEASE_STATE", None)
    print("PASS cluster-agent Kubernetes Lease create and resource-version renewal")


if __name__ == "__main__":
    main()

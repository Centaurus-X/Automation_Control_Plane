"""Typed Flux repository handoff, reconciliation and rollback tests."""

import os
import pathlib
import subprocess
import tempfile

from automation_fleet.planner import build_plan
from automation_fleet.provider_gitops import execute_operation, rollback
from automation_fleet.executor import execute_plan
from automation_fleet import journal
from common import catalog, desired_state, inventory, policy, release_root


HELM_SCRIPT = """#!/usr/bin/env bash
set -eu
arguments=" $* "
if [[ "$arguments" == *" version "* ]]; then echo 'v3.test'; exit 0; fi
if [[ "$arguments" == *" lint "* ]]; then echo 'lint ok'; exit 0; fi
if [[ "$arguments" == *" template "* ]]; then echo 'apiVersion: v1'; echo 'kind: Service'; echo 'metadata: {name: automation-postgresql}'; exit 0; fi
exit 2
"""


KUBECTL_SCRIPT = """#!/usr/bin/env bash
set -eu
arguments=" $* "
if [[ "$arguments" == *" version "* ]]; then echo '{"clientVersion":{"gitVersion":"test"}}'; exit 0; fi
if [[ "$arguments" == *" get serviceaccount default "* ]]; then echo 'serviceaccount/default'; exit 0; fi
if [[ "$arguments" == *" auth can-i "* ]]; then echo 'yes'; exit 0; fi
if [[ "$arguments" == *" get kustomizations.kustomize.toolkit.fluxcd.io "* ]]; then
  revision="$(git -C "$FAKE_GIT_WORKTREE" rev-parse HEAD)"
  printf '{"metadata":{"generation":2},"status":{"observedGeneration":2,"lastAppliedRevision":"main@sha1:%s","conditions":[{"type":"Ready","status":"True"}]}}\n' "$revision"
  exit 0
fi
if [[ "$arguments" == *" get deployments,statefulsets,daemonsets,pods,jobs,persistentvolumeclaims,services "* ]]; then
  echo '{"items":[{"kind":"Service","metadata":{"name":"automation-postgresql"}}]}'
  exit 0
fi
exit 2
"""


def _write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def _run_git(worktree, *arguments):
    result = subprocess.run(
        ["git", "-C", str(worktree)] + list(arguments),
        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr)


def _gitops_plan(worktree):
    inventory_value = inventory()
    inventory_value["targets"] = [inventory_value["targets"][0]]
    target = inventory_value["targets"][0]
    target["write_owner"] = "gitops_flux"
    target["gitops"] = {
        "worktree": str(worktree),
        "relative_path": "clusters/cluster-b/automation-system",
        "branch": "main",
        "remote": "origin",
        "push": False,
        "reconciler_name": "automation-system",
        "reconciler_namespace": "flux-system",
    }
    desired = desired_state(all_components=False)
    desired["placement"]["target_ids"] = ["cluster-b"]
    return build_plan(
        release_root(), inventory_value, catalog(), desired, policy())


def _step(plan, operation):
    for step in plan["steps"]:
        if step["operation"] == operation:
            return step
    raise AssertionError("missing operation " + operation)


def main():
    with tempfile.TemporaryDirectory(prefix="automation-gitops-") as directory:
        base = pathlib.Path(directory)
        binaries = base / "bin"
        worktree = base / "repo"
        binaries.mkdir()
        worktree.mkdir()
        _write_executable(binaries / "helm", HELM_SCRIPT)
        _write_executable(binaries / "kubectl", KUBECTL_SCRIPT)
        _run_git(worktree, "init", "--initial-branch=main")
        _run_git(worktree, "config", "user.name", "Automation Fleet Test")
        _run_git(worktree, "config", "user.email", "fleet@example.invalid")
        (worktree / "README.md").write_text("test\n", encoding="utf-8")
        _run_git(worktree, "add", "README.md")
        _run_git(worktree, "commit", "-m", "initial")
        _run_git(worktree, "remote", "add", "origin", str(worktree))
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(binaries) + os.pathsep + old_path
        os.environ["FAKE_GIT_WORKTREE"] = str(worktree)
        try:
            plan = _gitops_plan(worktree)
            assert plan["provider"] == "gitops_flux"
            assert execute_operation(release_root(), _step(plan, "discover"))["status"] == "SUCCEEDED"
            assert execute_operation(release_root(), _step(plan, "validate_target"))["status"] == "SUCCEEDED"
            assert execute_operation(release_root(), _step(plan, "diff"))["changed"]
            assert execute_operation(release_root(), _step(plan, "preflight"))["status"] == "SUCCEEDED"
            applied = execute_operation(release_root(), _step(plan, "apply"))
            assert not applied["idempotent"]
            assert execute_operation(release_root(), _step(plan, "wait_ready"))["status"] == "SUCCEEDED"
            assert execute_operation(release_root(), _step(plan, "verify"))["status"] == "SUCCEEDED"
            rolled_back = rollback(_step(plan, "apply"), applied["observed_commit"])
            assert rolled_back["status"] == "ROLLED_BACK"
            state_dir = base / "state"
            database = journal.connect(state_dir)
            try:
                journal.register_plan(database, plan, creator="test:operator")
                journal.approve_plan(database, plan["plan_id"], "test:approver")
            finally:
                database.close()
            execution = execute_plan(
                release_root(), state_dir, plan, allow_mutations=True)
            assert execution["status"] == "SUCCEEDED"
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("FAKE_GIT_WORKTREE", None)
    print("PASS Flux typed Git handoff, reconciler health, single-writer and rollback")


if __name__ == "__main__":
    main()

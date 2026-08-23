"""Provider contract tests with typed fake kubectl and Helm executables."""

import os
import pathlib
import tempfile

from automation_fleet.planner import build_plan
from automation_fleet.provider_kubernetes import execute_operation, rollback
from common import catalog, desired_state, inventory, policy, release_root


HELM_SCRIPT = """#!/usr/bin/env bash
set -eu
arguments=" $* "
if [[ "$arguments" == *" version "* ]]; then echo 'v3.test'; exit 0; fi
if [[ "$arguments" == *" list "* ]]; then echo '[]'; exit 0; fi
if [[ "$arguments" == *" lint "* ]]; then echo 'lint ok'; exit 0; fi
if [[ "$arguments" == *" template "* ]]; then echo 'apiVersion: v1'; echo 'kind: Service'; exit 0; fi
if [[ "$arguments" == *" history "* ]]; then
  if [[ -f "$FAKE_HELM_STATE" ]]; then echo '[{"revision":1}]'; exit 0; fi
  exit 1
fi
if [[ "$arguments" == *" upgrade "* ]]; then touch "$FAKE_HELM_STATE"; echo 'upgrade ok'; exit 0; fi
if [[ "$arguments" == *" rollback "* ]]; then echo 'rollback ok'; exit 0; fi
echo "unexpected helm invocation: $*" >&2
exit 2
"""


KUBECTL_SCRIPT = """#!/usr/bin/env bash
set -eu
arguments=" $* "
if [[ "$arguments" == *" version "* ]]; then echo '{"clientVersion":{"gitVersion":"test"}}'; exit 0; fi
if [[ "$arguments" == *" get namespace "* ]]; then echo '{"kind":"Namespace"}'; exit 0; fi
if [[ "$arguments" == *" get serviceaccount default "* ]]; then echo 'serviceaccount/default'; exit 0; fi
if [[ "$arguments" == *" auth can-i "* ]]; then echo 'yes'; exit 0; fi
if [[ "$arguments" == *" diff "* ]]; then cat >/dev/null; echo 'changed'; exit 1; fi
if [[ "$arguments" == *" get deployments,statefulsets "* ]]; then
  echo '{"items":[{"kind":"Deployment","metadata":{"name":"automation-test"}}]}'
  exit 0
fi
if [[ "$arguments" == *" rollout status "* ]]; then echo 'rollout complete'; exit 0; fi
if [[ "$arguments" == *" get deployments,statefulsets,daemonsets,pods,jobs,persistentvolumeclaims,services "* ]]; then
  echo '{"items":[{"kind":"Pod","metadata":{"name":"automation-test-pod","labels":{}},"status":{"conditions":[{"type":"Ready","status":"True"}]}},{"kind":"Deployment","metadata":{"name":"automation-test","labels":{}},"status":{"replicas":1,"readyReplicas":1,"availableReplicas":1}}]}'
  exit 0
fi
echo "unexpected kubectl invocation: $*" >&2
exit 2
"""


def _write_executable(path, text):
    path.write_text(text, encoding="utf-8")
    path.chmod(0o700)


def _step(plan, operation):
    step = dict(plan["steps"][0])
    step["operation"] = operation
    return step


def main():
    plan = build_plan(release_root(), inventory(), catalog(),
                      desired_state(all_components=False), policy())
    with tempfile.TemporaryDirectory(prefix="automation-fleet-provider-") as directory:
        root = pathlib.Path(directory)
        _write_executable(root / "helm", HELM_SCRIPT)
        _write_executable(root / "kubectl", KUBECTL_SCRIPT)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(root) + os.pathsep + old_path
        os.environ["FAKE_HELM_STATE"] = str(root / "helm.state")
        try:
            assert execute_operation(release_root(), _step(plan, "discover"))["status"] == "SUCCEEDED"
            assert execute_operation(release_root(), _step(plan, "validate_target"))["status"] == "SUCCEEDED"
            assert execute_operation(release_root(), _step(plan, "preflight"))["status"] == "SUCCEEDED"
            assert execute_operation(release_root(), _step(plan, "render"))["status"] == "SUCCEEDED"
            assert execute_operation(release_root(), _step(plan, "diff"))["changed"]
            assert execute_operation(release_root(), _step(plan, "backup"))["status"] == "SUCCEEDED"
            apply_result = execute_operation(release_root(), _step(plan, "apply"))
            assert apply_result["observed_revision"] == 1
            assert execute_operation(release_root(), _step(plan, "wait_ready"))["status"] == "SUCCEEDED"
            assert execute_operation(release_root(), _step(plan, "verify"))["ready_pods"] == 1
            assert rollback(_step(plan, "apply"), 1)["status"] == "ROLLED_BACK"
            drifted = _step(plan, "preflight")
            drifted["descriptor_digest"] = "sha256:" + ("2" * 64)
            try:
                execute_operation(release_root(), drifted)
            except ValueError as exc:
                assert "descriptor_drift" in str(exc)
            else:
                raise AssertionError("descriptor drift must fail closed")
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("FAKE_HELM_STATE", None)
    print("PASS Kubernetes/Helm provider contract and rollback")


if __name__ == "__main__":
    main()

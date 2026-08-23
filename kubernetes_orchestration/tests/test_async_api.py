"""Asynchronous Web API rollout and live status test."""

import os
import pathlib
import tempfile
import time

from automation_fleet.api import create_application
from automation_fleet.canonical import atomic_json
from common import catalog, desired_state, inventory, policy, release_root
from test_api import _header, _invoke


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
if [[ "$arguments" == *" get deployments,statefulsets "* ]]; then echo '{"items":[]}'; exit 0; fi
if [[ "$arguments" == *" get deployments,statefulsets,daemonsets,pods,jobs,persistentvolumeclaims,services "* ]]; then
  echo '{"items":[{"kind":"Service","metadata":{"name":"automation-postgres","labels":{}}}]}'
  exit 0
fi
echo "unexpected kubectl invocation: $*" >&2
exit 2
"""


def _write_executable(path, content):
    path.write_text(content, encoding="utf-8")
    path.chmod(0o700)


def main():
    with tempfile.TemporaryDirectory(prefix="automation-fleet-async-") as directory:
        base = pathlib.Path(directory)
        binaries = base / "bin"
        binaries.mkdir()
        _write_executable(binaries / "helm", HELM_SCRIPT)
        _write_executable(binaries / "kubectl", KUBECTL_SCRIPT)
        admin_token = "b" * 64
        operator_token = "o" * 64
        approver_token = "p" * 64
        atomic_json(base / "inventory.json", inventory())
        atomic_json(base / "catalog.json", catalog())
        atomic_json(base / "desired.json", desired_state(all_components=False))
        atomic_json(base / "policy.json", policy())
        config = {
            "inventory_path": str(base / "inventory.json"),
            "catalog_path": str(base / "catalog.json"),
            "desired_state_path": str(base / "desired.json"),
            "policy_path": str(base / "policy.json"),
            "state_dir": str(base / "state"),
            "allow_mutations": True,
            "cookie_secure": False,
            "observe_timeout_s": 1,
        }
        context = {
            "root": str(release_root()),
            "config": config,
            "tokens": {"admin": admin_token, "operator": operator_token,
                       "approver": approver_token},
            "agents": {},
            "debug": False,
            "workers": [],
        }
        application = create_application(context)
        old_path = os.environ.get("PATH", "")
        os.environ["PATH"] = str(binaries) + os.pathsep + old_path
        os.environ["FAKE_HELM_STATE"] = str(base / "helm.state")
        try:
            login = _invoke(
                application, "POST", "/api/v1/session", {"token": operator_token})
            cookie = _header(login, "Set-Cookie").split(";", 1)[0]
            csrf = login["body"]["csrf_token"]
            approver_login = _invoke(
                application, "POST", "/api/v1/session", {"token": approver_token})
            approver_cookie = _header(approver_login, "Set-Cookie").split(";", 1)[0]
            approver_csrf = approver_login["body"]["csrf_token"]
            created = _invoke(
                application, "POST", "/api/v1/plans",
                desired_state(all_components=False), cookie=cookie, csrf=csrf)
            plan_id = created["body"]["plan"]["plan_id"]
            _invoke(application, "POST", "/api/v1/plans/" + plan_id + "/approve",
                    {}, cookie=approver_cookie, csrf=approver_csrf)
            queued = _invoke(application, "POST", "/api/v1/plans/" + plan_id + "/apply",
                             {}, cookie=cookie, csrf=csrf)
            assert queued["status"].startswith("202")
            final_status = None
            for _ in range(100):
                observed = _invoke(application, "GET", "/api/v1/plans/" + plan_id,
                                   cookie=cookie)
                final_status = observed["body"]["status"]
                if final_status in {
                        "WAVE_APPROVAL_REQUIRED", "SUCCEEDED", "FAILED", "MANUAL_RECOVERY"}:
                    break
                time.sleep(0.05)
            assert final_status == "WAVE_APPROVAL_REQUIRED", final_status
            wave_approved = _invoke(
                application, "POST",
                "/api/v1/plans/" + plan_id + "/waves/2/approve", {},
                cookie=approver_cookie, csrf=approver_csrf)
            assert wave_approved["status"].startswith("200")
            resumed = _invoke(
                application, "POST", "/api/v1/plans/" + plan_id + "/apply",
                {}, cookie=cookie, csrf=csrf)
            assert resumed["status"].startswith("202")
            for _ in range(100):
                observed = _invoke(application, "GET", "/api/v1/plans/" + plan_id,
                                   cookie=cookie)
                final_status = observed["body"]["status"]
                if final_status in {"SUCCEEDED", "FAILED", "MANUAL_RECOVERY"}:
                    break
                time.sleep(0.05)
            assert final_status == "SUCCEEDED", final_status
            for worker in context["workers"]:
                worker.join(timeout=5)
                assert worker.exitcode == 0
        finally:
            os.environ["PATH"] = old_path
            os.environ.pop("FAKE_HELM_STATE", None)
    print("PASS asynchronous API rollout and live plan status")


if __name__ == "__main__":
    main()

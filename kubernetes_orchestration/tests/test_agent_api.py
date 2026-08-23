"""Outbound cluster-agent authentication, lease, task and result tests."""

import pathlib
import tempfile

from automation_fleet import agent_protocol, journal
from automation_fleet.api import create_application
from automation_fleet.canonical import digest_value
from automation_fleet.planner import build_plan
from common import catalog, desired_state, inventory, policy, release_root
from test_api import _invoke


def _agent_inventory():
    value = inventory()
    value["targets"] = [value["targets"][0]]
    value["targets"][0]["context"] = "in-cluster"
    value["targets"][0]["execution_mode"] = "agent"
    return value


def _agent_desired():
    value = desired_state(all_components=False)
    value["placement"]["target_ids"] = ["cluster-b"]
    return value


def main():
    with tempfile.TemporaryDirectory(prefix="automation-fleet-agent-api-") as directory:
        base = pathlib.Path(directory)
        token = "g" * 64
        context = {
            "root": str(release_root()),
            "config": {
                "state_dir": str(base / "state"),
                "allow_mutations": True,
                "cookie_secure": False,
            },
            "tokens": {"admin": "a" * 64},
            "agents": {"cluster-b": {
                "target_id": "cluster-b", "token": token, "tls_subject": None}},
            "debug": False,
        }
        plan = build_plan(
            release_root(), _agent_inventory(), catalog(), _agent_desired(), policy())
        database = journal.connect(context["config"]["state_dir"])
        try:
            journal.register_plan(database, plan, creator="test:operator")
            journal.approve_plan(database, plan["plan_id"], "test:approver")
            fencing = journal.next_fencing_token(database, "cluster-b")
            queued = journal.enqueue_agent_task(
                database, plan["plan_id"], plan["steps"][0], fencing)
        finally:
            database.close()
        application = create_application(context)
        authorization = "Bearer " + token
        lease_payload = {
            "target_id": "cluster-b", "holder_id": "pod-a", "ttl_s": 30,
            "capabilities": ["helm-v3"],
            "observation": {"target_id": "cluster-b", "status": "HEALTHY",
                            "resources": []},
        }
        lease = _invoke(
            application, "POST", "/api/v1/agents/cluster-b/lease",
            agent_protocol.envelope(token, lease_payload), authorization=authorization)
        assert lease["status"].startswith("200")
        lease_result = agent_protocol.verify_envelope(token, lease["body"])
        assert lease_result["acquired"] and lease_result["lease_epoch"] == 1
        claim_payload = {
            "target_id": "cluster-b", "holder_id": "pod-a", "lease_epoch": 1}
        claim = _invoke(
            application, "POST", "/api/v1/agents/cluster-b/tasks/claim",
            agent_protocol.envelope(token, claim_payload), authorization=authorization)
        claimed = agent_protocol.verify_envelope(token, claim["body"])["task"]
        assert claimed["task_id"] == queued["task_id"]
        task = claimed["task"]
        result = {
            "schema_version": 1,
            "plan_id": task["plan_id"],
            "step_id": task["step_id"],
            "target_id": task["target_id"],
            "idempotency_key": task["idempotency_key"],
            "fencing_token": task["fencing_token"],
            "status": "SUCCEEDED",
            "provider_result": {"status": "SUCCEEDED", "agent_test": True},
        }
        result["result_digest"] = digest_value(result)
        result_payload = {
            "target_id": "cluster-b", "holder_id": "pod-a", "lease_epoch": 1,
            "task_id": queued["task_id"], "status": "SUCCEEDED", "result": result,
        }
        accepted = _invoke(
            application, "POST",
            "/api/v1/agents/cluster-b/tasks/" + queued["task_id"] + "/result",
            agent_protocol.envelope(token, result_payload), authorization=authorization)
        assert accepted["status"].startswith("200")
        database = journal.connect(context["config"]["state_dir"])
        try:
            assert journal.agent_task_status(database, queued["task_id"])["status"] == "SUCCEEDED"
            assert journal.list_agents(database)[0]["status"] == "ONLINE"
        finally:
            database.close()
        invalid = _invoke(
            application, "POST", "/api/v1/agents/cluster-b/tasks/claim",
            {"payload": claim_payload, "signature": "sha256:" + ("0" * 64)},
            authorization=authorization)
        assert invalid["status"].startswith("400")
    print("PASS outbound cluster-agent auth, dual lease, signed task and result protocol")


if __name__ == "__main__":
    main()

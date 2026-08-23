"""End-to-end executor handoff to an outbound agent task worker."""

import pathlib
import tempfile
import threading
import time

from automation_fleet import journal
from automation_fleet.canonical import digest_value
from automation_fleet.executor import execute_plan
from automation_fleet.planner import build_plan
from common import catalog, desired_state, inventory, policy, release_root


def _plan():
    inventory_value = inventory()
    inventory_value["targets"] = [inventory_value["targets"][0]]
    target = inventory_value["targets"][0]
    target["context"] = "in-cluster"
    target["execution_mode"] = "agent"
    desired = desired_state(all_components=False)
    desired["placement"]["target_ids"] = [target["target_id"]]
    return build_plan(
        release_root(), inventory_value, catalog(), desired, policy())


def _result(task):
    value = {
        "schema_version": 1,
        "plan_id": task["plan_id"],
        "step_id": task["step_id"],
        "target_id": task["target_id"],
        "idempotency_key": task["idempotency_key"],
        "fencing_token": task["fencing_token"],
        "status": "SUCCEEDED",
        "provider_result": {"status": "SUCCEEDED", "remote": True},
    }
    value["result_digest"] = digest_value(value)
    return value


def _agent_worker(state_dir, target_id, expected_tasks, pause_after=False):
    database = journal.connect(state_dir)
    completed = 0
    try:
        while completed < expected_tasks:
            claimed = journal.claim_agent_task(database, target_id, "agent-pod", 1)
            if claimed is None:
                time.sleep(0.01)
                continue
            result = _result(claimed["task"])
            journal.finish_agent_task(
                database, target_id, "agent-pod", 1, claimed["task_id"],
                "SUCCEEDED", result)
            completed += 1
            if pause_after and completed == expected_tasks:
                journal.request_pause(database, claimed["task"]["plan_id"], "test:operator")
    finally:
        database.close()


def main():
    plan = _plan()
    with tempfile.TemporaryDirectory(prefix="automation-agent-executor-") as directory:
        state_dir = pathlib.Path(directory) / "state"
        database = journal.connect(state_dir)
        try:
            journal.register_plan(database, plan, creator="test:operator")
            journal.approve_plan(database, plan["plan_id"], "test:approver")
            lease = journal.acquire_agent_lease(
                database, plan["target_order"][0], "agent-pod", 300,
                ["helm-v3"], {"status": "HEALTHY"})
            assert lease["lease_epoch"] == 1
        finally:
            database.close()
        worker = threading.Thread(
            target=_agent_worker,
            args=(state_dir, plan["target_order"][0], 1, True),
            name="test-outbound-agent",
        )
        worker.start()
        paused = execute_plan(
            release_root(), state_dir, plan, allow_mutations=True)
        worker.join(timeout=10)
        assert not worker.is_alive()
        assert paused["status"] == "PAUSED"
        resume_worker = threading.Thread(
            target=_agent_worker,
            args=(state_dir, plan["target_order"][0], len(plan["steps"]) - 1),
            name="test-outbound-agent-resume",
        )
        resume_worker.start()
        result = execute_plan(
            release_root(), state_dir, plan, allow_mutations=True)
        resume_worker.join(timeout=10)
        assert not resume_worker.is_alive()
        assert result["status"] == "SUCCEEDED"
        assert len(result["steps"]) == len(plan["steps"])
    print("PASS executor to outbound-agent task handoff and signed result binding")


if __name__ == "__main__":
    main()

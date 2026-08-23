"""Journal idempotency, approval, fencing and audit tests."""

import tempfile

from automation_fleet import journal
from automation_fleet.planner import build_plan
from common import catalog, desired_state, inventory, policy, release_root


def main():
    plan = build_plan(release_root(), inventory(), catalog(),
                      desired_state(all_components=False), policy())
    with tempfile.TemporaryDirectory(prefix="automation-fleet-journal-") as state_dir:
        database = journal.connect(state_dir)
        try:
            journal.register_plan(database, plan, creator="test:creator")
            journal.register_plan(database, plan, creator="test:creator")
            assert not journal.is_approved(database, plan["plan_id"])
            try:
                journal.approve_plan(database, plan["plan_id"], "test:creator")
            except ValueError as exc:
                assert "four_eyes_required" in str(exc)
            else:
                raise AssertionError("plan creator must not approve the same plan")
            journal.approve_plan(database, plan["plan_id"], "test:approver")
            assert journal.is_approved(database, plan["plan_id"])
            assert journal.wave_is_approved(database, plan["plan_id"], 1)
            journal.approve_wave(database, plan["plan_id"], 2, "test:approver")
            assert journal.wave_is_approved(database, plan["plan_id"], 2)
            assert journal.load_plan(database, plan["plan_id"]) == plan
            first_token = journal.next_fencing_token(database, "cluster-a")
            second_token = journal.next_fencing_token(database, "cluster-a")
            assert (first_token, second_token) == (1, 2)
            step = plan["steps"][0]
            claim = journal.claim_step(database, plan["plan_id"], step, second_token)
            assert claim["execute"]
            journal.finish_step(database, plan["plan_id"], step["step_id"],
                                "SUCCEEDED", {"ok": True})
            resumed = journal.claim_step(database, plan["plan_id"], step, second_token)
            assert not resumed["execute"] and resumed["result"]["ok"]
            task = journal.enqueue_agent_task(
                database, plan["plan_id"], step, second_token)
            lease = journal.acquire_agent_lease(
                database, step["target_id"], "holder-a", 30,
                ["helm-v3"], {"status": "HEALTHY"})
            assert lease["acquired"] and lease["lease_epoch"] == 1
            claimed_task = journal.claim_agent_task(
                database, step["target_id"], "holder-a", 1)
            assert claimed_task["task_id"] == task["task_id"]
            journal.finish_agent_task(
                database, step["target_id"], "holder-a", 1,
                task["task_id"], "SUCCEEDED", {"ok": True})
            assert journal.agent_task_status(database, task["task_id"])["status"] == "SUCCEEDED"
            assert journal.list_agents(database)[0]["observation"]["status"] == "HEALTHY"
            second_task = journal.enqueue_agent_task(
                database, plan["plan_id"], plan["steps"][1], second_token)
            second_claim = journal.claim_agent_task(
                database, plan["steps"][1]["target_id"], "holder-a", 1)
            assert second_claim["task_id"] == second_task["task_id"]
            database.execute(
                "UPDATE agent_leases SET expires_at=0 WHERE target_id=?",
                (plan["steps"][1]["target_id"],))
            database.commit()
            renewed = journal.acquire_agent_lease(
                database, plan["steps"][1]["target_id"], "holder-a", 30,
                ["helm-v3"], {"status": "HEALTHY"})
            assert renewed["lease_epoch"] == 2
            reclaimed = journal.claim_agent_task(
                database, plan["steps"][1]["target_id"], "holder-a", 2)
            assert reclaimed["task_id"] == second_task["task_id"]
            journal.set_plan_status(database, plan["plan_id"], "RUNNING")
            journal.request_pause(database, plan["plan_id"], "test:operator")
            assert journal.plan_status_value(database, plan["plan_id"]) == "PAUSE_REQUESTED"
            rows = list(reversed(journal.list_audit(database)))
            previous = "sha256:" + ("0" * 64)
            for row in rows:
                assert row["previous_hash"] == previous
                previous = row["record_hash"]
            verification = journal.verify_audit_chain(database)
            assert verification["verified"] and verification["records"] == len(rows)
        finally:
            database.close()
    print("PASS journal approval, idempotency, fencing and audit hash chain")


if __name__ == "__main__":
    main()

"""Journaled wave execution across direct, GitOps and outbound agents."""

import fcntl
import pathlib
import time

from automation_fleet.canonical import atomic_json, digest_value
from automation_fleet import journal
from automation_fleet.providers import (
    execute_operation,
    rollback_operation,
    rollback_reference,
)
from automation_fleet.validation import validate_plan


def _verify_identity(plan):
    unsigned = dict(plan)
    observed = unsigned.pop("plan_id", None)
    expected = digest_value(unsigned)
    if observed != expected:
        raise ValueError("plan_identity: expected " + expected + ", got " + str(observed))


def _lock(state_dir):
    path = pathlib.Path(state_dir) / "executor.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = open(path, "a+", encoding="utf-8")
    try:
        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        raise RuntimeError("executor_locked: another rollout is active")
    return handle


def _evidence_path(state_dir, plan_id, step_id):
    safe_plan = plan_id.replace(":", "_")
    safe_step = step_id.replace("/", "__")
    return pathlib.Path(state_dir) / "evidence" / safe_plan / (safe_step + ".json")


def _validate_agent_result(plan, step, fencing_token, value):
    if not isinstance(value, dict):
        raise RuntimeError("agent_result: result payload is not an object")
    bindings = {
        "plan_id": plan["plan_id"],
        "step_id": step["step_id"],
        "target_id": step["target_id"],
        "idempotency_key": step["idempotency_key"],
        "fencing_token": int(fencing_token),
    }
    for key, expected in bindings.items():
        if value.get(key) != expected:
            raise RuntimeError("agent_result_binding: " + key + " does not match")
    if value.get("status") != "SUCCEEDED":
        raise RuntimeError("agent_operation_failed: " + str(value.get("error", "unknown")))
    provider_result = value.get("provider_result")
    if not isinstance(provider_result, dict):
        raise RuntimeError("agent_result: provider result is missing")
    unsigned = dict(value)
    observed = unsigned.pop("result_digest", None)
    if observed != digest_value(unsigned):
        raise RuntimeError("agent_result_digest: result digest is invalid")
    return provider_result


def _execute_agent_step(database, plan, step, fencing_token):
    queued = journal.enqueue_agent_task(database, plan["plan_id"], step, fencing_token)
    deadline = time.monotonic() + int(step.get("agent_task_timeout_s", 1200))
    while time.monotonic() < deadline:
        status = journal.agent_task_status(database, queued["task_id"])
        if status["status"] == "SUCCEEDED":
            return _validate_agent_result(plan, step, fencing_token, status["result"])
        if status["status"] == "FAILED":
            value = status.get("result") or {}
            raise RuntimeError("agent_operation_failed: " + str(value.get("error", "unknown")))
        time.sleep(0.25)
    raise TimeoutError("agent_task_timeout: outbound cluster agent did not finish the task")


def _execute_step(root, database, plan, step, fencing_token):
    if step.get("execution_mode") == "agent":
        return _execute_agent_step(database, plan, step, fencing_token)
    return execute_operation(root, step)


def _provider_state_key(step):
    return step["target_id"] + "/" + step["component_id"]


def _remember_provider_state(provider_state, step, evidence):
    if step.get("operation") != "apply" or not step.get("provider", "").startswith("gitops_"):
        return
    provider_result = evidence.get("result", evidence)
    revision = provider_result.get("observed_commit")
    if not isinstance(revision, str):
        raise RuntimeError("gitops_revision: apply did not return an observed commit")
    provider_state[_provider_state_key(step)] = revision


def _verify_provider_state(provider_state, step, provider_result):
    if (not step.get("provider", "").startswith("gitops_")
            or step.get("operation") not in {"wait_ready", "verify"}):
        return
    expected = provider_state.get(_provider_state_key(step))
    if expected is None:
        raise RuntimeError("gitops_revision: applied commit is unavailable")
    reconciliation = provider_result.get("reconciler", {})
    observed = reconciliation.get("revision")
    if not isinstance(observed, str) or expected not in observed:
        raise RuntimeError(
            "gitops_revision: reconciler has not observed the applied commit")


def _pause_if_requested(database, plan_id):
    if journal.plan_status_value(database, plan_id) != "PAUSE_REQUESTED":
        return False
    journal.set_plan_status(database, plan_id, "PAUSED")
    journal.append_audit(database, "plan_paused", {"plan_id": plan_id})
    return True


def _wave_is_ready(database, plan, wave):
    policy = plan.get("approval_policy", {})
    if not plan.get("approval_required") or not policy.get("per_wave", False):
        return True
    return journal.wave_is_approved(database, plan["plan_id"], wave)


def execute_plan(root, state_dir, plan, allow_mutations=False):
    validate_plan(plan)
    _verify_identity(plan)
    if not allow_mutations:
        raise RuntimeError("mutations_disabled: set FLEET_ALLOW_MUTATIONS=1 explicitly")
    lock_handle = _lock(state_dir)
    database = journal.connect(state_dir)
    started = time.time()
    tokens = {}
    try:
        journal.register_plan(database, plan)
        if plan.get("approval_required") and not journal.is_approved(database, plan["plan_id"]):
            raise RuntimeError("approval_required: approve the exact plan_id before apply")
        journal.set_plan_status(database, plan["plan_id"], "RUNNING")
        journal.append_audit(database, "plan_started", {"plan_id": plan["plan_id"]})
        results = []
        provider_state = {}
        active_wave = None
        for step in plan["steps"]:
            if _pause_if_requested(database, plan["plan_id"]):
                return {"status": "PAUSED", "plan_id": plan["plan_id"],
                        "steps": results}
            wave = int(step.get("wave", 0))
            if wave != active_wave:
                if not _wave_is_ready(database, plan, wave):
                    journal.set_plan_status(
                        database, plan["plan_id"], "WAVE_APPROVAL_REQUIRED")
                    journal.append_audit(database, "wave_approval_required", {
                        "plan_id": plan["plan_id"], "wave": wave})
                    return {
                        "status": "WAVE_APPROVAL_REQUIRED",
                        "plan_id": plan["plan_id"],
                        "wave": wave,
                        "steps": results,
                    }
                active_wave = wave
                journal.append_audit(database, "wave_started", {
                    "plan_id": plan["plan_id"], "wave": wave})
            target_id = step["target_id"]
            if target_id not in tokens:
                tokens[target_id] = journal.next_fencing_token(database, target_id)
            token = tokens[target_id]
            claim = journal.claim_step(database, plan["plan_id"], step, token)
            if not claim["execute"]:
                _remember_provider_state(provider_state, step, claim["result"])
                results.append({"step_id": step["step_id"], "resumed": True,
                                "result": claim["result"]})
                continue
            journal.append_audit(database, "step_started", {
                "plan_id": plan["plan_id"],
                "step_id": step["step_id"],
                "fencing_token": token,
                "attempt": claim["attempt"],
            })
            try:
                provider_result = _execute_step(
                    root, database, plan, step, token)
                _verify_provider_state(provider_state, step, provider_result)
                evidence = {
                    "schema_version": 1,
                    "plan_id": plan["plan_id"],
                    "step_id": step["step_id"],
                    "idempotency_key": step["idempotency_key"],
                    "fencing_token": token,
                    "target_id": target_id,
                    "component_id": step["component_id"],
                    "provider": step["provider"],
                    "execution_mode": step.get("execution_mode", "direct"),
                    "requested_chart_digest": step["chart_digest"],
                    "status": "SUCCEEDED",
                    "result": provider_result,
                }
                evidence["evidence_digest"] = digest_value(evidence)
                atomic_json(_evidence_path(state_dir, plan["plan_id"], step["step_id"]),
                            evidence)
                journal.finish_step(database, plan["plan_id"], step["step_id"],
                                    "SUCCEEDED", evidence)
                journal.append_audit(database, "step_succeeded", {
                    "plan_id": plan["plan_id"],
                    "step_id": step["step_id"],
                    "evidence_digest": evidence["evidence_digest"],
                })
                _remember_provider_state(provider_state, step, evidence)
                results.append({"step_id": step["step_id"], "resumed": False,
                                "result": evidence})
            except Exception as exc:
                failure = {
                    "schema_version": 1,
                    "plan_id": plan["plan_id"],
                    "step_id": step["step_id"],
                    "idempotency_key": step["idempotency_key"],
                    "fencing_token": token,
                    "status": "FAILED",
                    "error": str(exc)[:2000],
                }
                failure["evidence_digest"] = digest_value(failure)
                atomic_json(_evidence_path(state_dir, plan["plan_id"], step["step_id"]),
                            failure)
                journal.finish_step(database, plan["plan_id"], step["step_id"],
                                    "FAILED", failure)
                journal.set_plan_status(database, plan["plan_id"], "FAILED")
                journal.append_audit(database, "step_failed", {
                    "plan_id": plan["plan_id"],
                    "step_id": step["step_id"],
                    "error_digest": digest_value(str(exc)),
                })
                raise
        journal.set_plan_status(database, plan["plan_id"], "SUCCEEDED")
        journal.append_audit(database, "plan_succeeded", {
            "plan_id": plan["plan_id"],
            "duration_s": round(time.time() - started, 3),
        })
        return {"status": "SUCCEEDED", "plan_id": plan["plan_id"], "steps": results}
    finally:
        database.close()
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()


def rollback_plan(state_dir, plan, allow_mutations=False):
    validate_plan(plan)
    _verify_identity(plan)
    if not allow_mutations:
        raise RuntimeError("mutations_disabled: set FLEET_ALLOW_MUTATIONS=1 explicitly")
    lock_handle = _lock(state_dir)
    database = journal.connect(state_dir)
    try:
        results_by_step = {
            item["step_id"]: item["result"]
            for item in journal.successful_apply_results(database, plan["plan_id"])
        }
        step_map = {step["step_id"]: step for step in plan["steps"]}
        if not results_by_step:
            raise RuntimeError("rollback_unavailable: plan has no successful apply step")
        journal.set_plan_status(database, plan["plan_id"], "ROLLING_BACK")
        rolled_back = []
        for step_id, evidence in results_by_step.items():
            step = step_map.get(step_id)
            if step is None:
                raise ValueError("rollback_step: applied step is absent from plan")
            if step["component_id"] == "postgresql":
                raise RuntimeError(
                    "manual_recovery: automatic database rollback is forbidden")
            if step.get("execution_mode") == "agent":
                raise RuntimeError(
                    "manual_recovery: remote-agent rollback requires a new approved plan")
            rollback_ref = rollback_reference(step, evidence.get("result", {}))
            if rollback_ref is None:
                raise RuntimeError(
                    "manual_recovery: no safe provider rollback reference is available")
            result = rollback_operation(step, rollback_ref)
            rolled_back.append({"step_id": step_id, "result": result})
            journal.append_audit(database, "step_rolled_back", {
                "plan_id": plan["plan_id"],
                "step_id": step_id,
                "provider": step["provider"],
                "rollback_ref_digest": digest_value(rollback_ref),
            })
        journal.set_plan_status(database, plan["plan_id"], "ROLLED_BACK")
        return {"status": "ROLLED_BACK", "plan_id": plan["plan_id"],
                "steps": rolled_back}
    except Exception:
        journal.set_plan_status(database, plan["plan_id"], "MANUAL_RECOVERY")
        raise
    finally:
        database.close()
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)
        lock_handle.close()

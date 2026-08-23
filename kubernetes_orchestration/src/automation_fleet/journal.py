"""SQLite operation journal with plan binding and an audit hash chain."""

import json
import os
import pathlib
import sqlite3
import time

from automation_fleet.canonical import canonical_text, digest_value


SCHEMA = """
PRAGMA foreign_keys=ON;
PRAGMA journal_mode=WAL;
PRAGMA synchronous=FULL;
CREATE TABLE IF NOT EXISTS plans (
    plan_id TEXT PRIMARY KEY,
    plan_json TEXT NOT NULL,
    status TEXT NOT NULL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    plan_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    approved_at REAL NOT NULL,
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
);
CREATE TABLE IF NOT EXISTS plan_origins (
    plan_id TEXT PRIMARY KEY,
    principal TEXT NOT NULL,
    created_at REAL NOT NULL,
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
);
CREATE TABLE IF NOT EXISTS wave_approvals (
    plan_id TEXT NOT NULL,
    wave INTEGER NOT NULL,
    principal TEXT NOT NULL,
    approved_at REAL NOT NULL,
    PRIMARY KEY(plan_id, wave),
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
);
CREATE TABLE IF NOT EXISTS steps (
    plan_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    status TEXT NOT NULL,
    attempt INTEGER NOT NULL,
    fencing_token INTEGER,
    result_json TEXT,
    started_at REAL,
    finished_at REAL,
    PRIMARY KEY(plan_id, step_id),
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
);
CREATE TABLE IF NOT EXISTS target_fencing (
    target_id TEXT PRIMARY KEY,
    token INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS audit (
    sequence INTEGER PRIMARY KEY AUTOINCREMENT,
    previous_hash TEXT NOT NULL,
    record_hash TEXT NOT NULL,
    event_json TEXT NOT NULL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_leases (
    target_id TEXT PRIMARY KEY,
    holder_id TEXT NOT NULL,
    lease_epoch INTEGER NOT NULL,
    expires_at REAL NOT NULL,
    capabilities_json TEXT NOT NULL,
    observation_json TEXT NOT NULL DEFAULT '{}',
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS agent_tasks (
    task_id TEXT PRIMARY KEY,
    plan_id TEXT NOT NULL,
    step_id TEXT NOT NULL,
    target_id TEXT NOT NULL,
    task_json TEXT NOT NULL,
    status TEXT NOT NULL,
    lease_epoch INTEGER,
    result_json TEXT,
    created_at REAL NOT NULL,
    claimed_at REAL,
    finished_at REAL,
    UNIQUE(plan_id, step_id),
    FOREIGN KEY(plan_id) REFERENCES plans(plan_id)
);
"""


def _database_path(state_dir):
    root = pathlib.Path(state_dir)
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        os.chmod(root, 0o700)
    except OSError:
        pass
    return root / "journal.sqlite3"


def connect(state_dir):
    database = sqlite3.connect(str(_database_path(state_dir)), timeout=30)
    database.row_factory = sqlite3.Row
    database.executescript(SCHEMA)
    columns = {
        row["name"] for row in database.execute("PRAGMA table_info(agent_leases)")
    }
    if "observation_json" not in columns:
        database.execute(
            "ALTER TABLE agent_leases ADD COLUMN observation_json TEXT NOT NULL DEFAULT '{}'"
        )
        database.commit()
    return database


def register_plan(database, plan, creator=None):
    now = time.time()
    encoded = canonical_text(plan)
    row = database.execute(
        "SELECT plan_json FROM plans WHERE plan_id = ?", (plan["plan_id"],)
    ).fetchone()
    if row is not None and row["plan_json"] != encoded:
        raise ValueError("plan_collision: existing plan bytes differ")
    initial_status = "APPROVAL_REQUIRED" if plan.get("approval_required") else "READY"
    database.execute(
        "INSERT OR IGNORE INTO plans(plan_id, plan_json, status, created_at, updated_at) "
        "VALUES(?, ?, ?, ?, ?)",
        (plan["plan_id"], encoded, initial_status, now, now),
    )
    if creator:
        origin = database.execute(
            "SELECT principal FROM plan_origins WHERE plan_id=?", (plan["plan_id"],)
        ).fetchone()
        if origin is not None and origin["principal"] != creator:
            database.rollback()
            raise ValueError("plan_creator_collision: plan already has another creator")
        database.execute(
            "INSERT OR IGNORE INTO plan_origins(plan_id, principal, created_at) "
            "VALUES(?, ?, ?)",
            (plan["plan_id"], creator, now),
        )
    database.commit()
    if row is None:
        append_audit(database, "plan_registered", {"plan_id": plan["plan_id"]})


def load_plan(database, plan_id):
    row = database.execute(
        "SELECT plan_json FROM plans WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    if row is None:
        raise ValueError("plan_missing: unknown plan_id")
    return json.loads(row["plan_json"])


def approve_plan(database, plan_id, principal):
    plan = load_plan(database, plan_id)
    origin = database.execute(
        "SELECT principal FROM plan_origins WHERE plan_id=?", (plan_id,)
    ).fetchone()
    distinct = plan.get("approval_policy", {}).get("distinct_principal", False)
    if distinct and origin is not None and origin["principal"] == principal:
        raise ValueError("four_eyes_required: plan creator cannot approve the same plan")
    now = time.time()
    database.execute(
        "INSERT INTO approvals(plan_id, principal, approved_at) VALUES(?, ?, ?) "
        "ON CONFLICT(plan_id) DO UPDATE SET principal=excluded.principal, "
        "approved_at=excluded.approved_at",
        (plan_id, principal, now),
    )
    waves = plan.get("waves", [])
    if waves:
        first_wave = int(waves[0]["wave"])
        database.execute(
            "INSERT INTO wave_approvals(plan_id, wave, principal, approved_at) "
            "VALUES(?, ?, ?, ?) ON CONFLICT(plan_id, wave) DO UPDATE SET "
            "principal=excluded.principal, approved_at=excluded.approved_at",
            (plan_id, first_wave, principal, now),
        )
    database.execute(
        "UPDATE plans SET status='READY', updated_at=? WHERE plan_id=?", (now, plan_id)
    )
    database.commit()
    append_audit(database, "plan_approved", {"plan_id": plan_id, "principal": principal})


def is_approved(database, plan_id):
    row = database.execute(
        "SELECT 1 FROM approvals WHERE plan_id = ?", (plan_id,)
    ).fetchone()
    return row is not None


def approve_wave(database, plan_id, wave, principal):
    plan = load_plan(database, plan_id)
    if not is_approved(database, plan_id):
        raise ValueError("plan_approval_required: approve the exact plan before a wave")
    valid_waves = {int(item["wave"]) for item in plan.get("waves", [])}
    if int(wave) not in valid_waves:
        raise ValueError("wave_missing: plan does not contain wave " + str(wave))
    origin = database.execute(
        "SELECT principal FROM plan_origins WHERE plan_id=?", (plan_id,)
    ).fetchone()
    distinct = plan.get("approval_policy", {}).get("distinct_principal", False)
    if distinct and origin is not None and origin["principal"] == principal:
        raise ValueError("four_eyes_required: plan creator cannot approve a rollout wave")
    now = time.time()
    database.execute(
        "INSERT INTO wave_approvals(plan_id, wave, principal, approved_at) "
        "VALUES(?, ?, ?, ?) ON CONFLICT(plan_id, wave) DO UPDATE SET "
        "principal=excluded.principal, approved_at=excluded.approved_at",
        (plan_id, int(wave), principal, now),
    )
    database.execute(
        "UPDATE plans SET status='READY', updated_at=? WHERE plan_id=?",
        (now, plan_id),
    )
    database.commit()
    append_audit(database, "wave_approved", {
        "plan_id": plan_id, "wave": int(wave), "principal": principal})


def wave_is_approved(database, plan_id, wave):
    row = database.execute(
        "SELECT 1 FROM wave_approvals WHERE plan_id=? AND wave=?",
        (plan_id, int(wave)),
    ).fetchone()
    return row is not None


def set_plan_status(database, plan_id, status):
    cursor = database.execute(
        "UPDATE plans SET status=?, updated_at=? WHERE plan_id=?",
        (status, time.time(), plan_id),
    )
    if cursor.rowcount != 1:
        database.rollback()
        raise ValueError("plan_missing: unknown plan_id")
    database.commit()


def plan_status_value(database, plan_id):
    row = database.execute(
        "SELECT status FROM plans WHERE plan_id=?", (plan_id,)
    ).fetchone()
    if row is None:
        raise ValueError("plan_missing: unknown plan_id")
    return row["status"]


def request_pause(database, plan_id, principal):
    status = plan_status_value(database, plan_id)
    if status not in {"QUEUED", "RUNNING"}:
        raise ValueError("pause_state: plan is not running or queued")
    set_plan_status(database, plan_id, "PAUSE_REQUESTED")
    append_audit(database, "plan_pause_requested", {
        "plan_id": plan_id, "principal": principal})


def next_fencing_token(database, target_id):
    database.execute("BEGIN IMMEDIATE")
    row = database.execute(
        "SELECT token FROM target_fencing WHERE target_id = ?", (target_id,)
    ).fetchone()
    token = 1 if row is None else int(row["token"]) + 1
    database.execute(
        "INSERT INTO target_fencing(target_id, token) VALUES(?, ?) "
        "ON CONFLICT(target_id) DO UPDATE SET token=excluded.token",
        (target_id, token),
    )
    database.commit()
    return token


def claim_step(database, plan_id, step, fencing_token):
    try:
        database.execute("BEGIN IMMEDIATE")
        fence = database.execute(
            "SELECT token FROM target_fencing WHERE target_id=?",
            (step["target_id"],),
        ).fetchone()
        if fence is None or int(fence["token"]) != int(fencing_token):
            raise RuntimeError("stale_fencing_token: target fencing token is not current")
        row = database.execute(
            "SELECT * FROM steps WHERE plan_id=? AND step_id=?",
            (plan_id, step["step_id"]),
        ).fetchone()
        if row is not None:
            if row["idempotency_key"] != step["idempotency_key"]:
                raise ValueError("idempotency_collision: step identity changed")
            if row["status"] == "SUCCEEDED":
                database.commit()
                return {"execute": False, "result": json.loads(row["result_json"])}
            attempt = int(row["attempt"]) + 1
        else:
            attempt = 1
        database.execute(
            "INSERT INTO steps(plan_id, step_id, idempotency_key, status, attempt, "
            "fencing_token, started_at) VALUES(?, ?, ?, 'RUNNING', ?, ?, ?) "
            "ON CONFLICT(plan_id, step_id) DO UPDATE SET status='RUNNING', "
            "attempt=excluded.attempt, fencing_token=excluded.fencing_token, "
            "started_at=excluded.started_at, finished_at=NULL",
            (plan_id, step["step_id"], step["idempotency_key"], attempt,
             fencing_token, time.time()),
        )
        database.commit()
        return {"execute": True, "attempt": attempt}
    except Exception:
        database.rollback()
        raise


def finish_step(database, plan_id, step_id, status, result):
    cursor = database.execute(
        "UPDATE steps SET status=?, result_json=?, finished_at=? "
        "WHERE plan_id=? AND step_id=?",
        (status, canonical_text(result), time.time(), plan_id, step_id),
    )
    if cursor.rowcount != 1:
        database.rollback()
        raise ValueError("step_missing: step was not claimed")
    database.commit()


def successful_apply_results(database, plan_id):
    rows = database.execute(
        "SELECT s.step_id, s.result_json FROM steps s "
        "WHERE s.plan_id=? AND s.status='SUCCEEDED' AND s.step_id LIKE '%/apply' "
        "ORDER BY s.rowid DESC",
        (plan_id,),
    ).fetchall()
    return [
        {"step_id": row["step_id"], "result": json.loads(row["result_json"])}
        for row in rows
    ]


def enqueue_agent_task(database, plan_id, step, fencing_token):
    task = {
        "schema_version": 1,
        "plan_id": plan_id,
        "step_id": step["step_id"],
        "target_id": step["target_id"],
        "idempotency_key": step["idempotency_key"],
        "fencing_token": int(fencing_token),
        "step": step,
    }
    task_id = digest_value({
        "plan_id": plan_id,
        "step_id": step["step_id"],
        "fencing_token": int(fencing_token),
    })
    encoded = canonical_text(task)
    row = database.execute(
        "SELECT task_json, status, result_json FROM agent_tasks WHERE task_id=?",
        (task_id,),
    ).fetchone()
    if row is not None:
        if row["task_json"] != encoded:
            raise ValueError("agent_task_collision: existing task bytes differ")
        return {
            "task_id": task_id,
            "status": row["status"],
            "result": json.loads(row["result_json"]) if row["result_json"] else None,
        }
    database.execute(
        "INSERT INTO agent_tasks(task_id, plan_id, step_id, target_id, task_json, "
        "status, created_at) VALUES(?, ?, ?, ?, ?, 'QUEUED', ?)",
        (task_id, plan_id, step["step_id"], step["target_id"], encoded, time.time()),
    )
    database.commit()
    append_audit(database, "agent_task_queued", {
        "task_id": task_id, "plan_id": plan_id, "step_id": step["step_id"],
        "target_id": step["target_id"]})
    return {"task_id": task_id, "status": "QUEUED", "result": None}


def acquire_agent_lease(database, target_id, holder_id, ttl_s, capabilities,
                        observation=None):
    now = time.time()
    expires = now + int(ttl_s)
    database.execute("BEGIN IMMEDIATE")
    try:
        row = database.execute(
            "SELECT * FROM agent_leases WHERE target_id=?", (target_id,)
        ).fetchone()
        if row is not None and row["holder_id"] != holder_id and row["expires_at"] > now:
            database.commit()
            return {
                "acquired": False,
                "holder_id": row["holder_id"],
                "lease_epoch": int(row["lease_epoch"]),
                "expires_at": row["expires_at"],
            }
        if row is None:
            epoch = 1
        elif row["holder_id"] == holder_id and row["expires_at"] > now:
            epoch = int(row["lease_epoch"])
        else:
            epoch = int(row["lease_epoch"]) + 1
        database.execute(
            "INSERT INTO agent_leases(target_id, holder_id, lease_epoch, expires_at, "
            "capabilities_json, observation_json, updated_at) VALUES(?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(target_id) DO UPDATE SET holder_id=excluded.holder_id, "
            "lease_epoch=excluded.lease_epoch, expires_at=excluded.expires_at, "
            "capabilities_json=excluded.capabilities_json, "
            "observation_json=excluded.observation_json, updated_at=excluded.updated_at",
            (target_id, holder_id, epoch, expires, canonical_text(capabilities),
             canonical_text(observation or {}), now),
        )
        database.commit()
        return {
            "acquired": True,
            "holder_id": holder_id,
            "lease_epoch": epoch,
            "expires_at": expires,
        }
    except Exception:
        database.rollback()
        raise


def _require_agent_lease(database, target_id, holder_id, lease_epoch):
    row = database.execute(
        "SELECT * FROM agent_leases WHERE target_id=?", (target_id,)
    ).fetchone()
    if (row is None or row["holder_id"] != holder_id
            or int(row["lease_epoch"]) != int(lease_epoch)
            or float(row["expires_at"]) <= time.time()):
        raise RuntimeError("stale_agent_lease: agent lease is not current")
    return row


def claim_agent_task(database, target_id, holder_id, lease_epoch):
    database.execute("BEGIN IMMEDIATE")
    try:
        _require_agent_lease(database, target_id, holder_id, lease_epoch)
        database.execute(
            "UPDATE agent_tasks SET status='QUEUED', lease_epoch=NULL, claimed_at=NULL "
            "WHERE target_id=? AND status='CLAIMED' AND lease_epoch<>?",
            (target_id, int(lease_epoch)),
        )
        row = database.execute(
            "SELECT * FROM agent_tasks WHERE target_id=? AND status='QUEUED' "
            "ORDER BY created_at, task_id LIMIT 1",
            (target_id,),
        ).fetchone()
        if row is None:
            database.commit()
            return None
        database.execute(
            "UPDATE agent_tasks SET status='CLAIMED', lease_epoch=?, claimed_at=? "
            "WHERE task_id=?",
            (int(lease_epoch), time.time(), row["task_id"]),
        )
        database.commit()
        return {
            "task_id": row["task_id"],
            "task": json.loads(row["task_json"]),
            "lease_epoch": int(lease_epoch),
        }
    except Exception:
        database.rollback()
        raise


def finish_agent_task(database, target_id, holder_id, lease_epoch, task_id,
                      status, result):
    if status not in {"SUCCEEDED", "FAILED"}:
        raise ValueError("agent_task_status: unsupported result status")
    database.execute("BEGIN IMMEDIATE")
    try:
        _require_agent_lease(database, target_id, holder_id, lease_epoch)
        row = database.execute(
            "SELECT status, lease_epoch FROM agent_tasks WHERE task_id=? AND target_id=?",
            (task_id, target_id),
        ).fetchone()
        if row is None:
            raise ValueError("agent_task_missing: unknown task")
        if row["status"] in {"SUCCEEDED", "FAILED"}:
            database.commit()
            return
        if row["status"] != "CLAIMED" or int(row["lease_epoch"]) != int(lease_epoch):
            raise RuntimeError("stale_agent_task: task is not claimed by this lease")
        database.execute(
            "UPDATE agent_tasks SET status=?, result_json=?, finished_at=? WHERE task_id=?",
            (status, canonical_text(result), time.time(), task_id),
        )
        database.commit()
    except Exception:
        database.rollback()
        raise
    append_audit(database, "agent_task_finished", {
        "task_id": task_id, "target_id": target_id, "status": status,
        "result_digest": digest_value(result)})


def agent_task_status(database, task_id):
    row = database.execute(
        "SELECT status, result_json FROM agent_tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise ValueError("agent_task_missing: unknown task")
    return {
        "status": row["status"],
        "result": json.loads(row["result_json"]) if row["result_json"] else None,
    }


def load_agent_task(database, task_id):
    row = database.execute(
        "SELECT task_json FROM agent_tasks WHERE task_id=?", (task_id,)
    ).fetchone()
    if row is None:
        raise ValueError("agent_task_missing: unknown task")
    return json.loads(row["task_json"])


def list_agents(database):
    rows = database.execute(
        "SELECT target_id, holder_id, lease_epoch, expires_at, capabilities_json, "
        "observation_json, updated_at FROM agent_leases ORDER BY target_id"
    ).fetchall()
    now = time.time()
    result = []
    for row in rows:
        item = dict(row)
        item["capabilities"] = json.loads(item.pop("capabilities_json"))
        item["observation"] = json.loads(item.pop("observation_json"))
        item["status"] = "ONLINE" if item["expires_at"] > now else "OFFLINE"
        result.append(item)
    return result


def append_audit(database, event_type, payload):
    created_at = time.time()
    try:
        database.execute("BEGIN IMMEDIATE")
        row = database.execute(
            "SELECT record_hash FROM audit ORDER BY sequence DESC LIMIT 1"
        ).fetchone()
        previous_hash = row["record_hash"] if row is not None else "sha256:" + ("0" * 64)
        event = {
            "event_type": event_type,
            "payload": payload,
            "previous_hash": previous_hash,
            "created_at_unix_us": int(created_at * 1000000),
        }
        record_hash = digest_value(event)
        database.execute(
            "INSERT INTO audit(previous_hash, record_hash, event_json, created_at) "
            "VALUES(?, ?, ?, ?)",
            (previous_hash, record_hash, canonical_text(event), created_at),
        )
        database.commit()
        return record_hash
    except Exception:
        database.rollback()
        raise


def verify_audit_chain(database):
    rows = database.execute(
        "SELECT sequence, previous_hash, record_hash, event_json "
        "FROM audit ORDER BY sequence"
    ).fetchall()
    expected_previous = "sha256:" + ("0" * 64)
    verified = 0
    for row in rows:
        event = json.loads(row["event_json"])
        if row["previous_hash"] != expected_previous:
            raise ValueError("audit_chain: table previous_hash mismatch")
        if event.get("previous_hash") != expected_previous:
            raise ValueError("audit_chain: event previous_hash mismatch")
        if digest_value(event) != row["record_hash"]:
            raise ValueError("audit_chain: record hash mismatch")
        expected_previous = row["record_hash"]
        verified += 1
    return {"verified": True, "records": verified, "head": expected_previous}


def list_plans(database, limit=100):
    rows = database.execute(
        "SELECT p.plan_id, p.status, p.created_at, p.updated_at, "
        "a.principal, a.approved_at, o.principal AS created_by FROM plans p "
        "LEFT JOIN approvals a ON a.plan_id=p.plan_id "
        "LEFT JOIN plan_origins o ON o.plan_id=p.plan_id "
        "ORDER BY p.created_at DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    return [dict(row) for row in rows]


def list_audit(database, limit=200):
    rows = database.execute(
        "SELECT sequence, previous_hash, record_hash, event_json, created_at "
        "FROM audit ORDER BY sequence DESC LIMIT ?",
        (int(limit),),
    ).fetchall()
    result = []
    for row in rows:
        item = dict(row)
        item["event"] = json.loads(item.pop("event_json"))
        result.append(item)
    return result


def plan_status(database, plan_id):
    plan = database.execute(
        "SELECT plan_id, status, created_at, updated_at FROM plans WHERE plan_id=?",
        (plan_id,),
    ).fetchone()
    if plan is None:
        raise ValueError("plan_missing: unknown plan_id")
    steps = database.execute(
        "SELECT step_id, status, attempt, fencing_token, started_at, finished_at "
        "FROM steps WHERE plan_id=? ORDER BY rowid",
        (plan_id,),
    ).fetchall()
    result = dict(plan)
    result["approved"] = is_approved(database, plan_id)
    origin = database.execute(
        "SELECT principal, created_at FROM plan_origins WHERE plan_id=?", (plan_id,)
    ).fetchone()
    result["origin"] = dict(origin) if origin is not None else None
    approvals = database.execute(
        "SELECT wave, principal, approved_at FROM wave_approvals WHERE plan_id=? "
        "ORDER BY wave", (plan_id,)
    ).fetchall()
    result["wave_approvals"] = [dict(row) for row in approvals]
    result["steps"] = [dict(row) for row in steps]
    return result

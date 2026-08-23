"""Dependency-free WSGI Fleet API and Web UI service."""

import json
import mimetypes
import multiprocessing
import os
import pathlib
import traceback
from functools import partial
from wsgiref.simple_server import make_server

from automation_fleet import agent_protocol, auth, journal
from automation_fleet.canonical import atomic_json, digest_value, read_json
from automation_fleet.collector import collect_snapshot, metrics_text
from automation_fleet.executor import execute_plan, rollback_plan
from automation_fleet.planner import build_plan, plan_summary
from automation_fleet.validation import validate_identifier


MAX_BODY_BYTES = 1024 * 1024


def _json_bytes(value):
    return (json.dumps(value, ensure_ascii=False, sort_keys=True) + "\n").encode("utf-8")


def _respond(start_response, status, body, content_type="application/json", headers=None):
    response_headers = [
        ("Content-Type", content_type),
        ("Content-Length", str(len(body))),
        ("X-Content-Type-Options", "nosniff"),
        ("X-Frame-Options", "DENY"),
        ("Referrer-Policy", "no-referrer"),
        ("Content-Security-Policy",
         "default-src 'self'; script-src 'self'; style-src 'self'; "
         "img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'"),
    ]
    additional_headers = headers or []
    has_cache_policy = any(
        name.lower() == "cache-control" for name, unused_value in additional_headers)
    if not has_cache_policy:
        response_headers.append(("Cache-Control", "no-store"))
    response_headers.extend(additional_headers)
    start_response(status, response_headers)
    return [body]


def _json_response(start_response, status, value, headers=None):
    return _respond(start_response, status, _json_bytes(value), headers=headers)


def _error(start_response, status, code, message):
    return _json_response(start_response, status, {"ok": False, "error": code,
                                                    "message": message})


def _read_body(environ):
    length_text = environ.get("CONTENT_LENGTH", "0") or "0"
    try:
        length = int(length_text)
    except ValueError as exc:
        raise ValueError("content_length: invalid Content-Length") from exc
    if length < 0 or length > MAX_BODY_BYTES:
        raise ValueError("body_size: request body is too large")
    data = environ["wsgi.input"].read(length)
    if not data:
        return {}
    return json.loads(data.decode("utf-8"))


def _static(context, path, start_response):
    relative = "index.html" if path == "/" else path.removeprefix("/static/")
    if ".." in relative or relative.startswith("/"):
        return _error(start_response, "404 Not Found", "not_found", "resource not found")
    target = (pathlib.Path(context["root"]) / "web" / relative).resolve()
    web_root = (pathlib.Path(context["root"]) / "web").resolve()
    if web_root not in target.parents and target != web_root:
        return _error(start_response, "404 Not Found", "not_found", "resource not found")
    if not target.is_file():
        return _error(start_response, "404 Not Found", "not_found", "resource not found")
    data = target.read_bytes()
    mime = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    return _respond(start_response, "200 OK", data, content_type=mime,
                    headers=[("Cache-Control", "public, max-age=300")])


def _principal(context, environ, permission):
    principal = auth.authenticate(environ, context["tokens"])
    return auth.require_permission(principal, permission)


def _mutation_principal(context, environ, permission):
    principal = _principal(context, environ, permission)
    auth.require_csrf(environ, principal)
    return principal


def _actor(principal):
    return "web:" + principal["subject"]


def _plan_id_from_path(path, suffix):
    prefix = "/api/v1/plans/"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    value = path[len(prefix):-len(suffix)]
    return value if value.startswith("sha256:") and len(value) == 71 else None


def _login(context, environ, start_response):
    body = _read_body(environ)
    role = auth.authenticate_token(body.get("token"), context["tokens"])
    if role is None:
        return _error(start_response, "401 Unauthorized", "invalid_credentials",
                      "authentication failed")
    session = auth.create_session(role, context["tokens"])
    headers = [("Set-Cookie", auth.session_cookie(session, context["config"]["cookie_secure"]))]
    return _json_response(start_response, "200 OK", {
        "ok": True,
        "role": role,
        "subject": session["subject"],
        "permissions": auth.permissions_for_role(role),
        "csrf_token": session["csrf"],
        "expires": session["expires"],
        "mutations_enabled": context["config"]["allow_mutations"],
    }, headers=headers)


def _logout(context, environ, start_response):
    principal = _principal(context, environ, "read")
    auth.require_csrf(environ, principal)
    headers = [("Set-Cookie", auth.expired_cookie(context["config"]["cookie_secure"]))]
    return _json_response(start_response, "200 OK", {"ok": True}, headers=headers)


def _snapshot(context):
    inventory = read_json(context["config"]["inventory_path"])
    database = journal.connect(context["config"]["state_dir"])
    try:
        agents = journal.list_agents(database)
    finally:
        database.close()
    agent_states = {item["target_id"]: item for item in agents}
    return collect_snapshot(
        inventory,
        timeout_s=int(context["config"].get("observe_timeout_s", 30)),
        agent_states=agent_states,
    )


def _build_and_store_plan(context, desired, creator):
    inventory = read_json(context["config"]["inventory_path"])
    catalog = read_json(context["config"]["catalog_path"])
    policy = read_json(context["config"]["policy_path"])
    plan = build_plan(context["root"], inventory, catalog, desired, policy)
    database = journal.connect(context["config"]["state_dir"])
    try:
        journal.register_plan(database, plan, creator=creator)
    finally:
        database.close()
    plan_path = pathlib.Path(context["config"]["state_dir"]) / "plans" / (
        plan["plan_id"].replace(":", "_") + ".json")
    atomic_json(plan_path, plan)
    return plan


def _background_operation(root, state_dir, plan, allow_mutations, operation):
    try:
        if operation == "apply":
            execute_plan(root, state_dir, plan, allow_mutations=allow_mutations)
        elif operation == "rollback":
            rollback_plan(state_dir, plan, allow_mutations=allow_mutations)
        else:
            raise ValueError("background_operation: unsupported operation")
    except Exception:
        database = journal.connect(state_dir)
        try:
            current = journal.plan_status(database, plan["plan_id"])
            if current["status"] not in {
                    "FAILED", "MANUAL_RECOVERY", "PAUSED", "WAVE_APPROVAL_REQUIRED"}:
                journal.set_plan_status(database, plan["plan_id"], "FAILED")
            journal.append_audit(database, "background_operation_failed", {
                "plan_id": plan["plan_id"],
                "operation": operation,
            })
        finally:
            database.close()


def _reap_workers(context):
    active = []
    for worker in context.setdefault("workers", []):
        if worker.is_alive():
            active.append(worker)
        else:
            worker.join(timeout=0)
    context["workers"] = active


def _launch_operation(context, plan, operation):
    if not context["config"]["allow_mutations"]:
        raise RuntimeError("mutations_disabled: set FLEET_ALLOW_MUTATIONS=1 explicitly")
    _reap_workers(context)
    database = journal.connect(context["config"]["state_dir"])
    try:
        current = journal.plan_status(database, plan["plan_id"])
        if current["status"] in {
                "QUEUED", "RUNNING", "ROLLING_BACK", "PAUSE_REQUESTED"}:
            raise RuntimeError("operation_active: plan already has an active operation")
        status = "QUEUED" if operation == "apply" else "ROLLBACK_QUEUED"
        journal.set_plan_status(database, plan["plan_id"], status)
        journal.append_audit(database, "background_operation_queued", {
            "plan_id": plan["plan_id"],
            "operation": operation,
        })
    finally:
        database.close()
    process_context = multiprocessing.get_context("spawn")
    worker = process_context.Process(
        target=_background_operation,
        args=(context["root"], context["config"]["state_dir"], plan,
              context["config"]["allow_mutations"], operation),
        name="automation-fleet-" + operation,
    )
    worker.daemon = False
    try:
        worker.start()
    except Exception:
        database = journal.connect(context["config"]["state_dir"])
        try:
            journal.set_plan_status(database, plan["plan_id"], "FAILED")
        finally:
            database.close()
        raise
    context["workers"].append(worker)
    return {"status": status, "plan_id": plan["plan_id"], "pid": worker.pid}


def _agent_target_from_path(path, suffix):
    prefix = "/api/v1/agents/"
    if not path.startswith(prefix) or not path.endswith(suffix):
        return None
    target_id = path[len(prefix):-len(suffix)]
    if not target_id or "/" in target_id:
        return None
    return target_id


def _agent_authentication(context, environ, target_id):
    principal = agent_protocol.authenticate(
        environ.get("HTTP_AUTHORIZATION", ""), context.get("agents", {}), target_id,
        tls_subject=environ.get("SSL_CLIENT_S_DN"))
    if principal is None:
        raise PermissionError("agent_authentication_required")
    return principal


def _agent_body(context, environ, target_id):
    principal = _agent_authentication(context, environ, target_id)
    body = _read_body(environ)
    payload = agent_protocol.verify_envelope(principal["token"], body)
    if payload.get("target_id") != target_id:
        raise ValueError("agent_target_binding: target_id differs from authenticated target")
    return principal, payload


def _agent_response(start_response, token, value, status="200 OK"):
    return _json_response(
        start_response, status, agent_protocol.envelope(token, value))


def _agent_lease(context, environ, start_response, target_id):
    principal, payload = _agent_body(context, environ, target_id)
    holder_id = payload.get("holder_id")
    ttl_s = payload.get("ttl_s")
    capabilities = payload.get("capabilities")
    observation = payload.get("observation", {})
    if not isinstance(holder_id, str) or not 1 <= len(holder_id) <= 128:
        raise ValueError("agent_holder: holder_id is invalid")
    if not isinstance(ttl_s, int) or not 15 <= ttl_s <= 300:
        raise ValueError("agent_lease: ttl_s must be 15..300")
    if not isinstance(capabilities, list) or len(capabilities) != len(set(capabilities)):
        raise ValueError("agent_capabilities: capabilities must be unique")
    for capability in capabilities:
        validate_identifier(capability, "agent capability")
    if not isinstance(observation, dict):
        raise ValueError("agent_observation: observation must be an object")
    database = journal.connect(context["config"]["state_dir"])
    try:
        result = journal.acquire_agent_lease(
            database, target_id, holder_id, ttl_s, capabilities, observation)
    finally:
        database.close()
    return _agent_response(start_response, principal["token"], result)


def _agent_claim(context, environ, start_response, target_id):
    principal, payload = _agent_body(context, environ, target_id)
    holder_id = payload.get("holder_id")
    lease_epoch = payload.get("lease_epoch")
    if not isinstance(holder_id, str) or not isinstance(lease_epoch, int):
        raise ValueError("agent_lease: holder_id and lease_epoch are required")
    database = journal.connect(context["config"]["state_dir"])
    try:
        task = journal.claim_agent_task(
            database, target_id, holder_id, lease_epoch)
    finally:
        database.close()
    return _agent_response(start_response, principal["token"], {"task": task})


def _agent_result(context, environ, start_response, target_id, task_id):
    principal, payload = _agent_body(context, environ, target_id)
    if payload.get("task_id") != task_id:
        raise ValueError("agent_task_binding: task_id differs from request path")
    holder_id = payload.get("holder_id")
    lease_epoch = payload.get("lease_epoch")
    status = payload.get("status")
    result = payload.get("result")
    if (not isinstance(holder_id, str) or not isinstance(lease_epoch, int)
            or status not in {"SUCCEEDED", "FAILED"} or not isinstance(result, dict)):
        raise ValueError("agent_result: result envelope is invalid")
    unsigned = dict(result)
    observed_digest = unsigned.pop("result_digest", None)
    if observed_digest != digest_value(unsigned):
        raise ValueError("agent_result_digest: signed result digest is invalid")
    if result.get("status") != status or result.get("target_id") != target_id:
        raise ValueError("agent_result_binding: result status or target differs")
    database = journal.connect(context["config"]["state_dir"])
    try:
        task = journal.load_agent_task(database, task_id)
        bindings = {
            "plan_id": task["plan_id"],
            "step_id": task["step_id"],
            "target_id": task["target_id"],
            "idempotency_key": task["idempotency_key"],
            "fencing_token": int(task["fencing_token"]),
        }
        for key, expected in bindings.items():
            if result.get(key) != expected:
                raise ValueError("agent_result_binding: " + key + " differs from task")
        journal.finish_agent_task(
            database, target_id, holder_id, lease_epoch, task_id, status, result)
    finally:
        database.close()
    return _agent_response(start_response, principal["token"], {
        "accepted": True, "task_id": task_id, "status": status})


def _evidence_bundle(context, plan_id):
    database = journal.connect(context["config"]["state_dir"])
    try:
        plan = journal.load_plan(database, plan_id)
        status = journal.plan_status(database, plan_id)
        audit_rows = journal.list_audit(database, limit=100000)
        chain = journal.verify_audit_chain(database)
    finally:
        database.close()
    relevant_audit = [
        item for item in audit_rows
        if item.get("event", {}).get("payload", {}).get("plan_id") == plan_id
    ]
    evidence_root = pathlib.Path(context["config"]["state_dir"]) / "evidence" / (
        plan_id.replace(":", "_"))
    evidence = []
    if evidence_root.is_dir():
        for path in sorted(evidence_root.glob("*.json")):
            value = read_json(path)
            unsigned = dict(value)
            observed = unsigned.pop("evidence_digest", None)
            if observed != digest_value(unsigned):
                raise ValueError("evidence_digest: stored evidence is invalid")
            evidence.append(value)
    bundle = {
        "schema_version": 1,
        "plan": plan,
        "status": status,
        "evidence": evidence,
        "audit": relevant_audit,
        "audit_chain": chain,
    }
    bundle["bundle_digest"] = digest_value(bundle)
    return bundle


def _dispatch(context, environ, start_response):
    _reap_workers(context)
    method = environ.get("REQUEST_METHOD", "GET").upper()
    path = environ.get("PATH_INFO", "/")
    if method == "GET" and (path == "/" or path.startswith("/static/")):
        return _static(context, path, start_response)
    if method == "GET" and path == "/api/v1/health/live":
        return _json_response(start_response, "200 OK", {"status": "live"})
    if method == "GET" and path == "/api/v1/health/ready":
        return _json_response(start_response, "200 OK", {
            "status": "ready",
            "mutations_enabled": context["config"]["allow_mutations"],
            "pid": os.getpid(),
            "instance_id": context.get("instance_id", "embedded-test"),
            "agent_registry_entries": len(context.get("agents", {})),
        })
    lease_target = _agent_target_from_path(path, "/lease")
    if method == "POST" and lease_target:
        return _agent_lease(context, environ, start_response, lease_target)
    claim_target = _agent_target_from_path(path, "/tasks/claim")
    if method == "POST" and claim_target:
        return _agent_claim(context, environ, start_response, claim_target)
    result_parts = path.strip("/").split("/")
    if (method == "POST" and len(result_parts) == 7
            and result_parts[:3] == ["api", "v1", "agents"]
            and result_parts[4] == "tasks" and result_parts[6] == "result"):
        return _agent_result(
            context, environ, start_response, result_parts[3], result_parts[5])
    if method == "POST" and path == "/api/v1/session":
        return _login(context, environ, start_response)
    if method == "DELETE" and path == "/api/v1/session":
        return _logout(context, environ, start_response)
    if method == "GET" and path == "/api/v1/session":
        principal = _principal(context, environ, "read")
        return _json_response(start_response, "200 OK", {
            "authenticated": True,
            "role": principal["role"],
            "subject": principal["subject"],
            "permissions": auth.permissions_for_role(principal["role"]),
            "csrf_token": principal["csrf"],
            "mutations_enabled": context["config"]["allow_mutations"],
        })
    if method == "GET" and path == "/api/v1/snapshot":
        _principal(context, environ, "read")
        return _json_response(start_response, "200 OK", _snapshot(context))
    if method == "GET" and path == "/api/v1/desired":
        _principal(context, environ, "read")
        return _json_response(start_response, "200 OK",
                              read_json(context["config"]["desired_state_path"]))
    if method == "POST" and path == "/api/v1/plans":
        principal = _mutation_principal(context, environ, "plan")
        desired = _read_body(environ)
        plan = _build_and_store_plan(context, desired, _actor(principal))
        return _json_response(start_response, "201 Created", {
            "ok": True,
            "summary": plan_summary(plan),
            "plan": plan,
        })
    if method == "GET" and path == "/api/v1/plans":
        _principal(context, environ, "read")
        database = journal.connect(context["config"]["state_dir"])
        try:
            plans = journal.list_plans(database)
        finally:
            database.close()
        return _json_response(start_response, "200 OK", {"plans": plans})
    if method == "GET" and path.startswith("/api/v1/plans/") and path.count("/") == 4:
        _principal(context, environ, "read")
        plan_id = path.removeprefix("/api/v1/plans/")
        if not plan_id.startswith("sha256:") or len(plan_id) != 71:
            return _error(start_response, "404 Not Found", "not_found", "resource not found")
        database = journal.connect(context["config"]["state_dir"])
        try:
            status = journal.plan_status(database, plan_id)
            status["plan"] = journal.load_plan(database, plan_id)
        finally:
            database.close()
        return _json_response(start_response, "200 OK", status)
    approve_id = _plan_id_from_path(path, "/approve")
    if method == "POST" and approve_id:
        principal = _mutation_principal(context, environ, "approve")
        database = journal.connect(context["config"]["state_dir"])
        try:
            journal.approve_plan(database, approve_id, _actor(principal))
        finally:
            database.close()
        return _json_response(start_response, "200 OK", {"ok": True,
                                                          "plan_id": approve_id})
    path_parts = path.strip("/").split("/")
    if (method == "POST" and len(path_parts) == 7
            and path_parts[:3] == ["api", "v1", "plans"]
            and path_parts[4] == "waves" and path_parts[6] == "approve"):
        principal = _mutation_principal(context, environ, "approve")
        plan_id = path_parts[3]
        try:
            wave = int(path_parts[5])
        except ValueError as exc:
            raise ValueError("wave: wave must be an integer") from exc
        database = journal.connect(context["config"]["state_dir"])
        try:
            journal.approve_wave(database, plan_id, wave, _actor(principal))
        finally:
            database.close()
        return _json_response(start_response, "200 OK", {
            "ok": True, "plan_id": plan_id, "wave": wave})
    apply_id = _plan_id_from_path(path, "/apply")
    if method == "POST" and apply_id:
        _mutation_principal(context, environ, "execute")
        database = journal.connect(context["config"]["state_dir"])
        try:
            plan = journal.load_plan(database, apply_id)
        finally:
            database.close()
        result = _launch_operation(context, plan, "apply")
        return _json_response(start_response, "202 Accepted", result)
    rollback_id = _plan_id_from_path(path, "/rollback")
    if method == "POST" and rollback_id:
        _mutation_principal(context, environ, "execute")
        database = journal.connect(context["config"]["state_dir"])
        try:
            plan = journal.load_plan(database, rollback_id)
        finally:
            database.close()
        result = _launch_operation(context, plan, "rollback")
        return _json_response(start_response, "202 Accepted", result)
    pause_id = _plan_id_from_path(path, "/pause")
    if method == "POST" and pause_id:
        principal = _mutation_principal(context, environ, "pause")
        database = journal.connect(context["config"]["state_dir"])
        try:
            journal.request_pause(database, pause_id, _actor(principal))
        finally:
            database.close()
        return _json_response(start_response, "202 Accepted", {
            "status": "PAUSE_REQUESTED", "plan_id": pause_id})
    evidence_id = _plan_id_from_path(path, "/evidence")
    if method == "GET" and evidence_id:
        _principal(context, environ, "evidence")
        return _json_response(
            start_response, "200 OK", _evidence_bundle(context, evidence_id))
    if method == "GET" and path == "/api/v1/agents":
        _principal(context, environ, "read")
        database = journal.connect(context["config"]["state_dir"])
        try:
            agents = journal.list_agents(database)
        finally:
            database.close()
        return _json_response(start_response, "200 OK", {"agents": agents})
    if method == "GET" and path == "/api/v1/audit":
        _principal(context, environ, "audit")
        database = journal.connect(context["config"]["state_dir"])
        try:
            audit_rows = journal.list_audit(database)
            verification = journal.verify_audit_chain(database)
        finally:
            database.close()
        return _json_response(start_response, "200 OK", {
            "audit": audit_rows,
            "chain": verification,
        })
    if method == "GET" and path == "/metrics":
        snapshot = _snapshot(context)
        database = journal.connect(context["config"]["state_dir"])
        try:
            plans = journal.list_plans(database)
            try:
                journal.verify_audit_chain(database)
                audit_valid = True
            except ValueError:
                audit_valid = False
        finally:
            database.close()
        body = metrics_text(snapshot, plans, audit_valid=audit_valid).encode("utf-8")
        return _respond(start_response, "200 OK", body,
                        content_type="text/plain; version=0.0.4; charset=utf-8")
    return _error(start_response, "404 Not Found", "not_found", "resource not found")


def application(context, environ, start_response):
    try:
        return _dispatch(context, environ, start_response)
    except PermissionError as exc:
        status = "401 Unauthorized" if str(exc) == "authentication_required" else "403 Forbidden"
        return _error(start_response, status, "forbidden", str(exc))
    except (ValueError, json.JSONDecodeError) as exc:
        return _error(start_response, "400 Bad Request", "validation_failed", str(exc))
    except RuntimeError as exc:
        return _error(start_response, "409 Conflict", "operation_failed", str(exc))
    except Exception as exc:
        if context.get("debug"):
            traceback.print_exc()
        return _error(start_response, "500 Internal Server Error", "internal_error",
                      "request failed: " + type(exc).__name__)


def create_application(context):
    return partial(application, context)


def serve(context):
    listen = context["config"]["listen"]
    server = make_server(listen["host"], listen["port"], create_application(context))
    print("Automation Fleet UI listening on http://{}:{}".format(
        listen["host"], listen["port"]), flush=True)
    try:
        server.serve_forever()
    finally:
        server.server_close()

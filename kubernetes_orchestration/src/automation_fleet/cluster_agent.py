"""Outbound-only Kubernetes cluster agent with dual lease fencing."""

import argparse
import copy
import datetime
import json
import pathlib
import os
import shutil
import signal
import ssl
import time
import urllib.error
import urllib.parse
import urllib.request

from automation_fleet import agent_protocol
from automation_fleet.canonical import atomic_json, digest_value, read_json
from automation_fleet.process import run_process
from automation_fleet.providers import execute_operation, rollback_operation
from automation_fleet.provider_kubernetes import observe_target
from automation_fleet.validation import require, validate_identifier


def _tool(name):
    path = shutil.which(name)
    if path is None:
        raise RuntimeError("tool_missing: required executable is missing: " + name)
    return path


def load_agent_configuration(path):
    value = read_json(path)
    require(value.get("schema_version") == 1,
            "agent_config", "cluster agent schema_version must be 1")
    target_id = validate_identifier(value.get("target_id"), "target_id")
    holder_id = os.environ.get("AGENT_HOLDER_ID", value.get("holder_id"))
    require(isinstance(holder_id, str) and 1 <= len(holder_id) <= 128,
            "holder_id", "cluster agent holder_id is invalid")
    central_url = value.get("central_url")
    require(isinstance(central_url, str) and central_url == central_url.rstrip("/"),
            "central_url", "central_url must not have a trailing slash")
    insecure = bool(value.get("allow_insecure_http", False))
    require(central_url.startswith("https://")
            or insecure and central_url.startswith("http://"),
            "central_url", "HTTPS is required unless insecure HTTP is explicit")
    parsed_url = urllib.parse.urlparse(central_url)
    require(bool(parsed_url.hostname) and not parsed_url.username
            and not parsed_url.password and not parsed_url.query
            and not parsed_url.fragment,
            "central_url", "central_url authority or path is invalid")
    token_file = pathlib.Path(value.get("token_file", ""))
    require(token_file.is_absolute(),
            "agent_token", "cluster agent token_file must be absolute")
    namespace = validate_identifier(value.get("namespace"), "namespace")
    lease_name = validate_identifier(
        value.get("lease_name", "automation-cluster-agent"), "lease_name")
    ttl_s = value.get("lease_ttl_s", 30)
    poll_s = value.get("poll_interval_s", 2)
    require(isinstance(ttl_s, int) and 15 <= ttl_s <= 300,
            "agent_lease", "lease_ttl_s must be 15..300")
    require(isinstance(poll_s, int) and 1 <= poll_s <= max(1, ttl_s // 3),
            "agent_poll", "poll_interval_s must renew the lease in time")
    capabilities = value.get("capabilities", [])
    require(isinstance(capabilities, list)
            and len(capabilities) == len(set(capabilities)),
            "agent_capabilities", "capabilities must be a unique list")
    for capability in capabilities:
        validate_identifier(capability, "capability")
    health_path = pathlib.Path(value.get("health_file", "/tmp/agent-health.json"))
    allowed_health_roots = (
        pathlib.Path("/tmp"), pathlib.Path("/var/lib/automation-fleet-agent"))
    require(health_path.is_absolute() and health_path != pathlib.Path("/")
            and any(root == health_path.parent or root in health_path.parents
                    for root in allowed_health_roots),
            "agent_health", "health_file must use an agent-writable runtime path")
    result = copy.deepcopy(value)
    result.update({
        "target_id": target_id,
        "holder_id": holder_id,
        "central_url": central_url,
        "token_file": str(token_file),
        "namespace": namespace,
        "lease_name": lease_name,
        "lease_ttl_s": ttl_s,
        "poll_interval_s": poll_s,
        "capabilities": capabilities,
        "cluster_id": validate_identifier(
            value.get("cluster_id", target_id), "cluster_id"),
        "health_file": str(health_path),
    })
    return result


def _utc_text():
    now = datetime.datetime.now(datetime.timezone.utc)
    return now.isoformat(timespec="microseconds").replace("+00:00", "Z")


def _parse_time(value):
    if not isinstance(value, str):
        return None
    try:
        return datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _lease_manifest(config, resource_version=None):
    metadata = {
        "name": config["lease_name"],
        "namespace": config["namespace"],
        "labels": {
            "app.kubernetes.io/name": "automation-cluster-agent",
            "app.kubernetes.io/part-of": "automation-platform",
        },
    }
    if resource_version:
        metadata["resourceVersion"] = resource_version
    return {
        "apiVersion": "coordination.k8s.io/v1",
        "kind": "Lease",
        "metadata": metadata,
        "spec": {
            "holderIdentity": config["holder_id"],
            "leaseDurationSeconds": config["lease_ttl_s"],
            "renewTime": _utc_text(),
        },
    }


def _kubectl_lease(config, action, manifest=None):
    command = [
        _tool("kubectl"), "--namespace", config["namespace"], action,
    ]
    input_text = None
    if action == "get":
        command.extend(["lease", config["lease_name"], "--output=json"])
    else:
        command.extend(["-f", "-"])
        input_text = json.dumps(manifest, sort_keys=True) + "\n"
    return run_process(command, timeout_s=15, input_text=input_text)


def acquire_local_lease(config):
    """Acquire or renew the namespace Lease using resourceVersion CAS."""
    observed = _kubectl_lease(config, "get")
    if observed["return_code"] != 0:
        created = _kubectl_lease(config, "create", _lease_manifest(config))
        return created["return_code"] == 0
    try:
        lease = json.loads(observed["stdout"])
    except json.JSONDecodeError as exc:
        raise RuntimeError("agent_lease: kubectl returned invalid Lease JSON") from exc
    spec = lease.get("spec", {})
    holder = spec.get("holderIdentity")
    renew = _parse_time(spec.get("renewTime") or spec.get("acquireTime"))
    duration = int(spec.get("leaseDurationSeconds", config["lease_ttl_s"]))
    now = datetime.datetime.now(datetime.timezone.utc)
    expired = renew is None or renew + datetime.timedelta(seconds=duration) <= now
    if holder not in {None, "", config["holder_id"]} and not expired:
        return False
    resource_version = lease.get("metadata", {}).get("resourceVersion")
    replaced = _kubectl_lease(
        config, "replace", _lease_manifest(config, resource_version))
    return replaced["return_code"] == 0


def _ssl_context(config):
    ca_file = config.get("ca_file")
    return ssl.create_default_context(cafile=ca_file) if ca_file else ssl.create_default_context()


def _post(config, token, path, payload):
    body = json.dumps(
        agent_protocol.envelope(token, payload),
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    request = urllib.request.Request(
        config["central_url"] + path,
        data=body,
        method="POST",
        headers={
            "Accept": "application/json",
            "Authorization": "Bearer " + token,
            "Content-Type": "application/json",
        },
    )
    context = _ssl_context(config) if config["central_url"].startswith("https://") else None
    try:
        with urllib.request.urlopen(request, timeout=15, context=context) as response:
            value = json.load(response)
    except urllib.error.HTTPError as exc:
        detail = exc.read(1000).decode("utf-8", errors="replace")
        raise RuntimeError("agent_api_http: " + str(exc.code) + " " + detail) from exc
    return agent_protocol.verify_envelope(token, value)


def _validate_task(config, task):
    require(isinstance(task, dict) and task.get("schema_version") == 1,
            "agent_task", "agent task schema is invalid")
    require(task.get("target_id") == config["target_id"],
            "agent_task", "agent task target binding differs")
    step = task.get("step")
    require(isinstance(step, dict), "agent_task", "agent task step is missing")
    require(step.get("target_id") == config["target_id"]
            and step.get("execution_mode") == "agent"
            and step.get("provider") == "kubernetes_helm"
            and step.get("context") == "in-cluster",
            "agent_task", "agent task violates the local execution contract")
    require(task.get("idempotency_key") == step.get("idempotency_key"),
            "agent_task", "agent task idempotency binding differs")
    return step


def _task_result(task, status, provider_result=None, error=None):
    value = {
        "schema_version": 1,
        "plan_id": task["plan_id"],
        "step_id": task["step_id"],
        "target_id": task["target_id"],
        "idempotency_key": task["idempotency_key"],
        "fencing_token": int(task["fencing_token"]),
        "status": status,
    }
    if provider_result is not None:
        value["provider_result"] = provider_result
    if error is not None:
        value["error"] = str(error)[:2000]
    value["result_digest"] = digest_value(value)
    return value


def _execute_task(root, config, task):
    step = _validate_task(config, task)
    try:
        if step.get("operation") == "rollback":
            result = rollback_operation(step, step.get("rollback_ref"))
        else:
            result = execute_operation(root, step)
        return _task_result(task, "SUCCEEDED", provider_result=result)
    except Exception as exc:
        return _task_result(task, "FAILED", error=exc)


def _observation(config):
    target = {
        "target_id": config["target_id"],
        "cluster_id": config["cluster_id"],
        "context": "in-cluster",
        "namespace": config["namespace"],
    }
    return observe_target(target, timeout_s=15)


def agent_iteration(root, config, token, lease_epoch=None):
    if not acquire_local_lease(config):
        return {"status": "STANDBY", "lease_epoch": lease_epoch}
    lease_payload = {
        "target_id": config["target_id"],
        "holder_id": config["holder_id"],
        "ttl_s": config["lease_ttl_s"],
        "capabilities": config["capabilities"],
        "observation": _observation(config),
    }
    lease = _post(
        config, token,
        "/api/v1/agents/" + config["target_id"] + "/lease",
        lease_payload,
    )
    if not lease.get("acquired"):
        return {"status": "STANDBY", "lease_epoch": lease.get("lease_epoch")}
    epoch = int(lease["lease_epoch"])
    claim = _post(
        config, token,
        "/api/v1/agents/" + config["target_id"] + "/tasks/claim",
        {
            "target_id": config["target_id"],
            "holder_id": config["holder_id"],
            "lease_epoch": epoch,
        },
    )
    task_record = claim.get("task")
    if task_record is None:
        return {"status": "IDLE", "lease_epoch": epoch}
    task = task_record["task"]
    result = _execute_task(root, config, task)
    _post(
        config, token,
        "/api/v1/agents/" + config["target_id"] + "/tasks/"
        + task_record["task_id"] + "/result",
        {
            "target_id": config["target_id"],
            "holder_id": config["holder_id"],
            "lease_epoch": epoch,
            "task_id": task_record["task_id"],
            "status": result["status"],
            "result": result,
        },
    )
    return {"status": result["status"], "lease_epoch": epoch,
            "task_id": task_record["task_id"]}


def run_agent(root, config, once=False):
    token = agent_protocol.load_token(config["token_file"], config["target_id"])
    running = {"value": True}

    def stop(unused_signum, unused_frame):
        running["value"] = False

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    epoch = None
    while running["value"]:
        try:
            state = agent_iteration(root, config, token, lease_epoch=epoch)
            epoch = state.get("lease_epoch")
            atomic_json(config["health_file"], {
                "schema_version": 1,
                "target_id": config["target_id"],
                "observed_at_unix": int(time.time()),
                "state": state,
            })
            if once:
                return state
        except Exception as exc:
            print("cluster-agent error: " + str(exc), flush=True)
            atomic_json(config["health_file"], {
                "schema_version": 1,
                "target_id": config["target_id"],
                "observed_at_unix": int(time.time()),
                "state": {
                    "status": "ERROR",
                    "error_type": type(exc).__name__,
                    "error_digest": digest_value(str(exc)),
                },
            })
            if once:
                raise
        time.sleep(config["poll_interval_s"])
    return {"status": "STOPPED", "lease_epoch": epoch}


def _parser():
    parser = argparse.ArgumentParser(prog="automation-cluster-agent")
    parser.add_argument("--root", required=True)
    parser.add_argument("--config", required=True)
    parser.add_argument("--once", action="store_true")
    return parser


def main(argv=None):
    arguments = _parser().parse_args(argv)
    config = load_agent_configuration(arguments.config)
    result = run_agent(arguments.root, config, once=arguments.once)
    print(json.dumps(result, sort_keys=True), flush=True)


if __name__ == "__main__":
    main()

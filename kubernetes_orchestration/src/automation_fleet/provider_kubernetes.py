"""Typed Helm and Kubernetes provider functions."""

import json
import os
import pathlib
import shutil
import tempfile

from automation_fleet.canonical import digest_file, digest_tree, digest_value
from automation_fleet.process import require_success, run_process


RESOURCE_TYPES = "deployments,statefulsets,daemonsets,pods,jobs,persistentvolumeclaims,services"


COMPONENT_RESOURCES = {
    "postgresql": ["services"],
    "mqtt": [
        "statefulsets.apps", "services", "configmaps", "serviceaccounts",
        "poddisruptionbudgets.policy", "networkpolicies.networking.k8s.io",
    ],
    "master": [
        "deployments.apps", "jobs.batch", "services", "configmaps",
        "serviceaccounts", "persistentvolumeclaims", "poddisruptionbudgets.policy",
        "networkpolicies.networking.k8s.io",
    ],
    "proxy": [
        "deployments.apps", "jobs.batch", "services", "serviceaccounts",
        "persistentvolumeclaims", "poddisruptionbudgets.policy",
        "networkpolicies.networking.k8s.io",
    ],
    "mcp-controller": [
        "deployments.apps", "services", "serviceaccounts", "persistentvolumeclaims",
        "networkpolicies.networking.k8s.io",
    ],
    "automation-system": [
        "deployments.apps", "serviceaccounts", "persistentvolumeclaims",
        "poddisruptionbudgets.policy", "networkpolicies.networking.k8s.io",
    ],
    "automation-fleet": [
        "deployments.apps", "services", "configmaps", "serviceaccounts",
        "persistentvolumeclaims", "roles.rbac.authorization.k8s.io",
        "rolebindings.rbac.authorization.k8s.io",
        "networkpolicies.networking.k8s.io",
    ],
    "cluster-agent": [
        "deployments.apps", "configmaps", "serviceaccounts",
        "roles.rbac.authorization.k8s.io", "rolebindings.rbac.authorization.k8s.io",
        "leases.coordination.k8s.io", "poddisruptionbudgets.policy",
        "networkpolicies.networking.k8s.io",
    ],
    "observability": [
        "deployments.apps", "services", "configmaps", "serviceaccounts",
        "networkpolicies.networking.k8s.io",
    ],
}


def _tool(name):
    path = shutil.which(name)
    if path is None:
        raise RuntimeError("tool_missing: required executable is missing: " + name)
    return path


def _kubectl(step, *arguments):
    command = [_tool("kubectl")]
    if step["context"] != "in-cluster":
        command.extend(["--context", step["context"]])
    command.extend(["--namespace", step["namespace"]])
    return command + list(arguments)


def _helm(step, *arguments):
    command = [_tool("helm")]
    if step["context"] != "in-cluster":
        command.extend(["--kube-context", step["context"]])
    command.extend(["--namespace", step["namespace"]])
    return command + list(arguments)


def _chart_path(root, step):
    base = pathlib.Path(root).resolve()
    charts = (base / "deploy" / "helm").resolve()
    path = (base / step["chart"]).resolve()
    if path != charts and charts not in path.parents:
        raise ValueError("chart_escape: chart path escapes Helm directory")
    if not path.is_dir():
        raise ValueError("chart_missing: " + str(path))
    observed = digest_tree(path)
    if observed != step["chart_digest"]:
        raise ValueError("chart_drift: expected " + step["chart_digest"] + ", got " + observed)
    descriptors = (base / "component_descriptors").resolve()
    descriptor = (base / step["descriptor"]).resolve()
    if descriptors not in descriptor.parents or not descriptor.is_file():
        raise ValueError("descriptor_path: plan descriptor path is unsafe or missing")
    observed_descriptor = digest_file(descriptor)
    if observed_descriptor != step["descriptor_digest"]:
        raise ValueError(
            "descriptor_drift: expected " + step["descriptor_digest"]
            + ", got " + observed_descriptor)
    return path


def _write_values(values):
    descriptor, path = tempfile.mkstemp(prefix="automation-fleet-values-", suffix=".json")
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(values, handle, ensure_ascii=False, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
    except Exception:
        try:
            os.close(descriptor)
        except OSError:
            pass
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
        raise
    return path


def _run_with_values(step, command_builder, timeout_s):
    path = _write_values(step["values"])
    try:
        command = command_builder(path)
        return run_process(command, timeout_s=timeout_s)
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass


def _history(step):
    result = run_process(
        _helm(step, "history", step["release_name"], "--output", "json", "--max", "1"),
        timeout_s=30,
    )
    if result["return_code"] != 0:
        return None
    try:
        rows = json.loads(result["stdout"])
    except json.JSONDecodeError:
        return None
    if not isinstance(rows, list) or not rows:
        return None
    revision = rows[-1].get("revision")
    try:
        return int(revision)
    except (TypeError, ValueError):
        return None


def _permissions(step):
    resources = list(COMPONENT_RESOURCES.get(step["component_id"], []))
    driver = os.environ.get("HELM_DRIVER", "secret")
    storage_resource = "configmaps" if driver == "configmap" else "secrets"
    if storage_resource not in resources:
        resources.append(storage_resource)
    if step["component_id"] == "observability":
        operator = step["values"].get("prometheusOperator", {})
        if operator.get("serviceMonitorEnabled"):
            resources.append("servicemonitors.monitoring.coreos.com")
        if operator.get("rulesEnabled"):
            resources.append("prometheusrules.monitoring.coreos.com")
    result = []
    for resource in sorted(set(resources)):
        for verb in ("get", "list", "create", "update", "patch", "delete"):
            result.append((verb, resource))
    return result


def discover(root, step):
    _chart_path(root, step)
    namespace = require_success(
        run_process(_kubectl(step, "get", "serviceaccount", "default",
                             "--output=json"), timeout_s=30),
        "namespace access discovery",
    )
    releases = run_process(
        _helm(step, "list", "--output", "json", "--all"), timeout_s=30)
    require_success(releases, "Helm release discovery")
    return {
        "status": "SUCCEEDED",
        "namespace_digest": digest_value(namespace["stdout"]),
        "releases_digest": digest_value(releases["stdout"]),
        "target_id": step["target_id"],
    }


def validate_target(root, step):
    _chart_path(root, step)
    version = require_success(
        run_process(_kubectl(step, "version", "--client=true", "--output=json"),
                    timeout_s=20),
        "kubectl client validation",
    )
    namespace = require_success(
        run_process(_kubectl(step, "get", "serviceaccount", "default", "--output=name"),
                    timeout_s=30),
        "namespace access",
    )
    helm_version = require_success(
        run_process([_tool("helm"), "version", "--short"], timeout_s=20),
        "Helm client validation",
    )
    for resource in ("pods", "events"):
        for verb in ("get", "list"):
            permission = require_success(
                run_process(_kubectl(step, "auth", "can-i", verb, resource),
                            timeout_s=20),
                "Kubernetes read RBAC check",
            )
            if permission["stdout"].strip().lower() != "yes":
                raise RuntimeError(
                    "rbac_denied: " + verb + " permission is required for " + resource)
    return {
        "status": "SUCCEEDED",
        "kubectl_version_digest": digest_value(version["stdout"]),
        "helm_version": helm_version["stdout"].strip(),
        "namespace": namespace["stdout"].strip(),
    }


def preflight(root, step):
    chart = _chart_path(root, step)
    version = require_success(
        run_process(_kubectl(step, "version", "--client=true", "--output=json"), timeout_s=20),
        "kubectl client validation",
    )
    cluster = require_success(
        run_process(_kubectl(step, "get", "serviceaccount", "default", "--output=name"),
                    timeout_s=30),
        "namespace access",
    )
    helm_version = require_success(
        run_process([_tool("helm"), "version", "--short"], timeout_s=20),
        "Helm client validation",
    )
    lint = _run_with_values(
        step,
        command_builder=_lint_command_builder(step, chart),
        timeout_s=step["timeout_s"],
    )
    require_success(lint, "Helm lint")
    if step["write_owner"] == "helm":
        for verb, resource in _permissions(step):
            permission = require_success(
                run_process(_kubectl(step, "auth", "can-i", verb, resource),
                            timeout_s=20),
                "Kubernetes RBAC check",
            )
            if permission["stdout"].strip().lower() != "yes":
                raise RuntimeError(
                    "rbac_denied: " + verb + " permission is required for " + resource)
    return {
        "status": "SUCCEEDED",
        "kubectl_version_digest": digest_value(version["stdout"]),
        "helm_version": helm_version["stdout"].strip(),
        "namespace": cluster["stdout"].strip(),
        "chart_digest": step["chart_digest"],
    }


def _lint_builder(step, chart, values_path):
    return [
        _tool("helm"), "lint", str(chart),
        "--values", values_path,
        "--strict",
    ]


def _lint_command_builder(step, chart):
    from functools import partial
    return partial(_lint_builder, step, chart)


def _template_builder(step, chart, values_path):
    return _helm(
        step,
        "template", step["release_name"], str(chart),
        "--values", values_path,
        "--include-crds=false",
    )


def render(root, step):
    result = render_manifest(root, step)
    output = result["stdout"]
    warnings = result["stderr"]
    return {
        "status": "SUCCEEDED",
        "manifest_digest": digest_value(output),
        "manifest_bytes": len(output.encode("utf-8")),
        "warnings_digest": digest_value(warnings),
    }


def render_manifest(root, step):
    from functools import partial
    chart = _chart_path(root, step)
    result = _run_with_values(
        step,
        partial(_template_builder, step, chart),
        step["timeout_s"],
    )
    require_success(result, "Helm template")
    return result


def diff(root, step):
    rendered = render_manifest(root, step)
    result = run_process(
        _kubectl(
            step,
            "diff",
            "--server-side=true",
            "--field-manager=" + step.get("field_manager", "automation-fleet"),
            "-f", "-",
        ),
        timeout_s=step["timeout_s"],
        input_text=rendered["stdout"],
    )
    if result["return_code"] not in {0, 1}:
        require_success(result, "Kubernetes server-side diff")
    return {
        "status": "SUCCEEDED",
        "changed": result["return_code"] == 1,
        "diff_digest": digest_value(result["stdout"]),
        "manifest_digest": digest_value(rendered["stdout"]),
    }


def backup(root, step):
    _chart_path(root, step)
    revision = _history(step)
    live = run_process(
        _kubectl(
            step,
            "get", RESOURCE_TYPES,
            "--selector", "app.kubernetes.io/instance=" + step["release_name"],
            "--output", "json",
        ),
        timeout_s=60,
    )
    if live["return_code"] != 0:
        live_digest = digest_value({"status": "not_installed"})
    else:
        live_digest = digest_value(live["stdout"])
    database_sensitive = step["component_id"] in {"postgresql", "master", "proxy"}
    backup_ref = step.get("values", {}).get("operational", {}).get("databaseBackupRef")
    return {
        "status": "SUCCEEDED",
        "previous_revision": revision,
        "live_state_digest": live_digest,
        "database_backup_ref": backup_ref,
        "database_backup_required_before_production_apply": database_sensitive,
    }


def _upgrade_builder(step, chart, values_path):
    return _helm(
        step,
        "upgrade", "--install", step["release_name"], str(chart),
        "--values", values_path,
        "--create-namespace",
        "--atomic",
        "--wait",
        "--history-max", "10",
        "--timeout", str(step["timeout_s"]) + "s",
    )


def apply(root, step):
    from functools import partial
    if step["write_owner"] != "helm":
        raise RuntimeError("write_owner: target is observe-only")
    chart = _chart_path(root, step)
    previous_revision = _history(step)
    result = _run_with_values(
        step,
        partial(_upgrade_builder, step, chart),
        step["timeout_s"],
    )
    require_success(result, "Helm upgrade")
    current_revision = _history(step)
    if current_revision is None:
        raise RuntimeError("helm_history: applied revision could not be observed")
    return {
        "status": "SUCCEEDED",
        "previous_revision": previous_revision,
        "observed_revision": current_revision,
        "output_digest": digest_value(result["stdout"]),
        "chart_digest": step["chart_digest"],
    }


def _workloads(step):
    result = run_process(
        _kubectl(
            step,
            "get", "deployments,statefulsets",
            "--selector", "app.kubernetes.io/instance=" + step["release_name"],
            "--output", "json",
        ),
        timeout_s=30,
    )
    require_success(result, "workload discovery")
    data = json.loads(result["stdout"])
    references = []
    for item in data.get("items", []):
        kind = str(item.get("kind", "")).lower()
        name = item.get("metadata", {}).get("name")
        if kind in {"deployment", "statefulset"} and isinstance(name, str):
            references.append(kind + "/" + name)
    return sorted(references)


def wait_ready(root, step):
    _chart_path(root, step)
    workloads = _workloads(step)
    results = []
    for reference in workloads:
        result = run_process(
            _kubectl(
                step,
                "rollout", "status", reference,
                "--timeout", str(step["timeout_s"]) + "s",
            ),
            timeout_s=step["timeout_s"] + 5,
        )
        require_success(result, "rollout readiness for " + reference)
        results.append({
            "resource": reference,
            "output_digest": digest_value(result["stdout"]),
        })
    return {"status": "SUCCEEDED", "workloads": results}


def _pod_ready(item):
    conditions = item.get("status", {}).get("conditions", [])
    for condition in conditions:
        if condition.get("type") == "Ready":
            return condition.get("status") == "True"
    return False


def _resource_ready(item):
    kind = item.get("kind")
    status = item.get("status", {})
    spec = item.get("spec", {})
    if kind == "Pod":
        if status.get("phase") == "Succeeded":
            return True
        if status.get("phase") == "Failed":
            return False
        return _pod_ready(item)
    if kind == "Deployment":
        desired = int(spec.get("replicas", status.get("replicas", 1)) or 0)
        return (int(status.get("readyReplicas", 0) or 0) >= desired
                and int(status.get("availableReplicas", 0) or 0) >= desired)
    if kind == "StatefulSet":
        desired = int(spec.get("replicas", status.get("replicas", 1)) or 0)
        return int(status.get("readyReplicas", 0) or 0) >= desired
    if kind == "DaemonSet":
        desired = int(status.get("desiredNumberScheduled", 0) or 0)
        return desired > 0 and int(status.get("numberReady", 0) or 0) >= desired
    if kind == "Job":
        completions = int(spec.get("completions", 1) or 1)
        return (int(status.get("failed", 0) or 0) == 0
                and int(status.get("succeeded", 0) or 0) >= completions)
    if kind == "PersistentVolumeClaim":
        return status.get("phase") == "Bound"
    return True


def verify(root, step):
    _chart_path(root, step)
    result = run_process(
        _kubectl(
            step,
            "get", RESOURCE_TYPES,
            "--selector", "app.kubernetes.io/instance=" + step["release_name"],
            "--output", "json",
        ),
        timeout_s=60,
    )
    require_success(result, "resource verification")
    data = json.loads(result["stdout"])
    items = data.get("items", [])
    if not items:
        raise RuntimeError("resources_missing: release selector returned no resources")
    pods = [item for item in items if item.get("kind") == "Pod"]
    failed = sorted(
        item.get("metadata", {}).get("name", "unknown")
        for item in items
        if not _resource_ready(item)
    )
    if failed:
        raise RuntimeError("resources_not_ready: " + ",".join(failed))
    return {
        "status": "SUCCEEDED",
        "resource_count": len(items),
        "ready_pods": len(pods),
        "observed_digest": digest_value(data),
    }


def execute_operation(root, step):
    operations = {
        "discover": discover,
        "validate_target": validate_target,
        "preflight": preflight,
        "render": render,
        "diff": diff,
        "backup": backup,
        "apply": apply,
        "wait_ready": wait_ready,
        "verify": verify,
    }
    operation = operations.get(step["operation"])
    if operation is None:
        raise ValueError("operation: unsupported provider operation")
    return operation(root, step)


def rollback(step, revision):
    if step["write_owner"] != "helm":
        raise RuntimeError("write_owner: target is observe-only")
    if not isinstance(revision, int) or revision < 1:
        raise ValueError("rollback_revision: no safe previous Helm revision is available")
    result = run_process(
        _helm(
            step,
            "rollback", step["release_name"], str(revision),
            "--wait",
            "--timeout", str(step["timeout_s"]) + "s",
        ),
        timeout_s=step["timeout_s"] + 5,
    )
    require_success(result, "Helm rollback")
    return {
        "status": "ROLLED_BACK",
        "revision": revision,
        "output_digest": digest_value(result["stdout"]),
    }


def observe_target(target, timeout_s=30):
    step = {
        "context": target["context"],
        "namespace": target["namespace"],
    }
    result = run_process(
        _kubectl(
            step,
            "get", RESOURCE_TYPES,
            "--selector", "app.kubernetes.io/part-of=automation-platform",
            "--output", "json",
        ),
        timeout_s=timeout_s,
    )
    if result["return_code"] != 0:
        return {
            "target_id": target["target_id"],
            "cluster_id": target["cluster_id"],
            "namespace": target["namespace"],
            "status": "UNREACHABLE",
            "error": (result["stderr"].strip() or "kubectl failed")[:500],
            "resources": [],
        }
    try:
        data = json.loads(result["stdout"])
    except json.JSONDecodeError:
        return {
            "target_id": target["target_id"],
            "cluster_id": target["cluster_id"],
            "namespace": target["namespace"],
            "status": "INVALID_RESPONSE",
            "error": "kubectl returned invalid JSON",
            "resources": [],
        }
    resources = []
    for item in data.get("items", []):
        metadata = item.get("metadata", {})
        status = item.get("status", {})
        resource = {
            "kind": item.get("kind"),
            "name": metadata.get("name"),
            "namespace": metadata.get("namespace"),
            "labels": metadata.get("labels", {}),
            "phase": status.get("phase"),
            "ready": _resource_ready(item),
            "replicas": status.get("replicas"),
            "ready_replicas": status.get("readyReplicas"),
            "available_replicas": status.get("availableReplicas"),
            "succeeded": status.get("succeeded"),
            "failed": status.get("failed"),
        }
        resources.append(resource)
    resources.sort(key=_resource_key)
    unhealthy = sum(1 for item in resources if item["ready"] is False)
    return {
        "target_id": target["target_id"],
        "cluster_id": target["cluster_id"],
        "namespace": target["namespace"],
        "status": ("EMPTY" if not resources else
                   "HEALTHY" if unhealthy == 0 else "DEGRADED"),
        "resource_count": len(resources),
        "unhealthy_count": unhealthy,
        "resources": resources,
    }


def _resource_key(resource):
    return (str(resource.get("kind")), str(resource.get("name")))

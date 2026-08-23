"""Typed GitOps repository handoff for Argo CD and Flux write owners."""

import json
import pathlib
import re
import shutil
import time

from automation_fleet.canonical import atomic_write, digest_bytes, digest_value
from automation_fleet.process import require_success, run_process
from automation_fleet import provider_kubernetes


SUPPORTED_PROVIDERS = {"gitops_argocd", "gitops_flux"}


def _tool(name):
    path = shutil.which(name)
    if path is None:
        raise RuntimeError("tool_missing: required executable is missing: " + name)
    return path


def _worktree(step):
    settings = step.get("gitops")
    if not isinstance(settings, dict):
        raise ValueError("gitops: plan step lacks GitOps settings")
    root = pathlib.Path(settings["worktree"]).resolve()
    if str(root) == "/" or not root.is_dir() or not (root / ".git").exists():
        raise ValueError("gitops_worktree: configured path is not a Git worktree")
    relative = pathlib.PurePosixPath(settings["relative_path"])
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError("gitops_path: repository-relative path is unsafe")
    destination = (root / pathlib.Path(*relative.parts)).resolve()
    if root not in destination.parents:
        raise ValueError("gitops_path: destination escapes the worktree")
    return root, destination, settings


def _git(root, *arguments, timeout_s=60):
    return run_process([_tool("git"), "-C", str(root)] + list(arguments),
                       timeout_s=timeout_s)


def _head(root):
    result = require_success(_git(root, "rev-parse", "HEAD"), "Git HEAD discovery")
    value = result["stdout"].strip()
    if re.fullmatch(r"[0-9a-f]{40,64}", value) is None:
        raise RuntimeError("git_head: repository returned an invalid commit identity")
    return value


def _require_branch(root, expected):
    result = require_success(
        _git(root, "branch", "--show-current"), "Git branch discovery")
    observed = result["stdout"].strip()
    if observed != expected:
        raise RuntimeError(
            "git_branch: expected " + expected + ", observed " + observed)
    return observed


def _require_clean(root):
    result = require_success(
        _git(root, "status", "--porcelain=v1", "--untracked-files=all"),
        "Git worktree status")
    if result["stdout"].strip():
        raise RuntimeError("git_worktree_dirty: repository has uncommitted changes")


def discover(root, step):
    worktree, unused_destination, settings = _worktree(step)
    provider_kubernetes.render_manifest(root, step)
    return {
        "status": "SUCCEEDED",
        "provider": step["provider"],
        "branch": _require_branch(worktree, settings["branch"]),
        "head": _head(worktree),
        "worktree_digest": digest_value(str(worktree)),
    }


def validate_target(root, step):
    worktree, unused_destination, settings = _worktree(step)
    provider_result = provider_kubernetes.validate_target(root, step)
    remote = require_success(
        _git(worktree, "remote", "get-url", settings["remote"]),
        "Git remote validation")
    for key in ("user.name", "user.email"):
        require_success(_git(worktree, "config", "--get", key),
                        "Git identity validation")
    return {
        "status": "SUCCEEDED",
        "provider": step["provider"],
        "remote_digest": digest_value(remote["stdout"].strip()),
        "cluster_read_contract": provider_result,
    }


def render(root, step):
    return provider_kubernetes.render(root, step)


def _manifest(root, step):
    result = provider_kubernetes.render_manifest(root, step)
    return result["stdout"].encode("utf-8")


def diff(root, step):
    unused_worktree, destination, unused_settings = _worktree(step)
    desired = _manifest(root, step)
    manifest_path = destination / "manifest.yaml"
    observed = manifest_path.read_bytes() if manifest_path.is_file() else b""
    return {
        "status": "SUCCEEDED",
        "changed": observed != desired,
        "desired_manifest_digest": digest_bytes(desired),
        "observed_manifest_digest": digest_bytes(observed),
    }


def preflight(root, step):
    worktree, unused_destination, settings = _worktree(step)
    validation = provider_kubernetes.preflight(root, step)
    _require_branch(worktree, settings["branch"])
    _require_clean(worktree)
    return {
        "status": "SUCCEEDED",
        "provider": step["provider"],
        "head": _head(worktree),
        "validation": validation,
    }


def backup(root, step):
    worktree, destination, unused_settings = _worktree(step)
    provider_kubernetes.render_manifest(root, step)
    manifest_path = destination / "manifest.yaml"
    observed = manifest_path.read_bytes() if manifest_path.is_file() else b""
    database_sensitive = step["component_id"] in {"postgresql", "master", "proxy"}
    backup_ref = step.get("values", {}).get("operational", {}).get("databaseBackupRef")
    return {
        "status": "SUCCEEDED",
        "previous_commit": _head(worktree),
        "previous_manifest_digest": digest_bytes(observed),
        "database_backup_ref": backup_ref,
        "database_backup_required_before_production_apply": database_sensitive,
    }


def _metadata(step, manifest_digest):
    return {
        "schema_version": 1,
        "managed_by": "automation-fleet-v0028-r1",
        "provider": step["provider"],
        "target_id": step["target_id"],
        "component_id": step["component_id"],
        "release_name": step["release_name"],
        "chart_digest": step["chart_digest"],
        "descriptor_digest": step["descriptor_digest"],
        "effective_values_digest": step["effective_values_digest"],
        "idempotency_key": step["idempotency_key"],
        "manifest_digest": manifest_digest,
    }


def apply(root, step):
    worktree, destination, settings = _worktree(step)
    _require_branch(worktree, settings["branch"])
    _require_clean(worktree)
    previous_commit = _head(worktree)
    manifest = _manifest(root, step)
    destination.mkdir(parents=True, exist_ok=True)
    atomic_write(destination / "manifest.yaml", manifest, mode=0o644)
    kustomization = (
        "apiVersion: kustomize.config.k8s.io/v1beta1\n"
        "kind: Kustomization\n"
        "resources:\n"
        "  - manifest.yaml\n"
    ).encode("utf-8")
    atomic_write(destination / "kustomization.yaml", kustomization, mode=0o644)
    metadata = _metadata(step, digest_bytes(manifest))
    atomic_write(
        destination / "fleet-metadata.json",
        (json.dumps(metadata, indent=2, sort_keys=True) + "\n").encode("utf-8"),
        mode=0o644,
    )
    relative = settings["relative_path"]
    require_success(_git(worktree, "add", "--", relative), "GitOps staging")
    staged = _git(worktree, "diff", "--cached", "--quiet", "--", relative)
    if staged["return_code"] == 0:
        return {
            "status": "SUCCEEDED",
            "idempotent": True,
            "previous_commit": previous_commit,
            "observed_commit": previous_commit,
            "manifest_digest": metadata["manifest_digest"],
        }
    if staged["return_code"] != 1:
        require_success(staged, "GitOps staged diff")
    message = "automation-fleet " + step["target_id"] + " " + step["component_id"]
    require_success(_git(worktree, "commit", "-m", message), "GitOps commit")
    observed_commit = _head(worktree)
    if settings["push"]:
        require_success(
            _git(worktree, "push", "--porcelain", settings["remote"],
                 settings["branch"], timeout_s=step["timeout_s"]),
            "GitOps push",
        )
    return {
        "status": "SUCCEEDED",
        "idempotent": False,
        "previous_commit": previous_commit,
        "observed_commit": observed_commit,
        "manifest_digest": metadata["manifest_digest"],
        "metadata_digest": digest_value(metadata),
    }


def _reconciler_resource(step):
    settings = step["gitops"]
    if step["provider"] == "gitops_argocd":
        return "applications.argoproj.io", settings["reconciler_name"]
    return "kustomizations.kustomize.toolkit.fluxcd.io", settings["reconciler_name"]


def _reconciler_status(step):
    settings = step["gitops"]
    resource, name = _reconciler_resource(step)
    probe = dict(step)
    probe["namespace"] = settings["reconciler_namespace"]
    result = require_success(
        run_process(
            provider_kubernetes._kubectl(probe, "get", resource, name, "--output=json"),
            timeout_s=30,
        ),
        "GitOps reconciler observation",
    )
    value = json.loads(result["stdout"])
    if step["provider"] == "gitops_argocd":
        ready = (value.get("status", {}).get("sync", {}).get("status") == "Synced"
                 and value.get("status", {}).get("health", {}).get("status") == "Healthy")
        revision = value.get("status", {}).get("sync", {}).get("revision")
    else:
        ready = False
        for condition in value.get("status", {}).get("conditions", []):
            if condition.get("type") == "Ready":
                ready = condition.get("status") == "True"
        generation = value.get("metadata", {}).get("generation")
        observed = value.get("status", {}).get("observedGeneration")
        ready = ready and generation == observed
        revision = value.get("status", {}).get("lastAppliedRevision")
    return {
        "ready": ready,
        "revision": revision,
        "observed_digest": digest_value(value),
        "resource": resource + "/" + name,
    }


def wait_ready(root, step):
    deadline = time.monotonic() + int(step["timeout_s"])
    last = None
    while time.monotonic() < deadline:
        last = _reconciler_status(step)
        if last["ready"]:
            return {"status": "SUCCEEDED", "reconciler": last}
        time.sleep(2)
    raise TimeoutError(
        "gitops_reconcile_timeout: " + str(last.get("resource") if last else "unknown"))


def verify(root, step):
    reconciliation = _reconciler_status(step)
    if not reconciliation["ready"]:
        raise RuntimeError("gitops_not_ready: reconciler is not healthy and current")
    result = provider_kubernetes.verify(root, step)
    result["gitops_provider"] = step["provider"]
    result["reconciler"] = reconciliation
    return result


def execute_operation(root, step):
    if step.get("provider") not in SUPPORTED_PROVIDERS:
        raise ValueError("provider: unsupported GitOps provider")
    operations = {
        "discover": discover,
        "validate_target": validate_target,
        "render": render,
        "diff": diff,
        "preflight": preflight,
        "backup": backup,
        "apply": apply,
        "wait_ready": wait_ready,
        "verify": verify,
    }
    operation = operations.get(step["operation"])
    if operation is None:
        raise ValueError("operation: unsupported GitOps operation")
    return operation(root, step)


def rollback(step, commit):
    worktree, unused_destination, settings = _worktree(step)
    if not isinstance(commit, str) or re.fullmatch(r"[0-9a-f]{40,64}", commit) is None:
        raise ValueError("rollback_commit: invalid Git commit identity")
    _require_branch(worktree, settings["branch"])
    _require_clean(worktree)
    require_success(_git(worktree, "revert", "--no-edit", commit,
                         timeout_s=step["timeout_s"]), "GitOps revert")
    observed = _head(worktree)
    if settings["push"]:
        require_success(
            _git(worktree, "push", "--porcelain", settings["remote"],
                 settings["branch"], timeout_s=step["timeout_s"]),
            "GitOps rollback push",
        )
    return {
        "status": "ROLLED_BACK",
        "reverted_commit": commit,
        "observed_commit": observed,
    }

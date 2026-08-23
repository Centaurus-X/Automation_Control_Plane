"""Fail-closed static and integrity verification for the v0028-r1 release."""

import argparse
import ast
import hashlib
import json
import os
import pathlib
import re
import subprocess


CONTROL_MANIFEST_SHA256 = "451759c6e8b1f73636e65626c06ff79dc70a1fd9b3b0e9828d7f0902c5c01240"
CONTROL_ARCHIVE_SHA256 = "ccddf6ed4c959bf17cad4041c68b683eccc0110702c6ae46987b220d07d2fb03"
RUNTIME_ARCHIVE_SHA256 = "c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467"
RUNTIME_MANIFEST_SHA256 = "699c29f9cf58b1bd6bd7407e879b3b5b3f41ad978343037a1dc33a75ec25026d"
ARCHITECTURE_PLAN_SHA256 = "865c061bd73b2fddc756a91dee6c5b96058e8559f733bd0b9be10e8fdfac3509"
PARENT_RELEASE_SHA256 = "eb7b3ed52273dc6cdd6dd22516bc50cb516ccee58de2d11697fb074d8196a971"
EXPECTED_CONTROL_FILES = 588
EXPECTED_CONTROL_MANIFEST_ENTRIES = 587
FORBIDDEN_KINDS = {
    "ClusterRole", "ClusterRoleBinding", "CustomResourceDefinition",
    "MutatingWebhookConfiguration", "ValidatingWebhookConfiguration",
}


def _sha256_file(path):
    state = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            state.update(block)
    return state.hexdigest()


def _manifest(path):
    entries = {}
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        digest, relative = line.split(None, 1)
        relative = relative.strip().removeprefix("./")
        if not re.fullmatch(r"[0-9a-f]{64}", digest):
            raise ValueError("invalid manifest digest: " + relative)
        if relative in entries or ".." in pathlib.PurePosixPath(relative).parts:
            raise ValueError("invalid manifest path: " + relative)
        entries[relative] = digest
    return entries


def _verify_control_plane(root, checks):
    control = root / "control_plane_v0026_r1"
    manifest_path = control / "SHA256SUMS"
    if _sha256_file(manifest_path) != CONTROL_MANIFEST_SHA256:
        raise ValueError("preserved control-plane SHA256SUMS drift")
    entries = _manifest(manifest_path)
    if len(entries) != EXPECTED_CONTROL_MANIFEST_ENTRIES:
        raise ValueError("preserved control-plane manifest entry count drift")
    actual = set()
    for path in control.rglob("*"):
        if path.is_symlink():
            raise ValueError("symbolic link in preserved control plane: " + str(path))
        if path.is_file():
            actual.add(path.relative_to(control).as_posix())
    expected = set(entries)
    expected.add("SHA256SUMS")
    if actual != expected or len(actual) != EXPECTED_CONTROL_FILES:
        missing = sorted(expected - actual)
        extra = sorted(actual - expected)
        raise ValueError("preserved control-plane file-set drift: missing={} extra={}".format(
            missing[:3], extra[:3]))
    for relative, expected_digest in entries.items():
        observed = _sha256_file(control / relative)
        if observed != expected_digest:
            raise ValueError("preserved control-plane content drift: " + relative)
    if (control / "VERSION").read_text(encoding="utf-8").strip() != "0.26.0":
        raise ValueError("preserved control-plane version drift")
    if (control / "RELEASE_REVISION").read_text(encoding="utf-8").strip() != "v0026-r1":
        raise ValueError("preserved control-plane revision drift")
    checks.append("control_plane_byte_identity")


def _verify_identity(root, checks):
    if root.name != "automation_control_plane_release_v0028_r1":
        raise ValueError("release root name is invalid")
    if (root / "VERSION").read_text(encoding="utf-8").strip() != "0.28.0":
        raise ValueError("release VERSION is invalid")
    if (root / "RELEASE_REVISION").read_text(encoding="utf-8").strip() != "v0028-r1":
        raise ValueError("release revision is invalid")
    baselines = json.loads((root / "BASELINE_INPUTS.json").read_text(encoding="utf-8"))
    if baselines["parent_release"]["archive_sha256"] != PARENT_RELEASE_SHA256:
        raise ValueError("parent v0027-r2 release binding is invalid")
    if baselines["preserved_control_plane"]["archive_sha256"] != CONTROL_ARCHIVE_SHA256:
        raise ValueError("preserved control-plane archive binding is invalid")
    if (baselines["preserved_control_plane"]["internal_manifest_sha256"]
            != CONTROL_MANIFEST_SHA256):
        raise ValueError("preserved control-plane manifest binding is invalid")
    if baselines["external_runtime"]["archive_sha256"] != RUNTIME_ARCHIVE_SHA256:
        raise ValueError("external runtime digest binding is invalid")
    if baselines["external_runtime"]["internal_manifest_sha256"] != RUNTIME_MANIFEST_SHA256:
        raise ValueError("external runtime manifest binding is invalid")
    if baselines["external_runtime"]["embedded"] is not False:
        raise ValueError("runtime boundary must remain external")
    if baselines["architecture_plan"]["sha256"] != ARCHITECTURE_PLAN_SHA256:
        raise ValueError("architecture plan binding is invalid")
    orchestration = root / "kubernetes_orchestration"
    runtime_bound_files = (
        orchestration / "tools" / "validate_runtime_archive.py",
        orchestration / "scripts" / "build_runtime_image.sh",
        orchestration / "deploy" / "images" / "automation-system" / "Dockerfile",
    )
    for path in runtime_bound_files:
        if RUNTIME_ARCHIVE_SHA256 not in path.read_text(encoding="utf-8"):
            raise ValueError("runtime archive binding missing from " + str(path))
    validator = runtime_bound_files[0].read_text(encoding="utf-8")
    if RUNTIME_MANIFEST_SHA256 not in validator:
        raise ValueError("runtime internal manifest binding missing from validator")
    checks.append("release_identity_and_archive_boundary")


def _verify_python(orchestration, checks):
    problems = []
    files = sorted(orchestration.rglob("*.py"))
    for path in files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.ClassDef):
                problems.append(str(path) + ": user-defined class")
            if isinstance(node, ast.Lambda):
                problems.append(str(path) + ": lambda")
            decorators = getattr(node, "decorator_list", [])
            if decorators:
                problems.append(str(path) + ": decorator")
            if isinstance(node, ast.Call):
                if isinstance(node.func, ast.Name) and node.func.id in {"eval", "exec"}:
                    problems.append(str(path) + ": dynamic code execution")
                for keyword in node.keywords:
                    if keyword.arg == "shell" and isinstance(keyword.value, ast.Constant):
                        if keyword.value.value is True:
                            problems.append(str(path) + ": shell=True")
    if problems:
        raise ValueError("code standard violations: " + "; ".join(problems[:10]))
    checks.append("functional_python_code_standard:{}".format(len(files)))


def _verify_web(orchestration, checks):
    javascript = (orchestration / "web" / "app.js").read_text(encoding="utf-8")
    for forbidden in ("=>", "eval(", "new Function(", "innerHTML", "outerHTML"):
        if forbidden in javascript:
            raise ValueError("unsafe JavaScript construct: " + forbidden)
    html = (orchestration / "web" / "index.html").read_text(encoding="utf-8")
    if "<script src=\"/static/app.js\"" not in html:
        raise ValueError("Web UI does not load the external application script")
    if re.search(r"<script(?![^>]*\bsrc=)", html, re.IGNORECASE):
        raise ValueError("inline Web UI scripts are forbidden by CSP")
    checks.append("web_ui_csp_and_dom_safety")


def _verify_json(orchestration, checks):
    files = sorted(orchestration.rglob("*.json"))
    for path in files:
        json.loads(path.read_text(encoding="utf-8"))
    checks.append("json_parse:{}".format(len(files)))


def _verify_shell(orchestration, checks):
    files = sorted(orchestration.rglob("*.sh"))
    for path in files:
        result = subprocess.run(
            ["bash", "-n", str(path)], stdout=subprocess.PIPE,
            stderr=subprocess.PIPE, text=True, check=False)
        if result.returncode != 0:
            raise ValueError("shell syntax: {}: {}".format(path, result.stderr.strip()))
        if not os.access(path, os.X_OK):
            raise ValueError("shell script is not executable: " + str(path))
    checks.append("shell_syntax_and_mode:{}".format(len(files)))


def _verify_helm(orchestration, checks):
    helm = orchestration / "deploy" / "helm"
    charts = sorted(path.parent for path in helm.glob("automation-*/Chart.yaml"))
    if len(charts) != 10:
        raise ValueError("expected ten Helm charts including the umbrella")
    templates = sorted(helm.rglob("templates/*.yaml"))
    combined = "\n".join(path.read_text(encoding="utf-8") for path in templates)
    for kind in FORBIDDEN_KINDS:
        if re.search(r"(?m)^kind:\s*" + re.escape(kind) + r"\s*$", combined):
            raise ValueError("cluster-scoped Helm kind is forbidden: " + kind)
    for forbidden in ("pods/exec", "resources: [\"*\"]", "verbs: [\"*\"]", ":latest"):
        if forbidden in combined:
            raise ValueError("forbidden Helm permission or image reference: " + forbidden)
    if "secret: {" in combined or combined.count("secretName:") != combined.count("defaultMode:"):
        raise ValueError("Helm Secret volume modes must be explicit and non-world-readable")
    for path in templates:
        text = path.read_text(encoding="utf-8")
        if re.search(r"(?m)^kind:\s*(Deployment|StatefulSet|Job)\s*$", text):
            for required in (
                    "allowPrivilegeEscalation: false", "readOnlyRootFilesystem: true",
                    "drop: [\"ALL\"]", "seccompProfile"):
                if required not in text:
                    raise ValueError("workload security context missing {} in {}".format(
                        required, path))
    for chart in charts:
        if chart.name == "automation-control-plane":
            continue
        schema = json.loads((chart / "values.schema.json").read_text(encoding="utf-8"))
        if schema.get("$schema") != "http://json-schema.org/draft-07/schema#":
            raise ValueError("Helm values schema must use JSON Schema Draft-07: " + chart.name)
        if "$defs" in schema:
            raise ValueError("Helm values schema contains unsupported $defs: " + chart.name)
        if "enabled" not in schema.get("properties", {}):
            raise ValueError("umbrella-enabled schema compatibility missing: " + chart.name)
        values_text = (chart / "values.yaml").read_text(encoding="utf-8")
        if not re.search(r"(?m)^enabled:\s+true\s*$", values_text):
            raise ValueError("standalone chart must default enabled: " + chart.name)
        for template in sorted((chart / "templates").glob("*.yaml")):
            template_text = template.read_text(encoding="utf-8").strip()
            if (not template_text.startswith("{{- if .Values.enabled }}")
                    or not template_text.endswith("{{- end }}")):
                raise ValueError("chart enabled guard missing: " + str(template))
    mqtt = (helm / "automation-mqtt" / "templates" / "configmap.yaml").read_text(
        encoding="utf-8")
    if "nodes/{{ .Values.masterInstanceId }}/#" in mqtt:
        raise ValueError("MQTT master ACL is broader than the preserved baseline")
    runtime_schema_path = helm / "automation-system-soft" / "values.schema.json"
    runtime_schema = runtime_schema_path.read_text(encoding="utf-8")
    runtime_schema_value = json.loads(runtime_schema)
    runtime_properties = runtime_schema_value.get("properties", {})
    if RUNTIME_ARCHIVE_SHA256 not in runtime_schema or "c_hard_rt" in runtime_schema:
        raise ValueError("runtime archive or Kubernetes RT qualification boundary drift")
    if runtime_properties.get("siteId", {}).get("const") != "site-main":
        raise ValueError("runtime v0101 site identity binding is missing")
    if set(runtime_properties.get("nodeId", {}).get("enum", [])) != {"fn-01", "fn-02"}:
        raise ValueError("runtime v0101 node identity binding is missing")
    runtime_deployment = (helm / "automation-system-soft" / "templates"
                          / "deployment.yaml").read_text(encoding="utf-8")
    fleet_deployment = (helm / "automation-fleet" / "templates"
                        / "deployment.yaml").read_text(encoding="utf-8")
    if ("stage-mqtt-identity" not in runtime_deployment
            or "install -m 0600" not in runtime_deployment):
        raise ValueError("runtime private-key permission staging is missing")
    if "stage-auth" not in fleet_deployment or "install -m 0600" not in fleet_deployment:
        raise ValueError("Fleet token permission staging is missing")
    cluster_agent = helm / "automation-cluster-agent"
    agent_rbac = (cluster_agent / "templates" / "rbac.yaml").read_text(encoding="utf-8")
    agent_deployment = (cluster_agent / "templates" / "deployment.yaml").read_text(
        encoding="utf-8")
    agent_network = (cluster_agent / "templates" / "networkpolicy.yaml").read_text(
        encoding="utf-8")
    if ("coordination.k8s.io" not in agent_rbac or 'resources: ["leases"]' not in agent_rbac
            or "automation_fleet.cluster_agent" not in agent_deployment
            or "ingress: []" not in agent_network):
        raise ValueError("outbound cluster-agent Lease or network boundary is incomplete")
    required_sources = {
        "provider_gitops.py": ("gitops_argocd", "gitops_flux", "reconciler"),
        "cluster_agent.py": ("acquire_local_lease", "lease_epoch", "agent_iteration"),
        "placement.py": ("placement", "capability_missing", "canary_target_ids"),
    }
    source_root = orchestration / "src" / "automation_fleet"
    for name, fragments in required_sources.items():
        source = (source_root / name).read_text(encoding="utf-8")
        if any(fragment not in source for fragment in fragments):
            raise ValueError("required orchestration contract missing from " + name)
    vendored = helm / "automation-control-plane" / "charts"
    if vendored.exists():
        for source in charts:
            if source.name == "automation-control-plane":
                continue
            target = vendored / source.name
            if not target.is_dir():
                raise ValueError("vendored umbrella dependency missing: " + source.name)
            source_files = {
                path.relative_to(source).as_posix(): _sha256_file(path)
                for path in source.rglob("*") if path.is_file()
            }
            target_files = {
                path.relative_to(target).as_posix(): _sha256_file(path)
                for path in target.rglob("*") if path.is_file()
            }
            if source_files != target_files:
                raise ValueError("vendored umbrella dependency drift: " + source.name)
    checks.append("helm_security_agent_gitops_and_boundary:{}".format(len(charts)))


def _verify_file_hygiene(root, orchestration, checks):
    forbidden_suffixes = {".pyc", ".pyo", ".pem", ".p12", ".pfx"}
    for path in root.rglob("*"):
        if path.is_symlink():
            raise ValueError("symbolic links are forbidden: " + str(path))
        if path.name == "__pycache__" or path.suffix.lower() in forbidden_suffixes:
            raise ValueError("generated or secret material in release: " + str(path))
    embedded = [path for path in root.rglob("*.zip") if path.is_file()]
    if embedded:
        raise ValueError("nested release archives are forbidden: " + str(embedded[0]))
    for path in orchestration.rglob("*"):
        lowered = path.name.lower()
        if path.is_file() and (lowered.endswith(".key") or lowered == "admin.token"):
            raise ValueError("secret material in additive layer: " + str(path))
    checks.append("file_hygiene_and_external_runtime")


def _verify_release_manifest(root, checks):
    path = root / "SHA256SUMS"
    if not path.exists():
        checks.append("release_manifest_pending")
        return
    entries = _manifest(path)
    actual = {
        item.relative_to(root).as_posix()
        for item in root.rglob("*")
        if item.is_file() and item != path
    }
    if set(entries) != actual:
        raise ValueError("release SHA256SUMS file set does not match the release tree")
    for relative, expected in entries.items():
        if _sha256_file(root / relative) != expected:
            raise ValueError("release checksum mismatch: " + relative)
    checks.append("release_sha256_manifest:{}".format(len(entries)))


def verify(root):
    release_root = pathlib.Path(root).resolve()
    orchestration = release_root / "kubernetes_orchestration"
    checks = []
    _verify_identity(release_root, checks)
    _verify_control_plane(release_root, checks)
    _verify_python(orchestration, checks)
    _verify_web(orchestration, checks)
    _verify_json(orchestration, checks)
    _verify_shell(orchestration, checks)
    _verify_helm(orchestration, checks)
    _verify_file_hygiene(release_root, orchestration, checks)
    _verify_release_manifest(release_root, checks)
    return {"ok": True, "release": release_root.name, "checks": checks}


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--quiet", action="store_true")
    arguments = parser.parse_args()
    result = verify(arguments.root)
    if not arguments.quiet:
        print(json.dumps(result, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

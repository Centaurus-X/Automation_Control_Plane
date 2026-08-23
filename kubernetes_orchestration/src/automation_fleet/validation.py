"""Fail-closed validation for inventory, desired state, policy and plans."""

import copy
import pathlib
import re


IDENTIFIER = re.compile(r"^[a-z0-9](?:[a-z0-9.-]{0,61}[a-z0-9])?$")
DIGEST = re.compile(r"^sha256:[0-9a-f]{64}$")
IMAGE_REPOSITORY = re.compile(
    r"^(?:[A-Za-z0-9][A-Za-z0-9._-]*|"
    r"[A-Za-z0-9][A-Za-z0-9._-]*(?::[0-9]+)?"
    r"(?:/[A-Za-z0-9][A-Za-z0-9._-]*)+)$")
SECRET_KEY = re.compile(
    r"(?:password|passwd|secret|private[_-]?key|api[_-]?key|access[_-]?key|bearer|"
    r"token_value|credential|connection[_-]?(?:string|uri)|database[_-]?url)",
    re.IGNORECASE,
)
REFERENCE_KEY = re.compile(
    r"(?:_ref|_refs|secretref|secretrefs|secretname|secretnames|existingsecret|existingsecrets)$",
    re.IGNORECASE,
)
SECRET_CONTROL_KEYS = {"secret_policy"}
ALLOWED_TARGET_KINDS = {"kubernetes"}
ALLOWED_WRITE_OWNERS = {
    "helm", "gitops_argocd", "gitops_flux", "observe_only"}
ALLOWED_EXECUTION_MODES = {"direct", "agent"}
ALLOWED_PROVIDERS = {"kubernetes_helm", "gitops_argocd", "gitops_flux"}
WRITE_OWNER_PROVIDERS = {
    "helm": "kubernetes_helm",
    "gitops_argocd": "gitops_argocd",
    "gitops_flux": "gitops_flux",
}
ALLOWED_RUNTIME_PROFILES = {
    "kubernetes_soft_rt",
    "container_simulation",
}
RUNTIME_PROFILE_NODE_IDS = {
    "worker_simulation": "fn-01",
    "worker_fn02": "fn-02",
    "standalone": "fn-01",
}
PLAN_OPERATION_ORDER_V2 = (
    "discover", "validate_target", "render", "diff", "preflight", "backup",
    "apply", "wait_ready", "verify",
)
PLAN_OPERATIONS_V2 = set(PLAN_OPERATION_ORDER_V2)


def require(condition, code, message):
    if not condition:
        raise ValueError(code + ": " + message)


def validate_identifier(value, label):
    require(isinstance(value, str), "invalid_identifier", label + " must be a string")
    require(IDENTIFIER.fullmatch(value) is not None,
            "invalid_identifier", label + " is invalid")
    return value


def validate_digest(value, label, allow_placeholder=False):
    require(isinstance(value, str) and DIGEST.fullmatch(value) is not None,
            "invalid_digest", label + " must be an exact sha256 digest")
    if not allow_placeholder:
        require(value != "sha256:" + ("0" * 64),
                "placeholder_digest", label + " is still a placeholder")
    return value


def _scan_secret_values(value, path):
    if isinstance(value, dict):
        for key, item in value.items():
            next_path = path + "." + str(key)
            normalized_key = str(key).lower()
            if (normalized_key not in SECRET_CONTROL_KEYS
                    and SECRET_KEY.search(str(key))
                    and REFERENCE_KEY.search(str(key)) is None):
                if item not in (None, "", False):
                    raise ValueError(
                        "inline_secret_forbidden: secret-like value at " + next_path)
            _scan_secret_values(item, next_path)
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _scan_secret_values(item, path + "[" + str(index) + "]")


def _validate_string_map(value, label):
    require(isinstance(value, dict), label, label + " must be an object")
    for key, item in value.items():
        validate_identifier(key, label + " key")
        require(isinstance(item, str) and len(item) <= 253,
                label, label + " values must be bounded strings")


def _validate_gitops(value, target_id):
    require(isinstance(value, dict), "gitops", target_id + " requires gitops settings")
    required = (
        "worktree", "relative_path", "branch", "remote", "push",
        "reconciler_name", "reconciler_namespace",
    )
    for key in required:
        require(key in value, "gitops", target_id + " gitops." + key + " is required")
    worktree = pathlib.Path(value["worktree"])
    require(worktree.is_absolute() and str(worktree) != "/",
            "gitops_worktree", target_id + " worktree must be an absolute non-root path")
    relative = pathlib.PurePosixPath(value["relative_path"])
    require(not relative.is_absolute() and ".." not in relative.parts
            and bool(relative.parts),
            "gitops_path", target_id + " relative_path is unsafe")
    for key in ("branch", "remote"):
        require(isinstance(value[key], str)
                and re.fullmatch(r"[A-Za-z0-9._/-]{1,128}", value[key]) is not None
                and ".." not in value[key],
                "gitops", target_id + " gitops." + key + " is invalid")
    require(isinstance(value["push"], bool),
            "gitops", target_id + " gitops.push must be boolean")
    validate_identifier(value["reconciler_name"], "gitops reconciler_name")
    validate_identifier(value["reconciler_namespace"], "gitops reconciler_namespace")


def _validate_target(target):
    require(isinstance(target, dict), "target_type", "target must be an object")
    target_id = validate_identifier(target.get("target_id"), "target_id")
    require(target.get("target_kind") in ALLOWED_TARGET_KINDS,
            "target_kind", "unsupported target kind for " + target_id)
    validate_identifier(target.get("site_id"), "site_id")
    validate_identifier(target.get("cluster_id"), "cluster_id")
    validate_identifier(target.get("namespace"), "namespace")
    failure_domain = target.get("failure_domain", target.get("cluster_id"))
    target["failure_domain"] = validate_identifier(failure_domain, "failure_domain")
    write_owner = target.get("write_owner")
    require(write_owner in ALLOWED_WRITE_OWNERS,
            "write_owner", "target requires one explicit write owner")
    execution_mode = target.get("execution_mode", "direct")
    target["execution_mode"] = execution_mode
    require(execution_mode in ALLOWED_EXECUTION_MODES,
            "execution_mode", "unsupported execution mode for " + target_id)
    context = target.get("context")
    require(isinstance(context, str) and 0 < len(context) <= 253,
            "context", "target context is invalid")
    wave = target.get("wave", 0)
    require(isinstance(wave, int) and 0 <= wave <= 9999,
            "wave", "target wave must be an integer from 0 to 9999")
    capabilities = target.get("capabilities", [])
    require(isinstance(capabilities, list)
            and len(capabilities) == len(set(capabilities)),
            "capabilities", "target capabilities must be a unique list")
    for capability in capabilities:
        validate_identifier(capability, "capability")
    labels = target.get("labels", {})
    _validate_string_map(labels, "labels")
    if execution_mode == "agent":
        require(write_owner == "helm" and context == "in-cluster",
                "agent_target",
                "agent targets require write_owner=helm and context=in-cluster")
        timeout = target.get("agent_task_timeout_s", 1200)
        require(isinstance(timeout, int) and 30 <= timeout <= 7200,
                "agent_timeout", "agent task timeout must be 30..7200 seconds")
        target["agent_task_timeout_s"] = timeout
    if write_owner.startswith("gitops_"):
        require(execution_mode == "direct",
                "gitops_execution", "GitOps targets use direct repository handoff")
        _validate_gitops(target.get("gitops"), target_id)


def validate_inventory(value):
    require(isinstance(value, dict), "inventory_type", "inventory must be an object")
    require(value.get("schema_version") == 2,
            "inventory_schema", "inventory schema_version must be 2")
    validate_identifier(value.get("environment_id"), "environment_id")
    targets = value.get("targets")
    require(isinstance(targets, list) and targets,
            "inventory_targets", "inventory requires at least one target")
    normalized = copy.deepcopy(value)
    seen = set()
    for target in normalized["targets"]:
        _validate_target(target)
        target_id = target["target_id"]
        require(target_id not in seen,
                "duplicate_target", "duplicate target_id: " + target_id)
        seen.add(target_id)
    _scan_secret_values(normalized, "inventory")
    return normalized


def validate_catalog(value):
    require(isinstance(value, dict), "catalog_type", "catalog must be an object")
    require(value.get("schema_version") == 1,
            "catalog_schema", "catalog schema_version must be 1")
    validate_identifier(value.get("release_id"), "release_id")
    components = value.get("components")
    require(isinstance(components, dict) and components,
            "catalog_components", "catalog requires components")
    for component_id, component in components.items():
        validate_identifier(component_id, "component_id")
        require(isinstance(component, dict),
                "component_type", "component descriptor must be an object")
        validate_identifier(component.get("release_name"), "release_name")
        chart = component.get("chart")
        require(isinstance(chart, str) and chart.startswith("deploy/helm/")
                and ".." not in chart,
                "chart_path", "component chart path is unsafe")
        descriptor_path = component.get("descriptor")
        require(isinstance(descriptor_path, str)
                and descriptor_path.startswith("component_descriptors/")
                and ".." not in descriptor_path,
                "descriptor_path", "component descriptor path is unsafe")
        dependencies = component.get("dependencies", {})
        require(isinstance(dependencies, dict),
                "dependencies", "dependencies must be an object")
        groups = ("hard_deploy", "hard_runtime", "soft_runtime", "management",
                  "observability")
        for group in groups:
            items = dependencies.get(group, [])
            require(isinstance(items, list) and len(items) == len(set(items)),
                    "duplicate_dependency",
                    component_id + " " + group + " dependencies must be unique")
            for dependency in items:
                validate_identifier(dependency, "dependency")
                require(dependency in components,
                        "unknown_dependency",
                        component_id + " references unknown dependency " + dependency)
    _scan_secret_values(value, "catalog")
    return copy.deepcopy(value)


def _validate_component_values(component_id, descriptor, values):
    require(isinstance(values, dict),
            "component_values", component_id + " values must be an object")
    image = values.get("image")
    if component_id != "postgresql":
        require(isinstance(image, dict),
                "image", component_id + " requires image repository and digest")
        repository = image.get("repository")
        require(isinstance(repository, str)
                and IMAGE_REPOSITORY.fullmatch(repository) is not None
                and not repository.startswith("CHANGE_ME"),
                "image_repository", component_id + " image repository is invalid")
        validate_digest(image.get("digest"), component_id + ".image.digest")
    if component_id == "automation-system":
        profile = values.get("runtimeProfile")
        require(profile in ALLOWED_RUNTIME_PROFILES,
                "runtime_profile", "Kubernetes accepts only qualified soft/simulation profiles")
        runtime_digest = validate_digest(
            values.get("runtimeArchiveDigest"),
            "automation-system.runtimeArchiveDigest")
        expected_runtime_digest = descriptor.get("runtime_archive_digest")
        require(runtime_digest == expected_runtime_digest,
                "runtime_archive_digest",
                "automation-system archive digest does not match the release catalog")
        automation_profile = values.get("automationProfile", "worker_simulation")
        require(automation_profile in RUNTIME_PROFILE_NODE_IDS,
                "automation_profile", "automation-system profile is not in v0101")
        if values.get("siteId") is not None:
            require(values["siteId"] == "site-main",
                    "runtime_site_identity", "v0101 Kubernetes profiles use site-main")
        expected_node_id = RUNTIME_PROFILE_NODE_IDS[automation_profile]
        if values.get("nodeId") is not None:
            require(values["nodeId"] == expected_node_id,
                    "runtime_node_identity",
                    automation_profile + " requires nodeId " + expected_node_id)
        if profile == "container_simulation":
            require(automation_profile in {"worker_simulation", "standalone"},
                    "runtime_profile_mapping",
                    "container_simulation cannot select the fn-02 hardware profile")
        mqtt = values.get("mqtt", {})
        require(isinstance(mqtt, dict),
                "runtime_mqtt_profile", "automation-system mqtt must be an object")
        mqtt_enabled = mqtt.get("enabled")
        if mqtt_enabled is not None:
            expected_mqtt = automation_profile != "standalone"
            require(mqtt_enabled is expected_mqtt,
                    "runtime_mqtt_profile",
                    automation_profile + " has a fixed MQTT enablement contract")
    _scan_secret_values(values, "components." + component_id + ".values")


def _validate_selector(selector):
    require(isinstance(selector, dict),
            "placement_selector", "placement selector must be an object")
    list_fields = (
        "site_ids", "cluster_ids", "failure_domains", "required_capabilities",
    )
    for field in list_fields:
        values = selector.get(field, [])
        require(isinstance(values, list) and len(values) == len(set(values)),
                "placement_selector", field + " must be a unique list")
        for value in values:
            validate_identifier(value, field)
    execution_modes = selector.get("execution_modes", [])
    require(isinstance(execution_modes, list)
            and len(execution_modes) == len(set(execution_modes))
            and set(execution_modes) <= ALLOWED_EXECUTION_MODES,
            "placement_selector", "execution_modes contains an unsupported value")
    write_owners = selector.get("write_owners", [])
    require(isinstance(write_owners, list)
            and len(write_owners) == len(set(write_owners))
            and set(write_owners) <= ALLOWED_WRITE_OWNERS,
            "placement_selector", "write_owners contains an unsupported value")
    target_kind = selector.get("target_kind", "kubernetes")
    require(target_kind in ALLOWED_TARGET_KINDS,
            "placement_selector", "selector target_kind is unsupported")
    _validate_string_map(selector.get("match_labels", {}), "match_labels")
    maximum = selector.get("max_targets")
    require(maximum is None or isinstance(maximum, int) and 1 <= maximum <= 1000,
            "placement_selector", "max_targets must be 1..1000")


def validate_desired_state(value, catalog):
    require(isinstance(value, dict),
            "desired_type", "desired state must be an object")
    require(value.get("schema_version") == 1,
            "desired_schema", "desired state schema_version must be 1")
    validate_identifier(value.get("deployment_id"), "deployment_id")
    require(value.get("release_ref") == catalog.get("release_id"),
            "release_ref", "desired state release_ref does not match catalog")
    placement = value.get("placement")
    require(isinstance(placement, dict),
            "placement", "desired state requires placement")
    has_ids = "target_ids" in placement
    has_selector = "selector" in placement
    require(has_ids != has_selector,
            "placement", "placement requires exactly one of target_ids or selector")
    if has_ids:
        target_ids = placement.get("target_ids")
        require(isinstance(target_ids, list) and target_ids,
                "target_ids", "placement requires explicit target_ids")
        for target_id in target_ids:
            validate_identifier(target_id, "target_id")
        require(len(target_ids) == len(set(target_ids)),
                "duplicate_target", "placement target_ids must be unique")
    else:
        _validate_selector(placement.get("selector"))
    components = value.get("components")
    require(isinstance(components, dict) and components,
            "desired_components", "desired state requires components")
    unknown = sorted(set(components) - set(catalog["components"]))
    require(not unknown, "unknown_component", "unknown components: " + ",".join(unknown))
    enabled = 0
    for component_id, selection in components.items():
        require(isinstance(selection, dict),
                "component_selection", component_id + " selection must be an object")
        require(isinstance(selection.get("enabled"), bool),
                "component_enabled", component_id + " enabled must be boolean")
        if selection["enabled"]:
            enabled += 1
            _validate_component_values(
                component_id, catalog["components"][component_id],
                selection.get("values", {}))
    require(enabled > 0, "empty_deployment", "at least one component must be enabled")
    rollout = value.get("rollout", {})
    require(rollout.get("strategy", "waves") == "waves",
            "rollout_strategy", "only journaled wave rollout is supported")
    max_parallel = rollout.get("max_parallel", 1)
    require(isinstance(max_parallel, int) and max_parallel == 1,
            "max_parallel", "v0028-r1 uses deterministic max_parallel=1")
    require(rollout.get("stop_on_failure", True) is True,
            "stop_on_failure", "stop_on_failure must remain true")
    for field in ("approval_per_wave", "canary_per_failure_domain"):
        require(isinstance(rollout.get(field, True), bool),
                "rollout", field + " must be boolean")
    _scan_secret_values(value, "desired_state")
    return copy.deepcopy(value)


def validate_policy(value):
    require(isinstance(value, dict), "policy_type", "policy must be an object")
    require(value.get("schema_version") == 1,
            "policy_schema", "policy schema_version must be 1")
    require(value.get("require_approval") is True,
            "approval_policy", "exact plan approval must remain enabled")
    require(value.get("require_distinct_approver", True) is True,
            "approval_policy", "four-eyes approval must remain enabled")
    require(value.get("allow_database_automatic_rollback") is False,
            "database_rollback", "automatic database rollback is forbidden")
    require(value.get("allow_kubernetes_hard_rt") is False,
            "hard_rt_policy", "Kubernetes hard-RT is not qualified in v0028-r1")
    field_manager = value.get("field_manager")
    require(isinstance(field_manager, str) and field_manager,
            "field_manager", "policy requires a field_manager")
    allowed = value.get("allowed_write_owners", [
        "helm", "gitops_argocd", "gitops_flux"])
    require(isinstance(allowed, list) and allowed and len(allowed) == len(set(allowed)),
            "write_owner_policy", "allowed_write_owners must be a unique list")
    require(set(allowed) <= (ALLOWED_WRITE_OWNERS - {"observe_only"}),
            "write_owner_policy", "policy contains an unsupported write owner")
    timeout = value.get("max_agent_task_wait_s", 1800)
    require(isinstance(timeout, int) and 30 <= timeout <= 7200,
            "agent_timeout", "max_agent_task_wait_s must be 30..7200")
    _scan_secret_values(value, "policy")
    return copy.deepcopy(value)


def _validate_plan_v1(value):
    require(value.get("provider") == "kubernetes_helm"
            and value.get("provider_contract_version") == 1,
            "provider", "legacy plan provider contract is invalid")
    for step in value.get("steps", []):
        require(step.get("provider") == "kubernetes_helm"
                and step.get("provider_contract_version") == 1,
                "provider", "legacy plan step provider contract is invalid")
        require(step.get("operation") in {
            "preflight", "render", "apply", "wait_ready", "verify"},
            "operation", "unsupported legacy plan operation")


def _validate_plan_v2(value):
    require(value.get("provider_contract_version") == 2,
            "provider", "plan provider contract version must be 2")
    require(value.get("provider") in ALLOWED_PROVIDERS | {"mixed"},
            "provider", "plan provider summary is invalid")
    decisions = value.get("placement_decisions")
    require(isinstance(decisions, list) and decisions,
            "placement", "plan requires placement decisions")
    waves = value.get("waves")
    require(isinstance(waves, list) and waves,
            "rollout", "plan requires rollout waves")
    approval_policy = value.get("approval_policy")
    require(isinstance(approval_policy, dict)
            and approval_policy.get("distinct_principal") is True,
            "approval_policy", "plan requires four-eyes approval")
    require(value.get("approval_required") is True,
            "approval_policy", "plan approval cannot be disabled")
    steps = value.get("steps", [])
    require([step.get("order") for step in steps] == list(range(1, len(steps) + 1)),
            "step_order", "plan step order must be contiguous and canonical")
    target_order = value.get("target_order")
    component_order = value.get("component_order")
    require(isinstance(target_order, list) and target_order
            and len(target_order) == len(set(target_order)),
            "target_order", "plan target_order must be unique")
    require(isinstance(component_order, list) and component_order
            and len(component_order) == len(set(component_order)),
            "component_order", "plan component_order must be unique")
    observed_groups = []
    operations_by_group = {}
    target_waves = {}
    providers = set()
    for step in steps:
        provider = step.get("provider")
        providers.add(provider)
        require(provider in ALLOWED_PROVIDERS
                and step.get("provider_contract_version") == 2,
                "provider", "plan step provider contract is invalid")
        require(step.get("operation") in PLAN_OPERATIONS_V2,
                "operation", "unsupported plan operation")
        require(step.get("execution_mode") in ALLOWED_EXECUTION_MODES,
                "execution_mode", "plan step execution mode is invalid")
        if step.get("execution_mode") == "agent":
            require(provider == "kubernetes_helm",
                    "agent_provider", "cluster agents execute only Helm-owned targets")
        write_owner = step.get("write_owner")
        require(WRITE_OWNER_PROVIDERS.get(write_owner) == provider,
                "write_owner", "plan step provider does not match write owner")
        validate_identifier(step.get("failure_domain"), "failure_domain")
        field_manager = step.get("field_manager")
        require(isinstance(field_manager, str) and field_manager,
                "field_manager", "plan step field manager is missing")
        if step.get("execution_mode") == "agent":
            require(isinstance(step.get("agent_task_timeout_s"), int),
                    "agent_timeout", "agent plan step timeout is missing")
        if provider in {"gitops_argocd", "gitops_flux"}:
            require(isinstance(step.get("gitops"), dict),
                    "gitops", "GitOps plan step settings are missing")
        group = (step.get("target_id"), step.get("component_id"))
        if not observed_groups or observed_groups[-1] != group:
            observed_groups.append(group)
        operations_by_group.setdefault(group, []).append(step.get("operation"))
        wave = step.get("wave")
        require(isinstance(wave, int) and wave >= 0,
                "wave", "plan step wave must be a non-negative integer")
        previous_wave = target_waves.setdefault(step.get("target_id"), wave)
        require(previous_wave == wave,
                "wave", "all steps for a target must use one wave")
    expected_groups = [
        (target_id, component_id)
        for target_id in target_order
        for component_id in component_order
    ]
    require(observed_groups == expected_groups,
            "step_matrix", "plan target/component step matrix is incomplete or reordered")
    for group in expected_groups:
        require(operations_by_group.get(group) == list(PLAN_OPERATION_ORDER_V2),
                "operation_order", "plan operations are incomplete or reordered")
    provider_summary = next(iter(providers)) if len(providers) == 1 else "mixed"
    require(value.get("provider") == provider_summary,
            "provider", "plan provider summary differs from its steps")
    selected = [
        item.get("target_id") for item in decisions
        if isinstance(item, dict) and item.get("selected") is True
    ]
    require(set(selected) == set(target_order) and len(selected) == len(target_order),
            "placement", "selected placement decisions differ from target_order")
    wave_targets = {}
    for item in waves:
        require(isinstance(item, dict) and isinstance(item.get("wave"), int)
                and isinstance(item.get("target_ids"), list),
                "rollout", "plan wave record is invalid")
        for target_id in item["target_ids"]:
            require(target_id not in wave_targets,
                    "rollout", "target occurs in more than one wave")
            wave_targets[target_id] = item["wave"]
    require(wave_targets == target_waves,
            "rollout", "plan waves differ from step target waves")


def validate_plan(value):
    require(isinstance(value, dict), "plan_type", "plan must be an object")
    schema_version = value.get("schema_version")
    require(schema_version in {1, 2},
            "plan_schema", "plan schema_version must be 1 or 2")
    validate_digest(value.get("plan_id"), "plan_id")
    steps = value.get("steps")
    require(isinstance(steps, list) and steps,
            "plan_steps", "plan requires steps")
    identifiers = set()
    for step in steps:
        require(isinstance(step, dict), "step_type", "plan step must be an object")
        step_id = step.get("step_id")
        require(isinstance(step_id, str) and step_id and step_id not in identifiers,
                "step_id", "step_id is missing or duplicated")
        identifiers.add(step_id)
        validate_digest(step.get("idempotency_key"), "idempotency_key")
        validate_digest(step.get("chart_digest"), "chart_digest")
        validate_digest(step.get("descriptor_digest"), "descriptor_digest")
        descriptor = step.get("descriptor")
        require(isinstance(descriptor, str)
                and descriptor.startswith("component_descriptors/")
                and ".." not in descriptor,
                "descriptor_path", "plan descriptor path is unsafe")
        validate_digest(step.get("target_generation"), "target_generation")
        validate_digest(step.get("effective_values_digest"), "effective_values_digest")
    if schema_version == 1:
        _validate_plan_v1(value)
    else:
        _validate_plan_v2(value)
    _scan_secret_values(value, "plan")
    return copy.deepcopy(value)

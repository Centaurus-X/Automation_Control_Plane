"""Pure deterministic multi-provider and multi-cluster plan construction."""

import copy
import pathlib

from automation_fleet.canonical import digest_file, digest_tree, digest_value, read_json
from automation_fleet.dependency_graph import stable_topological_order
from automation_fleet.placement import rollout_waves, select_targets
from automation_fleet.validation import (
    RUNTIME_PROFILE_NODE_IDS,
    WRITE_OWNER_PROVIDERS,
    require,
    validate_catalog,
    validate_desired_state,
    validate_inventory,
    validate_plan,
    validate_policy,
)


OPERATIONS = (
    "discover",
    "validate_target",
    "render",
    "diff",
    "preflight",
    "backup",
    "apply",
    "wait_ready",
    "verify",
)


def _resolve_chart(root, relative):
    base = pathlib.Path(root).resolve()
    charts = (base / "deploy" / "helm").resolve()
    path = (base / relative).resolve()
    require(path == charts or charts in path.parents,
            "chart_escape", "chart path escapes the Helm directory")
    require(path.is_dir(), "chart_missing", "chart directory is missing: " + relative)
    require((path / "Chart.yaml").is_file(),
            "chart_missing", "Chart.yaml is missing: " + relative)
    return path


def _resolve_descriptor(root, component_id, relative, catalog_descriptor):
    base = pathlib.Path(root).resolve()
    descriptors = (base / "component_descriptors").resolve()
    path = (base / relative).resolve()
    require(descriptors in path.parents,
            "descriptor_escape", "descriptor path escapes component_descriptors")
    require(path.is_file(),
            "descriptor_missing", "component descriptor is missing: " + relative)
    value = read_json(path)
    require(value.get("schema_version") == 2,
            "descriptor_schema", "component descriptor schema_version must be 2")
    require(value.get("component_id") == component_id,
            "descriptor_identity", "component descriptor identity mismatch")
    descriptor_dependencies = value.get("dependencies", {})
    catalog_dependencies = catalog_descriptor.get("dependencies", {})
    for group in ("hard_deploy", "hard_runtime", "soft_runtime", "management",
                  "observability"):
        expected = sorted(catalog_dependencies.get(group, []))
        observed = sorted(descriptor_dependencies.get(group, []))
        require(observed == expected,
                "descriptor_dependency_drift",
                component_id + " " + group + " differs from the release catalog")
    return path, value


def _selected_components(catalog, desired):
    selected = []
    for component_id in sorted(desired["components"]):
        if desired["components"][component_id]["enabled"]:
            selected.append(component_id)
    return stable_topological_order(catalog["components"], selected)


def _required_capabilities(descriptors):
    required = set()
    for descriptor in descriptors.values():
        placement = descriptor.get("placement_contract", {})
        required.update(placement.get("required_capabilities", []))
    return sorted(required)


def _effective_values(target, component_id, source):
    values = copy.deepcopy(source)
    if component_id in {
            "mqtt", "master", "proxy", "mcp-controller", "automation-system"}:
        supplied_site = values.get("siteId")
        require(supplied_site in {None, target["site_id"]},
                "target_site_mismatch",
                component_id + " siteId conflicts with target " + target["target_id"])
        values["siteId"] = target["site_id"]
    if component_id == "automation-system":
        require(target["site_id"] == "site-main",
                "runtime_site_identity",
                "the unchanged v0101 Kubernetes profiles are bound to site-main")
        automation_profile = values.get("automationProfile", "worker_simulation")
        expected_node_id = RUNTIME_PROFILE_NODE_IDS[automation_profile]
        supplied_node_id = values.get("nodeId")
        require(supplied_node_id in {None, expected_node_id},
                "runtime_node_identity",
                automation_profile + " requires nodeId " + expected_node_id)
        values["automationProfile"] = automation_profile
        values["nodeId"] = expected_node_id
        mqtt = values.setdefault("mqtt", {})
        expected_mqtt = automation_profile != "standalone"
        require(mqtt.get("enabled") in {None, expected_mqtt},
                "runtime_mqtt_profile",
                automation_profile + " has a fixed MQTT enablement contract")
        mqtt["enabled"] = expected_mqtt
    if component_id == "cluster-agent":
        supplied_target = values.get("targetId")
        supplied_cluster = values.get("clusterId")
        require(supplied_target in {None, target["target_id"]},
                "agent_target_identity",
                "cluster-agent targetId conflicts with placement target")
        require(supplied_cluster in {None, target["cluster_id"]},
                "agent_cluster_identity",
                "cluster-agent clusterId conflicts with placement target")
        values["targetId"] = target["target_id"]
        values["clusterId"] = target["cluster_id"]
    return values


def _step(target, component_id, descriptor, values, operation, order,
          chart_digest, descriptor_digest, inventory_digest, desired_digest,
          policy_digest, policy):
    provider = WRITE_OWNER_PROVIDERS[target["write_owner"]]
    step_id = "{:04d}/{}/{}/{}/{}".format(
        target.get("wave", 0),
        target["target_id"],
        component_id,
        order,
        operation,
    )
    target_generation = digest_value(target)
    effective_values_digest = digest_value(values)
    identity = {
        "step_id": step_id,
        "target_id": target["target_id"],
        "component_id": component_id,
        "provider": provider,
        "execution_mode": target["execution_mode"],
        "operation": operation,
        "chart_digest": chart_digest,
        "descriptor_digest": descriptor_digest,
        "inventory_digest": inventory_digest,
        "desired_digest": desired_digest,
        "policy_digest": policy_digest,
        "target_generation": target_generation,
        "effective_values_digest": effective_values_digest,
    }
    result = {
        "step_id": step_id,
        "order": order,
        "wave": target.get("wave", 0),
        "target_id": target["target_id"],
        "site_id": target["site_id"],
        "failure_domain": target["failure_domain"],
        "cluster_id": target["cluster_id"],
        "context": target["context"],
        "namespace": target["namespace"],
        "write_owner": target["write_owner"],
        "execution_mode": target["execution_mode"],
        "target_generation": target_generation,
        "component_id": component_id,
        "provider": provider,
        "provider_contract_version": 2,
        "field_manager": policy["field_manager"],
        "release_name": descriptor["release_name"],
        "chart": descriptor["chart"],
        "chart_digest": chart_digest,
        "descriptor": descriptor["descriptor"],
        "descriptor_digest": descriptor_digest,
        "values": copy.deepcopy(values),
        "effective_values_digest": effective_values_digest,
        "operation": operation,
        "idempotency_key": digest_value(identity),
        "timeout_s": int(descriptor.get("timeout_s", 600)),
    }
    if target["execution_mode"] == "agent":
        result["agent_task_timeout_s"] = min(
            int(target.get("agent_task_timeout_s", 1200)),
            int(policy.get("max_agent_task_wait_s", 1800)),
        )
    if target["write_owner"].startswith("gitops_"):
        result["gitops"] = copy.deepcopy(target["gitops"])
    return result


def _risk_summary(components, targets, descriptors):
    risks = []
    if any(component in {"postgresql", "master", "proxy"} for component in components):
        risks.append({
            "code": "database_schema_not_auto_reversible",
            "severity": "high",
            "mitigation": "external_backup_and_manual_restore_contract",
        })
    if any(target["execution_mode"] == "agent" for target in targets):
        risks.append({
            "code": "remote_agent_execution",
            "severity": "medium",
            "mitigation": "signed_task_lease_fencing_and_timeout",
        })
    if any(target["write_owner"].startswith("gitops_") for target in targets):
        risks.append({
            "code": "asynchronous_gitops_reconciliation",
            "severity": "medium",
            "mitigation": "single_write_owner_and_live_verify",
        })
    irreversible = sorted(
        component_id for component_id, descriptor in descriptors.items()
        if descriptor.get("rollback_contract", {}).get("application_reversible") is False)
    level = "low"
    if risks:
        level = "high" if any(item["severity"] == "high" for item in risks) else "medium"
    return {
        "level": level,
        "risks": risks,
        "irreversible_components": irreversible,
    }


def build_plan(root, inventory_value, catalog_value, desired_value, policy_value=None):
    inventory = validate_inventory(inventory_value)
    catalog = validate_catalog(catalog_value)
    desired = validate_desired_state(desired_value, catalog)
    policy = validate_policy(policy_value or {
        "schema_version": 1,
        "require_approval": True,
        "require_distinct_approver": True,
        "allow_database_automatic_rollback": False,
        "allow_kubernetes_hard_rt": False,
        "allowed_write_owners": ["helm", "gitops_argocd", "gitops_flux"],
        "max_agent_task_wait_s": 1800,
        "field_manager": "automation-fleet-v0028-r1",
    })
    components = _selected_components(catalog, desired)
    descriptor_values = {}
    chart_digests = {}
    descriptor_digests = {}
    for component_id in components:
        catalog_descriptor = catalog["components"][component_id]
        chart_path = _resolve_chart(root, catalog_descriptor["chart"])
        chart_digests[component_id] = digest_tree(chart_path)
        descriptor_path, descriptor_value = _resolve_descriptor(
            root, component_id, catalog_descriptor["descriptor"], catalog_descriptor)
        descriptor_values[component_id] = descriptor_value
        descriptor_digests[component_id] = digest_file(descriptor_path)
    targets, placement_decisions = select_targets(
        inventory, desired["placement"], _required_capabilities(descriptor_values))
    observe_only = sorted(
        target["target_id"] for target in targets
        if target["write_owner"] == "observe_only")
    require(not observe_only, "observe_only_target",
            "deployment plan cannot mutate observe-only targets: " + ",".join(observe_only))
    disallowed_owners = sorted({
        target["write_owner"] for target in targets
        if target["write_owner"] not in policy["allowed_write_owners"]})
    require(not disallowed_owners, "write_owner_policy",
            "write owners forbidden by policy: " + ",".join(disallowed_owners))
    for target in targets:
        provider = WRITE_OWNER_PROVIDERS[target["write_owner"]]
        for component_id in components:
            supported = descriptor_values[component_id].get("providers", [])
            require(provider in supported,
                    "provider_unsupported",
                    component_id + " does not support provider " + provider)
    if "automation-fleet" in components:
        self_managed_targets = sorted(
            target["target_id"] for target in targets
            if target["context"] == "in-cluster")
        require(not self_managed_targets,
                "in_cluster_self_management",
                "Fleet self-upgrade requires an external management context: "
                + ",".join(self_managed_targets))
    if "cluster-agent" in components:
        invalid_bootstrap_targets = sorted(
            target["target_id"] for target in targets
            if target["context"] == "in-cluster"
            or target["execution_mode"] != "direct")
        require(not invalid_bootstrap_targets,
                "agent_bootstrap_context",
                "cluster-agent bootstrap requires a direct external context: "
                + ",".join(invalid_bootstrap_targets))
    inventory_digest = digest_value(inventory)
    catalog_digest = digest_value(catalog)
    desired_digest = digest_value(desired)
    policy_digest = digest_value(policy)
    steps = []
    order = 0
    for target in targets:
        for component_id in components:
            descriptor = catalog["components"][component_id]
            values = _effective_values(
                target, component_id,
                desired["components"][component_id]["values"])
            for operation in OPERATIONS:
                order += 1
                steps.append(_step(
                    target,
                    component_id,
                    descriptor,
                    values,
                    operation,
                    order,
                    chart_digests[component_id],
                    descriptor_digests[component_id],
                    inventory_digest,
                    desired_digest,
                    policy_digest,
                    policy,
                ))
    providers = sorted({step["provider"] for step in steps})
    per_wave = bool(desired.get("rollout", {}).get("approval_per_wave", True))
    unsigned = {
        "schema_version": 2,
        "release_id": catalog["release_id"],
        "deployment_id": desired["deployment_id"],
        "environment_id": inventory["environment_id"],
        "inventory_digest": inventory_digest,
        "catalog_digest": catalog_digest,
        "desired_digest": desired_digest,
        "policy_digest": policy_digest,
        "component_descriptor_digests": descriptor_digests,
        "provider": providers[0] if len(providers) == 1 else "mixed",
        "provider_contract_version": 2,
        "component_order": components,
        "target_order": [target["target_id"] for target in targets],
        "placement": copy.deepcopy(desired["placement"]),
        "placement_decisions": placement_decisions,
        "rollout": copy.deepcopy(desired.get("rollout", {})),
        "waves": rollout_waves(targets),
        "risk_summary": _risk_summary(components, targets, descriptor_values),
        "approval_required": bool(policy.get("require_approval", True)),
        "approval_policy": {
            "distinct_principal": bool(policy.get("require_distinct_approver", True)),
            "per_wave": per_wave,
        },
        "steps": steps,
    }
    plan = copy.deepcopy(unsigned)
    plan["plan_id"] = digest_value(unsigned)
    validate_plan(plan)
    return plan


def plan_summary(plan):
    components = sorted({step["component_id"] for step in plan["steps"]})
    targets = sorted({step["target_id"] for step in plan["steps"]})
    waves = sorted({step["wave"] for step in plan["steps"]})
    providers = sorted({step["provider"] for step in plan["steps"]})
    return {
        "plan_id": plan["plan_id"],
        "deployment_id": plan["deployment_id"],
        "components": components,
        "targets": targets,
        "waves": waves,
        "providers": providers,
        "steps": len(plan["steps"]),
        "approval_required": plan["approval_required"],
        "risk_summary": copy.deepcopy(plan.get("risk_summary", {})),
        "placement_decisions": copy.deepcopy(plan.get("placement_decisions", [])),
    }

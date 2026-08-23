"""Planner determinism, dependency and policy tests."""

from automation_fleet.planner import build_plan
from automation_fleet.validation import validate_plan, validate_policy
from common import catalog, clone, desired_state, inventory, policy, release_root


def _expect_value_error(action, expected):
    try:
        action()
    except ValueError as exc:
        assert expected in str(exc), str(exc)
        return
    raise AssertionError("expected ValueError containing " + expected)


def _build(desired=None, catalog_value=None):
    return build_plan(
        release_root(),
        inventory(),
        catalog_value or catalog(),
        desired or desired_state(),
        policy(),
    )


def _hard_rt_plan():
    desired = desired_state()
    desired["components"]["automation-system"]["values"]["runtimeProfile"] = "c_hard_rt_linux"
    return _build(desired=desired)


def _secret_plan():
    desired = desired_state()
    desired["components"]["master"]["values"]["password"] = "forbidden-value"
    return _build(desired=desired)


def _api_key_plan():
    desired = desired_state()
    desired["components"]["proxy"]["values"]["apiKey"] = "forbidden-value"
    return _build(desired=desired)


def _cycle_plan():
    catalog_value = clone(catalog())
    catalog_value["components"]["postgresql"]["dependencies"]["hard_deploy"] = ["master"]
    return _build(catalog_value=catalog_value)


def _missing_dependency_plan():
    desired = desired_state(all_components=False)
    desired["components"]["master"]["enabled"] = True
    return _build(desired=desired)


def _observe_only_plan():
    inventory_value = inventory()
    inventory_value["targets"][0]["write_owner"] = "observe_only"
    return build_plan(
        release_root(), inventory_value, catalog(),
        desired_state(all_components=False), policy())


def _wrong_runtime_digest_plan():
    desired = desired_state()
    desired["components"]["automation-system"]["values"]["runtimeArchiveDigest"] = (
        "sha256:" + ("2" * 64))
    return _build(desired=desired)


def _descriptor_dependency_drift_plan():
    catalog_value = clone(catalog())
    catalog_value["components"]["mqtt"]["dependencies"]["soft_runtime"] = ["master"]
    return _build(catalog_value=catalog_value)


def _duplicate_dependency_plan():
    catalog_value = clone(catalog())
    catalog_value["components"]["master"]["dependencies"]["hard_deploy"] = [
        "mqtt", "mqtt", "postgresql"]
    return _build(catalog_value=catalog_value)


def _runtime_site_identity_plan():
    inventory_value = inventory()
    inventory_value["targets"][0]["site_id"] = "site-other"
    return build_plan(
        release_root(), inventory_value, catalog(), desired_state(), policy())


def _runtime_node_identity_plan():
    desired = desired_state()
    values = desired["components"]["automation-system"]["values"]
    values["runtimeProfile"] = "kubernetes_soft_rt"
    values["automationProfile"] = "worker_fn02"
    values["nodeId"] = "fn-01"
    return _build(desired=desired)


def _runtime_profile_mapping_plan():
    desired = desired_state()
    values = desired["components"]["automation-system"]["values"]
    values["automationProfile"] = "worker_fn02"
    return _build(desired=desired)


def _standalone_mqtt_plan():
    desired = desired_state()
    values = desired["components"]["automation-system"]["values"]
    values["automationProfile"] = "standalone"
    values["mqtt"] = {"enabled": True}
    return _build(desired=desired)


def _tagged_repository_plan():
    desired = desired_state()
    desired["components"]["master"]["values"]["image"]["repository"] = "master:latest"
    return _build(desired=desired)


def _in_cluster_self_management_plan():
    inventory_value = inventory()
    inventory_value["targets"] = [inventory_value["targets"][0]]
    inventory_value["targets"][0]["context"] = "in-cluster"
    desired = desired_state(all_components=False)
    desired["placement"]["target_ids"] = [inventory_value["targets"][0]["target_id"]]
    desired["components"]["postgresql"]["enabled"] = False
    desired["components"]["automation-fleet"]["enabled"] = True
    return build_plan(
        release_root(), inventory_value, catalog(), desired, policy())


def _selector_plan():
    inventory_value = inventory()
    for target in inventory_value["targets"]:
        target["labels"] = {"environment": "test"}
    desired = desired_state(all_components=False)
    desired["placement"] = {"selector": {
        "target_kind": "kubernetes",
        "required_capabilities": ["network-policy"],
        "match_labels": {"environment": "test"},
        "max_targets": 1,
    }}
    return build_plan(
        release_root(), inventory_value, catalog(), desired, policy())


def main():
    policy_value = policy()
    policy_value["secret_policy"] = "references-only"
    assert validate_policy(policy_value)["secret_policy"] == "references-only"
    first = _build()
    second = _build()
    assert first == second
    assert first["plan_id"] == second["plan_id"]
    assert first["target_order"] == ["cluster-a", "cluster-b"]
    order = first["component_order"]
    assert order.index("mqtt") < order.index("master")
    assert order.index("postgresql") < order.index("master")
    assert order.index("master") < order.index("mcp-controller")
    assert all("timestamp" not in step for step in first["steps"])
    assert first["schema_version"] == 2
    assert first["provider_contract_version"] == 2
    assert len(first["steps"]) == 2 * 8 * 9
    assert [item["wave"] for item in first["waves"]] == [1, 2]
    assert first["waves"][0]["canary_target_ids"] == ["cluster-a"]
    assert all(step["execution_mode"] == "direct" for step in first["steps"])
    selector = _selector_plan()
    assert selector["target_order"] == ["cluster-a"]
    excluded = [item for item in selector["placement_decisions"]
                if item["target_id"] == "cluster-b"][0]
    assert excluded["reasons"] == ["selector_max_targets"]
    tampered = clone(first)
    tampered["steps"][0]["operation"] = "verify"
    try:
        validate_plan(tampered)
    except ValueError as exc:
        assert "operation_order" in str(exc)
    else:
        raise AssertionError("reordered plan operations must fail closed")
    _expect_value_error(_hard_rt_plan, "runtime_profile")
    _expect_value_error(_secret_plan, "inline_secret_forbidden")
    _expect_value_error(_api_key_plan, "inline_secret_forbidden")
    _expect_value_error(_cycle_plan, "dependency_cycle")
    _expect_value_error(_missing_dependency_plan, "hard_dependency_missing")
    _expect_value_error(_observe_only_plan, "observe_only_target")
    _expect_value_error(_wrong_runtime_digest_plan, "runtime_archive_digest")
    _expect_value_error(_descriptor_dependency_drift_plan, "descriptor_dependency_drift")
    _expect_value_error(_duplicate_dependency_plan, "duplicate_dependency")
    _expect_value_error(_runtime_site_identity_plan, "runtime_site_identity")
    _expect_value_error(_runtime_node_identity_plan, "runtime_node_identity")
    _expect_value_error(_runtime_profile_mapping_plan, "runtime_profile_mapping")
    _expect_value_error(_standalone_mqtt_plan, "runtime_mqtt_profile")
    _expect_value_error(_tagged_repository_plan, "image_repository")
    _expect_value_error(_in_cluster_self_management_plan, "in_cluster_self_management")
    print("PASS planner determinism, dependency DAG, target ownership, archive and RT policy")


if __name__ == "__main__":
    main()

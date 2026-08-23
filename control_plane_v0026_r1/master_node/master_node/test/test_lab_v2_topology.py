# -*- coding: utf-8 -*-

"""The generated lab fixture exercises real Cross-Worker dependencies."""

import json
import os

from src.library._generic_system_contract_library import config_graph, entity_registry, shard_library
from src.core.shard_materializer import create_shard_materializer
from src.core.scope_activation import plan_activations


REMOTE_CONTROLLER = "d11032fa-33db-56de-a4ed-7e16cca3ded7"
SIMULATOR_CONTROLLER = "62671112-766c-59ed-b0ff-30a49d48f747"


def main():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application",
        "initial_config_v9.json"))
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    assert len(entity_registry.decompose(config)) == 198
    graph = config_graph.build_graph(config)
    assert graph["controller_owners"][REMOTE_CONTROLLER] == "fn-02"
    for rule_id in ("3", "6"):
        assert graph["rules"][rule_id]["scope"] == shard_library.SCOPE_CROSS_WORKER
        assert graph["rules"][rule_id]["node_ids"] == ("fn-01", "fn-02")
    scopes = create_shard_materializer()["materialize"](config, 1)["scopes"]
    simulator = next(
        row for row in config["virtual_controllers"]
        if row["controller_uuid"] == SIMULATOR_CONTROLLER)
    assert simulator["virt_active"] is True
    assert (simulator["host"], simulator["port"]) == ("192.168.0.3", 5020)
    node_simulator = next(
        row for row in scopes["node:fn-01"]["config"]["virtual_controllers"]
        if row["controller_uuid"] == SIMULATOR_CONTROLLER)
    assert (node_simulator["host"], node_simulator["port"]) == (
        "192.168.0.3", 5020)
    assert [row["rule_id"] for row in scopes["node:fn-01"]["config"]["rules"]] \
        == [1, 2, 10, 11]
    assert [row["rule_id"] for row in scopes["node:fn-02"]["config"]["rules"]] \
        == [4]
    assert [row["rule_id"] for row in scopes[
        "cross:fn-01.default"]["config"]["rules"]] == [3, 6]
    state_1 = next(row for row in config["process_states"] if row["state_id"] == 1)
    assert state_1["mode_id"] == 2
    assert state_1["control_strategy"]["type"] == "PID"
    for rule_id, state_id in ((1, 1), (2, 11)):
        rule = next(row for row in config["rules"] if row["rule_id"] == rule_id)
        assert all(
            not (
                condition.get("src") == "state"
                and str(condition.get("state_id")) == str(state_id)
            )
            for condition in rule["when"]
        )
    activations = plan_activations(scopes, {})
    assert len(activations) == 20
    assert "process:1" not in activations
    assert activations["rule:3"]["active_scope"] == "cross:fn-01.default"
    print("PASS lab_v2 Cross ownership, activation and native fn-01 Modbus endpoint")


if __name__ == "__main__":
    main()

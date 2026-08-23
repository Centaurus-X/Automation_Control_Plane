# -*- coding: utf-8 -*-

"""Owner-bound latest-value Cross routing without rule evaluation."""

import json
import os
import time

from src.library._generic_system_contract_library import config_graph
from src.core.cross_fact_router import create_cross_fact_router
from src.core.shard_materializer import create_shard_materializer


def main():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application", "_legacy",
        "initial_config.json"))
    with open(path, "r", encoding="utf-8") as handle:
        config = config_graph.migrate_legacy_config(json.load(handle))["config"]
    scopes = create_shard_materializer()["materialize"](config, 1)["scopes"]
    sent = []

    def publish(node_id, envelope):
        sent.append((node_id, envelope))
        return True

    router = create_cross_fact_router(max_signal_age_s=30.0, publisher=publish)
    owners = config_graph.build_graph(config)["controller_owners"]
    count = router["replace_dependencies"](scopes, owners)
    assert count > 0
    identity = next(iter(router["context"]["dependencies"]))
    now = time.time()
    fact = {
        "controller_uuid": identity[0], "sensor_id": identity[1],
        "value": 12.5, "quality": "good", "sequence": 1,
        "timestamp": now,
    }
    result = router["route"](
        fact, source_node=owners[identity[0]], now_s=now)
    assert result["ok"] and result["routed"] >= 1
    assert all(node_id == "fn-01" for node_id, _envelope in sent)
    duplicate = router["route"](
        fact, source_node=owners[identity[0]], now_s=now)
    assert duplicate["ok"] and duplicate["duplicate"]
    rejected = router["route"](fact, source_node="fn-99", now_s=now)
    assert not rejected["ok"] and rejected["code"] == "ownership"
    invalid = dict(fact, sequence=2, timestamp="not-a-timestamp")
    rejected = router["route"](
        invalid, source_node=owners[identity[0]], now_s=now)
    assert not rejected["ok"] and rejected["code"] == "invalid_payload"
    print("PASS ownership-checked Cross fact routing")


if __name__ == "__main__":
    main()

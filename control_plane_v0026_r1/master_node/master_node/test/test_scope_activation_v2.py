# -*- coding: utf-8 -*-

"""Execution-scope activation, legacy-domain alias and v2 epoch fencing."""

from src.core.master_runtime import _validate_operation_scope
from src.core.scope_activation import plan_activations, validate_scope_intent


def main():
    snapshot = {
        "scope": "cross:fn-01.default",
        "scope_kind": "cross",
        "owner_node_id": "fn-01",
        "scope_revision": 3,
        "fingerprint": "a" * 64,
        "execution_epoch": 7,
    }
    requested = []

    def get_scope(scope):
        requested.append(scope)
        return snapshot if scope == snapshot["scope"] else None

    legacy = _validate_operation_scope(
        {"get_scope": get_scope}, "fn-01", {
            "execution_scope": "cross",
            "cross_domain_id": "cross-fn-01-default",
            "cross_revision": 3,
            "cross_fingerprint": "a" * 64,
            "distributed_protocol": "automation.distributed.v1",
        })
    assert legacy["ok"] and requested == ["cross:fn-01.default"]
    missing_epoch = validate_scope_intent(snapshot, "fn-01", {
        "distributed_protocol": "automation.distributed.v2",
        "cross_revision": 3,
    })
    assert not missing_epoch["ok"] and missing_epoch["code"] == "stale_execution_epoch"
    current = validate_scope_intent(snapshot, "fn-01", {
        "distributed_protocol": "automation.distributed.v2",
        "cross_revision": 3,
        "execution_epoch": 7,
    })
    assert current["ok"]
    stale = validate_scope_intent(snapshot, "fn-01", {
        "distributed_protocol": "automation.distributed.v2",
        "cross_revision": 3,
        "execution_epoch": 6,
    })
    assert not stale["ok"] and stale["code"] == "stale_execution_epoch"
    initial = plan_activations({"node:fn-01": {
        "config": {"rules": [{"rule_id": 1}]},
    }}, {})
    assert initial["rule:1"]["execution_epoch"] == 1
    stable = plan_activations({"node:fn-01": {
        "config": {"rules": [{"rule_id": 1}]},
    }}, initial)
    assert stable["rule:1"]["execution_epoch"] == 1
    shared_process = plan_activations({
        "node:fn-01": {"config": {
            "processes": [{"p_id": 1}],
            "process_states": [{"state_id": 1, "p_id": 1}],
        }},
        "node:fn-02": {"config": {
            "processes": [{"p_id": 1}],
            "process_states": [{"state_id": 2, "p_id": 1}],
        }},
    }, {})
    assert set(shared_process) == {"state:1", "state:2"}
    administrative_process = plan_activations({
        "node:fn-01": {"config": {
            "processes": [{"p_id": 9, "owner_node_id": "fn-01"}],
        }},
        "cross:fn-01.default": {"config": {
            "processes": [{"p_id": 9, "owner_node_id": "fn-01"}],
        }},
    }, {})
    assert administrative_process == {}
    try:
        plan_activations({"cross:fn-01.default": {
            "config": {"rules": [{"rule_id": 1}]},
        }}, initial)
    except ValueError as exc:
        assert "staged READY/activate/retire" in str(exc)
    else:
        raise AssertionError("unsafe immediate scope migration was accepted")
    print("PASS legacy alias, epoch fences and fail-closed scope migration gate")


if __name__ == "__main__":
    main()

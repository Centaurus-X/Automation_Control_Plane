# -*- coding: utf-8 -*-

"""Leader-promotion bootstrap, fail-closed readiness and Node-only scope tests."""

import copy
import json
import os
import tempfile
import threading
import time

from unittest import mock

from src.core.cluster_orchestrator import _notify_role, create_cluster_orchestrator
from src.core.master_runtime import (
    _authority_config_ready,
    _refresh_standby_authority_state,
    _seed_if_leader,
    _set_authority_services,
)
from src.core.scope_activation import plan_activations
from src.core.shard_materializer import create_shard_materializer
from src.library.master_heap_store import create_master_heap_store


def _config_without_cross():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application",
        "initial_config_v9.json"))
    with open(path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    config["rules"] = [
        rule for rule in config["rules"]
        if rule.get("deployment_status") != "active_cross"
    ]
    return config


def _full_config():
    path = os.path.abspath(os.path.join(
        os.path.dirname(__file__), "..", "_config", "application",
        "initial_config_v9.json"))
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def _write_config(directory, config):
    path = os.path.join(directory, "initial.json")
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle)
    return path


def _context(config_path, **kwargs):
    state = {"global": None, "scopes": {}, "activations": {}}
    counters = {"seeds": 0, "starts": 0, "stops": 0, "cross_refreshes": 0}
    heap = create_master_heap_store()
    materializer = create_shard_materializer(profile="lab_v2")

    def config_reader():
        global_snapshot = state.get("global")
        return copy.deepcopy(global_snapshot) if global_snapshot else None

    def seed(config, **_seed_kwargs):
        counters["seeds"] += 1
        if kwargs.get("exception_seed"):
            raise ValueError("execution entity is assigned to multiple scopes")
        if kwargs.get("broken_seed"):
            return {"ok": True, "status": "seeded_without_state"}
        materialized = materializer["materialize"](config, 1)
        state["activations"] = plan_activations(
            materialized["scopes"], state["activations"])
        state["global"] = {
            "revision": 1,
            "global_revision": 1,
            "fingerprint": materialized["global_fingerprint"],
            "config_data": copy.deepcopy(config),
        }
        state["scopes"] = copy.deepcopy(materialized["scopes"])
        return {"ok": True, "status": "seeded", "revision": 1}

    def load_state(_site_id):
        return copy.deepcopy(state)

    def get_scope_snapshots():
        return copy.deepcopy(state["scopes"])

    def replace_dependencies(_scopes, _owners):
        counters["cross_refreshes"] += 1
        return True

    def start_legacy():
        counters["starts"] += 1
        return True

    def stop_legacy(**_stop_kwargs):
        counters["stops"] += 1
        return True

    def no_legacy_config(_site_id):
        return None

    def renew_as_standby():
        return {"ok": True, "role": "standby"}

    context = {
        "config": {
            "site_id": "site_main",
            "initial_config_path": config_path,
        },
        "lock": threading.RLock(),
        "role_lock": threading.RLock(),
        "state": "starting",
        "last_error": None,
        "bootstrap": {
            "state": "not_started", "attempts": 0, "last_status": None,
            "last_error": None, "global_revision": 0,
            "required_node_scopes": (),
        },
        "heap": heap,
        "config_reader": config_reader,
        "config_authority": {"seed": seed},
        "store": {"authority": {
            "load_state": load_state,
            "get_scope_snapshots": get_scope_snapshots,
            "get_legacy_config": no_legacy_config,
        }},
        "cross_router": {"replace_dependencies": replace_dependencies},
        "legacy_mqtt": {"start": start_legacy, "stop": stop_legacy},
        "orchestrator": {"renew_now": renew_as_standby},
    }
    return context, state, counters


def _test_promotion_seeds_node_scopes_without_cross_requirement():
    config = _config_without_cross()
    with tempfile.TemporaryDirectory() as directory:
        context, state, counters = _context(_write_config(directory, config))
        waiting = _seed_if_leader(context)
        assert waiting["ok"] and waiting["status"] == "standby"
        assert context["bootstrap"]["state"] == "waiting_for_leader"
        assert not _authority_config_ready(context)
        assert _set_authority_services(context, "leader")
    assert counters["seeds"] == 1
    assert counters["starts"] == 1
    assert counters["cross_refreshes"] == 1
    assert set(state["scopes"]) == {"node:fn-01", "node:fn-02"}
    assert _authority_config_ready(context)
    assert context["bootstrap"]["state"] == "ready"
    assert context["bootstrap"]["required_node_scopes"] == (
        "node:fn-01", "node:fn-02")
    assert _set_authority_services(context, "leader")
    assert counters["seeds"] == 1


def _test_incomplete_seed_fails_closed():
    config = _config_without_cross()
    with tempfile.TemporaryDirectory() as directory:
        context, _state, counters = _context(
            _write_config(directory, config), broken_seed=True)
        assert not _set_authority_services(context, "leader")
    assert counters["starts"] == 0
    assert counters["stops"] == 1
    assert context["bootstrap"]["state"] == "failed"
    assert not _authority_config_ready(context)
    assert str(context["last_error"]).startswith("authority bootstrap:")


def _test_corrupt_heap_fingerprint_is_not_ready():
    config = _config_without_cross()
    with tempfile.TemporaryDirectory() as directory:
        context, _state, _counters = _context(_write_config(directory, config))
        assert _set_authority_services(context, "leader")
        global_snapshot = context["heap"]["get_global"]()
        global_snapshot["fingerprint"] = "f" * 64
        scopes = {
            scope: context["heap"]["get_scope"](scope)
            for scope in context["heap"]["get_scope_heads"]()
        }
        context["heap"]["replace"](global_snapshot, scopes)
        assert not _authority_config_ready(context)


def _test_standby_and_promotion_reload_a_newer_durable_revision():
    config = _config_without_cross()
    with tempfile.TemporaryDirectory() as directory:
        context, state, counters = _context(_write_config(directory, config))
        assert _set_authority_services(context, "leader")
        first_fingerprint = state["global"]["fingerprint"]
        updated = copy.deepcopy(config)
        updated["nodes"][0]["description"] = "durably updated by another leader"
        materialized = create_shard_materializer(profile="lab_v2")["materialize"](
            updated, 2, previous_heads=state["scopes"],
            previous_snapshots=state["scopes"],
        )
        state["global"] = {
            "revision": 2,
            "global_revision": 2,
            "fingerprint": materialized["global_fingerprint"],
            "config_data": updated,
        }
        state["scopes"] = copy.deepcopy(materialized["scopes"])
        assert _refresh_standby_authority_state(context)["ok"]
        assert context["heap"]["get_global"]()["global_revision"] == 2
        assert context["heap"]["get_global"]()["fingerprint"] != first_fingerprint
        assert _set_authority_services(context, "leader")
    assert counters["seeds"] == 1
    assert _authority_config_ready(context)


def _test_full_lab_cross_fixture_bootstraps_atomically():
    config = _full_config()
    with tempfile.TemporaryDirectory() as directory:
        context, state, counters = _context(_write_config(directory, config))
        assert _set_authority_services(context, "leader")
    assert counters["seeds"] == 1
    assert set(state["scopes"]) == {
        "node:fn-01", "node:fn-02", "cross:fn-01.default"}
    assert len(state["activations"]) == 20
    assert "process:1" not in state["activations"]
    assert state["activations"]["rule:3"]["active_scope"] \
        == "cross:fn-01.default"
    assert _authority_config_ready(context)


def _test_seed_exception_is_visible_and_fails_closed():
    config = _full_config()
    with tempfile.TemporaryDirectory() as directory:
        context, _state, counters = _context(
            _write_config(directory, config), exception_seed=True)
        with mock.patch("src.core.master_runtime.LOGGER.exception"):
            assert not _set_authority_services(context, "leader")
    assert counters["starts"] == 0
    assert counters["stops"] == 1
    assert context["bootstrap"]["state"] == "failed"
    assert context["bootstrap"]["last_status"] == \
        "configuration_seed_exception"
    assert "ValueError: execution entity" in context["bootstrap"]["last_error"]
    assert context["last_error"].startswith("authority bootstrap: ValueError:")
    assert not _authority_config_ready(context)


def _test_rejected_role_handler_is_observable():
    def reject_role(*_args, **_kwargs):
        return False

    context = {
        "lock": threading.RLock(),
        "role_change_handler": reject_role,
        "last_error": None,
        "statistics": {"maintenance_errors": 0},
    }
    assert not _notify_role(context, "leader", "standby")
    assert context["last_error"] == "authority role handler rejected leader"
    assert context["statistics"]["maintenance_errors"] == 1


def _test_rejected_promotion_never_runs_leader_maintenance():
    counters = {
        "expired": 0, "published": 0, "recovered": 0, "reconciled": 0,
    }
    handler_called = threading.Event()

    def acquire_or_renew(_site_id, _instance_id, **_kwargs):
        return {
            "ok": True,
            "role": "leader",
            "authority": {
                "authority_term": 7,
                "lease_token": "lease-7",
            },
        }

    def reject_role(*_args, **_kwargs):
        handler_called.set()
        return False

    def recover_uncertain(_site_id, _term):
        counters["recovered"] += 1

    def expire_operations(**_kwargs):
        counters["expired"] += 1
        return 1

    def publish_due_outbox(**_kwargs):
        counters["published"] += 1
        return 1

    def reconcile(_config):
        counters["reconciled"] += 1
        return {"ok": True}

    def config_reader():
        return {"config_data": {}}

    orchestrator = create_cluster_orchestrator(
        site_id="site_main",
        instance_id="master-01",
        leader_eligible=True,
        authority_store={"acquire_or_renew": acquire_or_renew},
        operation_store={"recover_uncertain": recover_uncertain},
        ownership={"reconcile": reconcile},
        config_reader=config_reader,
        publish_due_outbox=publish_due_outbox,
        expire_operations=expire_operations,
        role_change_handler=reject_role,
        loop_interval_s=0.05,
    )
    orchestrator["start"]()
    assert handler_called.wait(1.0)
    time.sleep(0.12)
    snapshot = orchestrator["stop"](join_timeout_s=1.0)
    assert snapshot["role"] == "leader"
    assert not snapshot["authority_services_ready"]
    assert counters == {
        "expired": 0, "published": 0, "recovered": 0, "reconciled": 0,
    }


def _test_startup_renew_recovers_new_term_exactly_once():
    recovered = []
    role_notified = threading.Event()

    def acquire_or_renew(_site_id, _instance_id, **_kwargs):
        return {
            "ok": True,
            "role": "leader",
            "authority": {
                "authority_term": 11,
                "lease_token": "lease-11",
            },
        }

    def accept_role(*_args, **_kwargs):
        role_notified.set()
        return True

    def recover_uncertain(site_id, authority_term):
        recovered.append((site_id, authority_term))

    def no_expiry(**_kwargs):
        return 0

    def no_publish(**_kwargs):
        return 0

    def no_reconcile(_config):
        return {"ok": True}

    def config_reader():
        return {"config_data": {}}

    orchestrator = create_cluster_orchestrator(
        site_id="site_main",
        instance_id="master-01",
        leader_eligible=True,
        authority_store={"acquire_or_renew": acquire_or_renew},
        operation_store={"recover_uncertain": recover_uncertain},
        ownership={"reconcile": no_reconcile},
        config_reader=config_reader,
        publish_due_outbox=no_publish,
        expire_operations=no_expiry,
        role_change_handler=accept_role,
        loop_interval_s=0.05,
    )

    renewed = orchestrator["renew_now"]()
    assert renewed["role"] == "leader"
    assert recovered == [("site_main", 11)]
    assert not orchestrator["snapshot"]()["authority_services_ready"]

    orchestrator["start"]()
    assert role_notified.wait(1.0)
    snapshot = orchestrator["stop"](join_timeout_s=1.0)
    assert snapshot["authority_services_ready"]
    assert recovered == [("site_main", 11)]


def main():
    tests = (
        _test_promotion_seeds_node_scopes_without_cross_requirement,
        _test_incomplete_seed_fails_closed,
        _test_corrupt_heap_fingerprint_is_not_ready,
        _test_standby_and_promotion_reload_a_newer_durable_revision,
        _test_full_lab_cross_fixture_bootstraps_atomically,
        _test_seed_exception_is_visible_and_fails_closed,
        _test_rejected_role_handler_is_observable,
        _test_rejected_promotion_never_runs_leader_maintenance,
        _test_startup_renew_recovers_new_term_exactly_once,
    )
    for test in tests:
        test()
    print("PASS Master bootstrap/readiness and Cross-independent Node scopes")


if __name__ == "__main__":
    main()

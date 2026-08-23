"""Command-line entry point for planning, execution and the Web UI."""

import argparse
import json
import os
import pathlib
import sys

from automation_fleet import agent_protocol, auth, journal
from automation_fleet.api import serve
from automation_fleet.canonical import atomic_json, read_json
from automation_fleet.collector import collect_snapshot
from automation_fleet.configuration import load_configuration, load_documents
from automation_fleet.executor import execute_plan, rollback_plan
from automation_fleet.planner import build_plan, plan_summary
from automation_fleet.validation import validate_catalog, validate_inventory


def _print_json(value):
    print(json.dumps(value, indent=2, ensure_ascii=False, sort_keys=True))


def _parser():
    parser = argparse.ArgumentParser(prog="fleetctl")
    parser.add_argument("--root", required=True)
    parser.add_argument("--config")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("validate")
    plan = commands.add_parser("plan")
    plan.add_argument("--desired")
    plan.add_argument("--output", required=True)
    approve = commands.add_parser("approve")
    approve.add_argument("--plan-id", required=True)
    approve.add_argument("--principal", default="local-operator")
    apply_command = commands.add_parser("apply")
    apply_command.add_argument("--plan", required=True)
    rollback_command = commands.add_parser("rollback")
    rollback_command.add_argument("--plan", required=True)
    status = commands.add_parser("status")
    status.add_argument("--plan-id")
    commands.add_parser("snapshot")
    commands.add_parser("serve")
    return parser


def _context(config):
    return {
        "root": config["root"],
        "config": config,
        "tokens": auth.load_tokens(config),
        "agents": agent_protocol.load_registry(config.get("agent_registry_path")),
        "debug": os.environ.get("FLEET_DEBUG", "0") == "1",
        "instance_id": os.environ.get("FLEET_INSTANCE_ID", "direct-cli"),
    }


def _validate(config):
    documents = load_documents(config)
    validate_inventory(documents["inventory"])
    validate_catalog(documents["catalog"])
    plan = build_plan(
        config["root"],
        documents["inventory"],
        documents["catalog"],
        documents["desired"],
        documents["policy"],
    )
    return {
        "ok": True,
        "release": "v0027-r3",
        "plan_summary": plan_summary(plan),
        "mutations_enabled": config["allow_mutations"],
    }


def _plan(config, desired_path, output):
    documents = load_documents(config)
    desired = read_json(desired_path) if desired_path else documents["desired"]
    plan = build_plan(
        config["root"], documents["inventory"], documents["catalog"], desired,
        documents["policy"],
    )
    database = journal.connect(config["state_dir"])
    try:
        journal.register_plan(database, plan, creator="cli:planner")
    finally:
        database.close()
    atomic_json(output, plan)
    return plan_summary(plan)


def _prepare_service(config):
    validation = _validate(config)
    database = journal.connect(config["state_dir"])
    try:
        audit = journal.verify_audit_chain(database)
    finally:
        database.close()
    return {"validation": validation, "audit": audit}


def main(argv=None):
    arguments = _parser().parse_args(argv)
    config = load_configuration(arguments.root, arguments.config)
    try:
        if arguments.command == "validate":
            result = _validate(config)
        elif arguments.command == "plan":
            result = _plan(config, arguments.desired, arguments.output)
        elif arguments.command == "approve":
            database = journal.connect(config["state_dir"])
            try:
                journal.approve_plan(database, arguments.plan_id, arguments.principal)
            finally:
                database.close()
            result = {"ok": True, "plan_id": arguments.plan_id}
        elif arguments.command == "apply":
            plan = read_json(arguments.plan)
            result = execute_plan(config["root"], config["state_dir"], plan,
                                  allow_mutations=config["allow_mutations"])
        elif arguments.command == "rollback":
            plan = read_json(arguments.plan)
            result = rollback_plan(config["state_dir"], plan,
                                   allow_mutations=config["allow_mutations"])
        elif arguments.command == "status":
            database = journal.connect(config["state_dir"])
            try:
                result = (journal.plan_status(database, arguments.plan_id)
                          if arguments.plan_id else {"plans": journal.list_plans(database)})
            finally:
                database.close()
        elif arguments.command == "snapshot":
            inventory = read_json(config["inventory_path"])
            database = journal.connect(config["state_dir"])
            try:
                agent_rows = journal.list_agents(database)
            finally:
                database.close()
            agent_states = {item["target_id"]: item for item in agent_rows}
            result = collect_snapshot(
                inventory, int(config.get("observe_timeout_s", 30)), agent_states)
        elif arguments.command == "serve":
            _prepare_service(config)
            serve(_context(config))
            return
        else:
            raise ValueError("unknown command")
        _print_json(result)
    except Exception as exc:
        _print_json({"ok": False, "error": str(exc), "type": type(exc).__name__})
        raise SystemExit(1) from exc


if __name__ == "__main__":
    main()

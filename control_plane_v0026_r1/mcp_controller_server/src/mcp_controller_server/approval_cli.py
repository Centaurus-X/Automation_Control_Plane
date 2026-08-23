# -*- coding: utf-8 -*-

"""Local operator CLI for external change approvals."""

import argparse
import json
import os
import sys

from mcp_controller_server.approval_store import (
    approve_plan,
    create_approval_store,
    get_plan,
)
from mcp_controller_server.config import load_config


def _parser():
    parser = argparse.ArgumentParser(description="Approve or inspect MCP change plans")
    parser.add_argument(
        "--config", default=os.environ.get("MCP_CONTROLLER_CONFIG"),
        help="controller configuration JSON",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    approve = subparsers.add_parser("approve", help="approve one exact plan digest")
    approve.add_argument("--plan-id", required=True)
    approve.add_argument("--payload-sha256", required=True)
    approve.add_argument("--approver", required=True)
    status = subparsers.add_parser("status", help="read one plan without its payload")
    status.add_argument("--plan-id", required=True)
    return parser


def main(argv=None):
    arguments = _parser().parse_args(argv)
    config = load_config(arguments.config, check_files=False)
    policy = config["policy"]
    store = create_approval_store(
        policy["state_db"], policy["approval_ttl_s"],
        automatic=policy["approval_mode"] == "automatic",
        max_plans=policy["max_approval_plans"],
    )
    if arguments.command == "approve":
        result = approve_plan(
            store, arguments.plan_id, arguments.payload_sha256, arguments.approver)
    else:
        result = get_plan(store, arguments.plan_id)
    sys.stdout.write(json.dumps(result, sort_keys=True, ensure_ascii=False) + "\n")
    return 0 if result.get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())

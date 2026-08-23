# -*- coding: utf-8 -*-

"""Executable entry point for HTTP, stdio and configuration validation."""

import argparse
import json
import os
import sys

from mcp_controller_server.config import load_config
from mcp_controller_server.stdio_transport import run_stdio


def _parser():
    parser = argparse.ArgumentParser(description="Automation MCP controller server")
    parser.add_argument(
        "--config", default=os.environ.get("MCP_CONTROLLER_CONFIG"),
        help="controller configuration JSON",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)
    subparsers.add_parser("http", help="run authenticated Streamable HTTP")
    subparsers.add_parser("stdio", help="run newline-delimited stdio MCP")
    check = subparsers.add_parser("check-config", help="validate configuration")
    check.add_argument("--skip-runtime-files", action="store_true")
    return parser


def _run_http(config, config_path):
    try:
        import uvicorn
    except ImportError as exc:
        raise RuntimeError("uvicorn is required for HTTP mode") from exc
    if config_path:
        os.environ["MCP_CONTROLLER_CONFIG"] = os.path.abspath(config_path)
    uvicorn.run(
        "mcp_controller_server.http_transport:application",
        host=config["http"]["host"],
        port=config["http"]["port"],
        log_level="info",
        access_log=False,
        lifespan="on",
    )
    return 0


def main(argv=None):
    arguments = _parser().parse_args(argv)
    check_files = not bool(
        arguments.command == "check-config" and arguments.skip_runtime_files)
    try:
        config = load_config(arguments.config, check_files=check_files)
        if arguments.command == "check-config":
            sys.stdout.write(json.dumps({
                "ok": True,
                "instance_id": config["server"]["instance_id"],
                "site_id": config["mqtt"]["site_id"],
                "system_boundary": "mqtt_only",
            }, sort_keys=True) + "\n")
            return 0
        if arguments.command == "stdio":
            return run_stdio(config)
        return _run_http(config, arguments.config or config.get("_config_path"))
    except Exception as exc:
        sys.stderr.write("mcp_controller_server: " + str(exc) + "\n")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())

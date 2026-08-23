# -*- coding: utf-8 -*-

# master_node/src/master_main.py

"""Standalone Master-Node process entry point."""

import logging
import os
import signal
import threading

from functools import partial

from src.core.master_runtime import create_master_runtime
from src.system.bootstrap_system_interface import configure_logging, load_master_config


def _signal_stop(stop_event, signum, _frame):
    logging.getLogger("master_node").info("Shutdown signal received: %s", signum)
    stop_event.set()


def main():
    """Start the Master and block until SIGINT or SIGTERM."""
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    config_path = os.environ.get(
        "MASTER_CONFIG_PATH",
        os.path.join(project_root, "_config", "application", "master_config.json"),
    )
    config = load_master_config(config_path, project_root=project_root)
    logger = configure_logging(config.get("logging") or {})
    stop_event = threading.Event()
    signal.signal(signal.SIGINT, partial(_signal_stop, stop_event))
    signal.signal(signal.SIGTERM, partial(_signal_stop, stop_event))
    runtime = create_master_runtime(config)
    runtime["start"]()
    logger.info(
        "Master started: site=%s instance=%s",
        config["site_id"], config["instance_id"],
    )
    try:
        while not stop_event.wait(1.0):
            continue
    finally:
        runtime["stop"]()
        logger.info("Master stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

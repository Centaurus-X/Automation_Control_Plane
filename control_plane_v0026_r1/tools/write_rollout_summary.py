#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Write the rollout result atomically without including credentials."""

import argparse
import json
import os
import tempfile

from datetime import datetime, timezone
from pathlib import Path


def _write_atomic(path, report):
    path = path.expanduser().resolve()
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="." + path.name + ".",
        dir=str(path.parent),
        text=True,
    )
    temporary = Path(temporary_name)
    try:
        os.fchmod(descriptor, 0o600)
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as stream:
            json.dump(report, stream, indent=2, sort_keys=True)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        path.chmod(0o600)
    finally:
        if temporary.exists():
            temporary.unlink()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--status", choices=("PASSED", "FAILED"), required=True)
    parser.add_argument("--exit-code", type=int, required=True)
    parser.add_argument("--stage", required=True)
    parser.add_argument("--version", required=True)
    parser.add_argument("--revision", required=True)
    parser.add_argument("--runtime-revision", required=True)
    parser.add_argument("--mode", choices=("reuse", "fresh", "upgrade"), required=True)
    parser.add_argument("--site-id", required=True)
    parser.add_argument("--node-id", required=True)
    parser.add_argument("--worker-target", required=True)
    parser.add_argument("--log-directory", required=True)
    arguments = parser.parse_args()

    report = {
        "run_id": arguments.run_id,
        "status": arguments.status,
        "exit_code": arguments.exit_code,
        "stage": arguments.stage,
        "release": {
            "version": arguments.version,
            "revision": arguments.revision,
        },
        "runtime": {
            "version": arguments.version,
            "revision": arguments.runtime_revision,
        },
        "deployment_mode": arguments.mode,
        "site_id": arguments.site_id,
        "node_id": arguments.node_id,
        "worker_target": arguments.worker_target,
        "log_directory": arguments.log_directory,
        "completed_at_utc": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }
    _write_atomic(arguments.output, report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

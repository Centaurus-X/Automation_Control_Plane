#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Minimal dependency-free functional test runner."""

import os
import runpy
import sys
import traceback


def main():
    root = os.path.dirname(os.path.abspath(__file__))
    files = sorted(
        os.path.join(root, name) for name in os.listdir(root)
        if name.startswith("test_") and name.endswith(".py")
    )
    executed = 0
    failures = []
    for path in files:
        namespace = runpy.run_path(path)
        tests = sorted(
            name for name, value in namespace.items()
            if name.startswith("test_") and callable(value)
        )
        for name in tests:
            label = os.path.basename(path) + "::" + name
            try:
                namespace[name]()
                print("PASS " + label)
            except Exception:
                failures.append(label)
                print("FAIL " + label, file=sys.stderr)
                traceback.print_exc()
            executed += 1
    if failures:
        print("FAILED {} of {} tests".format(len(failures), executed), file=sys.stderr)
        return 1
    print("PASS complete mcp_controller_server suite: {} tests".format(executed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

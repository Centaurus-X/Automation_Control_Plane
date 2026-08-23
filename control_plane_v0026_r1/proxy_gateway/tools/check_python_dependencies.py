#!/usr/bin/env python3
"""Verify exact direct requirement versions and their import boundaries."""

import argparse
import importlib
import importlib.metadata
import re
import sys

from pathlib import Path


IMPORT_NAMES = {
    "anyio": "anyio",
    "argon2-cffi": "argon2",
    "argon2-cffi-bindings": "_argon2_cffi_bindings",
    "attrs": "attrs",
    "cffi": "cffi",
    "click": "click",
    "cryptography": "cryptography",
    "h11": "h11",
    "idna": "idna",
    "jsonschema": "jsonschema",
    "jsonschema-specifications": "jsonschema_specifications",
    "paho-mqtt": "paho.mqtt.client",
    "psycopg": "psycopg",
    "psycopg-binary": "psycopg_binary",
    "psycopg-pool": "psycopg_pool",
    "pycparser": "pycparser",
    "pymodbus": "pymodbus",
    "pyyaml": "yaml",
    "referencing": "referencing",
    "rpds-py": "rpds",
    "starlette": "starlette",
    "typing-extensions": "typing_extensions",
    "uvicorn": "uvicorn",
    "websockets": "websockets",
}
PIN_PATTERN = re.compile(
    r"^([A-Za-z0-9_.-]+)(?:\[[A-Za-z0-9_.,-]+\])?"
    r"==([A-Za-z0-9_.+!-]+)$"
)


def normalize(name):
    return re.sub(r"[-_.]+", "-", name).lower()


def read_pins(path, state=None):
    state = state or {"seen": set(), "pins": {}}
    path = path.resolve()
    if path in state["seen"]:
        return state["pins"]
    state["seen"].add(path)
    for number, raw_line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw_line.split("#", 1)[0].strip()
        if not line:
            continue
        if line.startswith(("-r ", "--requirement ")):
            nested = line.split(None, 1)[1].strip()
            read_pins(path.parent / nested, state)
            continue
        match = PIN_PATTERN.fullmatch(line)
        if not match:
            raise ValueError(
                "{}:{} is not an exact supported requirement pin: {}".format(
                    path, number, line
                )
            )
        name = normalize(match.group(1))
        version = match.group(2)
        previous = state["pins"].get(name)
        if previous is not None and previous != version:
            raise ValueError(
                "conflicting pins for {}: {} and {}".format(name, previous, version)
            )
        state["pins"][name] = version
    return state["pins"]


def verify_pins(pins):
    failures = []
    for name in sorted(pins):
        expected = pins[name]
        module = IMPORT_NAMES.get(name)
        if module is None:
            failures.append("no import boundary is declared for {}".format(name))
            continue
        try:
            actual = importlib.metadata.version(name)
        except importlib.metadata.PackageNotFoundError:
            failures.append("{}=={} is missing".format(name, expected))
            continue
        if actual != expected:
            failures.append(
                "{} version mismatch: expected {}, found {}".format(
                    name, expected, actual
                )
            )
            continue
        try:
            importlib.import_module(module)
        except Exception as error:  # Import failures are the boundary under test.
            failures.append("{} import failed: {}".format(module, error))
    return failures


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("requirements", nargs="+", type=Path)
    parser.add_argument(
        "--expect",
        action="append",
        default=[],
        help="additional exact distribution pin, for example name==1.2.3",
    )
    arguments = parser.parse_args()
    try:
        pins = {}
        for requirement in arguments.requirements:
            for name, version in read_pins(requirement).items():
                if name in pins and pins[name] != version:
                    raise ValueError("conflicting pin for " + name)
                pins[name] = version
        for value in arguments.expect:
            match = PIN_PATTERN.fullmatch(value)
            if not match:
                raise ValueError("invalid --expect pin: " + value)
            pins[normalize(match.group(1))] = match.group(2)
    except (OSError, ValueError) as error:
        print("BLOCKED dependency manifest: {}".format(error), file=sys.stderr)
        return 65
    failures = verify_pins(pins)
    if failures:
        for failure in failures:
            print("BLOCKED dependency: " + failure, file=sys.stderr)
        return 69
    print(
        "PASS exact Python dependencies: "
        + ", ".join("{}=={}".format(name, pins[name]) for name in sorted(pins))
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

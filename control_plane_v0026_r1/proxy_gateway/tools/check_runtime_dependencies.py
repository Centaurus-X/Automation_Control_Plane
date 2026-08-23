# -*- coding: utf-8 -*-

"""Import the service's pinned runtime boundaries inside the release image."""

import importlib
import importlib.metadata
import sys


MODULES = {
    "master": ("psycopg", "psycopg_pool"),
    "proxy": ("anyio", "argon2", "psycopg", "psycopg_pool", "starlette", "uvicorn", "websockets"),
}
PSYCOPG_IMPLEMENTATIONS = {"master": "python", "proxy": "binary"}


def main():
    service = sys.argv[1] if len(sys.argv) > 1 else ""
    modules = MODULES.get(service)
    if not modules:
        print("unknown service", file=sys.stderr)
        return 64
    versions = []
    for name in modules:
        importlib.import_module(name)
        try:
            version = importlib.metadata.version(name.replace("_", "-"))
        except importlib.metadata.PackageNotFoundError:
            version = "imported"
        versions.append(name + "=" + version)
    psycopg = importlib.import_module("psycopg")
    expected_implementation = PSYCOPG_IMPLEMENTATIONS[service]
    actual_implementation = getattr(psycopg.pq, "__impl__", None)
    if actual_implementation != expected_implementation:
        print(
            "psycopg implementation mismatch: expected {} got {}".format(
                expected_implementation, actual_implementation),
            file=sys.stderr,
        )
        return 65
    libpq_version = psycopg.pq.version()
    if not isinstance(libpq_version, int) or libpq_version <= 0:
        print("invalid libpq runtime version", file=sys.stderr)
        return 65
    versions.append("psycopg_impl=" + actual_implementation)
    versions.append("libpq=" + str(libpq_version))
    print("PASS runtime dependencies: " + ", ".join(versions))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

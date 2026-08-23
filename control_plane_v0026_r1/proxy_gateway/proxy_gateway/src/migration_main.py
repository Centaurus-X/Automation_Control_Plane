# -*- coding: utf-8 -*-

# proxy_gateway/src/migration_main.py

"""One-shot Proxy schema migration process."""

import os

from src.library.migrations import apply_migrations
from src.library.postgresql import env_database_options


def main():
    project_root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
    directory = os.path.join(project_root, "deployments", "migrations")
    result = apply_migrations(env_database_options("PROXY_MIGRATOR_DB_"), directory)
    print("Applied Proxy migrations: " + str(result["applied_versions"]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

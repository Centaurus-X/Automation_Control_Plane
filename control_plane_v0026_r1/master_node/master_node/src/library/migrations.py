# -*- coding: utf-8 -*-

# master_node/src/library/migrations.py

"""Checksum-guarded, single-writer PostgreSQL schema migrations."""

import hashlib
import os
import re

from functools import partial

from src.library.postgresql import create_pool


MIGRATION_PATTERN = re.compile(r"^(\d+)_([a-z0-9_]+)\.sql$")


def discover_migrations(directory):
    """Return deterministic migration descriptors."""
    migrations = []
    for name in sorted(os.listdir(directory)):
        match = MIGRATION_PATTERN.fullmatch(name)
        if not match:
            continue
        path = os.path.join(directory, name)
        with open(path, "rb") as handle:
            content = handle.read()
        migrations.append({
            "version": int(match.group(1)),
            "name": match.group(2),
            "path": path,
            "content": content,
            "checksum": hashlib.sha256(content).hexdigest(),
        })
    versions = [item["version"] for item in migrations]
    if len(versions) != len(set(versions)):
        raise ValueError("duplicate migration version")
    return migrations


def _migration_state(connection):
    exists = connection.execute(
        "SELECT to_regclass('master_control.schema_migrations') AS relation"
    ).fetchone()
    if not exists or not exists.get("relation"):
        return {}
    rows = connection.execute(
        "SELECT version, name, checksum FROM master_control.schema_migrations"
    ).fetchall()
    return {int(row["version"]): row for row in rows}


def _apply_one(connection, migration):
    statements = split_sql_statements(migration["content"].decode("utf-8"))
    for statement in statements:
        connection.execute(statement, prepare=False)
    connection.execute(
        "INSERT INTO master_control.schema_migrations(version, name, checksum) "
        "VALUES (%s, %s, %s)",
        (migration["version"], migration["name"], migration["checksum"]),
    )
    return migration["version"]


def split_sql_statements(source):
    """Split PostgreSQL DDL while preserving strings and dollar-quoted bodies."""
    statements = []
    buffer = []
    index = 0
    quote = None
    dollar = None
    line_comment = False
    block_depth = 0
    while index < len(source):
        character = source[index]
        following = source[index:index + 2]
        if line_comment:
            buffer.append(character)
            line_comment = character != "\n"
            index += 1
            continue
        if block_depth:
            if following == "/*":
                block_depth += 1
                buffer.append(following)
                index += 2
            elif following == "*/":
                block_depth -= 1
                buffer.append(following)
                index += 2
            else:
                buffer.append(character)
                index += 1
            continue
        if dollar:
            if source.startswith(dollar, index):
                buffer.append(dollar)
                index += len(dollar)
                dollar = None
            else:
                buffer.append(character)
                index += 1
            continue
        if quote:
            buffer.append(character)
            if character == "\\" and index + 1 < len(source):
                buffer.append(source[index + 1])
                index += 2
                continue
            if character == quote:
                if index + 1 < len(source) and source[index + 1] == quote:
                    buffer.append(source[index + 1])
                    index += 2
                    continue
                quote = None
            index += 1
            continue
        if following == "--":
            line_comment = True
            buffer.append(following)
            index += 2
            continue
        if following == "/*":
            block_depth = 1
            buffer.append(following)
            index += 2
            continue
        if character in ("'", '"'):
            quote = character
            buffer.append(character)
            index += 1
            continue
        if character == "$":
            match = re.match(r"\$[A-Za-z_][A-Za-z0-9_]*\$|\$\$", source[index:])
            if match:
                dollar = match.group(0)
                buffer.append(dollar)
                index += len(dollar)
                continue
        if character == ";":
            statement = "".join(buffer).strip()
            if statement:
                statements.append(statement)
            buffer = []
            index += 1
            continue
        buffer.append(character)
        index += 1
    trailing = "".join(buffer).strip()
    if trailing:
        statements.append(trailing)
    if quote or dollar or block_depth:
        raise ValueError("migration contains an unterminated SQL construct")
    return statements


def apply_migrations(database_options, directory):
    """Apply each pending migration once under an advisory transaction lock."""
    context = create_pool(database_options)
    applied = []
    try:
        with context["pool"].connection() as connection:
            with connection.transaction():
                connection.execute("SELECT pg_advisory_xact_lock(%s)", (730017001,))
                state = _migration_state(connection)
                for migration in discover_migrations(directory):
                    previous = state.get(migration["version"])
                    if previous and previous["checksum"] != migration["checksum"]:
                        raise RuntimeError("applied migration checksum differs")
                    if previous:
                        continue
                    applied.append(_apply_one(connection, migration))
    finally:
        context["close"]()
    return {"ok": True, "applied_versions": applied}

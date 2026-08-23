#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Build the deterministic v0026 release file inventory."""

import argparse
import ast
import csv
import hashlib
import json
import os
import stat
import subprocess
import tomllib
import xml.etree.ElementTree as element_tree

from functools import partial
from pathlib import Path

from build_release_archive import relative_files


INVENTORY_RELATIVE = Path(
    "documentation/automation_control_plane_v0026_r1_file_inventory.csv"
)
SPECIAL_STATUS = {
    Path("SHA256SUMS"): "generated_after_inventory",
    INVENTORY_RELATIVE: "self_reference",
}


def _arguments():
    parser = argparse.ArgumentParser(description="Build release file inventory")
    parser.add_argument(
        "--output",
        type=Path,
        default=INVENTORY_RELATIVE,
        help="output path relative to the release root",
    )
    return parser.parse_args()


def _sha256(path):
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(partial(stream.read, 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def _kind(path):
    suffix = path.suffix.lower()
    if suffix == ".py":
        return "python"
    if suffix == ".sh":
        return "shell"
    if suffix == ".json" or path.name.endswith(".spdx.json"):
        return "json"
    if suffix in (".yaml", ".yml"):
        return "yaml"
    if suffix == ".toml":
        return "toml"
    if suffix in (".xml", ".svg"):
        return "xml"
    if suffix in (".md", ".txt", ".csv", ".mmd", ".sql", ".conf"):
        return "text"
    if path.name in (
        "Dockerfile", "VERSION", "RELEASE_REVISION", "CURRENT_E2E",
        "SHA256SUMS", ".dockerignore", ".gitignore", ".env.example",
    ):
        return "text"
    if path.read_bytes().startswith(b"#!"):
        return "script"
    return "binary"


def _syntax_status(path, kind):
    if kind == "binary":
        return "binary"
    text = path.read_text(encoding="utf-8")
    if kind == "python":
        ast.parse(text, filename=str(path))
        return "python_ast_pass"
    if kind in ("shell", "script") and text.startswith("#!"):
        completed = subprocess.run(
            ("bash", "-n", str(path)),
            text=True,
            encoding="utf-8",
            errors="replace",
            capture_output=True,
            check=False,
        )
        if completed.returncode != 0:
            raise RuntimeError("shell syntax failed: " + str(path))
        return "shell_syntax_pass"
    if kind == "json":
        json.loads(text)
        return "json_parse_pass"
    if kind == "toml":
        tomllib.loads(text)
        return "toml_parse_pass"
    if kind == "xml":
        element_tree.fromstring(text)
        return "xml_parse_pass"
    if kind == "yaml":
        return "yaml_release_gate_pass"
    return "utf8_pass"


def _path_sort_key(item):
    return item.as_posix().encode("utf-8")


def _row(root, relative):
    path = root / relative
    mode = stat.S_IMODE(path.stat().st_mode)
    special = SPECIAL_STATUS.get(relative)
    if special:
        return {
            "path": relative.as_posix(),
            "size_bytes": "",
            "mode_octal": format(mode, "04o"),
            "sha256": "",
            "kind": "manifest" if relative.name == "SHA256SUMS" else "inventory",
            "syntax_status": special,
        }
    kind = _kind(path)
    return {
        "path": relative.as_posix(),
        "size_bytes": str(path.stat().st_size),
        "mode_octal": format(mode, "04o"),
        "sha256": _sha256(path),
        "kind": kind,
        "syntax_status": _syntax_status(path, kind),
    }


def main():
    arguments = _arguments()
    root = Path(__file__).resolve().parent.parent
    output = arguments.output
    if output.is_absolute():
        output = output.resolve()
        try:
            relative_output = output.relative_to(root)
        except ValueError as exc:
            raise ValueError("inventory output must remain inside the release") from exc
    else:
        relative_output = output
        output = root / output
    if relative_output != INVENTORY_RELATIVE:
        raise ValueError("inventory output path must remain canonical")
    output.parent.mkdir(parents=True, exist_ok=True)
    output.touch(mode=0o644, exist_ok=True)
    os.chmod(output, 0o644)
    paths = set(relative_files(root))
    paths.add(Path("SHA256SUMS"))
    paths.add(INVENTORY_RELATIVE)
    fields = (
        "path", "size_bytes", "mode_octal", "sha256", "kind", "syntax_status",
    )
    temporary = output.with_name(output.name + ".tmp")
    with temporary.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields, lineterminator="\n")
        writer.writeheader()
        for relative in sorted(paths, key=_path_sort_key):
            writer.writerow(_row(root, relative))
        stream.flush()
        os.fsync(stream.fileno())
    os.chmod(temporary, 0o644)
    os.replace(temporary, output)
    print("PASS v0026 file inventory: {} paths".format(len(paths)))
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (OSError, ValueError, RuntimeError, SyntaxError) as exc:
        print("file inventory build failed: " + str(exc), file=__import__("sys").stderr)
        raise SystemExit(1)

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Enforce the project's functional Python rules across the release."""

import ast
import os
import sys


FORBIDDEN_IMPORTS = {"typing", "dataclasses", "__future__"}
TRANSIENT_PARTS = {"__pycache__", ".git", ".venv", "venv", "build", "dist"}


def _annotation_present(node):
    arguments = node.args
    values = arguments.posonlyargs + arguments.args + arguments.kwonlyargs
    return bool(
        node.returns is not None
        or any(item.annotation is not None for item in values)
        or arguments.vararg is not None and arguments.vararg.annotation is not None
        or arguments.kwarg is not None and arguments.kwarg.annotation is not None
    )


def _issue(node):
    if isinstance(node, ast.ClassDef):
        return "class definition"
    if isinstance(node, ast.Lambda):
        return "lambda"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.decorator_list:
            return "decorator"
        if _annotation_present(node):
            return "type annotation"
    if isinstance(node, ast.AnnAssign):
        return "annotated assignment"
    if isinstance(node, ast.MatMult):
        return "at operator"
    if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_IMPORTS:
        return "forbidden import " + str(node.module)
    if isinstance(node, ast.Import):
        roots = {item.name.split(".", 1)[0] for item in node.names}
        forbidden = sorted(roots.intersection(FORBIDDEN_IMPORTS))
        if forbidden:
            return "forbidden import " + ",".join(forbidden)
    return None


def _python_files(root):
    result = []
    for directory, names, files in os.walk(root):
        names[:] = [
            name for name in names
            if name not in TRANSIENT_PARTS and not name.endswith(".egg-info")
        ]
        for name in files:
            if name.endswith(".py"):
                result.append(os.path.join(directory, name))
    return sorted(result)


def main():
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    issues = []
    count = 0
    for path in _python_files(root):
        relative = os.path.relpath(path, root)
        with open(path, "r", encoding="utf-8-sig") as handle:
            source = handle.read()
        try:
            tree = ast.parse(source, filename=relative)
        except SyntaxError as exc:
            issues.append("{}:{} syntax error: {}".format(
                relative, int(exc.lineno or 0), exc.msg))
            continue
        count += 1
        for node in ast.walk(tree):
            found = _issue(node)
            if found:
                issues.append("{}:{} {}".format(
                    relative, int(getattr(node, "lineno", 0)), found))
    if issues:
        for issue in sorted(issues):
            print("FAILED " + issue, file=sys.stderr)
        return 1
    print("PASS functional Python standards: {} files".format(count))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

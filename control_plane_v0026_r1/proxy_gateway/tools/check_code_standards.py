# -*- coding: utf-8 -*-

"""Fail-fast AST and dependency-boundary checks for the project specification."""

import ast
import os
import sys


def _annotation_present(node):
    arguments = list(node.args.posonlyargs) + list(node.args.args) + list(node.args.kwonlyargs)
    return bool(
        node.returns is not None
        or any(argument.annotation is not None for argument in arguments)
        or (node.args.vararg and node.args.vararg.annotation is not None)
        or (node.args.kwarg and node.args.kwarg.annotation is not None)
    )


def _node_issues(node):
    issues = []
    if isinstance(node, ast.ClassDef):
        issues.append("class definition")
    if isinstance(node, ast.Lambda):
        issues.append("lambda")
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.decorator_list:
            issues.append("decorator")
        if _annotation_present(node):
            issues.append("type annotation")
    if isinstance(node, ast.AnnAssign):
        issues.append("annotated assignment")
    if isinstance(node, ast.MatMult):
        issues.append("@ operator")
    if isinstance(node, ast.ImportFrom) and node.module in ("typing", "dataclasses", "__future__"):
        issues.append("forbidden import " + str(node.module))
    if isinstance(node, ast.Import):
        names = {item.name for item in node.names}
        forbidden = names.intersection({"typing", "dataclasses"})
        if forbidden:
            issues.append("forbidden import " + sorted(forbidden)[0])
    if isinstance(node, ast.Name) and node.id == "SimpleNamespace" \
            or isinstance(node, ast.Attribute) and node.attr == "SimpleNamespace":
        issues.append("SimpleNamespace")
    return issues


def _scan_file(path):
    with open(path, "r", encoding="utf-8") as handle:
        source = handle.read()
    tree = ast.parse(source, filename=path)
    issues = []
    for node in ast.walk(tree):
        for label in _node_issues(node):
            issues.append("{}:{}: {}".format(path, getattr(node, "lineno", 1), label))
    return issues


def _python_files(roots):
    paths = []
    for root in roots:
        for directory, names, files in os.walk(root):
            names[:] = sorted(name for name in names if name != "__pycache__")
            for name in sorted(files):
                if name.endswith(".py"):
                    paths.append(os.path.join(directory, name))
    return paths


def _dependency_issues(paths, service):
    issues = []
    forbidden = ("adapter", "fieldbus", "_controller_thread", "modbus") \
        if service == "master" else ("master_node", "distributed_coordinator", "fieldbus", "modbus")
    for path in paths:
        normalized = path.replace(os.sep, "/")
        if "/test/" in normalized or "/packages/" in normalized:
            continue
        with open(path, "r", encoding="utf-8") as handle:
            tree = ast.parse(handle.read(), filename=path)
        for node in ast.walk(tree):
            names = []
            if isinstance(node, ast.Import):
                names = [item.name for item in node.names]
            elif isinstance(node, ast.ImportFrom):
                names = [str(node.module or "")]
            for imported in names:
                if any(part in imported.lower() for part in forbidden):
                    issues.append("{}:{}: forbidden dependency {}".format(
                        path, getattr(node, "lineno", 1), imported))
    return issues


def main():
    if len(sys.argv) < 3:
        print("usage: check_code_standards.py SERVICE ROOT [ROOT ...]", file=sys.stderr)
        return 64
    service = sys.argv[1]
    paths = _python_files(sys.argv[2:])
    issues = []
    for path in paths:
        issues.extend(_scan_file(path))
    issues.extend(_dependency_issues(paths, service))
    if issues:
        print("\n".join(sorted(set(issues))), file=sys.stderr)
        return 1
    print("PASS code standards: {} Python files".format(len(paths)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Enforce hard functional-Python rules across the complete active release."""

import ast
import re
import sys

from pathlib import Path


TRANSIENT_PARTS = frozenset(("__pycache__", ".git", ".venv", "venv"))
HEREDOC_PATTERN = re.compile(r"<<'(?P<marker>PY[A-Z0-9_]*)'")
FORBIDDEN_IMPORTS = frozenset(("typing", "dataclasses", "__future__"))


def _function_annotation_present(node):
    arguments = node.args
    values = arguments.posonlyargs + arguments.args + arguments.kwonlyargs
    return bool(
        node.returns is not None
        or any(item.annotation is not None for item in values)
        or arguments.vararg is not None
        and arguments.vararg.annotation is not None
        or arguments.kwarg is not None
        and arguments.kwarg.annotation is not None
    )


def _explicit_parameter_count(node):
    arguments = node.args
    return len(arguments.posonlyargs) + len(arguments.args) \
        + len(arguments.kwonlyargs)


def _node_issue(node):
    if isinstance(node, ast.ClassDef):
        return "class definition"
    if isinstance(node, ast.Lambda):
        return "lambda"
    if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
        if node.decorator_list:
            return "decorator"
        if _function_annotation_present(node):
            return "type annotation"
    if isinstance(node, ast.AnnAssign):
        return "annotated assignment"
    if isinstance(node, ast.MatMult):
        return "at operator"
    if isinstance(node, ast.ImportFrom) and node.module in FORBIDDEN_IMPORTS:
        return "forbidden import " + str(node.module)
    if isinstance(node, ast.Import):
        names = {item.name.split(".", 1)[0] for item in node.names}
        forbidden = sorted(names.intersection(FORBIDDEN_IMPORTS))
        if forbidden:
            return "forbidden import " + ",".join(forbidden)
    if isinstance(node, ast.Name) and node.id == "SimpleNamespace":
        return "SimpleNamespace"
    if isinstance(node, ast.Attribute) and node.attr == "SimpleNamespace":
        return "SimpleNamespace"
    return None


def _scan_tree(tree, label):
    issues = []
    advisory = 0
    for node in ast.walk(tree):
        issue = _node_issue(node)
        if issue:
            issues.append("{}:{} {}".format(
                label, int(getattr(node, "lineno", 0)), issue))
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) \
                and _explicit_parameter_count(node) > 3:
            advisory += 1
    return issues, advisory


def _parse(source, label):
    try:
        return ast.parse(source, filename=label)
    except SyntaxError as exc:
        raise RuntimeError(
            "Python syntax error in {}:{}: {}".format(
                label, int(exc.lineno or 0), exc.msg)) from exc


def _embedded_python(tree):
    values = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Constant) or not isinstance(node.value, str):
            continue
        source = node.value
        stripped = source.lstrip()
        if "\n" not in source or not stripped.startswith((
                "import ", "from ", "#!/usr/bin/env python")):
            continue
        try:
            embedded = ast.parse(source)
        except SyntaxError:
            continue
        values.append((int(getattr(node, "lineno", 0)), embedded))
    return values


def _scan_python(path, root):
    relative = path.relative_to(root).as_posix()
    source = path.read_text(encoding="utf-8-sig")
    tree = _parse(source, relative)
    issues, advisory = _scan_tree(tree, relative)
    for line, embedded in _embedded_python(tree):
        found, count = _scan_tree(
            embedded, "{}#embedded:{}".format(relative, line))
        issues.extend(found)
        advisory += count
    return issues, advisory


def _python_heredocs(path, root):
    relative = path.relative_to(root).as_posix()
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    values = []
    index = 0
    while index < len(lines):
        match = HEREDOC_PATTERN.search(lines[index])
        if match is None:
            index += 1
            continue
        marker = match.group("marker")
        start = index + 1
        index = start
        body = []
        while index < len(lines) and lines[index] != marker:
            body.append(lines[index])
            index += 1
        if index >= len(lines):
            raise RuntimeError(
                "unterminated Python heredoc in {}:{}".format(relative, start))
        source = "\n".join(body) + "\n"
        values.append(("{}#heredoc:{}".format(relative, start + 1), source))
        index += 1
    return values


def _release_files(root, suffix):
    return sorted(
        path for path in root.rglob("*" + suffix)
        if path.is_file()
        and not any(part in TRANSIENT_PARTS for part in path.relative_to(root).parts)
    )


def main():
    root = Path(__file__).resolve().parent.parent
    issues = []
    advisory = 0
    python_files = _release_files(root, ".py")
    for path in python_files:
        found, count = _scan_python(path, root)
        issues.extend(found)
        advisory += count
    heredoc_count = 0
    for path in _release_files(root, ".sh"):
        for label, source in _python_heredocs(path, root):
            tree = _parse(source, label)
            found, count = _scan_tree(tree, label)
            issues.extend(found)
            advisory += count
            heredoc_count += 1
    if issues:
        print("FAILED release-wide code standards:", file=sys.stderr)
        for issue in sorted(issues):
            print("  " + issue, file=sys.stderr)
        return 1
    print(
        "PASS release-wide code standards: {} Python files, {} embedded "
        "Python heredocs; advisory functions with >3 explicit parameters={}".format(
            len(python_files), heredoc_count, advisory))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
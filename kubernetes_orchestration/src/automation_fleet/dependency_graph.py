"""Stable functional dependency graph operations."""


def _insert_sorted(values, value):
    values.append(value)
    values.sort()


def stable_topological_order(components, selected):
    selected_set = set(selected)
    incoming = {}
    outgoing = {}
    for component_id in sorted(selected_set):
        incoming[component_id] = set()
        outgoing[component_id] = set()
    for component_id in sorted(selected_set):
        descriptor = components[component_id]
        dependencies = descriptor.get("dependencies", {}).get("hard_deploy", [])
        for dependency in dependencies:
            if dependency not in components:
                raise ValueError(
                    "unknown_dependency: " + component_id + " requires " + dependency)
            if dependency not in selected_set:
                raise ValueError(
                    "hard_dependency_missing: " + component_id + " requires " + dependency)
            incoming[component_id].add(dependency)
            outgoing[dependency].add(component_id)
    ready = sorted(name for name in selected_set if not incoming[name])
    result = []
    while ready:
        current = ready.pop(0)
        result.append(current)
        for dependent in sorted(outgoing[current]):
            incoming[dependent].discard(current)
            if not incoming[dependent] and dependent not in result and dependent not in ready:
                _insert_sorted(ready, dependent)
    if len(result) != len(selected_set):
        remaining = sorted(selected_set - set(result))
        raise ValueError("dependency_cycle: " + ",".join(remaining))
    return result

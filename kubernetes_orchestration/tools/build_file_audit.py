"""Build a deterministic full-file audit for the release wrapper."""

import argparse
import hashlib
import json
import pathlib


def _sha256(path):
    state = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            state.update(block)
    return state.hexdigest()


def _category(relative):
    if relative.startswith("control_plane_v0026_r1/"):
        return "preserved_control_plane"
    if "/documentation/" in "/" + relative or relative.endswith(".md"):
        return "documentation"
    if "/tests/" in "/" + relative:
        return "test"
    if "/deploy/helm/" in "/" + relative:
        return "helm"
    if "/web/" in "/" + relative:
        return "web_ui"
    if relative.endswith((".py", ".sh", "Dockerfile")):
        return "code_or_build"
    return "configuration_or_metadata"


def build(root, output):
    release_root = pathlib.Path(root).resolve()
    output_path = pathlib.Path(output).resolve()
    entries = []
    excluded = {output_path, release_root / "SHA256SUMS"}
    for path in sorted(release_root.rglob("*")):
        if not path.is_file() or path in excluded:
            continue
        relative = path.relative_to(release_root).as_posix()
        entries.append({
            "path": relative,
            "bytes": path.stat().st_size,
            "sha256": _sha256(path),
            "category": _category(relative),
        })
    category_counts = {}
    total_bytes = 0
    for entry in entries:
        category = entry["category"]
        category_counts[category] = category_counts.get(category, 0) + 1
        total_bytes += entry["bytes"]
    report = {
        "schema_version": 1,
        "release": release_root.name,
        "deterministic": True,
        "self_excluded": output_path.relative_to(release_root).as_posix(),
        "release_manifest_excluded": "SHA256SUMS",
        "summary": {
            "audited_files": len(entries),
            "audited_bytes": total_bytes,
            "categories": category_counts,
        },
        "files": entries,
    }
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(
        json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        encoding="utf-8", newline="\n")
    return report


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True)
    arguments = parser.parse_args()
    report = build(arguments.root, arguments.output)
    print(json.dumps(report["summary"], indent=2, sort_keys=True))


if __name__ == "__main__":
    main()

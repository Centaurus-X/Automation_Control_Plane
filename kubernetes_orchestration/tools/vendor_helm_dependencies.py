"""Vendor local Helm dependencies for an offline-ready umbrella chart."""

import argparse
import pathlib
import shutil


DEPENDENCIES = (
    "automation-postgresql-integration",
    "automation-mqtt",
    "automation-master",
    "automation-proxy",
    "automation-mcp-controller",
    "automation-system-soft",
    "automation-fleet",
    "automation-cluster-agent",
    "automation-observability",
)


def vendor(root):
    orchestration = pathlib.Path(root).resolve()
    helm = orchestration / "deploy" / "helm"
    destination = helm / "automation-control-plane" / "charts"
    destination.mkdir(parents=True, exist_ok=True)
    for name in DEPENDENCIES:
        source = helm / name
        target = destination / name
        if target.exists():
            shutil.rmtree(target)
        shutil.copytree(source, target)
    return len(DEPENDENCIES)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", required=True)
    arguments = parser.parse_args()
    count = vendor(arguments.root)
    print("Vendored Helm dependencies: " + str(count))


if __name__ == "__main__":
    main()

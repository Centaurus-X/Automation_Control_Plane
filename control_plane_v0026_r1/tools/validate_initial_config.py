# -*- coding: utf-8 -*-

"""Fail-fast profile validation for a Master-v2 initial authority config."""

import argparse
import json

from src.library._generic_system_contract_library import config_graph, entity_registry
from src.library._generic_type_library import fingerprint


def _arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("path")
    parser.add_argument(
        "--profile", choices=("lab_v2", "production_v2"), required=True)
    return parser.parse_args()


def main():
    arguments = _arguments()
    with open(arguments.path, "r", encoding="utf-8") as handle:
        config = json.load(handle)
    report = config_graph.validate_authority_config(
        config, profile=arguments.profile)
    if not report.get("ok"):
        for error in report.get("errors") or ():
            print("ERROR " + str(error))
        return 65
    print("PASS {} config: {} entities, sha256 {}".format(
        arguments.profile, len(entity_registry.decompose(config)),
        fingerprint(config)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

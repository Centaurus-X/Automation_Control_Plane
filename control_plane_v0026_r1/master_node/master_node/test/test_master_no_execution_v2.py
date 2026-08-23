# -*- coding: utf-8 -*-

"""Hard release gate proving that Master execution modules are absent."""

import os


FORBIDDEN_FILES = (
    "core/global_automation.py", "core/automation_authority.py",
    "core/_automation_thread.py", "library/_automation_engine.py",
    "library/_event_interface.py", "library/_generic_finite_state_machine.py",
    "library/_generic_rule_engine.py", 
    "library/_state_shard_manager.py",
)

FORBIDDEN_IMPORTS = (
    "src.core.global_automation",
    "src.core.automation_authority",
    "src.library._automation_engine",
    "src.library._generic_rule_engine",
    "src.library._generic_finite_state_machine",
)


def main():
    root = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "src"))
    for relative in FORBIDDEN_FILES:
        assert not os.path.exists(os.path.join(root, relative)), relative
    for directory, _names, files in os.walk(root):
        for name in files:
            if not name.endswith(".py"):
                continue
            path = os.path.join(directory, name)
            with open(path, "r", encoding="utf-8") as handle:
                source = handle.read()
            assert not any(value in source for value in FORBIDDEN_IMPORTS), path
    runtime_path = os.path.join(root, "core", "master_runtime.py")
    with open(runtime_path, "r", encoding="utf-8") as handle:
        runtime = handle.read()
    assert '"master_automation_execution": False' in runtime
    print("PASS Master contains no Rule/FSM/Automation execution code")


if __name__ == "__main__":
    main()

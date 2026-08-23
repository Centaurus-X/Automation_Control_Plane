# -*- coding: utf-8 -*-

"""Offline dependency bootstrap security and determinism regressions."""

import pathlib
import tempfile


ROOT = pathlib.Path(__file__).resolve().parents[2]
MODULE_PATH = ROOT / "tools" / "container_dependency_bootstrap.py"


def load_module():
    namespace = {
        "__file__": str(MODULE_PATH),
        "__name__": "container_dependency_bootstrap_test",
    }
    source = MODULE_PATH.read_text(encoding="utf-8")
    exec(compile(source, str(MODULE_PATH), "exec"), namespace)
    return namespace


def main():
    module = load_module()
    with tempfile.TemporaryDirectory(prefix="proxy-wheelhouse-test-") as temporary:
        wheelhouse = pathlib.Path(temporary)
        first = wheelhouse / "first-1.0-py3-none-any.whl"
        second = wheelhouse / "second-2.0-py3-none-any.whl"
        first.write_bytes(b"first-wheel")
        second.write_bytes(b"second-wheel")
        module["write_manifest"](wheelhouse)
        fingerprint = module["verify_manifest"](wheelhouse)
        assert len(fingerprint) == 64
        first.write_bytes(b"corrupt-wheel")
        try:
            module["verify_manifest"](wheelhouse)
        except RuntimeError as error:
            assert "checksum mismatch" in str(error)
        else:
            raise AssertionError("corrupt wheelhouse was accepted")

    temporary = pathlib.Path("/runtime/temporary")
    command = module["repair_command"](temporary)
    assert "--no-index" in command
    assert command[command.index("--find-links") + 1] == str(module["WHEELHOUSE"])
    assert command[command.index("--target") + 1] == str(temporary)
    assert "--only-binary=:all:" in command
    assert "https://" not in " ".join(command)
    print("PASS verified-wheelhouse-only Proxy dependency repair contract")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

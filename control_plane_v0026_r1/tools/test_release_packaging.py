#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Regression checks for release-tree and archive safety boundaries."""

import shutil
import tempfile

from pathlib import Path

from build_release_archive import (
    external_worker_contract_required,
    expected_release_directory,
    read_active_e2e,
    read_e2e_packages,
    reject_transient_content,
)


def assert_directory_rejected(root, relative):
    target = root / relative
    target.mkdir(parents=True)
    try:
        reject_transient_content(root)
    except RuntimeError as error:
        if relative.as_posix() not in str(error):
            raise AssertionError("wrong transient path reported: " + str(error)) from error
    else:
        raise AssertionError(relative.as_posix() + " was accepted")
    target.rmdir()
    parent = target.parent
    while parent != root and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def assert_file_rejected(root, relative):
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("home = /usr/bin\n", encoding="utf-8")
    try:
        reject_transient_content(root)
    except RuntimeError as error:
        if relative.as_posix() not in str(error):
            raise AssertionError("wrong transient path reported: " + str(error)) from error
    else:
        raise AssertionError(relative.as_posix() + " was accepted")
    target.unlink()
    parent = target.parent
    while parent != root and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def assert_mcp_secret_readme_preserved(root):
    relative = Path("mcp_controller_server/secrets/README.md")
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text("# Runtime secrets\n", encoding="utf-8")
    reject_transient_content(root)
    target.unlink()
    target.parent.rmdir()
    target.parent.parent.rmdir()


def assert_shell_carriage_return_rejected(root):
    relative = Path("tools/unsafe.sh")
    target = root / relative
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(b"#!/bin/sh\r\nprintf 'unsafe'\r\n")
    try:
        reject_transient_content(root)
    except RuntimeError as error:
        detail = str(error)
        assert relative.as_posix() in detail
        assert "carriage return" in detail
    else:
        raise AssertionError("shell script with carriage returns was accepted")
    target.unlink()
    target.parent.rmdir()


def assert_symlink_rejected(root):
    linked_target = root / "linked-target"
    linked_target.write_text("x", encoding="utf-8")
    linked = root / "linked"
    try:
        linked.symlink_to(linked_target)
    except OSError as error:
        linked_target.unlink()
        if getattr(error, "winerror", None) == 1314:
            return False
        raise
    try:
        reject_transient_content(root)
    except RuntimeError as error:
        assert "symbolic link" in str(error)
    else:
        raise AssertionError("symbolic link was accepted")
    finally:
        linked.unlink(missing_ok=True)
        linked_target.unlink(missing_ok=True)
    return True

def test_selector(root):
    integration = root / "integration_test"
    active = integration / "e2e_v0042"
    active.mkdir(parents=True)
    (active / "VERSION").write_text("1.2.3-e2e.1\n", encoding="utf-8")
    (integration / "CURRENT_E2E").write_text("e2e_v0042\n", encoding="utf-8")
    assert read_active_e2e(root) == "e2e_v0042"
    historical = integration / "e2e_v0041"
    historical.mkdir()
    (historical / "VERSION").write_text("1.2.2-e2e.1\n", encoding="utf-8")
    assert read_e2e_packages(root) == ("e2e_v0041", "e2e_v0042")
    (integration / "CURRENT_E2E").write_text("../escape\n", encoding="utf-8")
    try:
        read_active_e2e(root)
    except RuntimeError:
        pass
    else:
        raise AssertionError("unsafe active E2E selector was accepted")


def test_external_worker_contract_policy(root):
    release = root / "automation_control_plane_release_v0026_r1"
    release.mkdir()
    assert external_worker_contract_required(release, False)
    assert not external_worker_contract_required(release, True)

    archive = root / "automation_system_v0101.zip"
    archive.write_bytes(b"worker")
    try:
        external_worker_contract_required(release, True)
    except RuntimeError as error:
        assert "only allowed when" in str(error)
    else:
        raise AssertionError("available Worker ZIP bypassed strict validation")
    archive.unlink()

    sidecar = root / "automation_system_v0101.zip.sha256"
    sidecar.write_text("invalid\n", encoding="utf-8")
    try:
        external_worker_contract_required(release, True)
    except RuntimeError as error:
        assert "only allowed when" in str(error)
    else:
        raise AssertionError("available Worker sidecar bypassed strict validation")
    sidecar.unlink()
    release.rmdir()


def main():
    root = Path(tempfile.mkdtemp(prefix="automation-release-builder-"))
    symlink_checked = False
    try:
        cases = (
            Path(".git"),
            Path("mqtt_broker/backup"),
            Path("mqtt_broker/runtime-pki-handoff"),
            Path("postgres_system/runtime-tls-handoff"),
            Path("mcp_controller_server/runtime-secrets"),
            Path("mcp_controller_server/.test-tmp"),
            Path("integration_test/e2e_v0042/results"),
            Path("integration_test/e2e_v0042/runtime"),
            Path("integration_test/e2e_v0042/.venv"),
        )
        for relative in cases:
            assert_directory_rejected(root, relative)
        sensitive_files = (
            Path("external-env/pyvenv.cfg"),
            Path("private/credentials.json"),
            Path("private/rollout_automation.json"),
            Path("private/rollout_automation.env"),
            Path("private/worker.key"),
            Path("private/client.pem"),
            Path("private/client.p12"),
            Path("private/client.pfx"),
            Path("mcp_controller_server/secrets/mcp_auth.json"),
        )
        for relative in sensitive_files:
            assert_file_rejected(root, relative)
        assert_mcp_secret_readme_preserved(root)
        assert_shell_carriage_return_rejected(root)
        symlink_checked = assert_symlink_rejected(root)
        test_selector(root)
        test_external_worker_contract_policy(root)
        assert expected_release_directory("v0017-r13") == \
            "automation_control_plane_release_v0017_r13"
    finally:
        shutil.rmtree(root)
    capability = "" if symlink_checked else \
        " (Windows symlink creation unavailable)"
    print(
        "PASS release builder rejects VCS, runtime, local configuration, "
        "credentials, private keys and links" + capability
    )
    return 0

if __name__ == "__main__":
    raise SystemExit(main())

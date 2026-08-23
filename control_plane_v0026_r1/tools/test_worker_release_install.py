#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""Regression tests for the recoverable Worker archive installer."""

import hashlib
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import zipfile

from pathlib import Path

from install_worker_release import (
    PRESERVED_CONFIG_RELATIVE,
    capture_committed_config,
    extract_archive,
    install_release,
    sha256,
    validate_archive,
    verify_release_tree,
)


VERSION = "automation_system_v0101"
LEGACY_VERSION = "automation_system_v0083"


def _file_hash(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _config_document(marker, version=9, grouped=False):
    document = {
        "meta": [{
            "version": version,
            "schema": "device_state_norm",
            "scope": "node_local",
            "node_id": "fn-01",
        }],
        "controllers": [{"controller_uuid": "controller-1"}],
        "sensors": [{"sensor_id": 1, "controller_uuid": "controller-1"}],
        "actuators": [{"actuator_id": 1, "controller_uuid": "controller-1"}],
        "processes": [],
        "process_states": [],
        "rules": [],
        "commissioned_marker": marker,
    }
    if grouped:
        document["sensors"] = [{"controller_uuid": "controller-1", "rows": []}]
    return document


def _write_config(path, marker, version=9, grouped=False):
    path.write_text(
        json.dumps(
            _config_document(marker, version=version, grouped=grouped),
            sort_keys=True) + "\n",
        encoding="utf-8",
        newline="\n")


def _write_manifest(root):
    files = sorted(
        path for path in root.rglob("*")
        if path.is_file() and path.name != "SHA256SUMS"
    )
    lines = [
        "{}  ./{}".format(_file_hash(path), path.relative_to(root).as_posix())
        for path in files
    ]
    (root / "SHA256SUMS").write_text(
        "\n".join(lines) + "\n", encoding="utf-8", newline="\n")


def _build_tree(parent, marker, version=VERSION):
    root = parent / version
    (root / "_runtime").mkdir(parents=True)
    (root / "logs").mkdir()
    (root / "_config" / "certs").mkdir(parents=True)
    library_root = root / "src" / "library"
    library_root.mkdir(parents=True)
    config_path = root.joinpath(*PRESERVED_CONFIG_RELATIVE.parts)
    config_path.parent.mkdir(parents=True)
    (root / "VERSION").write_text(version + "\n", encoding="utf-8")
    (root / "run_worker.sh").write_text("#!/bin/sh\n# " + marker + "\n", encoding="utf-8")
    (root / "_config" / "certs" / "README.md").write_text(
        "identity injected at rollout\n", encoding="utf-8")
    (library_root / "_generic_type_library.py").write_text(
        "def verify_storage_config_data(document):\n"
        "    marker = document.get('commissioned_marker')\n"
        "    return {'status': ('FAILED' if marker == "
        "'incomplete-worker-contract' else 'OK')}\n",
        encoding="utf-8")
    _write_config(config_path, marker)
    _write_manifest(root)
    return root


def _build_legacy_tree(parent):
    root = parent / LEGACY_VERSION
    config_root = root / "_config" / "config_data"
    config_root.mkdir(parents=True)
    (root / "VERSION").write_text(LEGACY_VERSION + "\n", encoding="utf-8")
    (root / "run_worker.sh").write_text("#!/bin/sh\n", encoding="utf-8")
    (config_root / "device_state_data_v5.json").write_text(
        '{"meta":[{"version":5}],"sensors":[{"rows":[]}]}\n',
        encoding="utf-8")
    (config_root / "device_state_data_v7.json").write_text(
        '{"meta":[{"version":7}],"sensors":[{"rows":[]}]}\n',
        encoding="utf-8")
    _write_manifest(root)
    return root


def _zip_tree(root, destination):
    with zipfile.ZipFile(destination, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for path in sorted(root.rglob("*")):
            relative = root.name + "/" + path.relative_to(root).as_posix()
            if path.is_dir():
                info = zipfile.ZipInfo(relative + "/")
                info.create_system = 3
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
                archive.writestr(info, b"")
            else:
                info = zipfile.ZipInfo(relative)
                info.create_system = 3
                mode = 0o755 if path.suffix == ".sh" else 0o644
                info.external_attr = (stat.S_IFREG | mode) << 16
                archive.writestr(info, path.read_bytes())


def _extract_validated(archive, install_root, manifest_hash):
    stage = Path(tempfile.mkdtemp(prefix=".stage-", dir=install_root))
    extracted = extract_archive(archive, stage, VERSION)
    verify_release_tree(extracted, VERSION, manifest_hash)
    return stage, extracted


def main():
    rollout_text = Path(__file__).parent.parent.joinpath(
        "rollout_automation.sh").read_text(encoding="utf-8")
    assert '--config-drift-policy "$config_drift_policy"' in rollout_text
    assert '"$WORKER_MANIFEST_SHA256" "$WORKER_CONFIG_DRIFT_POLICY"' in rollout_text
    assert "shift 6" in rollout_text
    temporary = Path(tempfile.mkdtemp(prefix="worker-release-installer-"))
    try:
        home = temporary / "home"
        source = temporary / "source"
        home.mkdir()
        source.mkdir()
        install_root = home / "python-skripte_22" / "__NEW_FILES__"
        old_root = home / "__OLD__"
        install_root.mkdir(parents=True)

        first_tree = _build_tree(source / "first", "first")
        first_archive = home / (VERSION + ".zip")
        _zip_tree(first_tree, first_archive)
        first_hash = sha256(first_archive)
        first_manifest = sha256(first_tree / "SHA256SUMS")
        assert validate_archive(first_archive, VERSION, first_hash) == first_hash
        stage, extracted = _extract_validated(
            first_archive, install_root, first_manifest)
        target, archived, preserved, config_result = install_release(
            extracted, install_root, old_root, VERSION,
            config_drift_policy="preserve")
        shutil.rmtree(stage)
        assert target.is_dir() and not archived and not preserved
        assert config_result["status"] == "no_active_release"

        sequence = target / "_runtime" / "sensor_sequence.json"
        sequence.write_text('{"reserved_until":8192}\n', encoding="utf-8")
        os.chmod(sequence, 0o600)
        config_path = target.joinpath(*PRESERVED_CONFIG_RELATIVE.parts)
        _write_config(config_path, "commissioned-drift")
        commissioned_payload = config_path.read_bytes()
        commissioned_sha256 = sha256(config_path)

        second_tree = _build_tree(source / "second", "second")
        second_archive = home / (VERSION + "-second.zip")
        _zip_tree(second_tree, second_archive)
        second_hash = sha256(second_archive)
        second_manifest = sha256(second_tree / "SHA256SUMS")
        validate_archive(second_archive, VERSION, second_hash)
        stage, extracted = _extract_validated(
            second_archive, install_root, second_manifest)
        target, archived, preserved, config_result = install_release(
            extracted, install_root, old_root, VERSION,
            config_drift_policy="preserve")
        shutil.rmtree(stage)
        assert len(archived) == 1
        assert preserved == ["sensor_sequence.json"]
        assert config_result == {
            "status": "preserved",
            "source_version": VERSION,
            "baseline_sha256": _file_hash(
                first_tree.joinpath(*PRESERVED_CONFIG_RELATIVE.parts)),
            "preserved_sha256": commissioned_sha256,
        }
        assert "second" in (target / "run_worker.sh").read_text(encoding="utf-8")
        assert sequence.read_text(encoding="utf-8").strip() == '{"reserved_until":8192}'
        assert config_path.read_bytes() == commissioned_payload
        verify_release_tree(
            target, VERSION, second_manifest, allow_preserved_runtime=True,
            preserved_config_sha256=commissioned_sha256)

        # An unchanged manifest-baseline config is not treated as a commit.
        second_baseline = second_tree.joinpath(*PRESERVED_CONFIG_RELATIVE.parts).read_bytes()
        config_path.write_bytes(second_baseline)
        third_tree = _build_tree(source / "third", "third")
        third_archive = home / (VERSION + "-third.zip")
        _zip_tree(third_tree, third_archive)
        third_manifest = sha256(third_tree / "SHA256SUMS")
        validate_archive(third_archive, VERSION, sha256(third_archive))
        stage, extracted = _extract_validated(
            third_archive, install_root, third_manifest)
        target, _archived, _preserved, config_result = install_release(
            extracted, install_root, old_root, VERSION,
            config_drift_policy="preserve")
        shutil.rmtree(stage)
        config_path = target.joinpath(*PRESERVED_CONFIG_RELATIVE.parts)
        assert config_result["status"] == "baseline"
        assert config_result["preserved_sha256"] is None
        assert config_path.read_bytes() == third_tree.joinpath(
            *PRESERVED_CONFIG_RELATIVE.parts).read_bytes()

        # Grouped, V5 and V7 payloads at the V9 filename are never preserved.
        for suffix, version_value, grouped in (
                ("grouped", 9, True),
                ("v5", 5, False),
                ("v7", 7, False)):
            _write_config(
                config_path, "invalid-" + suffix,
                version=version_value, grouped=grouped)
            replacement = _build_tree(source / ("invalid-" + suffix), "new-" + suffix)
            replacement_archive = home / (VERSION + "-" + suffix + ".zip")
            _zip_tree(replacement, replacement_archive)
            replacement_manifest = sha256(replacement / "SHA256SUMS")
            validate_archive(
                replacement_archive, VERSION, sha256(replacement_archive))
            stage, extracted = _extract_validated(
                replacement_archive, install_root, replacement_manifest)
            target, _archived, _preserved, config_result = install_release(
                extracted, install_root, old_root, VERSION,
                config_drift_policy="preserve")
            shutil.rmtree(stage)
            config_path = target.joinpath(*PRESERVED_CONFIG_RELATIVE.parts)
            assert config_result["status"] == "invalid_noncanonical_config"
            assert config_result["preserved_sha256"] is None
            assert config_path.read_bytes() == replacement.joinpath(
                *PRESERVED_CONFIG_RELATIVE.parts).read_bytes()

        # Flat V9 shape alone is insufficient: the incoming Worker's complete
        # storage contract must accept a commissioned file before preservation.
        incomplete = _config_document("incomplete-worker-contract")
        config_path.write_text(
            json.dumps(incomplete, sort_keys=True) + "\n",
            encoding="utf-8",
            newline="\n")
        contract_tree = _build_tree(source / "contract", "contract-new")
        contract_archive = home / (VERSION + "-contract.zip")
        _zip_tree(contract_tree, contract_archive)
        contract_manifest = sha256(contract_tree / "SHA256SUMS")
        validate_archive(
            contract_archive, VERSION, sha256(contract_archive))
        stage, extracted = _extract_validated(
            contract_archive, install_root, contract_manifest)
        target, _archived, _preserved, config_result = install_release(
            extracted, install_root, old_root, VERSION,
            config_drift_policy="preserve")
        shutil.rmtree(stage)
        config_path = target.joinpath(*PRESERVED_CONFIG_RELATIVE.parts)
        assert config_result["status"] == "invalid_worker_contract_config"
        assert config_result["preserved_sha256"] is None
        assert config_path.read_bytes() == contract_tree.joinpath(
            *PRESERVED_CONFIG_RELATIVE.parts).read_bytes()

        # A committed config may only be preserved through the incoming
        # release's canonical Type Authority module.
        _write_config(config_path, "validator-required")
        validator_tree = _build_tree(
            source / "missing-validator", "missing-validator-new")
        validator_tree.joinpath(
            "src", "library", "_generic_type_library.py").unlink()
        _write_manifest(validator_tree)
        validator_archive = home / (VERSION + "-missing-validator.zip")
        _zip_tree(validator_tree, validator_archive)
        validator_manifest = sha256(validator_tree / "SHA256SUMS")
        validate_archive(
            validator_archive, VERSION, sha256(validator_archive))
        stage, extracted = _extract_validated(
            validator_archive, install_root, validator_manifest)
        try:
            install_release(
                extracted, install_root, old_root, VERSION,
                config_drift_policy="preserve")
        except RuntimeError as error:
            assert "V9 contract validator is missing or unsafe" in str(error)
        else:
            raise AssertionError("Worker release without Type Authority was installed")
        finally:
            shutil.rmtree(stage)
        assert target.is_dir()
        assert _config_document("validator-required") == json.loads(
            config_path.read_text(encoding="utf-8"))

        # Strict policy always installs the new canonical package baseline.
        _write_config(config_path, "valid-but-strict")
        strict_tree = _build_tree(source / "strict", "strict-new")
        strict_archive = home / (VERSION + "-strict.zip")
        _zip_tree(strict_tree, strict_archive)
        strict_manifest = sha256(strict_tree / "SHA256SUMS")
        validate_archive(strict_archive, VERSION, sha256(strict_archive))
        stage, extracted = _extract_validated(
            strict_archive, install_root, strict_manifest)
        target, _archived, _preserved, config_result = install_release(
            extracted, install_root, old_root, VERSION,
            config_drift_policy="strict")
        shutil.rmtree(stage)
        config_path = target.joinpath(*PRESERVED_CONFIG_RELATIVE.parts)
        assert config_result["status"] == "strict"
        assert config_result["source_version"] is None
        assert config_path.read_bytes() == strict_tree.joinpath(
            *PRESERVED_CONFIG_RELATIVE.parts).read_bytes()

        # A real older v0083 can contribute only the exact canonical V9 file.
        older_install_root = home / "older-upgrade"
        older_install_root.mkdir()
        older_tree = _build_tree(source / "older", "older-baseline", LEGACY_VERSION)
        shutil.copytree(older_tree, older_install_root / LEGACY_VERSION)
        older_config = (older_install_root / LEGACY_VERSION).joinpath(
            *PRESERVED_CONFIG_RELATIVE.parts)
        _write_config(older_config, "older-commissioned-v9")
        older_result = capture_committed_config(
            older_install_root, VERSION, "preserve")
        assert older_result["status"] == "eligible"
        assert older_result["source_version"] == LEGACY_VERSION
        assert older_result["preserved_sha256"] == sha256(older_config)
        older_payload = older_config.read_bytes()
        stage, extracted = _extract_validated(
            strict_archive, older_install_root, strict_manifest)
        older_target, older_archived, _runtime, older_result = install_release(
            extracted, older_install_root, home / "older-OLD", VERSION,
            config_drift_policy="preserve")
        shutil.rmtree(stage)
        assert len(older_archived) == 1
        assert older_result["status"] == "preserved"
        assert older_result["source_version"] == LEGACY_VERSION
        assert older_target.joinpath(
            *PRESERVED_CONFIG_RELATIVE.parts).read_bytes() == older_payload

        # Legacy v0083 V5/V7 filenames are outside the preservation contract.
        legacy_install_root = home / "legacy-upgrade"
        legacy_install_root.mkdir()
        _build_legacy_tree(legacy_install_root)
        legacy_result = capture_committed_config(
            legacy_install_root, VERSION, "preserve")
        assert legacy_result["status"] == "invalid_release_manifest"
        assert legacy_result["preserved_sha256"] is None

        unexpected_runtime = target / "_runtime" / "unexpected.json"
        unexpected_runtime.write_text('{}\n', encoding="utf-8")
        try:
            verify_release_tree(
                target, VERSION, strict_manifest, allow_preserved_runtime=True)
        except RuntimeError:
            pass
        else:
            raise AssertionError("unexpected Worker runtime file was accepted")
        unexpected_runtime.unlink()

        malicious = home / "malicious.zip"
        with zipfile.ZipFile(malicious, "w") as archive:
            archive.writestr("../escape", b"unsafe")
        try:
            validate_archive(malicious, VERSION, sha256(malicious))
        except RuntimeError:
            pass
        else:
            raise AssertionError("path traversal Worker ZIP was accepted")

        cli_root = home / "cli-install"
        cli_old = home / "cli-old"
        completed = subprocess.run(
            [
                sys.executable,
                str(Path(__file__).with_name("install_worker_release.py")),
                "--archive", str(first_archive),
                "--home", str(home),
                "--install-root", str(cli_root),
                "--old-root", str(cli_old),
                "--expected-version", VERSION,
                "--expected-sha256", first_hash,
                "--expected-manifest-sha256", first_manifest,
                "--config-drift-policy", "strict",
                "--verify-only",
            ],
            check=True,
            capture_output=True,
            text=True)
        cli_result = json.loads(completed.stdout)
        assert cli_result["config_drift_policy"] == "strict"
        assert cli_result["config_preservation_status"] == "verify_only"
        assert cli_result["preserved_config_path"] is None
        assert cli_result["preserved_config_sha256"] is None
    finally:
        shutil.rmtree(temporary, ignore_errors=True)
    print(
        "PASS Worker release installer: hash, manifest, path fence, OLD/runtime "
        "preservation and canonical committed V9-only config upgrade")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

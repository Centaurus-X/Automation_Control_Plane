# -*- coding: utf-8 -*-

"""Static regression gates for the venv-free validation container."""

import os
import shutil
import subprocess
import tempfile

from pathlib import Path


def require_fragments(path, fragments):
    content = path.read_text(encoding="utf-8")
    missing = [fragment for fragment in fragments if fragment not in content]
    if missing:
        raise AssertionError(
            "{} misses required validation fragments: {}".format(
                path, ", ".join(missing)))
    return content


def shell_command(path, path_prefix=None):
    if os.name != "nt":
        return [str(path)]
    bash = os.environ.get("AUTOMATION_TEST_BASH") or shutil.which("bash")
    if not bash:
        raise AssertionError("Bash is required for the Windows shell contract test")
    if path_prefix is not None:
        return [
            bash,
            "-lc",
            'export PATH="$1:$PATH"\nexec "$2"',
            "validation-shell",
            portable_path_forms(path_prefix)[-1],
            portable_path_forms(path)[-1],
        ]
    return [bash, str(path)]

def portable_path_forms(path):
    native = str(path)
    if os.name != "nt":
        return (native,)
    drive, tail = os.path.splitdrive(native)
    posix = "/" + drive[0].lower() + tail.replace("\\", "/") \
        if drive else native.replace("\\", "/")
    return tuple(dict.fromkeys((native, posix)))


def shell_path(path):
    return portable_path_forms(path)[-1]


def write_windows_install_shim(fake_bin):
    if os.name != "nt":
        return
    fake_install = fake_bin / "install"
    fake_install.write_text(
        "#!/usr/bin/env bash\n"
        "set -eu\n"
        "[[ \"${1:-}\" == -d ]] || exit 64\n"
        "shift\n"
        "if [[ \"${1:-}\" == -m ]]; then shift 2; fi\n"
        "mkdir -p -- \"$@\"\n",
        encoding="utf-8",
    )
    fake_install.chmod(0o700)

def test_e2e_path_mapping(root):
    with tempfile.TemporaryDirectory(prefix="automation-validation-paths-") as temporary:
        temporary_root = Path(temporary)
        fake_bin = temporary_root / "bin"
        result_root = temporary_root / "results"
        outside_root = temporary_root / "outside"
        fake_bin.mkdir()
        write_windows_install_shim(fake_bin)
        result_root.mkdir(mode=0o700)
        outside_root.mkdir(mode=0o700)
        proxy_log = result_root / "proxy.log"
        protocol_log = result_root / "protocol.log"
        secret_log = result_root / "secret.log"
        outside_log = outside_root / "outside.log"
        for path in (proxy_log, protocol_log, secret_log, outside_log):
            path.write_text("validation-log\n", encoding="utf-8")
        docker_environment = temporary_root / "docker-environment"
        docker_arguments = temporary_root / "docker-arguments"
        fake_docker = fake_bin / "docker"
        fake_docker.write_text(
            "#!/usr/bin/env bash\n"
            "set -eu\n"
            "if [[ \"${1:-}\" == run ]]; then\n"
            "  for name in E2E_CA_FILE E2E_PROXY_LOG_SOURCE "
            "E2E_PROTOCOL_LOG_SOURCE E2E_SECRET_LOG_SOURCE E2E_RESULT_FILE "
            "E2E_LOG_FILE E2E_RECOVERY_FILE; do\n"
            "    printf '%s=%s\\n' \"$name\" \"${!name:-}\"\n"
            "  done >\"$FAKE_DOCKER_ENVIRONMENT\"\n"
            "  printf '%s\\n' \"$@\" >\"$FAKE_DOCKER_ARGUMENTS\"\n"
            "fi\n"
            "exit 0\n",
            encoding="utf-8",
        )
        fake_docker.chmod(0o700)
        environment = os.environ.copy()
        for name in tuple(environment):
            if name.startswith("E2E_") or name == "CONTROL_PLANE_VALIDATION_ACTIVE":
                environment.pop(name, None)
        environment.update({
            "PATH": str(fake_bin) + os.pathsep + environment["PATH"],
            "FAKE_DOCKER_ENVIRONMENT": shell_path(docker_environment),
            "FAKE_DOCKER_ARGUMENTS": shell_path(docker_arguments),
            "CONTROL_PLANE_VALIDATION_IMAGE": "automation-validation-path-test:latest",
            "E2E_RESULT_DIR": shell_path(result_root),
            "E2E_CA_FILE": shell_path(root / "README.md"),
            "E2E_PROXY_LOG_SOURCE": shell_path(proxy_log),
            "E2E_PROTOCOL_LOG_SOURCE": shell_path(protocol_log),
            "E2E_SECRET_LOG_SOURCE": shell_path(secret_log),
            "E2E_RESULT_FILE": shell_path(result_root / "custom-result.json"),
            "E2E_LOG_FILE": shell_path(result_root / "custom-log.jsonl"),
            "E2E_RECOVERY_FILE": shell_path(result_root / "custom-recovery.json"),
        })
        command = shell_command(
            root / "integration_test" / "e2e_v0025" / "run_e2e.sh",
            fake_bin,
        )
        completed = subprocess.run(
            command, cwd=root, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, check=False,
        )
        if completed.returncode != 0:
            raise AssertionError("mapped E2E wrapper failed: " + completed.stdout)
        captured = {}
        for line in docker_environment.read_text(encoding="utf-8").splitlines():
            name, separator, value = line.partition("=")
            if separator:
                captured[name] = value
        expected = {
            "E2E_CA_FILE": "/release/README.md",
            "E2E_PROXY_LOG_SOURCE": "/results/proxy.log",
            "E2E_PROTOCOL_LOG_SOURCE": "/results/protocol.log",
            "E2E_SECRET_LOG_SOURCE": "/results/secret.log",
            "E2E_RESULT_FILE": "/results/custom-result.json",
            "E2E_LOG_FILE": "/results/custom-log.jsonl",
            "E2E_RECOVERY_FILE": "/results/custom-recovery.json",
        }
        for name, value in expected.items():
            if captured.get(name) != value:
                raise AssertionError(
                    "wrong mapped path for {}: {}".format(name, captured.get(name)))
        arguments = docker_arguments.read_text(encoding="utf-8").splitlines()
        required_mounts = (
            tuple(
                "type=bind,src={},dst=/release,readonly".format(value)
                for value in portable_path_forms(root)
            ),
            tuple(
                "type=bind,src={},dst=/results".format(value)
                for value in portable_path_forms(result_root)
            ),
        )
        for alternatives in required_mounts:
            if not any(value in arguments for value in alternatives):
                raise AssertionError(
                    "validation Docker mounts differ: " + repr(arguments))
        tmpfs = "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777"
        if tmpfs not in arguments:
            raise AssertionError(
                "validation tempfile mount is not executable: " + repr(arguments))

        docker_environment.unlink()
        environment["E2E_PROXY_LOG_SOURCE"] = shell_path(outside_log)
        blocked = subprocess.run(
            command, cwd=root, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, check=False,
        )
        if blocked.returncode != 64 or \
                "must be inside the release or E2E_RESULT_DIR" not in blocked.stdout:
            raise AssertionError("external E2E path was not blocked: " + blocked.stdout)
        if docker_environment.exists():
            raise AssertionError("external E2E path reached Docker")

        escape = result_root / "escape"
        try:
            escape.symlink_to(outside_root, target_is_directory=True)
        except OSError as error:
            if getattr(error, "winerror", None) == 1314:
                return False
            raise
        environment["E2E_PROXY_LOG_SOURCE"] = shell_path(escape / outside_log.name)
        blocked = subprocess.run(
            command, cwd=root, env=environment,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
            text=True, check=False,
        )
        if blocked.returncode != 64:
            raise AssertionError("symlink path escape was not blocked: " + blocked.stdout)
        return True

def main():
    root = Path(__file__).resolve().parent.parent
    runner = require_fragments(root / "tools" / "run_in_validation_container.sh", (
        "docker build --pull",
        "docker run",
        "--network host",
        "--read-only",
        "/tmp:rw,exec,nosuid,nodev,size=512m,mode=1777",
        "CONTROL_PLANE_VALIDATION_ACTIVE=/release",
        "E2E_RESULT_DIR=/results",
        "automation-control-plane-validation:0.26.0",
    ))
    dockerfile = require_fragments(root / "runtime" / "validation" / "Dockerfile", (
        "FROM python:3.14.6-slim-bookworm",
        "requirements-validation.txt",
        "integration_test/e2e_v0025/requirements-e2e.txt",
        "python -m pip check",
        "psycopg-binary==3.3.4",
        "procps",
    ))
    dockerignore = require_fragments(root / ".dockerignore", (
        "**",
        "!requirements-validation.txt",
        "!runtime/validation/Dockerfile",
        "!master_node/requirements.txt",
        "!proxy_gateway/requirements.txt",
        "!integration_test/e2e_v0025/requirements-e2e.txt",
    ))
    require_fragments(root / "integration_test" / "e2e_v0025" / "run_e2e.sh", (
        "map_validation_path",
        "E2E_RESULT_FILE E2E_LOG_FILE E2E_RECOVERY_FILE",
        "must be inside the release or E2E_RESULT_DIR",
        "CONTROL_PLANE_VALIDATION_RESULT_DIR",
    ))
    entrypoints = (
        root / "test_static_all.sh",
        root / "proxy_gateway" / "test.sh",
        root / "integration_test" / "e2e_v0025" / "test_static.sh",
        root / "integration_test" / "e2e_v0025" / "run_e2e.sh",
    )
    for path in entrypoints:
        content = path.read_text(encoding="utf-8")
        if "run_in_validation_container.sh" not in content:
            raise AssertionError("container validation wrapper missing: " + str(path))
        if "run_in_host_validation_env.sh" in content:
            raise AssertionError("legacy host validation wrapper remains: " + str(path))
    forbidden = ("import venv", "venv.EnvBuilder", "python3-venv")
    combined = runner + dockerfile + dockerignore
    if any(fragment in combined for fragment in forbidden):
        raise AssertionError("validation path still creates a host virtual environment")
    symlink_checked = test_e2e_path_mapping(root)
    capability = "" if symlink_checked else " (Windows symlink creation unavailable)"
    print("PASS venv-free, pinned Docker validation runner contract" + capability)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

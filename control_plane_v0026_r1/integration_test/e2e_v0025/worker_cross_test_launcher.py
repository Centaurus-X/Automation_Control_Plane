#!/usr/bin/env python3
# -*- coding: utf-8 -*-

"""External fn-01 cross-runtime launcher without Worker source changes."""

import argparse
import datetime
import hashlib
import importlib.util
import json
import os
import pathlib
import signal
import subprocess
import sys
import tempfile
import time


VERSION = "0.25.0-e2e.3"
EXPECTED_WORKER_ARCHIVE_SHA256 = (
    "c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467"
)
STATUS_CODES = {"PASSED": 0, "FAILED": 1, "BLOCKED": 3, "SKIPPED": 4}


def _utc_now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def _load_json(path, label):
    try:
        value = json.loads(pathlib.Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise RuntimeError("BLOCKED: invalid {}: {}".format(label, exc)) from exc
    if not isinstance(value, dict):
        raise RuntimeError("BLOCKED: {} must contain a JSON object".format(label))
    return value


def _sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        while True:
            block = handle.read(1024 * 1024)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def _inside(path, root):
    try:
        pathlib.Path(path).resolve().relative_to(pathlib.Path(root).resolve())
        return True
    except ValueError:
        return False


def _validate_command(value, label):
    if not isinstance(value, list) or not value:
        raise RuntimeError("BLOCKED: {} must be a non-empty argv array".format(label))
    if any(not isinstance(item, str) or not item for item in value):
        raise RuntimeError("BLOCKED: {} contains an invalid argv item".format(label))
    return list(value)


def _validate_plan(plan):
    required = (
        "worker_root",
        "worker_archive",
        "app_config_relative_path",
        "normal_stop_command",
        "normal_start_command",
        "normal_status_command",
        "private_runtime_dir",
        "recovery_file",
    )
    missing = [name for name in required if not plan.get(name)]
    if missing:
        raise RuntimeError("BLOCKED: Worker launcher plan misses " + ", ".join(missing))
    root = pathlib.Path(plan["worker_root"]).expanduser().resolve()
    archive = pathlib.Path(plan["worker_archive"]).expanduser().resolve()
    app_config = (root / str(plan["app_config_relative_path"])).resolve()
    runtime = pathlib.Path(plan["private_runtime_dir"]).expanduser().resolve()
    recovery = pathlib.Path(plan["recovery_file"]).expanduser().resolve()
    if not root.is_dir():
        raise RuntimeError("BLOCKED: external Worker root is unavailable: " + str(root))
    if not archive.is_file():
        raise RuntimeError("BLOCKED: external Worker archive is unavailable: " + str(archive))
    if not app_config.is_file() or not _inside(app_config, root):
        raise RuntimeError("BLOCKED: Worker app_config is missing or escapes Worker root")
    if _inside(runtime, root):
        raise RuntimeError("BLOCKED: private runtime directory must remain outside Worker root")
    if recovery.parent != runtime:
        raise RuntimeError("BLOCKED: recovery file must be directly inside private runtime directory")
    expected = str(plan.get("expected_archive_sha256") or EXPECTED_WORKER_ARCHIVE_SHA256)
    if expected != EXPECTED_WORKER_ARCHIVE_SHA256:
        raise RuntimeError("BLOCKED: Worker archive expectation differs from v0025 contract")
    context = {
        "plan": plan,
        "root": root,
        "archive": archive,
        "app_config": app_config,
        "runtime": runtime,
        "recovery": recovery,
        "expected_archive_sha256": expected,
        "profile": str(plan.get("active_profile") or "worker_simulation"),
        "python": str(plan.get("python_executable") or sys.executable),
        "startup_grace_s": float(plan.get("startup_grace_s") or 3.0),
        "command_timeout_s": float(plan.get("command_timeout_s") or 30.0),
    }
    for name in ("normal_stop_command", "normal_start_command", "normal_status_command"):
        context[name] = _validate_command(plan[name], name)
    if context["startup_grace_s"] <= 0 or context["command_timeout_s"] <= 0:
        raise RuntimeError("BLOCKED: launcher timeouts must be positive")
    return context


def _safe_detail(text):
    value = str(text or "").replace("\x00", "")
    return value[-2048:]


def _run_command(context, name):
    completed = subprocess.run(
        context[name],
        cwd=str(context["root"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=context["command_timeout_s"],
    )
    return {
        "returncode": completed.returncode,
        "output": _safe_detail(completed.stdout),
    }


def _write_json_exclusive(path, value):
    path = pathlib.Path(path)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path.parent, 0o700)
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        os.write(descriptor, payload.encode("utf-8") + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


def _replace_json(path, value):
    path = pathlib.Path(path)
    temporary = path.with_name(path.name + ".tmp-" + str(os.getpid()))
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        payload = json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True)
        os.write(descriptor, payload.encode("utf-8") + b"\n")
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    os.replace(temporary, path)
    os.chmod(path, 0o600)


def _read_recovery(context):
    if not context["recovery"].exists():
        return None
    return _load_json(context["recovery"], "Worker recovery journal")


def _preflight(context, **kwargs):
    archive_hash = _sha256(context["archive"])
    app_hash = _sha256(context["app_config"])
    if archive_hash != context["expected_archive_sha256"]:
        raise RuntimeError(
            "BLOCKED: external Worker archive hash differs: " + archive_hash
        )
    recovery = _read_recovery(context)
    if recovery and recovery.get("status") not in ("COMPLETED",):
        raise RuntimeError("BLOCKED: open Worker recovery journal requires restore first")
    status = _run_command(context, "normal_status_command")
    if kwargs.get("require_running", True) and status["returncode"] != 0:
        raise RuntimeError("BLOCKED: normal fn-01 process is not healthy")
    return {
        "status": "PASSED",
        "worker_archive_sha256": archive_hash,
        "app_config_sha256": app_hash,
        "normal_worker_running": status["returncode"] == 0,
        "worker_package_modified": False,
    }


def _set_cross_enabled(document, profile):
    if not isinstance(document, dict):
        raise RuntimeError("BLOCKED: Worker app_config root must be a mapping")
    profiles = document.get("profiles")
    if not isinstance(profiles, dict) or not isinstance(profiles.get(profile), dict):
        raise RuntimeError("BLOCKED: active Worker profile is absent in app_config")
    selected = profiles[profile]
    cross = selected.get("cross_runtime")
    if not isinstance(cross, dict) or "enabled" not in cross:
        raise RuntimeError("BLOCKED: cross_runtime.enabled is absent from Worker profile")
    cross["enabled"] = True
    if cross.get("enabled") is not True:
        raise RuntimeError("BLOCKED: cross_runtime overlay did not become true")
    return document


def _write_overlay(context, recovery):
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("BLOCKED: PyYAML from requirements-e2e.txt is unavailable") from exc
    with open(context["app_config"], "r", encoding="utf-8") as handle:
        document = yaml.safe_load(handle)
    document = _set_cross_enabled(document, context["profile"])
    overlay = context["runtime"] / ("app-config-" + recovery["run_id"] + ".yaml")
    descriptor = os.open(overlay, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        payload = yaml.safe_dump(document, allow_unicode=True, sort_keys=False)
        os.write(descriptor, payload.encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    recovery["overlay_path"] = str(overlay)
    recovery["overlay_sha256"] = _sha256(overlay)
    return overlay


def _wrapper_source(context, overlay):
    values = {
        "worker_root": str(context["root"]),
        "overlay": str(overlay),
        "profile": context["profile"],
        "bootstrap_module": str(
            context["plan"].get("bootstrap_module") or "src.system.bootstrap_system_interface"
        ),
        "main_module": str(context["plan"].get("main_module") or "src.sync_xserver_main"),
    }
    literal = json.dumps(values, ensure_ascii=True, sort_keys=True)
    return """#!/usr/bin/env python3
import importlib
import json
import os
import sys

OPTIONS = json.loads({literal!r})
sys.path.insert(0, OPTIONS["worker_root"])
os.environ["AUTOMATION_PROFILE"] = OPTIONS["profile"]
bootstrap = importlib.import_module(OPTIONS["bootstrap_module"])
bootstrap.CONTEXT["options"]["default_config_path"] = OPTIONS["overlay"]
if "--preflight" in sys.argv:
    validator = getattr(bootstrap, "validate_worker_environment", None)
    if not callable(validator):
        raise RuntimeError("Worker bootstrap has no validate_worker_environment")
    result = validator(
        config_path=OPTIONS["overlay"], active_profile=OPTIONS["profile"])
    if result is False:
        raise RuntimeError("Worker overlay preflight was rejected")
    raise SystemExit(0)
runtime = importlib.import_module(OPTIONS["main_module"])
raise SystemExit(runtime.main())
""".format(literal=literal)


def _write_wrapper(context, recovery):
    overlay = pathlib.Path(recovery["overlay_path"])
    wrapper = context["runtime"] / ("worker-wrapper-" + recovery["run_id"] + ".py")
    descriptor = os.open(wrapper, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o700)
    try:
        os.write(descriptor, _wrapper_source(context, overlay).encode("utf-8"))
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
    recovery["wrapper_path"] = str(wrapper)
    recovery["wrapper_sha256"] = _sha256(wrapper)
    return wrapper


def _prepare_existing_recovery(context):
    existing = _read_recovery(context)
    if not existing:
        return
    if existing.get("status") != "COMPLETED":
        raise RuntimeError("BLOCKED: open Worker recovery journal requires restore first")
    archive = context["recovery"].with_name(
        context["recovery"].name + ".completed-" + str(existing.get("run_id") or "old")
    )
    if archive.exists():
        raise RuntimeError("BLOCKED: completed Worker recovery archive already exists")
    os.replace(context["recovery"], archive)


def _start(context, run_id):
    _preflight(context)
    _prepare_existing_recovery(context)
    context["runtime"].mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(context["runtime"], 0o700)
    recovery = {
        "version": VERSION,
        "run_id": run_id,
        "status": "OPEN",
        "started_at": _utc_now(),
        "updated_at": _utc_now(),
        "worker_archive_sha256": _sha256(context["archive"]),
        "app_config_sha256": _sha256(context["app_config"]),
        "normal_worker_stopped": False,
        "test_worker_pid": None,
        "overlay_path": None,
        "wrapper_path": None,
        "worker_package_modified": False,
    }
    _write_json_exclusive(context["recovery"], recovery)
    stopped = _run_command(context, "normal_stop_command")
    if stopped["returncode"] != 0:
        recovery["status"] = "FAILED"
        recovery["failure"] = "normal Worker stop command failed"
        recovery["updated_at"] = _utc_now()
        _replace_json(context["recovery"], recovery)
        raise RuntimeError("FAILED: normal fn-01 process could not be stopped")
    recovery["normal_worker_stopped"] = True
    recovery["updated_at"] = _utc_now()
    _replace_json(context["recovery"], recovery)
    overlay = _write_overlay(context, recovery)
    wrapper = _write_wrapper(context, recovery)
    _replace_json(context["recovery"], recovery)
    checked = subprocess.run(
        [context["python"], str(wrapper), "--preflight"],
        cwd=str(context["runtime"]),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        check=False,
        timeout=context["command_timeout_s"],
    )
    if checked.returncode != 0:
        raise RuntimeError("FAILED: Worker overlay preflight failed: " + _safe_detail(checked.stdout))
    log_path = context["runtime"] / ("worker-cross-" + run_id + ".log")
    descriptor = os.open(log_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    output = os.fdopen(descriptor, "w", encoding="utf-8")
    try:
        process = subprocess.Popen(
            [context["python"], str(wrapper)],
            cwd=str(context["runtime"]),
            env=dict(os.environ, AUTOMATION_PROFILE=context["profile"]),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
    finally:
        output.close()
    recovery["test_worker_pid"] = process.pid
    recovery["test_worker_log"] = str(log_path)
    recovery["updated_at"] = _utc_now()
    _replace_json(context["recovery"], recovery)
    time.sleep(context["startup_grace_s"])
    if process.poll() is not None:
        raise RuntimeError("FAILED: cross-runtime Worker process exited during startup")
    return {
        "status": "PASSED",
        "run_id": run_id,
        "test_worker_pid": process.pid,
        "overlay_sha256": recovery["overlay_sha256"],
        "worker_archive_sha256": recovery["worker_archive_sha256"],
        "worker_package_modified": False,
    }


def _pid_matches(recovery):
    pid = int(recovery.get("test_worker_pid") or 0)
    wrapper = str(recovery.get("wrapper_path") or "")
    if pid <= 1 or not wrapper:
        return False
    command_path = pathlib.Path("/proc") / str(pid) / "cmdline"
    try:
        command = command_path.read_bytes().replace(b"\x00", b" ").decode(
            "utf-8", errors="replace"
        )
    except OSError:
        return False
    return wrapper in command


def _stop_test_worker(recovery):
    pid = int(recovery.get("test_worker_pid") or 0)
    if pid <= 1:
        return True
    if not _pid_matches(recovery):
        process_path = pathlib.Path("/proc") / str(pid)
        if process_path.exists():
            raise RuntimeError("FAILED: refusing to signal a PID not owned by the E2E wrapper")
        return True
    os.killpg(pid, signal.SIGTERM)
    deadline = time.monotonic() + 15.0
    while time.monotonic() < deadline:
        if not (pathlib.Path("/proc") / str(pid)).exists():
            return True
        time.sleep(0.2)
    if _pid_matches(recovery):
        os.killpg(pid, signal.SIGKILL)
    return not (pathlib.Path("/proc") / str(pid)).exists()


def _remove_private_paths(context, recovery):
    for name in ("wrapper_path", "overlay_path"):
        path = recovery.get(name)
        if not path:
            continue
        candidate = pathlib.Path(path).resolve()
        if candidate.parent != context["runtime"]:
            raise RuntimeError("FAILED: refusing to remove a path outside private runtime")
        try:
            candidate.unlink()
        except FileNotFoundError:
            pass


def _restore(context):
    recovery = _read_recovery(context)
    if not recovery:
        raise RuntimeError("BLOCKED: no Worker recovery journal exists")
    if recovery.get("status") == "COMPLETED":
        return {"status": "PASSED", "already_restored": True}
    recovery["status"] = "RECOVERING"
    recovery["updated_at"] = _utc_now()
    _replace_json(context["recovery"], recovery)
    stopped = _stop_test_worker(recovery)
    _remove_private_paths(context, recovery)
    started = {"returncode": 0, "output": "not required"}
    if recovery.get("normal_worker_stopped"):
        started = _run_command(context, "normal_start_command")
    healthy = _run_command(context, "normal_status_command")
    archive_hash = _sha256(context["archive"])
    app_hash = _sha256(context["app_config"])
    restored = (
        stopped
        and started["returncode"] == 0
        and healthy["returncode"] == 0
        and archive_hash == recovery.get("worker_archive_sha256")
        and app_hash == recovery.get("app_config_sha256")
    )
    recovery["status"] = "COMPLETED" if restored else "FAILED"
    recovery["updated_at"] = _utc_now()
    recovery["restore_confirmed"] = restored
    recovery["final_worker_archive_sha256"] = archive_hash
    recovery["final_app_config_sha256"] = app_hash
    _replace_json(context["recovery"], recovery)
    if not restored:
        raise RuntimeError("FAILED: fn-01 Worker restore or source-hash verification failed")
    return {
        "status": "PASSED",
        "restore_confirmed": True,
        "worker_archive_sha256": archive_hash,
        "app_config_sha256": app_hash,
        "worker_package_modified": False,
    }


def _status(context):
    recovery = _read_recovery(context)
    if not recovery:
        return {"status": "SKIPPED", "detail": "no launcher run exists"}
    running = _pid_matches(recovery)
    return {
        "status": "PASSED" if running else "SKIPPED",
        "run_id": recovery.get("run_id"),
        "recovery_status": recovery.get("status"),
        "test_worker_running": running,
    }


def _self_test():
    document = {
        "profiles": {"worker_simulation": {"cross_runtime": {"enabled": False}}}
    }
    updated = _set_cross_enabled(document, "worker_simulation")
    if updated["profiles"]["worker_simulation"]["cross_runtime"]["enabled"] is not True:
        raise AssertionError("cross overlay self-test failed")
    source_context = {
        "root": pathlib.Path("/tmp/worker"),
        "profile": "worker_simulation",
        "plan": {},
    }
    wrapper = _wrapper_source(source_context, pathlib.Path("/tmp/overlay.yaml"))
    compile(wrapper, "generated-worker-wrapper.py", "exec")
    if "default_config_path" not in wrapper or "sync_xserver_main" not in wrapper:
        raise AssertionError("generated wrapper misses required Worker integration")
    with tempfile.TemporaryDirectory(prefix="worker-launcher-self-test-") as temporary:
        path = pathlib.Path(temporary) / "state.json"
        _write_json_exclusive(path, {"status": "OPEN"})
        _replace_json(path, {"status": "COMPLETED"})
        if _load_json(path, "self-test")["status"] != "COMPLETED":
            raise AssertionError("atomic recovery journal self-test failed")
        if (path.stat().st_mode & 0o777) != 0o600:
            raise AssertionError("recovery journal mode is not 0600")
    print("PASS Worker cross launcher self-test")
    return 0


def _parser():
    parser = argparse.ArgumentParser(
        description="External, source-preserving Worker v0101 cross-runtime launcher"
    )
    parser.add_argument("--plan", help="operator-owned Worker launcher plan JSON")
    parser.add_argument(
        "--action", choices=("preflight", "start", "status", "restore"), default="preflight"
    )
    parser.add_argument("--run-id", default="e2e-" + str(int(time.time())))
    parser.add_argument("--self-test", action="store_true")
    parser.add_argument("--version", action="version", version=VERSION)
    return parser


def _emit(status, **kwargs):
    value = {"version": VERSION, "status": status, "updated_at": _utc_now()}
    value.update(kwargs)
    print(json.dumps(value, ensure_ascii=False, sort_keys=True))
    return STATUS_CODES[status]


def main():
    arguments = _parser().parse_args()
    if arguments.self_test:
        return _self_test()
    if not arguments.plan:
        return _emit("BLOCKED", detail="--plan is required outside --self-test")
    context = None
    try:
        context = _validate_plan(_load_json(arguments.plan, "Worker launcher plan"))
        if arguments.action == "preflight":
            result = _preflight(context)
        elif arguments.action == "start":
            result = _start(context, arguments.run_id)
        elif arguments.action == "status":
            result = _status(context)
        else:
            result = _restore(context)
        status = result.pop("status")
        return _emit(status, **result)
    except Exception as exc:
        text = str(exc)
        if (
            arguments.action == "start"
            and context is not None
            and context["recovery"].exists()
        ):
            try:
                restored = _restore(context)
                text += "; automatic restore confirmed={}".format(
                    bool(restored.get("restore_confirmed") or restored.get("already_restored"))
                )
            except Exception as restore_error:
                text += "; automatic restore failed: " + str(restore_error)
        status = "BLOCKED" if text.startswith("BLOCKED:") else "FAILED"
        return _emit(status, detail=text.split(":", 1)[-1].strip())


if __name__ == "__main__":
    raise SystemExit(main())

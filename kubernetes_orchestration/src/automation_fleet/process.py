"""Bounded subprocess execution without shell interpretation."""

import os
import subprocess


MAX_CAPTURE_BYTES = 2 * 1024 * 1024


def _validate_command(command):
    if not isinstance(command, (list, tuple)) or not command:
        raise ValueError("command_type: command must be a non-empty sequence")
    result = []
    for argument in command:
        if not isinstance(argument, str) or "\x00" in argument:
            raise ValueError("command_argument: command arguments must be safe strings")
        result.append(argument)
    return result


def _bounded_text(value):
    data = (value or "").encode("utf-8", errors="replace")
    if len(data) <= MAX_CAPTURE_BYTES:
        return data.decode("utf-8", errors="replace")
    suffix = b"\n[output truncated by automation fleet provider]\n"
    return (data[:MAX_CAPTURE_BYTES - len(suffix)] + suffix).decode(
        "utf-8", errors="replace")


def run_process(command, timeout_s=60, input_text=None, environment=None, cwd=None):
    arguments = _validate_command(command)
    if input_text is not None and len(input_text.encode("utf-8")) > MAX_CAPTURE_BYTES:
        raise ValueError("command_input: process input exceeds the safe limit")
    process_environment = os.environ.copy()
    process_environment.update({
        "LC_ALL": "C.UTF-8",
        "LANG": "C.UTF-8",
        "PAGER": "cat",
        "HELM_DRIVER": process_environment.get("HELM_DRIVER", "secret"),
    })
    if environment:
        for key, value in environment.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError("environment_type: environment entries must be strings")
            process_environment[key] = value
    try:
        completed = subprocess.run(
            arguments,
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout_s,
            cwd=cwd,
            env=process_environment,
            check=False,
        )
        return {
            "command": arguments,
            "return_code": completed.returncode,
            "stdout": _bounded_text(completed.stdout),
            "stderr": _bounded_text(completed.stderr),
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "command": arguments,
            "return_code": 124,
            "stdout": _bounded_text(exc.stdout),
            "stderr": _bounded_text(exc.stderr),
            "timed_out": True,
        }


def require_success(result, label):
    if result["return_code"] != 0:
        detail = result["stderr"].strip() or result["stdout"].strip()
        if len(detail) > 1000:
            detail = detail[:1000] + "..."
        raise RuntimeError(label + " failed: " + detail)
    return result

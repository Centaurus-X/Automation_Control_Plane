# -*- coding: utf-8 -*-

"""Drive one authenticated v2 config commit through the public HTTPS API."""

import argparse
import http.cookiejar
import json
import ssl
import urllib.error
import urllib.request


def _arguments():
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--username", required=True)
    parser.add_argument("--password-file", required=True)
    parser.add_argument("--output", required=True)
    return parser.parse_args()


def _client():
    cookies = http.cookiejar.CookieJar()
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    return urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(cookies),
        urllib.request.HTTPSHandler(context=context),
    ), cookies


def _request(opener, url, **kwargs):
    body = kwargs.get("body")
    headers = {
        "Accept": "application/json",
        "User-Agent": "automation-control-plane-lab-v2/0.26.0",
    }
    headers.update(kwargs.get("headers") or {})
    encoded = None
    if body is not None:
        encoded = json.dumps(
            body, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        headers["Content-Type"] = "application/json"
    request = urllib.request.Request(
        url, data=encoded, headers=headers,
        method=kwargs.get("method") or ("POST" if encoded is not None else "GET"),
    )
    try:
        with opener.open(request, timeout=15.0) as response:
            result = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        try:
            result = json.loads(exc.read().decode("utf-8"))
        except Exception:
            result = {"ok": False, "code": "http_error", "status": exc.code}
    if not isinstance(result, dict):
        raise RuntimeError("API response is not a JSON object")
    return result


def _require(result, action):
    if not result.get("ok"):
        raise RuntimeError("{} failed: {}".format(
            action, result.get("code") or result.get("message") or "unknown"))
    return result


def main():
    arguments = _arguments()
    with open(arguments.password_file, "r", encoding="utf-8") as handle:
        password = handle.read().strip()
    opener, cookies = _client()
    base = arguments.base_url.rstrip("/")
    _require(_request(opener, base + "/api/v1/auth/login", body={
        "username": arguments.username,
        "password": password,
    }), "login")
    csrf = next(
        (cookie.value for cookie in cookies if cookie.name == "automation_csrf"), None)
    if not csrf:
        raise RuntimeError("login did not return the CSRF cookie")
    history = _require(_request(
        opener, base + "/api/v1/config/history?limit=1"), "config history")
    rows = history.get("history") or []
    if not rows:
        raise RuntimeError("config history is empty")
    base_revision = int(rows[0].get("global_revision") or rows[0].get("revision") or 0)
    if base_revision < 1:
        raise RuntimeError("config history has no valid global revision")
    marker = "lab-v2-patch-revision-{}".format(base_revision + 1)
    proposal = _require(_request(
        opener,
        base + "/api/v1/config/proposals",
        body={
            "base_revision": base_revision,
            "reason": "automated lab-v2 Node/Cross Patch acceptance",
            "changes": [
                {"operation": "set", "path": "rules.0.description", "value": marker + "-fn01"},
                {"operation": "set", "path": "rules.2.description", "value": marker + "-cross"},
                {"operation": "set", "path": "rules.3.description", "value": marker + "-fn02"},
            ],
        },
        headers={
            "Idempotency-Key": "lab-v2-patch-{}".format(base_revision + 1),
            "X-CSRF-Token": csrf,
        },
    ), "config proposal")
    revision = int(proposal.get("revision") or 0)
    if revision != base_revision + 1:
        raise RuntimeError("config proposal did not create exactly one new revision")
    result = {
        "ok": True,
        "base_revision": base_revision,
        "revision": revision,
        "marker": marker,
        "rollout_ids": proposal.get("rollout_ids") or [],
    }
    with open(arguments.output, "w", encoding="utf-8") as handle:
        json.dump(result, handle, ensure_ascii=False, indent=2, sort_keys=True)
        handle.write("\n")
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""WSGI authentication, CSRF, planning and approval tests."""

import io
import json
import pathlib
import tempfile

from automation_fleet.api import create_application
from automation_fleet.canonical import atomic_json
from common import catalog, desired_state, inventory, policy, release_root


def _invoke(application, method, path, body=None, cookie=None, csrf=None,
            authorization=None, tls_subject=None):
    payload = b"" if body is None else json.dumps(body).encode("utf-8")
    environ = {
        "REQUEST_METHOD": method,
        "PATH_INFO": path,
        "CONTENT_LENGTH": str(len(payload)),
        "CONTENT_TYPE": "application/json",
        "HTTP_HOST": "127.0.0.1:9080",
        "HTTP_ORIGIN": "http://127.0.0.1:9080",
        "wsgi.input": io.BytesIO(payload),
    }
    if cookie:
        environ["HTTP_COOKIE"] = cookie
    if csrf:
        environ["HTTP_X_CSRF_TOKEN"] = csrf
    if authorization:
        environ["HTTP_AUTHORIZATION"] = authorization
    if tls_subject:
        environ["SSL_CLIENT_S_DN"] = tls_subject
    result = {}

    def capture(status, headers):
        result["status"] = status
        result["headers"] = headers

    chunks = application(environ, capture)
    result["body"] = json.loads(b"".join(chunks).decode("utf-8"))
    return result


def _header(response, name):
    for key, value in response["headers"]:
        if key.lower() == name.lower():
            return value
    return None


def main():
    with tempfile.TemporaryDirectory(prefix="automation-fleet-api-") as directory:
        base = pathlib.Path(directory)
        admin_token = "a" * 64
        operator_token = "o" * 64
        approver_token = "p" * 64
        viewer_token = "v" * 64
        atomic_json(base / "inventory.json", inventory())
        atomic_json(base / "catalog.json", catalog())
        atomic_json(base / "desired.json", desired_state())
        atomic_json(base / "policy.json", policy())
        config = {
            "inventory_path": str(base / "inventory.json"),
            "catalog_path": str(base / "catalog.json"),
            "desired_state_path": str(base / "desired.json"),
            "policy_path": str(base / "policy.json"),
            "state_dir": str(base / "state"),
            "allow_mutations": False,
            "cookie_secure": False,
            "observe_timeout_s": 1,
        }
        context = {
            "root": str(release_root()),
            "config": config,
            "tokens": {"admin": admin_token, "operator": operator_token,
                       "approver": approver_token, "viewer": viewer_token},
            "agents": {},
            "debug": False,
        }
        application = create_application(context)
        login = _invoke(application, "POST", "/api/v1/session", {"token": operator_token})
        assert login["status"].startswith("200")
        session_cookie = _header(login, "Set-Cookie")
        assert "HttpOnly" in session_cookie and "SameSite=Strict" in session_cookie
        assert [value for name, value in login["headers"]
                if name.lower() == "cache-control"] == ["no-store"]
        cookie = session_cookie.split(";", 1)[0]
        csrf = login["body"]["csrf_token"]
        desired = _invoke(application, "GET", "/api/v1/desired", cookie=cookie)
        assert desired["status"].startswith("200")
        denied = _invoke(application, "POST", "/api/v1/plans",
                         desired_state(), cookie=cookie)
        assert denied["status"].startswith("403")
        created = _invoke(application, "POST", "/api/v1/plans",
                          desired_state(), cookie=cookie, csrf=csrf)
        assert created["status"].startswith("201")
        plan_id = created["body"]["plan"]["plan_id"]
        same_principal = _invoke(application, "POST",
                                 "/api/v1/plans/" + plan_id + "/approve",
                                 {}, cookie=cookie, csrf=csrf)
        assert same_principal["status"].startswith("403")
        approver_login = _invoke(
            application, "POST", "/api/v1/session", {"token": approver_token})
        approver_cookie = _header(approver_login, "Set-Cookie").split(";", 1)[0]
        approver_csrf = approver_login["body"]["csrf_token"]
        approved = _invoke(application, "POST",
                           "/api/v1/plans/" + plan_id + "/approve",
                           {}, cookie=approver_cookie, csrf=approver_csrf)
        assert approved["status"].startswith("200")
        plans = _invoke(application, "GET", "/api/v1/plans", cookie=approver_cookie)
        assert plans["body"]["plans"][0]["principal"] == "web:token:approver"
        evidence = _invoke(
            application, "GET", "/api/v1/plans/" + plan_id + "/evidence",
            cookie=approver_cookie)
        assert evidence["status"].startswith("200")
        assert evidence["body"]["bundle_digest"].startswith("sha256:")
        viewer_login = _invoke(
            application, "POST", "/api/v1/session", {"token": viewer_token})
        viewer_cookie = _header(viewer_login, "Set-Cookie").split(";", 1)[0]
        audit_denied = _invoke(
            application, "GET", "/api/v1/audit", cookie=viewer_cookie)
        assert audit_denied["status"].startswith("403")
    print("PASS WSGI session, CSRF, deterministic plan and exact approval")


if __name__ == "__main__":
    main()

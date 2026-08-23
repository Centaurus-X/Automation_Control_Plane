# -*- coding: utf-8 -*-

"""Strict shared REST/WSS client-command dispatcher regression tests."""

import asyncio
import copy
import datetime
import json
import logging
import threading
import uuid

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.core.client_command_dispatcher import create_client_command_dispatcher


SITE_ID = "site-main"
ALL_ACTIONS = frozenset((
    "users.list",
    "user.create",
    "roles.list",
    "permissions.list",
    "api_tokens.list",
    "api_tokens.create",
    "api_tokens.revoke",
    "sites.list",
    "nodes.list",
    "node.get",
    "runtime.status",
    "config.get",
    "config.history",
    "config.diff",
    "config.propose",
    "config.rollback",
    "actuator.request",
    "operation.get",
    "audit.list",
    "node.drain",
    "node.state.set",
    "node.admission.approve",
    "terminal.execute",
))
MUTATIONS = frozenset((
    "user.create",
    "api_tokens.create",
    "api_tokens.revoke",
    "config.propose",
    "config.rollback",
    "actuator.request",
    "node.drain",
    "node.state.set",
    "node.admission.approve",
    "terminal.execute",
))


def _principal(**kwargs):
    permissions = kwargs.get("permissions")
    if permissions is None:
        permissions = (
            "user.manage", "role.manage", "node.read", "node.drain",
            "node.admit",
            "config.read", "config.propose", "config.rollback",
            "actuator.request", "operation.read", "audit.read",
            "node.manage", "terminal.execute",
        )
    return {
        "subject": kwargs.get("subject", "user-main"),
        "permissions": list(permissions),
        "site_scopes": list(kwargs.get("site_scopes", (SITE_ID,))),
        "resource_scopes": copy.deepcopy(kwargs.get("resource_scopes", [])),
        "_authentication_method": kwargs.get("authentication_method", "api_token"),
        "session_id": kwargs.get("session_id"),
    }


def _rate_consume(state, key, **kwargs):
    state["rate_calls"].append({
        "key": key,
        "limit": kwargs.get("limit"),
        "window_s": kwargs.get("window_s"),
    })
    return {"allowed": True, "remaining": 99}


def _audit_append(state, record):
    state["audit_records"].append(copy.deepcopy(record))
    return make_success()


def _audit_list(state, **kwargs):
    return copy.deepcopy(state["audit_records"][:int(kwargs.get("limit") or 100)])


def _authenticate_session(state, request):
    state["session_auth_calls"].append(copy.deepcopy(request))
    if request.get("session_token") != "session-ok" or \
            request.get("csrf_required") and request.get("csrf_token") != "csrf-ok":
        return make_error("authorization_denied", "session or CSRF denied")
    return make_success(principal=_principal(
        authentication_method="session", session_id="session-01"))


def _create_user(state, request):
    state["create_user_calls"].append(copy.deepcopy(request))
    return make_success(user={
        "user_id": "created-user", "username": request["username"]})


def _issue_api_token(state, request):
    state["issued_tokens"].append(copy.deepcopy(request))
    return make_success(
        token="secret-issued-once",
        metadata={
            "token_id": uuid.uuid4(),
            "created_at": datetime.datetime(
                2026, 7, 19, 0, 0, tzinfo=datetime.timezone.utc),
        },
    )


def _list_users(state, **kwargs):
    return copy.deepcopy(state["users"][:int(kwargs.get("limit") or 100)])


def _list_roles(state):
    return copy.deepcopy(state["roles"])


def _list_permissions(state):
    return sorted(_principal()["permissions"])


def _list_api_tokens(state, user_id, **kwargs):
    assert user_id == "user-main"
    return copy.deepcopy(state["tokens"][:int(kwargs.get("limit") or 100)])


def _revoke_api_token(state, request):
    state["revocations"].append(copy.deepcopy(request))
    return make_success(revoked=True, token_id=request["token_id"])


def _command(state, name, *args, **kwargs):
    state["command_calls"].append({
        "name": name,
        "args": copy.deepcopy(args),
        "kwargs": copy.deepcopy(kwargs),
    })
    if name == "list_nodes":
        return make_success(nodes=[
            {"node_id": "node-a", "site_id": SITE_ID},
            {"node_id": "node-b", "site_id": SITE_ID},
        ])
    if name == "runtime_status":
        return make_success(status="READY")
    if name == "get_config":
        if kwargs.get("scope") is not None:
            return make_success(mirror={
                "scope": kwargs["scope"],
                "authority_epoch": 7,
                "scope_revision": 184,
                "fingerprint": "b" * 64,
            })
        revision = kwargs.get("revision") or 1
        return make_success(config={
            "revision": revision,
            "config_data": {"nodes": {"revision": revision}},
        })
    if name == "config_history":
        return make_success(history=[{"revision": 1}])
    if name == "get_operation":
        return make_success(operation={"operation_id": args[1]})
    return make_success(
        accepted=True, state="ACCEPTED", operation_id=str(uuid.uuid4()))


def _claim_local_action(state, request):
    identity = (
        request["user_id"], request["site_id"], request["action"],
        request["idempotency_key"],
    )
    with state["idempotency_lock"]:
        existing = state["local_actions"].get(identity)
        if existing:
            if existing["payload_fingerprint"] != request["payload_fingerprint"]:
                return make_error(
                    "conflict", "idempotency key was reused with another payload")
            return make_success(
                duplicate=True, action_record=copy.deepcopy(existing))
        record = {
            "request_id": request["request_id"],
            "payload_fingerprint": request["payload_fingerprint"],
            "state": "PENDING",
            "result": None,
        }
        state["local_actions"][identity] = record
        state["request_to_identity"][request["request_id"]] = identity
        return make_success(duplicate=False, action_record=copy.deepcopy(record))


def _complete_local_action(state, request_id, result):
    with state["idempotency_lock"]:
        identity = state["request_to_identity"].get(request_id)
        if not identity:
            return make_error("not_found", "local action is unknown")
        state["local_actions"][identity]["result"] = copy.deepcopy(result)
        state["local_actions"][identity]["state"] = "COMPLETED"
    return make_success()


def _runtime_snapshot(state):
    return {"ready": True, "calls": len(state["command_calls"])}


def _state():
    return {
        "users": [{"user_id": "user-main", "username": "main"}],
        "roles": [{
            "role_name": "operator",
            "permissions": ["node.read"],
        }],
        "tokens": [{"token_id": str(uuid.uuid4()), "name": "existing"}],
        "rate_calls": [],
        "audit_records": [],
        "session_auth_calls": [],
        "create_user_calls": [],
        "issued_tokens": [],
        "revocations": [],
        "command_calls": [],
        "local_actions": {},
        "request_to_identity": {},
        "idempotency_lock": threading.Lock(),
    }


def _dispatcher(state, authority_mode="master_global"):
    commands = {
        name: partial(_command, state, name)
        for name in (
            "list_nodes", "runtime_status", "get_config", "config_history",
            "propose_config", "rollback_config", "request_actuator",
            "get_operation", "drain_node", "set_node_state",
            "approve_node", "execute_terminal",
        )
    }
    return create_client_command_dispatcher(
        site_id=SITE_ID,
        instance_id="proxy-01",
        authority_mode=authority_mode,
        auth={
            "authenticate_session": partial(_authenticate_session, state),
            "create_user": partial(_create_user, state),
            "issue_api_token": partial(_issue_api_token, state),
        },
        identity={
            "list_users": partial(_list_users, state),
            "list_roles": partial(_list_roles, state),
            "list_permissions": partial(_list_permissions, state),
            "list_api_tokens": partial(_list_api_tokens, state),
            "revoke_api_token": partial(_revoke_api_token, state),
        },
        rate_limit={"consume": partial(_rate_consume, state)},
        audit={
            "append": partial(_audit_append, state),
            "list": partial(_audit_list, state),
        },
        commands=commands,
        gateway_store={
            "claim_local_action": partial(_claim_local_action, state),
            "complete_local_action": partial(_complete_local_action, state),
        },
        runtime_snapshot=partial(_runtime_snapshot, state),
        idempotency_key=b"dispatcher-test-hmac-key-32-bytes",
        logger=logging.getLogger("test.client-command-dispatcher"),
    )


def _payloads():
    operation_id = str(uuid.uuid4())
    controller_id = str(uuid.uuid4())
    token_id = str(uuid.uuid4())
    return {
        "users.list": {"limit": 1},
        "user.create": {"username": "new-user", "password": "correct horse"},
        "roles.list": {},
        "permissions.list": {},
        "api_tokens.list": {"limit": 1},
        "api_tokens.create": {
            "name": "empty-scope-token", "permissions": [], "site_scopes": []},
        "api_tokens.revoke": {"token_id": token_id},
        "sites.list": {},
        "nodes.list": {},
        "node.get": {"node_id": "node-a"},
        "runtime.status": {},
        "config.get": {"revision": 1},
        "config.history": {"limit": 1},
        "config.diff": {"from_revision": 1, "to_revision": 2},
        "config.propose": {
            "base_revision": 0,
            "changes": [{"operation": "update", "path": "nodes.node-a", "value": {}}],
        },
        "config.rollback": {"base_revision": 2, "target_revision": 1},
        "actuator.request": {
            "controller_uuid": controller_id, "actuator_id": 1, "value": True},
        "operation.get": {"operation_id": operation_id},
        "audit.list": {"limit": 1},
        "node.drain": {"node_id": "node-a", "deadline_s": 30},
        "node.state.set": {"node_id": "node-a", "desired_state": "MAINTENANCE"},
        "node.admission.approve": {"node_id": "node-a"},
        "terminal.execute": {"command": "status"},
    }


def _request(action, payload, **kwargs):
    request = {
        "transport": kwargs.get("transport", "wss"),
        "site_id": SITE_ID,
        "source_ip": "127.0.0.1",
        "user_agent": "dispatcher-test-agent",
        "principal": copy.deepcopy(kwargs.get("principal") or _principal()),
        "request_id": kwargs.get("request_id", str(uuid.uuid4())),
        "action": action,
        "ttl_s": kwargs.get("ttl_s", 30.0),
        "payload": copy.deepcopy(payload),
    }
    if action in MUTATIONS or "idempotency_key" in kwargs:
        request["idempotency_key"] = kwargs.get(
            "idempotency_key", "idempotency-" + action)
    if "csrf_token" in kwargs:
        request["csrf_token"] = kwargs["csrf_token"]
    if "session_token" in kwargs:
        request["session_token"] = kwargs["session_token"]
    return request


async def _registry_and_payload_scenario():
    state = _state()
    dispatcher = _dispatcher(state)
    assert set(dispatcher["actions"]) == ALL_ACTIONS
    assert len(dispatcher["actions"]) == 23
    assert set(dispatcher["mutations"]) == MUTATIONS

    for action, payload in _payloads().items():
        result = await dispatcher["dispatch"](_request(action, payload))
        assert result.get("ok"), (action, result)
        assert result.get("code") != "invalid_payload", (action, result)

    strict_cases = (
        _request("nodes.list", {"unknown": True}),
        _request("node.get", {}),
        _request("users.list", {"limit": True}),
        _request("api_tokens.create", {
            "name": "duplicate", "permissions": ["node.read", "node.read"]}),
        _request("node.state.set", {
            "node_id": "node-a", "desired_state": "ROOT"}),
    )
    for request in strict_cases:
        result = await dispatcher["dispatch"](request)
        assert not result["ok"] and result["code"] == "invalid_payload"
    unknown = await dispatcher["dispatch"](_request("shell.root", {}))
    assert not unknown["ok"] and unknown["code"] == "invalid_payload"


async def _authorization_scenario():
    state = _state()
    dispatch = _dispatcher(state)["dispatch"]

    denied_role = await dispatch(_request(
        "users.list", {}, principal=_principal(permissions=("node.read",))))
    assert denied_role["code"] == "authorization_denied"
    assert state["audit_records"][-1]["decision"] == "DENY"
    assert not any(
        record["decision"] == "ALLOW" and record["request_id"] ==
        state["audit_records"][-1]["request_id"]
        for record in state["audit_records"])

    denied_site = await dispatch(_request(
        "nodes.list", {}, principal=_principal(site_scopes=("site_other",))))
    assert denied_site["code"] == "authorization_denied"
    assert state["audit_records"][-1]["decision"] == "DENY"

    restricted = _principal(
        permissions=("node.read",),
        resource_scopes=[{
            "site_id": SITE_ID,
            "resource_type": "node",
            "resource_pattern": "node-a",
        }],
    )
    allowed = await dispatch(_request(
        "node.get", {"node_id": "node-a"}, principal=restricted))
    denied_resource = await dispatch(_request(
        "node.get", {"node_id": "node-b"}, principal=restricted))
    assert allowed["ok"]
    assert denied_resource["code"] == "authorization_denied"
    assert state["audit_records"][-1]["decision"] == "DENY"

    operation_id = str(uuid.uuid4())
    operation_restricted = _principal(
        permissions=("operation.read",),
        resource_scopes=[{
            "site_id": SITE_ID,
            "resource_type": "operation",
            "resource_pattern": operation_id,
        }],
    )
    operation_allowed = await dispatch(_request(
        "operation.get", {"operation_id": operation_id},
        principal=operation_restricted))
    operation_denied = await dispatch(_request(
        "operation.get", {"operation_id": str(uuid.uuid4())},
        principal=operation_restricted))
    assert operation_allowed["ok"]
    assert operation_denied["code"] == "authorization_denied"
    assert state["audit_records"][-1]["decision"] == "DENY"


async def _csrf_and_empty_scope_scenario():
    state = _state()
    dispatch = _dispatcher(state)["dispatch"]
    session_principal = _principal(
        authentication_method="session", session_id="session-01")
    payload = {"name": "session-token", "permissions": [], "site_scopes": []}

    denied = await dispatch(_request(
        "api_tokens.create", payload,
        principal=session_principal,
        session_token="session-ok",
        csrf_token="wrong",
        idempotency_key="csrf-denied",
    ))
    assert denied["code"] == "authorization_denied"
    assert not state["issued_tokens"]

    allowed = await dispatch(_request(
        "api_tokens.create", payload,
        principal=session_principal,
        session_token="session-ok",
        csrf_token="csrf-ok",
        idempotency_key="csrf-allowed",
    ))
    assert allowed["ok"]
    assert state["session_auth_calls"][-1] == {
        "session_token": "session-ok",
        "csrf_token": "csrf-ok",
        "csrf_required": True,
        "source_ip": "127.0.0.1",
        "user_agent": "dispatcher-test-agent",
    }
    assert state["issued_tokens"][-1]["permissions"] == []
    assert state["issued_tokens"][-1]["site_scopes"] == []

    session_call_count = len(state["session_auth_calls"])
    bearer = await dispatch(_request(
        "api_tokens.create", payload,
        principal=_principal(authentication_method="api_token"),
        idempotency_key="bearer-no-csrf",
    ))
    assert bearer["ok"]
    assert len(state["session_auth_calls"]) == session_call_count
    assert state["issued_tokens"][-1]["permissions"] == []
    assert state["issued_tokens"][-1]["site_scopes"] == []


async def _idempotency_scenario():
    state = _state()
    dispatch = _dispatcher(state)["dispatch"]
    payload = {"username": "idempotent-user", "password": "not-persisted"}

    missing = _request("user.create", payload)
    missing.pop("idempotency_key")
    missing_result = await dispatch(missing)
    assert missing_result["code"] == "invalid_payload"
    assert not state["create_user_calls"]

    first = await dispatch(_request(
        "user.create", payload, idempotency_key="same-local-action"))
    duplicate = await dispatch(_request(
        "user.create", payload, idempotency_key="same-local-action"))
    conflict = await dispatch(_request(
        "user.create",
        {"username": "different-user", "password": "not-persisted"},
        idempotency_key="same-local-action",
    ))
    assert first["ok"] and duplicate["ok"]
    assert duplicate["reply"]["duplicate"] is True
    assert conflict["code"] == "conflict"
    assert len(state["create_user_calls"]) == 1
    persisted = next(iter(state["local_actions"].values()))["result"]
    assert "password" not in str(persisted).lower()

    concurrent_state = _state()
    concurrent_dispatch = _dispatcher(concurrent_state)["dispatch"]
    requests = (
        _request("user.create", payload, idempotency_key="concurrent-action"),
        _request("user.create", payload, idempotency_key="concurrent-action"),
    )
    results = await asyncio.gather(*(
        concurrent_dispatch(request) for request in requests
    ))
    assert all(result["ok"] for result in results)
    assert len(concurrent_state["create_user_calls"]) == 1

    token_state = _state()
    token_dispatch = _dispatcher(token_state)["dispatch"]
    token_request = _request(
        "api_tokens.create",
        {"name": "native-postgresql-token"},
        idempotency_key="native-postgresql-token",
    )
    token_result = await token_dispatch(token_request)
    token_duplicate = await token_dispatch(token_request)
    assert token_result["ok"] and token_duplicate["ok"]
    assert len(token_state["issued_tokens"]) == 1

    token_persisted = next(iter(token_state["local_actions"].values()))["result"]
    json.dumps(token_persisted, ensure_ascii=False, allow_nan=False)
    assert isinstance(token_persisted["metadata"]["token_id"], str)
    assert isinstance(token_persisted["metadata"]["created_at"], str)
    assert "secret-issued-once" not in json.dumps(token_persisted)
    assert "token" not in token_persisted
    assert token_persisted["secret_already_delivered"] is True

    duplicate_reply = token_duplicate["reply"]
    json.dumps(duplicate_reply, ensure_ascii=False, allow_nan=False)
    assert duplicate_reply["duplicate"] is True
    assert duplicate_reply["secret_already_delivered"] is True
    assert isinstance(duplicate_reply["metadata"]["token_id"], str)
    assert isinstance(duplicate_reply["metadata"]["created_at"], str)
    assert "secret-issued-once" not in json.dumps(duplicate_reply)
    assert "token" not in duplicate_reply


async def _node_local_contract_scenario():
    state = _state()
    dispatch = _dispatcher(state, authority_mode="node_local")["dispatch"]
    scoped = await dispatch(_request(
        "config.get", {"scope": "node:node-a"}))
    assert scoped["ok"] and scoped["reply"]["mirror"]["scope"] == "node:node-a"

    operation = {
        "op": "replace_field",
        "section": "rules",
        "entity_key": "rule:17",
        "json_pointer": "/enabled",
        "expected_value_hash": "a" * 64,
        "value": True,
    }
    proposal = {
        "target_node_id": "node-a",
        "operation": "scope_operations",
        "operation_format": "config_scope_operations_v1",
        "scope": "node:node-a",
        "base_authority_epoch": 7,
        "base_scope_revision": 184,
        "base_scope_fingerprint": "b" * 64,
        "reason": "canonical proxy relay",
        "operations": [operation],
    }
    result = await dispatch(_request(
        "config.propose", proposal,
        idempotency_key="canonical-proposal-01"))
    assert result["ok"], result
    call = state["command_calls"][-1]
    assert call["name"] == "propose_config"
    relayed = call["args"][1]
    assert relayed["operations"] == [operation]
    assert "base_revision" not in relayed and "changes" not in relayed

    legacy = await dispatch(_request(
        "config.propose",
        {
            "base_revision": 0,
            "changes": [{"path": "rules.0.enabled", "value": True}],
        },
        idempotency_key="legacy-proposal-denied",
    ))
    assert legacy["code"] == "unsupported_operation_format"
    invalid_hash = copy.deepcopy(proposal)
    invalid_hash["base_scope_fingerprint"] = "B" * 64
    rejected_hash = await dispatch(_request(
        "config.propose", invalid_hash,
        idempotency_key="invalid-hash"))
    assert rejected_hash["code"] == "invalid_payload"
    invalid_operation = copy.deepcopy(proposal)
    invalid_operation["operations"][0]["path"] = "rules.0.enabled"
    rejected_operation = await dispatch(_request(
        "config.propose", invalid_operation,
        idempotency_key="invalid-operation"))
    assert rejected_operation["code"] == "unsupported_operation_format"
    diff = await dispatch(_request(
        "config.diff", {"from_revision": 1, "to_revision": 2}))
    rollback = await dispatch(_request(
        "config.rollback",
        {"base_revision": 2, "target_revision": 1},
        idempotency_key="rollback-denied",
    ))
    assert diff["code"] == "unsupported_operation_format"
    assert rollback["code"] == "unsupported_operation_format"


def _test_dispatcher_contracts():
    asyncio.run(_registry_and_payload_scenario())
    asyncio.run(_node_local_contract_scenario())
    asyncio.run(_authorization_scenario())
    asyncio.run(_csrf_and_empty_scope_scenario())
    asyncio.run(_idempotency_scenario())


def main():
    _test_dispatcher_contracts()
    print("PASS test_client_command_dispatcher (23 actions, canonical relay, policy, idempotency)")


if __name__ == "__main__":
    main()

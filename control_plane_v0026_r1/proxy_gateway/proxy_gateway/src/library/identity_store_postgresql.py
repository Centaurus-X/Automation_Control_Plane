# -*- coding: utf-8 -*-

# proxy_gateway/src/library/identity_store_postgresql.py

"""Shared PostgreSQL users, credentials, roles, sessions and API tokens."""

import copy
import hmac
import uuid

from functools import partial

from src.library._generic_system_contract_library.errors import make_error, make_success
from src.library.postgresql import jsonb


def normalize_username(value):
    """Normalize login names deterministically without locale dependence."""
    normalized = str(value or "").strip().casefold()
    if not normalized or len(normalized) > 128:
        raise ValueError("username is invalid")
    return normalized


def create_user(database, request):
    """Create one local user and its already-Argon2id credential atomically."""
    values = copy.deepcopy(request)
    normalized = normalize_username(values["username"])

    def write(connection):
        user_id = str(values.get("user_id") or uuid.uuid4())
        role = None
        if values.get("role_name"):
            role = connection.execute(
                "SELECT role_id FROM proxy_gateway.roles WHERE role_name=%s",
                (values["role_name"],),
            ).fetchone()
            if not role:
                return make_error("not_found", "role is unknown")
        try:
            row = connection.execute(
                "INSERT INTO proxy_gateway.users("
                "user_id,username,username_normalized,display_name,email,enabled) "
                "VALUES (%s,%s,%s,%s,%s,%s) RETURNING *",
                (
                    user_id, values["username"], normalized, values.get("display_name"),
                    values.get("email"), bool(values.get("enabled", True)),
                ),
            ).fetchone()
        except Exception as exc:
            if getattr(exc, "sqlstate", None) == "23505":
                return make_error("conflict", "username already exists")
            raise
        connection.execute(
            "INSERT INTO proxy_gateway.password_credentials("
            "user_id,password_hash,algorithm,must_change) VALUES (%s,%s,'argon2id',%s)",
            (user_id, values["password_hash"], bool(values.get("must_change", False))),
        )
        if role:
            connection.execute(
                "INSERT INTO proxy_gateway.user_roles(user_id,role_id,site_id,granted_by) "
                "VALUES (%s,%s,%s,%s)",
                (
                    user_id, role["role_id"], values["site_id"],
                    values.get("granted_by") or user_id,
                ),
            )
        return make_success(user=_public_user(row))

    return database["transaction"](write)


def _public_user(row):
    if not row:
        return None
    allowed = (
        "user_id", "username", "display_name", "email", "enabled",
        "locked_until", "created_at", "updated_at",
    )
    return {key: copy.deepcopy(row.get(key)) for key in allowed}


def get_credentials(database, username):
    """Return one credential verification record for the auth boundary."""
    normalized = normalize_username(username)

    def read(connection):
        row = connection.execute(
            "SELECT u.*, c.password_hash, c.must_change FROM proxy_gateway.users u "
            "JOIN proxy_gateway.password_credentials c USING(user_id) "
            "WHERE u.username_normalized=%s",
            (normalized,),
        ).fetchone()
        return dict(row) if row else None

    return database["transaction"](read, isolation_level="READ COMMITTED")


def has_role(database, request):
    """Check one exact user, role and site assignment."""
    values = copy.deepcopy(request)

    def read(connection):
        row = connection.execute(
            "SELECT EXISTS("
            "SELECT 1 FROM proxy_gateway.user_roles ur "
            "JOIN proxy_gateway.roles r USING(role_id) "
            "WHERE ur.user_id=%s AND r.role_name=%s AND ur.site_id=%s"
            ") AS assigned",
            (
                values["user_id"], values["role_name"], values["site_id"],
            ),
        ).fetchone()
        return bool(row and row["assigned"])

    return database["transaction"](read, isolation_level="READ COMMITTED")


def replace_password_for_role(database, request):
    """Replace one enabled role-bound credential and revoke active sessions."""
    values = copy.deepcopy(request)

    def write(connection):
        row = connection.execute(
            "SELECT u.user_id FROM proxy_gateway.users u "
            "WHERE u.user_id=%s AND u.enabled=true AND EXISTS("
            "SELECT 1 FROM proxy_gateway.user_roles ur "
            "JOIN proxy_gateway.roles r USING(role_id) "
            "WHERE ur.user_id=u.user_id AND r.role_name=%s AND ur.site_id=%s"
            ") FOR UPDATE OF u",
            (
                values["user_id"], values["role_name"], values["site_id"],
            ),
        ).fetchone()
        if not row:
            return make_error(
                "authorization_denied",
                "existing user is not an enabled administrator",
            )
        connection.execute(
            "UPDATE proxy_gateway.password_credentials SET "
            "password_hash=%s,algorithm='argon2id',changed_at=clock_timestamp(),"
            "must_change=false WHERE user_id=%s",
            (values["password_hash"], row["user_id"]),
        )
        connection.execute(
            "UPDATE proxy_gateway.users SET failed_login_count=0,"
            "locked_until=NULL,updated_at=clock_timestamp() WHERE user_id=%s",
            (row["user_id"],),
        )
        connection.execute(
            "UPDATE proxy_gateway.sessions SET "
            "revoked_at=COALESCE(revoked_at,clock_timestamp()),"
            "revoke_reason=COALESCE(revoke_reason,'credential_rotated') "
            "WHERE user_id=%s AND revoked_at IS NULL",
            (row["user_id"],),
        )
        connection.execute(
            "INSERT INTO proxy_gateway.security_events("
            "security_event_id,event_type,severity,principal_subject,details) "
            "VALUES (%s,'bootstrap_admin_credential_updated','WARNING',%s,"
            "jsonb_build_object('site_id',%s::text,'role_name',%s::text))",
            (
                str(uuid.uuid4()), str(row["user_id"]), values["site_id"],
                values["role_name"],
            ),
        )
        return make_success(user_id=str(row["user_id"]))

    return database["transaction"](write)


def record_login_failure(database, request):
    """Persist failure telemetry and apply a bounded account lockout."""
    values = copy.deepcopy(request)
    normalized = normalize_username(values["username"])
    maximum = max(3, int(values.get("max_failures", 5)))
    lock_s = max(5, int(values.get("lock_seconds", 300)))

    def write(connection):
        connection.execute(
            "INSERT INTO proxy_gateway.login_attempts("
            "username_normalized,source_ip,success,reason_code) VALUES (%s,%s,false,%s)",
            (normalized, values.get("source_ip"), values.get("reason_code", "invalid_credentials")),
        )
        row = connection.execute(
            "UPDATE proxy_gateway.users SET failed_login_count=failed_login_count+1, "
            "locked_until=CASE WHEN failed_login_count+1 >= %s THEN "
            "clock_timestamp()+(%s*interval '1 second') ELSE locked_until END, "
            "updated_at=clock_timestamp() WHERE username_normalized=%s RETURNING *",
            (maximum, lock_s, normalized),
        ).fetchone()
        return _public_user(row)

    return database["transaction"](write, isolation_level="READ COMMITTED")


def record_login_success(database, request):
    """Reset failure state and append a successful login attempt."""
    values = copy.deepcopy(request)
    normalized = normalize_username(values["username"])

    def write(connection):
        connection.execute(
            "INSERT INTO proxy_gateway.login_attempts("
            "username_normalized,source_ip,success,reason_code) VALUES (%s,%s,true,'ok')",
            (normalized, values.get("source_ip")),
        )
        connection.execute(
            "UPDATE proxy_gateway.users SET failed_login_count=0,locked_until=NULL,"
            "updated_at=clock_timestamp() WHERE username_normalized=%s",
            (normalized,),
        )
        return True

    return database["transaction"](write, isolation_level="READ COMMITTED")


def create_session(database, request):
    """Persist only hashes for one rotating server-side session."""
    values = copy.deepcopy(request)

    def write(connection):
        session_id = str(values.get("session_id") or uuid.uuid4())
        row = connection.execute(
            "INSERT INTO proxy_gateway.sessions("
            "session_id,user_id,token_hash,csrf_hash,client_binding_hash,expires_at,"
            "absolute_expires_at,rotated_from) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                session_id, values["user_id"], values["token_hash"], values["csrf_hash"],
                values.get("client_binding_hash"), values["expires_at"],
                values["absolute_expires_at"], values.get("rotated_from"),
            ),
        ).fetchone()
        return make_success(session=dict(row))

    return database["transaction"](write)


def get_session(database, request):
    """Resolve one active shared session and apply bounded sliding expiry."""
    values = copy.deepcopy(request)
    sliding_s = max(60, int(values.get("sliding_seconds", 1800)))

    def write(connection):
        row = connection.execute(
            "SELECT s.*,u.username,u.display_name,u.enabled FROM proxy_gateway.sessions s "
            "JOIN proxy_gateway.users u USING(user_id) WHERE s.token_hash=%s "
            "AND s.revoked_at IS NULL AND s.expires_at>clock_timestamp() "
            "AND s.absolute_expires_at>clock_timestamp() AND u.enabled=true FOR UPDATE OF s",
            (values["token_hash"],),
        ).fetchone()
        if not row:
            return None
        expected = row.get("client_binding_hash")
        if expected and not hmac.compare_digest(
                str(expected), str(values.get("client_binding_hash") or "")):
            return None
        updated = connection.execute(
            "UPDATE proxy_gateway.sessions SET last_seen_at=clock_timestamp(), "
            "expires_at=LEAST(absolute_expires_at,clock_timestamp()+(%s*interval '1 second')) "
            "WHERE session_id=%s RETURNING *",
            (sliding_s, row["session_id"]),
        ).fetchone()
        result = dict(row)
        result.update(dict(updated))
        return result

    return database["transaction"](write, isolation_level="READ COMMITTED")


def revoke_session(database, request):
    """Revoke one session by hash or ID across all Proxy instances."""
    values = copy.deepcopy(request)

    def write(connection):
        row = connection.execute(
            "UPDATE proxy_gateway.sessions SET revoked_at=clock_timestamp(),revoke_reason=%s "
            "WHERE (%s::uuid IS NOT NULL AND session_id=%s::uuid) "
            "OR (%s::text IS NOT NULL AND token_hash=%s::text) "
            "RETURNING session_id",
            (
                values.get("reason", "logout"), values.get("session_id"), values.get("session_id"),
                values.get("token_hash"), values.get("token_hash"),
            ),
        ).fetchone()
        return bool(row)

    return database["transaction"](write, isolation_level="READ COMMITTED")


def rotate_session(database, request):
    """Atomically revoke one active session and replace it with a new token hash."""
    values = copy.deepcopy(request)

    def write(connection):
        previous = connection.execute(
            "SELECT * FROM proxy_gateway.sessions WHERE token_hash=%s AND revoked_at IS NULL "
            "AND expires_at>clock_timestamp() FOR UPDATE",
            (values["old_token_hash"],),
        ).fetchone()
        if not previous:
            return make_error("authentication_required", "session is no longer active")
        session_id = str(values.get("session_id") or uuid.uuid4())
        expires_at = min(
            values["expires_at"], previous["absolute_expires_at"])
        row = connection.execute(
            "INSERT INTO proxy_gateway.sessions("
            "session_id,user_id,token_hash,csrf_hash,client_binding_hash,expires_at,"
            "absolute_expires_at,rotated_from) VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *",
            (
                session_id, previous["user_id"], values["token_hash"], values["csrf_hash"],
                values.get("client_binding_hash"), expires_at,
                previous["absolute_expires_at"], previous["session_id"],
            ),
        ).fetchone()
        connection.execute(
            "UPDATE proxy_gateway.sessions SET revoked_at=clock_timestamp(),"
            "revoke_reason='rotated' WHERE session_id=%s",
            (previous["session_id"],),
        )
        return make_success(session=dict(row))

    return database["transaction"](write)


def effective_access(database, user_id):
    """Resolve atomic permissions plus site and resource scopes from shared RBAC."""
    def read(connection):
        permissions = connection.execute(
            "SELECT DISTINCT rp.permission_name FROM proxy_gateway.user_roles ur "
            "JOIN proxy_gateway.role_permissions rp USING(role_id) WHERE ur.user_id=%s "
            "ORDER BY rp.permission_name",
            (user_id,),
        ).fetchall()
        sites = connection.execute(
            "SELECT DISTINCT site_id FROM proxy_gateway.user_roles WHERE user_id=%s ORDER BY site_id",
            (user_id,),
        ).fetchall()
        scopes = connection.execute(
            "SELECT rs.site_id,rs.resource_type,rs.resource_pattern FROM proxy_gateway.resource_scopes rs "
            "WHERE (rs.subject_type='USER' AND rs.subject_id=%s) OR "
            "(rs.subject_type='ROLE' AND rs.subject_id IN ("
            "SELECT role_id FROM proxy_gateway.user_roles WHERE user_id=%s)) "
            "ORDER BY rs.site_id,rs.resource_type,rs.resource_pattern",
            (user_id, user_id),
        ).fetchall()
        return {
            "permissions": [row["permission_name"] for row in permissions],
            "site_scopes": [row["site_id"] for row in sites],
            "resource_scopes": [dict(row) for row in scopes],
        }

    return database["transaction"](read, isolation_level="READ COMMITTED")


def list_users(database, **kwargs):
    """Return bounded users without credentials or token material."""
    limit = min(200, max(1, int(kwargs.get("limit", 100))))

    def read(connection):
        rows = connection.execute(
            "SELECT * FROM proxy_gateway.users ORDER BY username_normalized LIMIT %s",
            (limit,),
        ).fetchall()
        return [_public_user(row) for row in rows]

    return database["transaction"](read, isolation_level="READ COMMITTED")


def grant_role(database, request):
    """Grant one named role for one exact site."""
    values = copy.deepcopy(request)

    def write(connection):
        role = connection.execute(
            "SELECT role_id FROM proxy_gateway.roles WHERE role_name=%s",
            (values["role_name"],),
        ).fetchone()
        if not role:
            return make_error("not_found", "role is unknown")
        connection.execute(
            "INSERT INTO proxy_gateway.user_roles(user_id,role_id,site_id,granted_by) "
            "VALUES (%s,%s,%s,%s) ON CONFLICT DO NOTHING",
            (values["user_id"], role["role_id"], values["site_id"], values.get("granted_by")),
        )
        return make_success()

    return database["transaction"](write)


def list_roles(database):
    """Return roles and their atomic permissions."""
    def read(connection):
        rows = connection.execute(
            "SELECT r.role_id,r.role_name,r.description,r.builtin,"
            "COALESCE(jsonb_agg(rp.permission_name ORDER BY rp.permission_name) "
            "FILTER (WHERE rp.permission_name IS NOT NULL),'[]'::jsonb) AS permissions "
            "FROM proxy_gateway.roles r LEFT JOIN proxy_gateway.role_permissions rp USING(role_id) "
            "GROUP BY r.role_id ORDER BY r.role_name"
        ).fetchall()
        return [dict(row) for row in rows]

    return database["transaction"](read, isolation_level="READ COMMITTED")


def list_permissions(database):
    """Return the stable permission catalog."""
    def read(connection):
        rows = connection.execute(
            "SELECT permission_name,description FROM proxy_gateway.permissions "
            "ORDER BY permission_name"
        ).fetchall()
        return [dict(row) for row in rows]

    return database["transaction"](read, isolation_level="READ COMMITTED")


def create_api_token(database, request):
    """Persist one scoped API-token hash and return metadata only."""
    values = copy.deepcopy(request)

    def write(connection):
        token_id = str(values.get("token_id") or uuid.uuid4())
        row = connection.execute(
            "INSERT INTO proxy_gateway.api_tokens("
            "token_id,user_id,token_prefix,token_hash,name,permissions,site_scopes,expires_at) "
            "VALUES (%s,%s,%s,%s,%s,%s,%s,%s) RETURNING token_id,token_prefix,name,expires_at",
            (
                token_id, values["user_id"], values["token_prefix"], values["token_hash"],
                values["name"], jsonb(values.get("permissions") or []),
                jsonb(values.get("site_scopes") or []), values.get("expires_at"),
            ),
        ).fetchone()
        return make_success(token=dict(row))

    return database["transaction"](write)


def get_api_token(database, token_hash):
    """Resolve one active API token and its enabled user."""
    def write(connection):
        row = connection.execute(
            "SELECT t.*,u.username,u.enabled FROM proxy_gateway.api_tokens t "
            "JOIN proxy_gateway.users u USING(user_id) WHERE t.token_hash=%s "
            "AND t.revoked_at IS NULL AND (t.expires_at IS NULL OR t.expires_at>clock_timestamp()) "
            "AND u.enabled=true",
            (token_hash,),
        ).fetchone()
        if row:
            connection.execute(
                "UPDATE proxy_gateway.api_tokens SET last_used_at=clock_timestamp() WHERE token_id=%s",
                (row["token_id"],),
            )
        return dict(row) if row else None

    return database["transaction"](write, isolation_level="READ COMMITTED")


def list_api_tokens(database, user_id, **kwargs):
    """List bounded token metadata without returning keyed hashes."""
    limit = min(200, max(1, int(kwargs.get("limit", 100))))

    def read(connection):
        rows = connection.execute(
            "SELECT token_id,token_prefix,name,permissions,site_scopes,created_at,"
            "expires_at,last_used_at,revoked_at FROM proxy_gateway.api_tokens "
            "WHERE user_id=%s ORDER BY created_at DESC LIMIT %s",
            (user_id, limit),
        ).fetchall()
        return [dict(row) for row in rows]

    return database["transaction"](read, isolation_level="READ COMMITTED")


def revoke_api_token(database, request):
    """Revoke one token, optionally constrained to its owning user."""
    values = copy.deepcopy(request)

    def write(connection):
        row = connection.execute(
            "UPDATE proxy_gateway.api_tokens SET revoked_at=COALESCE(revoked_at,clock_timestamp()) "
            "WHERE token_id=%s AND (%s::uuid IS NULL OR user_id=%s::uuid) "
            "RETURNING token_id,user_id,revoked_at",
            (values["token_id"], values.get("user_id"), values.get("user_id")),
        ).fetchone()
        return make_success(token=dict(row)) if row else make_error(
            "not_found", "API token is unknown")

    return database["transaction"](write, isolation_level="READ COMMITTED")


def build_identity_capabilities(database):
    """Expose the narrow shared identity-store port."""
    return {
        "create_user": partial(create_user, database),
        "get_credentials": partial(get_credentials, database),
        "has_role": partial(has_role, database),
        "replace_password_for_role": partial(
            replace_password_for_role, database),
        "record_login_failure": partial(record_login_failure, database),
        "record_login_success": partial(record_login_success, database),
        "create_session": partial(create_session, database),
        "get_session": partial(get_session, database),
        "revoke_session": partial(revoke_session, database),
        "rotate_session": partial(rotate_session, database),
        "effective_access": partial(effective_access, database),
        "list_users": partial(list_users, database),
        "grant_role": partial(grant_role, database),
        "list_roles": partial(list_roles, database),
        "list_permissions": partial(list_permissions, database),
        "create_api_token": partial(create_api_token, database),
        "get_api_token": partial(get_api_token, database),
        "list_api_tokens": partial(list_api_tokens, database),
        "revoke_api_token": partial(revoke_api_token, database),
    }

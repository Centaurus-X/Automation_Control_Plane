-- proxy_gateway/deployments/migrations/001_proxy_gateway.sql
-- Shared identity, authorization, session, replay and audit schema.

CREATE SCHEMA IF NOT EXISTS proxy_gateway AUTHORIZATION proxy_migrator;
REVOKE ALL ON SCHEMA proxy_gateway FROM PUBLIC;
SET search_path TO proxy_gateway, pg_catalog;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version bigint PRIMARY KEY,
    name text NOT NULL,
    checksum char(64) NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS users (
    user_id uuid PRIMARY KEY,
    username text NOT NULL,
    username_normalized text NOT NULL UNIQUE,
    display_name text,
    email text,
    enabled boolean NOT NULL DEFAULT true,
    failed_login_count integer NOT NULL DEFAULT 0 CHECK (failed_login_count >= 0),
    locked_until timestamptz,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS password_credentials (
    user_id uuid PRIMARY KEY REFERENCES users(user_id) ON DELETE CASCADE,
    password_hash text NOT NULL,
    algorithm text NOT NULL CHECK (algorithm = 'argon2id'),
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    must_change boolean NOT NULL DEFAULT false
);

CREATE TABLE IF NOT EXISTS roles (
    role_id uuid PRIMARY KEY,
    role_name text NOT NULL UNIQUE,
    description text,
    builtin boolean NOT NULL DEFAULT false,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS permissions (
    permission_name text PRIMARY KEY,
    description text NOT NULL
);

CREATE TABLE IF NOT EXISTS user_roles (
    user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    role_id uuid NOT NULL REFERENCES roles(role_id) ON DELETE CASCADE,
    site_id text NOT NULL,
    granted_by uuid REFERENCES users(user_id),
    granted_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (user_id, role_id, site_id)
);

CREATE TABLE IF NOT EXISTS role_permissions (
    role_id uuid NOT NULL REFERENCES roles(role_id) ON DELETE CASCADE,
    permission_name text NOT NULL REFERENCES permissions(permission_name) ON DELETE CASCADE,
    PRIMARY KEY (role_id, permission_name)
);

CREATE TABLE IF NOT EXISTS resource_scopes (
    scope_id uuid PRIMARY KEY,
    subject_type text NOT NULL CHECK (subject_type IN ('USER', 'ROLE')),
    subject_id uuid NOT NULL,
    site_id text NOT NULL,
    resource_type text NOT NULL,
    resource_pattern text NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (subject_type, subject_id, site_id, resource_type, resource_pattern)
);

CREATE TABLE IF NOT EXISTS sessions (
    session_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    token_hash char(64) NOT NULL UNIQUE,
    csrf_hash char(64) NOT NULL,
    client_binding_hash char(64),
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    absolute_expires_at timestamptz NOT NULL,
    revoked_at timestamptz,
    revoke_reason text,
    rotated_from uuid REFERENCES sessions(session_id) ON DELETE SET NULL,
    CHECK (expires_at <= absolute_expires_at)
);

CREATE TABLE IF NOT EXISTS api_tokens (
    token_id uuid PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
    token_prefix text NOT NULL,
    token_hash char(64) NOT NULL UNIQUE,
    name text NOT NULL,
    permissions jsonb NOT NULL DEFAULT '[]'::jsonb,
    site_scopes jsonb NOT NULL DEFAULT '[]'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz,
    last_used_at timestamptz,
    revoked_at timestamptz
);

CREATE TABLE IF NOT EXISTS login_attempts (
    attempt_id bigserial PRIMARY KEY,
    username_normalized text NOT NULL,
    source_ip inet,
    success boolean NOT NULL,
    reason_code text NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS rate_limit_state (
    bucket_key char(64) PRIMARY KEY,
    window_started_at timestamptz NOT NULL,
    request_count integer NOT NULL CHECK (request_count >= 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS worker_registry_cache (
    site_id text NOT NULL,
    node_id text NOT NULL,
    payload jsonb NOT NULL,
    source_event_id text NOT NULL,
    observed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (site_id, node_id)
);

CREATE TABLE IF NOT EXISTS event_replay_cache (
    event_sequence bigserial PRIMARY KEY,
    event_id text NOT NULL UNIQUE,
    site_id text NOT NULL,
    event_type text NOT NULL,
    resource_type text,
    resource_id text,
    envelope jsonb NOT NULL,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL
);

CREATE TABLE IF NOT EXISTS command_requests (
    request_id text PRIMARY KEY,
    correlation_id text NOT NULL,
    idempotency_key text,
    proxy_instance_id text NOT NULL,
    user_id uuid NOT NULL REFERENCES users(user_id),
    site_id text NOT NULL,
    event_type text NOT NULL,
    state text NOT NULL CHECK (state IN ('PENDING', 'ACCEPTED', 'COMPLETED', 'FAILED', 'EXPIRED')),
    request_payload jsonb NOT NULL,
    envelope jsonb NOT NULL,
    result jsonb,
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (user_id, site_id, event_type, idempotency_key)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id uuid PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    request_id text NOT NULL,
    correlation_id text,
    principal_subject text,
    session_id text,
    source_ip inet,
    proxy_instance_id text NOT NULL,
    site_id text,
    resource text NOT NULL,
    action text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('ALLOW', 'DENY', 'ERROR')),
    reason_code text NOT NULL,
    operation_id text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE TABLE IF NOT EXISTS security_events (
    security_event_id uuid PRIMARY KEY,
    event_type text NOT NULL,
    severity text NOT NULL CHECK (severity IN ('INFO', 'WARNING', 'HIGH', 'CRITICAL')),
    principal_subject text,
    source_ip inet,
    request_id text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_sessions_active
    ON sessions(token_hash, expires_at) WHERE revoked_at IS NULL;
CREATE INDEX IF NOT EXISTS idx_login_attempts_lookup
    ON login_attempts(username_normalized, source_ip, occurred_at DESC);
CREATE INDEX IF NOT EXISTS idx_replay_site_sequence
    ON event_replay_cache(site_id, event_sequence);
CREATE INDEX IF NOT EXISTS idx_replay_expiry
    ON event_replay_cache(expires_at);
CREATE INDEX IF NOT EXISTS idx_command_correlation
    ON command_requests(correlation_id);
CREATE INDEX IF NOT EXISTS idx_command_due
    ON command_requests(proxy_instance_id,state,next_attempt_at)
    WHERE state IN ('PENDING','ACCEPTED');
CREATE INDEX IF NOT EXISTS idx_audit_site_time
    ON audit_log(site_id, occurred_at DESC);

CREATE OR REPLACE FUNCTION reject_append_only_change()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'append-only relation % cannot be changed', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$function$;

DROP TRIGGER IF EXISTS audit_log_append_only ON audit_log;
CREATE TRIGGER audit_log_append_only
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION reject_append_only_change();

DROP TRIGGER IF EXISTS security_events_append_only ON security_events;
CREATE TRIGGER security_events_append_only
BEFORE UPDATE OR DELETE ON security_events
FOR EACH ROW EXECUTE FUNCTION reject_append_only_change();

INSERT INTO permissions(permission_name, description) VALUES
    ('node.read', 'Read node and cluster state'),
    ('node.drain', 'Request a controlled node drain'),
    ('node.admit', 'Approve a pending worker admission'),
    ('config.read', 'Read configuration and history'),
    ('config.propose', 'Propose bounded configuration changes'),
    ('config.rollback', 'Propose rollback to an earlier revision'),
    ('actuator.request', 'Request a fenced remote actuator operation'),
    ('operation.read', 'Read operation state'),
    ('user.manage', 'Manage local users'),
    ('role.manage', 'Manage roles and grants'),
    ('audit.read', 'Read security and audit records'),
    ('terminal.execute', 'Execute allowlisted domain console commands')
ON CONFLICT (permission_name) DO NOTHING;

INSERT INTO roles(role_id, role_name, description, builtin) VALUES
    ('00000000-0000-0000-0000-000000000001', 'viewer', 'Read-only site access', true),
    ('00000000-0000-0000-0000-000000000002', 'operator', 'Process operation access', true),
    ('00000000-0000-0000-0000-000000000003', 'engineer', 'Configuration engineering access', true),
    ('00000000-0000-0000-0000-000000000004', 'site_admin', 'Site user and worker administration', true),
    ('00000000-0000-0000-0000-000000000005', 'security_admin', 'Session, token and audit administration', true),
    ('00000000-0000-0000-0000-000000000006', 'platform_admin', 'Full platform administration', true)
ON CONFLICT (role_name) DO NOTHING;

INSERT INTO role_permissions(role_id, permission_name)
SELECT '00000000-0000-0000-0000-000000000001', value
FROM (VALUES ('node.read'), ('config.read'), ('operation.read')) AS p(value)
ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(role_id, permission_name)
SELECT '00000000-0000-0000-0000-000000000002', value
FROM (VALUES ('node.read'), ('config.read'), ('operation.read'), ('actuator.request'), ('terminal.execute')) AS p(value)
ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(role_id, permission_name)
SELECT '00000000-0000-0000-0000-000000000003', value
FROM (VALUES ('node.read'), ('config.read'), ('config.propose'), ('config.rollback'), ('operation.read'), ('terminal.execute')) AS p(value)
ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(role_id, permission_name)
SELECT '00000000-0000-0000-0000-000000000004', value
FROM (VALUES ('node.read'), ('node.drain'), ('node.admit'), ('config.read'), ('operation.read'), ('user.manage'), ('role.manage'), ('terminal.execute')) AS p(value)
ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(role_id, permission_name)
SELECT '00000000-0000-0000-0000-000000000005', value
FROM (VALUES ('node.read'), ('operation.read'), ('user.manage'), ('role.manage'), ('audit.read')) AS p(value)
ON CONFLICT DO NOTHING;
INSERT INTO role_permissions(role_id, permission_name)
SELECT '00000000-0000-0000-0000-000000000006', permission_name FROM permissions
ON CONFLICT DO NOTHING;

GRANT USAGE ON SCHEMA proxy_gateway TO proxy_app;
GRANT SELECT, INSERT, UPDATE, DELETE ON ALL TABLES IN SCHEMA proxy_gateway TO proxy_app;
REVOKE UPDATE, DELETE ON audit_log, security_events FROM proxy_app;
REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM proxy_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA proxy_gateway TO proxy_app;
ALTER DEFAULT PRIVILEGES FOR ROLE proxy_migrator IN SCHEMA proxy_gateway
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO proxy_app;
ALTER DEFAULT PRIVILEGES FOR ROLE proxy_migrator IN SCHEMA proxy_gateway
    GRANT USAGE, SELECT ON SEQUENCES TO proxy_app;

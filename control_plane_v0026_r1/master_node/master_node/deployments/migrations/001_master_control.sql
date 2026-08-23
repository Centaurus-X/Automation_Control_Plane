-- master_node/deployments/migrations/001_master_control.sql
-- Authoritative control-plane schema. All timestamps are UTC timestamptz.

CREATE SCHEMA IF NOT EXISTS master_control AUTHORIZATION master_migrator;
REVOKE ALL ON SCHEMA master_control FROM PUBLIC;
SET search_path TO master_control, pg_catalog;

CREATE TABLE IF NOT EXISTS schema_migrations (
    version bigint PRIMARY KEY,
    name text NOT NULL,
    checksum char(64) NOT NULL,
    applied_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS authority_state (
    site_id text PRIMARY KEY,
    authority_term bigint NOT NULL DEFAULT 0 CHECK (authority_term >= 0),
    leader_instance_id text,
    lease_token uuid,
    lease_until timestamptz,
    config_revision bigint NOT NULL DEFAULT 0 CHECK (config_revision >= 0),
    config_fingerprint char(64) NOT NULL DEFAULT repeat('0', 64),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    CHECK ((leader_instance_id IS NULL) = (lease_token IS NULL))
);

CREATE TABLE IF NOT EXISTS config_snapshots (
    site_id text NOT NULL,
    revision bigint NOT NULL CHECK (revision > 0),
    fingerprint char(64) NOT NULL,
    config_data jsonb NOT NULL,
    authority_term bigint NOT NULL CHECK (authority_term > 0),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    created_by text NOT NULL,
    PRIMARY KEY (site_id, revision)
);

CREATE TABLE IF NOT EXISTS config_commits (
    commit_id uuid PRIMARY KEY,
    site_id text NOT NULL,
    revision bigint NOT NULL,
    base_revision bigint NOT NULL,
    authority_term bigint NOT NULL,
    fingerprint char(64) NOT NULL,
    principal_subject text NOT NULL,
    request_id text NOT NULL,
    reason text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (site_id, request_id),
    FOREIGN KEY (site_id, revision)
        REFERENCES config_snapshots(site_id, revision)
);

CREATE TABLE IF NOT EXISTS config_commit_changes (
    commit_id uuid NOT NULL REFERENCES config_commits(commit_id),
    sequence_no integer NOT NULL CHECK (sequence_no >= 0),
    operation text NOT NULL CHECK (operation IN ('set', 'update', 'append', 'delete')),
    path text NOT NULL,
    before_value jsonb,
    after_value jsonb,
    PRIMARY KEY (commit_id, sequence_no)
);

CREATE TABLE IF NOT EXISTS config_rollouts (
    rollout_id uuid PRIMARY KEY,
    site_id text NOT NULL,
    revision bigint NOT NULL,
    authority_term bigint NOT NULL,
    state text NOT NULL CHECK (
        state IN ('PENDING', 'DISPATCHED', 'APPLIED', 'FAILED', 'STALE', 'CANCELLED')
    ),
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    FOREIGN KEY (site_id, revision)
        REFERENCES config_snapshots(site_id, revision)
);

CREATE TABLE IF NOT EXISTS nodes (
    site_id text NOT NULL,
    node_id text NOT NULL,
    certificate_identity text,
    admission_state text NOT NULL DEFAULT 'PENDING' CHECK (
        admission_state IN ('PENDING', 'APPROVED', 'DENIED', 'QUARANTINED')
    ),
    desired_state text NOT NULL DEFAULT 'ACTIVE' CHECK (
        desired_state IN ('ACTIVE', 'DRAINING', 'MAINTENANCE', 'QUARANTINED')
    ),
    observed_state text NOT NULL DEFAULT 'PENDING' CHECK (
        observed_state IN ('PENDING', 'ACTIVE', 'DEGRADED', 'DRAINING', 'MAINTENANCE', 'QUARANTINED', 'OFFLINE')
    ),
    software_version text,
    protocol_version text,
    authority_term bigint NOT NULL DEFAULT 0,
    config_revision bigint NOT NULL DEFAULT 0,
    config_fingerprint char(64),
    capabilities jsonb NOT NULL DEFAULT '{}'::jsonb,
    runtime_nonce text,
    first_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (site_id, node_id),
    UNIQUE (site_id, certificate_identity)
);

CREATE TABLE IF NOT EXISTS node_admissions (
    admission_id uuid PRIMARY KEY,
    site_id text NOT NULL,
    node_id text NOT NULL,
    requested_identity text,
    fingerprint text,
    decision text NOT NULL CHECK (decision IN ('PENDING', 'APPROVED', 'DENIED', 'QUARANTINED')),
    reason text,
    decided_by text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    decided_at timestamptz,
    FOREIGN KEY (site_id, node_id) REFERENCES nodes(site_id, node_id)
);

CREATE TABLE IF NOT EXISTS node_heartbeats (
    heartbeat_id bigserial PRIMARY KEY,
    site_id text NOT NULL,
    node_id text NOT NULL,
    message_id text NOT NULL,
    authority_term bigint NOT NULL,
    config_revision bigint NOT NULL,
    config_fingerprint char(64),
    health jsonb NOT NULL DEFAULT '{}'::jsonb,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (site_id, node_id, message_id),
    FOREIGN KEY (site_id, node_id) REFERENCES nodes(site_id, node_id)
);

CREATE TABLE IF NOT EXISTS node_desired_state (
    site_id text NOT NULL,
    node_id text NOT NULL,
    desired_state text NOT NULL CHECK (
        desired_state IN ('ACTIVE', 'DRAINING', 'MAINTENANCE', 'QUARANTINED')
    ),
    reason text,
    changed_by text NOT NULL,
    changed_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (site_id, node_id),
    FOREIGN KEY (site_id, node_id) REFERENCES nodes(site_id, node_id)
);

CREATE TABLE IF NOT EXISTS controller_ownership (
    site_id text NOT NULL,
    controller_uuid uuid NOT NULL,
    owner_node_id text NOT NULL,
    authority_term bigint NOT NULL CHECK (authority_term > 0),
    lease_token uuid NOT NULL,
    lease_until timestamptz NOT NULL,
    config_revision bigint NOT NULL CHECK (config_revision > 0),
    acquired_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (site_id, controller_uuid),
    FOREIGN KEY (site_id, owner_node_id) REFERENCES nodes(site_id, node_id)
);

CREATE TABLE IF NOT EXISTS controller_leases (
    lease_id uuid PRIMARY KEY,
    site_id text NOT NULL,
    controller_uuid uuid NOT NULL,
    owner_node_id text NOT NULL,
    authority_term bigint NOT NULL,
    config_revision bigint NOT NULL,
    issued_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    expires_at timestamptz NOT NULL,
    released_at timestamptz,
    release_reason text
);

CREATE TABLE IF NOT EXISTS config_rollout_nodes (
    rollout_id uuid NOT NULL REFERENCES config_rollouts(rollout_id),
    node_id text NOT NULL,
    message_id text NOT NULL,
    state text NOT NULL CHECK (
        state IN ('PENDING', 'DISPATCHED', 'APPLIED', 'FAILED', 'STALE', 'CANCELLED')
    ),
    result jsonb,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (rollout_id, node_id),
    UNIQUE (message_id)
);

CREATE TABLE IF NOT EXISTS inbox (
    site_id text NOT NULL,
    source_service text NOT NULL,
    source_instance_id text NOT NULL,
    message_id text NOT NULL,
    event_type text NOT NULL,
    payload_fingerprint char(64) NOT NULL,
    state text NOT NULL CHECK (state IN ('RECEIVED', 'COMPLETED', 'REJECTED')),
    result jsonb,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    completed_at timestamptz,
    PRIMARY KEY (site_id, source_service, source_instance_id, message_id)
);

CREATE TABLE IF NOT EXISTS outbox (
    message_id text PRIMARY KEY,
    site_id text NOT NULL,
    target_service text NOT NULL,
    target_instance_id text NOT NULL,
    event_type text NOT NULL,
    envelope jsonb NOT NULL,
    state text NOT NULL DEFAULT 'PENDING' CHECK (state IN ('PENDING', 'SENT', 'ACKED', 'FAILED', 'CANCELLED')),
    attempt_count integer NOT NULL DEFAULT 0 CHECK (attempt_count >= 0),
    next_attempt_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    last_error_code text,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS actuator_operations (
    operation_id text PRIMARY KEY,
    site_id text NOT NULL,
    idempotency_key text NOT NULL,
    source_service text NOT NULL,
    source_instance_id text NOT NULL,
    principal_subject text NOT NULL,
    controller_uuid uuid NOT NULL,
    actuator_id text NOT NULL,
    requested_value jsonb NOT NULL,
    owner_node_id text NOT NULL,
    authority_term bigint NOT NULL,
    config_revision bigint NOT NULL,
    lease_token uuid NOT NULL,
    state text NOT NULL CHECK (
        state IN ('RECEIVED', 'ROUTED', 'DISPATCHED', 'CONFIRMED', 'WRITE_UNVERIFIED', 'FAILED', 'REJECTED', 'UNCERTAIN', 'CANCELLED')
    ),
    request_payload jsonb NOT NULL,
    result jsonb,
    expires_at timestamptz NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (site_id, source_service, principal_subject, idempotency_key)
);

CREATE TABLE IF NOT EXISTS operation_events (
    event_id uuid PRIMARY KEY,
    operation_id text NOT NULL REFERENCES actuator_operations(operation_id),
    state text NOT NULL,
    details jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS sensor_latest (
    site_id text NOT NULL,
    controller_uuid uuid NOT NULL,
    sensor_id text NOT NULL,
    source_node_id text NOT NULL,
    value jsonb,
    quality text NOT NULL,
    source_sequence bigint,
    measured_at timestamptz,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (site_id, controller_uuid, sensor_id)
);

CREATE TABLE IF NOT EXISTS system_metadata (
    site_id text NOT NULL,
    metadata_key text NOT NULL,
    metadata_value jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (site_id, metadata_key)
);

CREATE TABLE IF NOT EXISTS audit_log (
    audit_id uuid PRIMARY KEY,
    occurred_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    request_id text NOT NULL,
    correlation_id text,
    principal_subject text,
    session_id text,
    source_ip inet,
    instance_id text NOT NULL,
    site_id text NOT NULL,
    resource text NOT NULL,
    action text NOT NULL,
    decision text NOT NULL CHECK (decision IN ('ALLOW', 'DENY', 'ERROR')),
    reason_code text NOT NULL,
    config_revision bigint,
    operation_id text,
    details jsonb NOT NULL DEFAULT '{}'::jsonb
);

CREATE INDEX IF NOT EXISTS idx_nodes_last_seen
    ON nodes(site_id, last_seen_at DESC);
CREATE INDEX IF NOT EXISTS idx_heartbeats_retention
    ON node_heartbeats(received_at);
CREATE INDEX IF NOT EXISTS idx_rollout_nodes_state
    ON config_rollout_nodes(state, updated_at);
CREATE INDEX IF NOT EXISTS idx_outbox_due
    ON outbox(state, next_attempt_at) WHERE state IN ('PENDING', 'SENT');
CREATE INDEX IF NOT EXISTS idx_operations_state
    ON actuator_operations(site_id, state, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_audit_lookup
    ON audit_log(site_id, occurred_at DESC);

CREATE OR REPLACE FUNCTION reject_immutable_change()
RETURNS trigger
LANGUAGE plpgsql
AS $function$
BEGIN
    RAISE EXCEPTION 'immutable relation % cannot be changed', TG_TABLE_NAME
        USING ERRCODE = '55000';
END;
$function$;

DROP TRIGGER IF EXISTS config_snapshots_immutable ON config_snapshots;
CREATE TRIGGER config_snapshots_immutable
BEFORE UPDATE OR DELETE ON config_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

DROP TRIGGER IF EXISTS audit_log_immutable ON audit_log;
CREATE TRIGGER audit_log_immutable
BEFORE UPDATE OR DELETE ON audit_log
FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

GRANT USAGE ON SCHEMA master_control TO master_app;
GRANT SELECT, INSERT, UPDATE ON ALL TABLES IN SCHEMA master_control TO master_app;
GRANT DELETE ON controller_ownership TO master_app;
GRANT DELETE ON node_heartbeats, inbox, outbox TO master_app;
REVOKE UPDATE, DELETE ON config_snapshots, config_commits, config_commit_changes,
    operation_events, audit_log FROM master_app;
REVOKE INSERT, UPDATE, DELETE ON schema_migrations FROM master_app;
GRANT USAGE, SELECT ON ALL SEQUENCES IN SCHEMA master_control TO master_app;
ALTER DEFAULT PRIVILEGES FOR ROLE master_migrator IN SCHEMA master_control
    GRANT SELECT, INSERT, UPDATE ON TABLES TO master_app;
ALTER DEFAULT PRIVILEGES FOR ROLE master_migrator IN SCHEMA master_control
    GRANT USAGE, SELECT ON SEQUENCES TO master_app;

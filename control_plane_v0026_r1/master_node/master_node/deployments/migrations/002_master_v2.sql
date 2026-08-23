-- master_node/deployments/migrations/002_master_v2.sql
-- Master v2 entity, scope, rollout, proposal and activation persistence.

SET search_path TO master_control, pg_catalog;

DO $migration$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.columns
        WHERE table_schema='master_control'
          AND table_name='config_commits'
          AND column_name='revision'
    ) THEN
        ALTER TABLE config_rollout_nodes RENAME TO legacy_config_rollout_nodes_v1;
        ALTER TABLE config_rollouts RENAME TO legacy_config_rollouts_v1;
        ALTER TABLE config_commit_changes RENAME TO legacy_config_commit_changes_v1;
        ALTER TABLE config_commits RENAME TO legacy_config_commits_v1;
        ALTER TABLE config_snapshots RENAME TO legacy_config_snapshots_v1;
    END IF;
END
$migration$;

CREATE TABLE IF NOT EXISTS config_commits (
    commit_id text PRIMARY KEY,
    operation_id text NOT NULL UNIQUE,
    global_revision bigint NOT NULL UNIQUE CHECK (global_revision > 0),
    source_kind text NOT NULL,
    source_node_id text,
    base_global_revision bigint,
    status text NOT NULL CHECK (status IN ('accepted', 'rejected')),
    request_payload jsonb NOT NULL,
    result_payload jsonb NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS config_entities (
    section text NOT NULL,
    entity_key text NOT NULL,
    entity_revision bigint NOT NULL CHECK (entity_revision > 0),
    payload jsonb NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    last_commit_id text NOT NULL REFERENCES config_commits(commit_id),
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (section, entity_key)
);

CREATE TABLE IF NOT EXISTS config_entity_versions (
    commit_id text NOT NULL REFERENCES config_commits(commit_id),
    op_index integer NOT NULL CHECK (op_index >= 0),
    section text NOT NULL,
    entity_key text NOT NULL,
    operation text NOT NULL CHECK (operation IN ('upsert_entity', 'delete_entity')),
    before_payload jsonb,
    after_payload jsonb,
    PRIMARY KEY (commit_id, op_index)
);

CREATE TABLE IF NOT EXISTS scope_heads (
    scope text PRIMARY KEY,
    scope_kind text NOT NULL CHECK (scope_kind IN ('node', 'cross')),
    owner_node_id text,
    scope_revision bigint NOT NULL CHECK (scope_revision > 0),
    global_revision bigint NOT NULL CHECK (global_revision > 0),
    fingerprint char(64) NOT NULL,
    contracts_version text NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch > 0),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS scope_snapshots (
    scope text NOT NULL,
    scope_revision bigint NOT NULL CHECK (scope_revision > 0),
    global_revision bigint NOT NULL CHECK (global_revision > 0),
    fingerprint char(64) NOT NULL,
    canonical_config jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope, scope_revision)
);

CREATE TABLE IF NOT EXISTS scope_ops (
    scope text NOT NULL,
    target_scope_revision bigint NOT NULL CHECK (target_scope_revision > 0),
    op_index integer NOT NULL CHECK (op_index >= 0),
    operation text NOT NULL CHECK (operation IN ('upsert_entity', 'delete_entity', 'replace_field')),
    section text NOT NULL,
    entity_key text NOT NULL,
    json_pointer text,
    value jsonb,
    expected_value_hash char(64),
    value_hash char(64),
    commit_id text NOT NULL REFERENCES config_commits(commit_id),
    PRIMARY KEY (scope, target_scope_revision, op_index)
);

CREATE TABLE IF NOT EXISTS shard_rollouts (
    rollout_id text PRIMARY KEY,
    node_id text NOT NULL,
    scope text NOT NULL,
    target_scope_revision bigint NOT NULL CHECK (target_scope_revision > 0),
    target_fingerprint char(64) NOT NULL,
    coordinator_term bigint NOT NULL CHECK (coordinator_term > 0),
    transfer_id text,
    state text NOT NULL CHECK (
        state IN ('staged', 'sending', 'received', 'applied', 'converged',
                  'retry_wait', 'failed', 'superseded')
    ),
    attempts integer NOT NULL DEFAULT 0 CHECK (attempts >= 0),
    next_attempt_at timestamptz,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (node_id, scope, target_scope_revision)
);

CREATE TABLE IF NOT EXISTS worker_proposals (
    proposal_id text PRIMARY KEY,
    node_id text NOT NULL,
    scope text NOT NULL,
    base_scope_revision bigint NOT NULL CHECK (base_scope_revision >= 0),
    base_scope_fingerprint char(64) NOT NULL,
    operation_id text NOT NULL UNIQUE,
    operations jsonb NOT NULL,
    state text NOT NULL CHECK (
        state IN ('received', 'accepted', 'already_committed', 'rebase_required',
                  'rejected', 'not_authorized', 'converged')
    ),
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS scope_activations (
    entity_key text PRIMARY KEY,
    active_scope text NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch > 0),
    state text NOT NULL CHECK (state IN ('staged', 'active', 'retiring', 'inactive', 'failed')),
    last_commit_id text NOT NULL REFERENCES config_commits(commit_id),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

ALTER TABLE nodes ADD COLUMN IF NOT EXISTS contracts_version text;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS protocol_versions jsonb NOT NULL DEFAULT '[]'::jsonb;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS observed_global_revision bigint NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS node_replica_scope text;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS node_replica_revision bigint NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS node_replica_fingerprint char(64);
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS cross_replica_scope text;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS cross_replica_revision bigint NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS cross_replica_fingerprint char(64);
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS live_config_revision bigint NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS pending_proposal_count integer NOT NULL DEFAULT 0;
ALTER TABLE nodes ADD COLUMN IF NOT EXISTS max_packet_size integer NOT NULL DEFAULT 1048576;

ALTER TABLE actuator_operations ADD COLUMN IF NOT EXISTS execution_scope text;
ALTER TABLE actuator_operations ADD COLUMN IF NOT EXISTS execution_epoch bigint;

CREATE INDEX IF NOT EXISTS idx_config_entity_versions_identity
    ON config_entity_versions(section, entity_key);
CREATE INDEX IF NOT EXISTS idx_scope_snapshots_global
    ON scope_snapshots(global_revision, scope);
CREATE INDEX IF NOT EXISTS idx_scope_ops_patch
    ON scope_ops(scope, target_scope_revision, op_index);
CREATE INDEX IF NOT EXISTS idx_shard_rollouts_due
    ON shard_rollouts(state, next_attempt_at)
    WHERE state IN ('staged', 'sending', 'retry_wait');
CREATE INDEX IF NOT EXISTS idx_worker_proposals_node
    ON worker_proposals(node_id, state, updated_at DESC);

DROP TRIGGER IF EXISTS scope_snapshots_immutable ON scope_snapshots;
CREATE TRIGGER scope_snapshots_immutable
BEFORE UPDATE OR DELETE ON scope_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

DROP TRIGGER IF EXISTS config_entity_versions_immutable ON config_entity_versions;
CREATE TRIGGER config_entity_versions_immutable
BEFORE UPDATE OR DELETE ON config_entity_versions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

DROP TRIGGER IF EXISTS scope_ops_immutable ON scope_ops;
CREATE TRIGGER scope_ops_immutable
BEFORE UPDATE OR DELETE ON scope_ops
FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

GRANT SELECT, INSERT, UPDATE ON config_commits, config_entities,
    config_entity_versions, scope_heads, scope_snapshots, scope_ops,
    shard_rollouts, worker_proposals, scope_activations TO master_app;
REVOKE UPDATE, DELETE ON config_commits, config_entity_versions,
    scope_snapshots, scope_ops FROM master_app;
GRANT DELETE ON scope_heads, scope_activations TO master_app;

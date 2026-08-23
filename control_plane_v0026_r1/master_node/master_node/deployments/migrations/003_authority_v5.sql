SET search_path TO master_control, public;

CREATE TABLE IF NOT EXISTS worker_mirror_heads (
    scope text PRIMARY KEY,
    scope_kind text NOT NULL CHECK (scope_kind IN ('node', 'cross')),
    owner_node_id text,
    source_node_id text NOT NULL,
    authority_epoch bigint NOT NULL CHECK (authority_epoch >= 0),
    scope_revision bigint NOT NULL CHECK (scope_revision >= 0),
    execution_epoch bigint NOT NULL CHECK (execution_epoch >= 0),
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    contracts_version text NOT NULL,
    type_schema_version text NOT NULL,
    config_norm_version jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS worker_mirror_snapshots (
    scope text NOT NULL,
    authority_epoch bigint NOT NULL CHECK (authority_epoch >= 0),
    scope_revision bigint NOT NULL CHECK (scope_revision >= 0),
    execution_epoch bigint NOT NULL CHECK (execution_epoch >= 0),
    fingerprint char(64) NOT NULL CHECK (fingerprint ~ '^[0-9a-f]{64}$'),
    contracts_version text NOT NULL,
    type_schema_version text NOT NULL,
    config_norm_version jsonb NOT NULL,
    source_node_id text NOT NULL,
    snapshot jsonb NOT NULL,
    received_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope, authority_epoch, scope_revision, execution_epoch)
);

CREATE TABLE IF NOT EXISTS cross_registry_heads (
    scope text PRIMARY KEY,
    owner_node_id text NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch >= 0),
    transition_id text NOT NULL UNIQUE,
    activation_proof jsonb NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS cross_registry_transitions (
    transition_id text PRIMARY KEY,
    scope text NOT NULL,
    old_owner_node_id text NOT NULL,
    new_owner_node_id text NOT NULL,
    previous_execution_epoch bigint NOT NULL,
    execution_epoch bigint NOT NULL CHECK (execution_epoch >= 0),
    state text NOT NULL,
    activation_proof jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (scope, execution_epoch)
);

CREATE TABLE IF NOT EXISTS authority_transfers (
    transfer_id text PRIMARY KEY,
    scope text NOT NULL,
    state text NOT NULL,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE IF NOT EXISTS worker_restores (
    restore_id text PRIMARY KEY,
    scope text NOT NULL,
    state text NOT NULL,
    record jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE INDEX IF NOT EXISTS idx_worker_mirror_snapshots_received
    ON worker_mirror_snapshots(received_at DESC, scope);
CREATE INDEX IF NOT EXISTS idx_cross_registry_transitions_scope
    ON cross_registry_transitions(scope, execution_epoch DESC);
CREATE INDEX IF NOT EXISTS idx_authority_transfers_scope
    ON authority_transfers(scope, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_worker_restores_scope
    ON worker_restores(scope, updated_at DESC);

DROP TRIGGER IF EXISTS worker_mirror_snapshots_immutable ON worker_mirror_snapshots;
CREATE TRIGGER worker_mirror_snapshots_immutable
BEFORE UPDATE OR DELETE ON worker_mirror_snapshots
FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

DROP TRIGGER IF EXISTS cross_registry_transitions_immutable ON cross_registry_transitions;
CREATE TRIGGER cross_registry_transitions_immutable
BEFORE UPDATE OR DELETE ON cross_registry_transitions
FOR EACH ROW EXECUTE FUNCTION reject_immutable_change();

GRANT SELECT, INSERT, UPDATE ON worker_mirror_heads, cross_registry_heads,
    authority_transfers, worker_restores TO master_app;
GRANT SELECT, INSERT ON worker_mirror_snapshots,
    cross_registry_transitions TO master_app;
GRANT DELETE ON worker_mirror_heads, cross_registry_heads,
    authority_transfers, worker_restores TO master_app;
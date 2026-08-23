-- Automation Proxy Gateway v0015 additive local-action idempotency registry.

SET search_path TO proxy_gateway, pg_catalog;

CREATE TABLE IF NOT EXISTS local_action_idempotency (
    request_id text PRIMARY KEY,
    user_id uuid NOT NULL REFERENCES users(user_id),
    site_id text NOT NULL,
    action text NOT NULL,
    idempotency_key text NOT NULL,
    payload_fingerprint text NOT NULL,
    state text NOT NULL CHECK (state IN ('PENDING', 'COMPLETED', 'FAILED')),
    result jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (user_id, site_id, action, idempotency_key)
);

CREATE INDEX IF NOT EXISTS idx_local_action_idempotency_updated
    ON local_action_idempotency(updated_at);

GRANT SELECT, INSERT, UPDATE ON local_action_idempotency TO proxy_app;
REVOKE DELETE ON local_action_idempotency FROM proxy_app;

-- Persist the Master-issued operation identity on the already durable Proxy
-- command row.  This gives the event fanout a local, authoritative
-- operation_id -> requesting user mapping without changing Master or Worker.
ALTER TABLE command_requests
    ADD COLUMN IF NOT EXISTS operation_id text;

UPDATE command_requests
SET operation_id = NULLIF(result->>'operation_id', '')
WHERE operation_id IS NULL
  AND jsonb_typeof(result) = 'object'
  AND result ? 'operation_id';

CREATE INDEX IF NOT EXISTS idx_command_operation_owner
    ON command_requests(site_id, operation_id)
    WHERE operation_id IS NOT NULL;

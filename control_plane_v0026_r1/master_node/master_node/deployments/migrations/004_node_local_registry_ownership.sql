SET search_path TO master_control, public;

ALTER TABLE controller_ownership
    DROP CONSTRAINT IF EXISTS controller_ownership_config_revision_check;

ALTER TABLE controller_ownership
    ADD CONSTRAINT controller_ownership_config_revision_check
    CHECK (config_revision >= 0);

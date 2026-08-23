#!/usr/bin/env bash

set -Eeuo pipefail

replication_password="$(< /run/secrets/replication_password)"
master_migrator_password="$(< /run/secrets/master_migrator_password)"
master_app_password="$(< /run/secrets/master_app_password)"
proxy_migrator_password="$(< /run/secrets/proxy_migrator_password)"
proxy_app_password="$(< /run/secrets/proxy_app_password)"

psql --set=ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
  --set=replication_password="$replication_password" \
  --set=master_migrator_password="$master_migrator_password" \
  --set=master_app_password="$master_app_password" \
  --set=proxy_migrator_password="$proxy_migrator_password" \
  --set=proxy_app_password="$proxy_app_password" <<'SQL'
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
REVOKE ALL ON DATABASE automation_platform FROM PUBLIC;

CREATE ROLE replicator LOGIN REPLICATION;
ALTER ROLE replicator PASSWORD :'replication_password';
SELECT pg_create_physical_replication_slot('automation_standby')
WHERE NOT EXISTS (
    SELECT 1 FROM pg_replication_slots WHERE slot_name='automation_standby'
);

CREATE ROLE master_migrator LOGIN NOINHERIT;
ALTER ROLE master_migrator PASSWORD :'master_migrator_password';
CREATE ROLE master_app LOGIN NOINHERIT;
ALTER ROLE master_app PASSWORD :'master_app_password';

CREATE ROLE proxy_migrator LOGIN NOINHERIT;
ALTER ROLE proxy_migrator PASSWORD :'proxy_migrator_password';
CREATE ROLE proxy_app LOGIN NOINHERIT;
ALTER ROLE proxy_app PASSWORD :'proxy_app_password';

GRANT CONNECT ON DATABASE automation_platform TO
    master_migrator, master_app, proxy_migrator, proxy_app;
GRANT CREATE ON DATABASE automation_platform TO master_migrator, proxy_migrator;
ALTER ROLE master_app SET search_path = master_control, pg_catalog;
ALTER ROLE proxy_app SET search_path = proxy_gateway, pg_catalog;
ALTER ROLE master_migrator SET search_path = master_control, pg_catalog;
ALTER ROLE proxy_migrator SET search_path = proxy_gateway, pg_catalog;
SQL

unset replication_password master_migrator_password master_app_password
unset proxy_migrator_password proxy_app_password

# PostgreSQL-System

Eigenständiges PostgreSQL-18.4-Paket mit Primary, Hot Standby,
Streaming-Replikation, TLS-only HBA, getrennten App-/Migratorrollen und
persistenten Volumes. Port 5432 wird produktiv nicht auf den Host publiziert.

## Daten und Rollen

```text
automation_platform
├── master_control  -> master_app / master_migrator
└── proxy_gateway   -> proxy_app  / proxy_migrator
```

Der Worker und der MQTT-Broker erhalten weder Credentials noch Netzwerkzugang.
Das externe Docker-Netz `automation_database` wird als internes Netz mit dem
festen Subnetz `172.30.0.0/24` erzeugt und gegen `pg_hba.conf` geprüft.

PostgreSQL 18 verwendet hier bewusst:

```text
Volume-Mount: /var/lib/postgresql
PGDATA:       /var/lib/postgresql/18/docker
```

## Skripte

```bash
./test_static.sh
./deploy.sh                  # produktives TLS erforderlich
./deploy_test.sh             # selbstsigniertes Lab-TLS + Loopback-Ports
./test.sh                    # Recreate, Replikation, Backup und Restore
./scripts/verify_replication.sh
./scripts/test_persistence.sh
./scripts/backup.sh
./scripts/restore_test.sh
./scripts/promote_standby.sh --confirm-primary-is-fenced
./scripts/destroy_data.sh --confirm-destroy-all-data
./status.sh
./logs.sh
./stop.sh
```

Produktive Zertifikate werden über `POSTGRES_CA_SOURCE`,
`POSTGRES_PRIMARY_CERT_SOURCE`, `POSTGRES_PRIMARY_KEY_SOURCE`,
`POSTGRES_STANDBY_CERT_SOURCE` und `POSTGRES_STANDBY_KEY_SOURCE` übergeben.
Der Primary benötigt SANs für `automation-postgres` und `postgres-primary`, der
Standby für `postgres-standby`.

`backup/`, WAL-Archiv und Datenvolumes sind unterschiedliche Dinge:
Streaming-Replikation ersetzt kein Backup. Die erzeugten Backups und WALs
müssen regelmäßig auf ein unabhängiges, off-host Ziel übertragen werden.

Primary und Standby auf demselben Docker-Host sind ausschließlich für das Lab
geeignet. Produktion verlangt getrennte Failure Domains.

v0004 bereitet `/backup` und `/archive` bei jedem Containerstart explizit als
PostgreSQL-UID/GID mit Modus 0700 vor. Auch der initiale Standby-Basebackup
prüft den Primary-Namen und die CA mit `sslmode=verify-full`.

# Release-Validierung v0012

Stand: 16. Juli 2026  
Artefakt: `automation_control_plane_release_v0012.zip`  
Korrekturstand: `r6`

## 1. Korrekturen

- `r1`: Master-Compose-`build`-Blöcke korrekt unter den Services
  verschachtelt; statisches Duplicate-Key-Gate ergänzt.
- `r2`: Master baut auf dem lokal erzeugten no-GIL-Runtime-Image ohne
  Registry-`--pull`; Fresh purgt vor dem Build; Worker-Quellarchiv, Overlays,
  Dockerfile, Compose-Projekt und Worker-Lifecycle vollständig aus der Control
  Plane entfernt.
- `r3`: Der `cp314t`-Master nutzt Pure-Python-psycopg mit System-`libpq`; der
  normale `cp314`-Proxy behält das passende Binary-Wheel. Build- und
  Regressionstests erzwingen beide Implementierungen und prüfen libpq.
- `r4`: Der direkte Fast-Rollout `./rollout.sh --fresh` wird ausschließlich
  auf das Lab abgebildet. Ein Stub-Test prüft seine Argumentweitergabe ohne
  Docker-Seiteneffekt; die explizite Produktionsschranke bleibt unverändert.
- `r5`: Master-Control-Ergebnisse werden vor der dauerhaften JSONB-Inbox-
  Speicherung normalisiert; PostgreSQL-Zeitstempel aus `nodes.list` sind damit
  sicher serialisierbar. Der Proxy abonniert nur noch Master-originierte
  Control-Events statt des Worker-/Control-Sammelfilters. Admin- und
  Loopback-Health-Diagnosen wurden präzisiert.
- `r6`: Der Stage-4-MQTT-Test verwendet für den Proxy-Publish-Deny nun ein
  zulässiges Master-Eventtopic statt eines durch die r5-ACL gesperrten Worker-
  Topics. Ein separater payload-basierter Read-Deny-Oracle prüft weiterhin,
  dass der Proxy keine Worker-Telemetrie erhält.

## 2. Bewertungsregel

Ein Gate ist nur `PASS`, wenn es tatsächlich ausgeführt wurde. `NOT_RUN`
bezeichnet nicht ausgeführte Zielhosttests. `BLOCKED` bezeichnet eine bekannte
Spezifikationslücke.

## 3. Ausgeführte lokale Gates

| Gate | Status | Ergebnis |
| --- | --- | --- |
| Master-Codegrenzen und 19 Tests | PASS | 72 Python-Dateien; keine Master-Ausführung |
| Entity/Graph/Shard/Fixture | PASS | 201 Entities, 3 deterministische Scopes |
| Full/Patch/Chunk-Planung | PASS | v0082-Full-Fallback, Hash-/Paketlogik |
| Proposal-/Cross-Ownership | PASS | Owner-, Feld-, Term- und Epoch-Fences |
| Proxy-Suite | PASS | Auth, RBAC, CSRF, DB und ASGI |
| Broker/Postgres static | PASS | Config, Rollen, TLS/ACL und Lifecycle |
| Compose/YAML | PASS | eindeutige Master-Service-Keys |
| Fresh-Reihenfolge | PASS | Purge vor Build, Upgrade vorstaged |
| Direkter Fast-Rollout | PASS | `--fresh`/`--upgrade` verlustfrei ans Lab weitergereicht |
| Worker-Paketgrenze | PASS | keine Worker-Distribution im ZIP/Lifecycle |
| Python-/psycopg-ABI-Grenze | PASS | Master=`python`+libpq, Proxy=`binary` |
| Control-Inbox-JSONB | PASS | Zeitstempel vor Persistierung normalisiert |
| Proxy-Control-Eventfilter | PASS | nur `events/+/master-01`, keine Worker-Telemetrie |
| MQTT-ACL-Test-Oracle | PASS | Master-Publish-Deny und Worker-Read-Deny gemockt |
| Archivhygiene | PASS | vollständige Manifeste, Baum und CRC |

Reproduzierbare Befehle:

```bash
./master_node/test.sh
./test_static_all.sh
VALIDATE_ARCHIVE_TREE=1 ./tools/validate_release.sh
./tools/build_release_archive.py
```

## 4. Worker-Systemgrenze

Die Control Plane enthält keine Worker-Codebasis. Master-seitige
Worker-Kommunikation bleibt notwendiger Bestandteil der Control Plane:

- MQTT-v1/v2-Topics und mTLS-ACLs,
- Registration, Heartbeat und Admission,
- Node-/Cross-Shardmaterialisierung,
- Full/Patch/Chunk-Planung und Full-Fallback,
- Proposal-, Cross-Fact- und Remote-Intent-Fencing.

Der reale v0082-Worker wird aus seinem eigenen Release auf seinem eigenen Host
mit `AUTOMATION_PROFILE=worker_simulation ./run_worker.sh` gestartet. Seine
Tests und sein Lifecycle bleiben unabhängig.

## 5. Nicht ausgeführte laufende Gates

Im Erstellungsumfeld war kein Docker-Daemon verfügbar:

| Gate | Status |
| --- | --- |
| CPython-3.14.6t-Sourcebuild und no-GIL-Probe | NOT_RUN |
| Mosquitto mTLS und cert-gebundene ACLs | NOT_RUN |
| PostgreSQL Primary/Standby/TLS/Migration | NOT_RUN |
| Master-/Proxy-Container und DB-Probes | NOT_RUN |
| externer Worker v0082 Registration/Heartbeat | NOT_RUN |
| externe Node-/Cross-Full/Patch/Chunk-Konvergenz | NOT_RUN |
| externer Modbus-/Cross-Site-Readback | NOT_RUN |
| Master-/Broker-Ausfall und lokale Worker-Autonomie | NOT_RUN |

Control-Plane-Zielhostlauf:

```bash
./rollout.sh --fresh --profile lab_v2
```

Worker-/Modbus-/Cross-Site-Gates werden danach getrennt mit dem unabhängig
installierten Worker durchgeführt und nicht vom Control-Plane-Rollout
vorgetäuscht.

## 6. Blocker Online-Scope-Migration

Die Spezifikation verlangt für Node→Cross, Cross→Node und Ownerwechsel:

```text
staged -> Worker READY -> Epoch commit -> Activation ACK
       -> new active -> old retiring -> old inactive
```

Scope-/Epoch-Fences sind vorhanden. Die vollständige verteilte Zustandsmaschine
ist jedoch nicht umgesetzt. Laufende Moves oder Entfernungen aktiver
Execution-Entities werden deshalb fail-closed abgelehnt. Das Gate bleibt
`BLOCKED`.

## 7. Releaseentscheidung

Das ZIP ist ein statisch und funktional selbstgetesteter Control-Plane-v0012-
Release-Candidate mit sauberer externer Worker-Grenze. Es ist keine vollständig
grüne formale Freigabe, bevor die realen Control-Plane- und externen Worker-
Tests bestanden sind und die Online-Scope-Migration geschlossen oder als
Betriebsrestriktion formell akzeptiert wurde.

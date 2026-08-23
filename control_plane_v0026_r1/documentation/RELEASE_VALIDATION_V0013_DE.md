# Release-Validierung v0013

Stand: 17. Juli 2026  
Artefakt: `automation_control_plane_release_v0013.zip`
Korrekturstand: `v0013-r3`

## 1. Releaseinhalt

v0013 übernimmt die gehärtete v0012-Control-Plane und korrigiert den
Master-v2-Bootstrap, Leader-Übergang und die semantische Readiness. Enthalten
sind ausschließlich Mosquitto, PostgreSQL, Master, Proxy, gemeinsamer Vertrag,
Runtime-Build, Migrationen, Dokumentation und Control-Plane-Tests. Eine Worker-
Distribution ist ausdrücklich nicht enthalten.

## 2. Neue Korrekturen

- idempotenter Config-Seed bei Fresh-Start und späterer Leader-Promotion;
- Reload eines dauerhaften neueren PostgreSQL-Zustands vor Worker-Ingress;
- fail-closed Start bei unvollständigem globalen Zustand oder fehlenden
  Node-Scopes;
- `config_ready` und Bootstrap-Diagnostik im Master-Health-Snapshot;
- semantischer Runtime-Probe in der laufenden Rollout-Abnahme;
- expliziter Regressionsnachweis, dass Node-Full-Sync nicht von Cross-Site
  abhängt;
- fn-01-Node-Shard behält den nativen Simulator-Endpunkt
  `192.168.0.3:5020` auch nach Master-Full-Sync;
- vollständige Versionsanhebung auf Control Plane `0.13.0`, PostgreSQL
  `18.4-v13`, no-GIL-Runtime `3.14.6-v13`, Master/Proxy `0.13.0`.
- Korrektur des real reproduzierten Stage-8-Fehlers, bei dem eine unowned
  Prozessdefinition in mehreren Shards fälschlich als mehrfach aktive
  Execution-Entity behandelt wurde;
- vollständiger Cross-Seed-Regressionspfad durch Materialisierung,
  Activation-Plan und vorbereiteten PostgreSQL-Commit;
- sichtbare Bootstrap-/Role-Handler-Exceptions statt dauerhaftem
  `state=seeding` ohne Fehlerursache;
- OCI-Revisionsgate `org.opencontainers.image.revision=v0013-r3` für das
  laufende Master-Image;
- Worker-Readiness und Hardware-Ownership erst nach exakter Node-Fingerprint-
  Konvergenz, unabhängig vom optionalen Cross-Scope;
- ursprüngliche Aktor-TTL im dauerhaften Outbox-Datensatz und unveränderte
  Weitergabe an den Worker;
- `DISPATCHED` beim erfolgreichen Transport-Publish, korrelierte positive und
  negative Worker-ACKs sowie Schließen der Execute-Outbox durch das terminale
  Worker-Resultat;
- fail-closed Operationsablauf: `FAILED` vor Publish beziehungsweise
  `UNCERTAIN` nach geöffnetem Dispatchfenster.

## 3. Bewertungsregel

Ein Gate ist nur `PASS`, wenn es tatsächlich ausgeführt wurde. `NOT_RUN`
bezeichnet einen nicht verfügbaren realen Zielhostlauf. `BLOCKED` bezeichnet
eine bekannte Spezifikationsgrenze.

## 4. Lokal ausgeführte Gates

| Gate | Status | Ergebnis |
| --- | --- | --- |
| Master Bootstrap/Leader/Readiness | PASS | Seed, Reload und Fail-closed |
| Vollständiger Lab-Cross-Seed | PASS | 3 Scopes, 20 eindeutige Activation-Anker |
| Node ohne Cross | PASS | v0082 Full unabhängig von Cross-Capability |
| Worker-Konvergenz | PASS | ACTIVE nur bei exaktem Node-Fingerprint |
| Aktor-Lifecycle | PASS | Publish, ACK/NACK, Resultat, TTL und Ablauf |
| Master-Suite | PASS | alle statischen und funktionalen Master-Tests |
| v0082-Referenzsuite | PASS | 36 Tests: MQTT, Sync, Simulation, Cross, Identity |
| Proxy-Suite | PASS | Auth, RBAC, CSRF, ASGI und DB-Mocks |
| Broker/PostgreSQL static | PASS | TLS-/ACL-/Rollen- und Lifecycle-Gates |
| Releasegrenze | PASS | kein Worker-Artefakt oder Worker-Lifecycle |
| Archivhygiene | PASS | Manifeste, exakter Baum, CRC und reproduzierbares ZIP |

Die endgültigen Testzahlen stehen zusätzlich in `TEST_RESULTS.txt`; das
erzeugte Root-`SHA256SUMS` deckt jeden Archivinhalt exakt ab. Der äußere
ZIP-Hash wird vom reproduzierbaren Buildwerkzeug nach der Erstellung
ausgegeben.

## 5. Zielhost-Gates

Im Erstellungsumfeld ist kein Docker-Daemon verfügbar. Folgende Prüfungen
bleiben deshalb bis zum realen `--fresh`-Lauf `NOT_RUN`:

- CPython-3.14.6t-Sourcebuild und no-GIL-Probe;
- Mosquitto-mTLS und zertifikatgebundene ACLs;
- PostgreSQL Primary/Standby/TLS/Migration;
- Master-/Proxy-Container und semantischer Runtime-Probe;
- externe Worker-v0082-Admission und Node-Shard-Konvergenz;
- WSS, Aktoroperation, Sensorstream und Modbus-Readback;
- Cross-Site-Fact/Intent/Readback und Ausfalltests.

Der Zielhostlauf lautet:

```bash
./rollout.sh --fresh --profile lab_v2
```

Er gilt nur dann als bestanden, wenn Stufe 8 einschließlich
`test_runtime_state.sh` grün ist und der externe Worker anschließend
`APPROVED`, online und auf dem aktuellen Node-Fingerprint konvergiert.

## 6. Bekannter Blocker

Die vollständige verteilte Online-Migration
`staged → READY → activate → retire` ist nicht implementiert. Unsichere
Moves/Removals aktiver Execution-Entities werden weiterhin fail-closed
abgelehnt. Dieses Gate bleibt `BLOCKED`.

## 7. Releaseentscheidung

Das Archiv ist ein statisch und funktional selbstgetesteter v0013-Lab-
Release-Candidate. Es darf erst nach den realen Docker-/mTLS-/PostgreSQL-/
Worker-/Modbus-Gates als praktisch abgenommen markiert werden; die normative
Online-Scope-Migration bleibt davon unabhängig blockiert.

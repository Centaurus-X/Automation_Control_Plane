# Release-Validierung v0014

Stand: 17. Juli 2026  
Artefakt: `automation_control_plane_release_v0014.zip`  
Korrekturstand: `0.14.0 / v0014-r1`, E2E `0.14.0-e2e.0`

## Bewertungsregel

`PASS` bedeutet tatsächlich lokal ausgeführt. `NOT_RUN` bedeutet, dass der
erforderliche Docker-Daemon oder reale Zielhost in der Buildumgebung nicht
verfügbar ist. `BLOCKED` bezeichnet eine bekannte Architekturgrenze.

## Lokal ausgeführte Gates

| Gate | Status | Abdeckung |
| --- | --- | --- |
| Master-Code-Standard | PASS | funktional, keine Klassen/Decorators/Typannotationen im Master-Baum |
| Master-Unit-/Contract-Suite | PASS | Bootstrap, Leader, Authority, Rollout, ACK, Outbox, Shards, Cross |
| Reale Node-Fixture-Größe | PASS | 8 KiB negativ, 4 MiB positiv, v0082 Full |
| Proxy-Suite | PASS | Auth, Rollen, CSRF, ASGI, Sessions, DB-Mocks |
| MQTT/PostgreSQL statisch | PASS | TLS-/ACL-/Rollen-/Lifecycle-Konfiguration |
| Integrierter E2E offline | PASS | Auswahl, Endpoint-Guard, Readiness, Restore, RFC 6455 |
| Shell-/Python-/JSON-Syntax | PASS | gesamter Releasebaum |
| Worker-Grenze | PASS | keine Worker-Distribution und kein Worker-Lifecycle |
| Archivhygiene | PASS | keine Secrets, Keys, Bytecode, Logs, Backups oder Runtimezustände |
| Nested/Root SHA256SUMS | PASS | exakte Baumabdeckung |
| ZIP-Struktur/CRC/Pfade | PASS | reproduzierbarer Builder und entpackter Neutest |

Die maschinenlesbaren Einzelresultate und Zählwerte stehen in
`TEST_RESULTS.txt` und `TEST_RESULTS_MASTER_V2.txt`. Der äußere ZIP-Hash wird
vom Builder ausgegeben und zusätzlich bei der Auslieferung genannt.

## Zielhost-Gates

Folgende Prüfungen bleiben in der Buildumgebung `NOT_RUN`:

- CPython-3.14.6t-Sourcebuild und no-GIL-Probe im Container,
- Mosquitto-mTLS, Zertifikat-CNs und laufende ACL-Oracles,
- PostgreSQL Primary/Standby/TLS/Replikation und echte Migration,
- Master-/Proxy-Container sowie Runtime-Readiness,
- externer Worker v0082, reale Node-Shard-Anwendung und ACK,
- WSS über das Zielnetz,
- Aktoroperation, Modbus-Readback und Restore auf `192.168.0.3:5020`,
- echter Zwei-Worker-Cross-Fact-/Remote-Intent-Pfad und Kill-Tests.

Ausführung:

```bash
./rollout.sh --fresh --profile lab_v2
./proxy_gateway/create_admin.sh admin
# externen Worker v0082 mit der frisch erzeugten mTLS-Identität starten
./test_worker_auto_admission.sh fn-01 120
export AUTOMATION_ADMIN_PASSWORD='ein-passwort-mit-mindestens-14-zeichen'
./rollout.sh test-e2e
```

Alle fünf Befunde müssen grün sein: Control-Plane-Stufe 8, Admission,
Node-Revision/Fingerprint, E2E-Terminalzustand und Restore.

## Bekannte Grenze

Die vollständige Online-Scope-Migration ausführbarer Entities
`staged -> READY -> activate -> retire` ist nicht implementiert. Der aktuelle
Code lehnt unsichere Scope-Moves fail-closed ab; dieses Gate bleibt `BLOCKED`.

## Releaseentscheidung

v0014 ist als vollständig gebauter und offline verifizierter
Control-Plane-Release-Candidate freigabefähig. Eine Aussage zur realen
Systemabnahme darf erst nach den oben aufgeführten Zielhost-Gates erfolgen.

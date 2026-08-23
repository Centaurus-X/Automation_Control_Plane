# Release-Validierung Automation Control Plane v0016-r8

Stand: 2026-07-19  
Release: `0.16.0 / v0016-r8`  
E2E-Paket: `0.16.0-e2e.5`

## Statuslegende

- `PASS`: in der lokalen Analyseumgebung mit Exit-Code 0 ausgeführt;
- `FIELD_EVIDENCE_R7`: durch den realen r7-Zielhostlauf belegt;
- `NOT_RUN`: in dieser Umgebung technisch nicht ausführbar;
- `TARGET_GATE_R8`: auf dem Referenz-Teststand zwingend auszuführen.

## Reale r7-Feldevidenz

Der Fresh-Rollout selbst war erfolgreich. Broker, PostgreSQL Primary/Standby,
Master und Proxy liefen gesund. HTTPS/TLS, Direct-WSS, Authentisierung,
Origin-/Pfad-Negativtests, Read-Kommandos, HTTP-Parität, Worker-Admission,
`sensor.value`, Filterwechsel, Replay und Pagination bestanden.

Der Core-E2E war dennoch nicht erfolgreich. Nach `SEC-004 PASSED` fehlten
sämtliche Config-Prüfungen. Der Cleanup endete 90 Sekunden später mit einem
falsch negativen Konvergenzfehler. Parallel zeigte der Proxy eine Sekunde nach
dem Pagination-Stresstest einen `connection.send()`-Timeout.

Die Codekorrelation ergibt vier getrennte Ursachen:

1. `update` wurde auf `nodes.N.description` mit Stringwert angewendet; der
   Master erlaubt `update` korrekt nur für Dictionaries;
2. die Node-Beschreibung änderte zusätzlich den Cross-Shard, obwohl v0082
   keine Cross-Runtime-Fähigkeit meldet;
3. 217 Pagination-Kommandos wurden auf einem nicht gelesenen, weiterhin auf
   Command-Events abonnierten Primärsocket gespiegelt;
4. Restore-Readiness setzte `observed_global_revision`,
   `live_config_revision` und `node_replica_revision` fälschlich gleich und
   verdeckte anschließend die primäre Exception.

Zertifikatskette und WSS-Handshake sind nicht die Ursache: `TLS-001`,
`TLS-002`, `TLS-003`, `WSS-CONTROL`, `WSS-EVENTS` sowie mehrere Live-, Replay-
und Filterprüfungen waren bereits bestanden.

## r8-Korrekturen

- scope-sichere Config-Mutation über `controllers.N.tags` und `set`;
- Auswahl eines aktiven Owner-Controllers, bevorzugt aus den vom Worker
  gemeldeten `controller_uuids`;
- reale Materialisierung vor dem Commit: Node muss sich ändern, Cross muss
  unverändert bleiben;
- permanente Selbstprüfung über `initial_config_v2.json` und die echte
  Master-Funktion `config_authority.apply_changes()`;
- getrennte monotone Mindestwerte für Authority-, lokale Worker- und
  Node-Replica-Revision;
- primäre Exception und Cleanup-Exception bleiben gemeinsam sichtbar;
- Command-Filter wird vor der 217-Event-Pagination vom dauerhaft gehaltenen
  Event-Socket entfernt; der separate Replay-Socket prüft die Pagination
  unverändert vollständig;
- Proxy behandelt einen tatsächlichen Slow-Client-Send-Timeout als kontrollierte
  Trennung mit Close-Code `1013` statt als internen Handler-Crash.

Nicht geändert wurden CA, Zertifikate, TLS-Kontexte, Ports, WSS-Pfade,
MQTT-Protokoll, PostgreSQL-Schema und Worker-v0082-Runtimecode.

## Lokal ausgeführte Prüfungen

| Prüffeld | Status | Ergebnis |
| --- | --- | --- |
| Master Standards | PASS | 85 Python-Dateien |
| Master Unit/Contracts | PASS | 31/31 ausführbare Testprogramme |
| Proxy Standards | PASS | 60 Python-Dateien |
| Proxy Unit/Contracts | PASS | 16/16 ausführbare Testprogramme |
| Slow-Client-WSS-Regression | PASS | Send-Timeout → Close `1013`, Task-Cleanup |
| Config-Engine-Kompatibilität | PASS | reales `initial_config_v2`, `set` akzeptiert |
| Scope-Isolation | PASS | Node-Fingerprint ändert sich, Cross bleibt identisch |
| Legacy-Revisionssemantik | PASS | global `1`, lokal `2`, Replica `1` konvergiert |
| E2E static/self-test | PASS | AST, JSON-Schemata, Real-Config-Selftest, isolierte Gates |
| Direct TLS-WSS | PASS | Control, Events, Origin, Auth, Limits, Shutdown |
| MQTT static | PASS | PKI, ACL, Scripts, idempotente Test-PKI |
| PostgreSQL static | PASS | Deployment-/TLS-/Konfigurationsgates |
| Release-Packaging-Regression | PASS | VCS-, Backup-, Results-, Recovery-, venv-Rejection |
| Validation-Container-Vertrag | PASS | gepinnt, venv-frei, read-only Release-Grenze |
| Lifecycle-/Image-/Upgrade-Gates | PASS | Fresh, Upgrade, Purge, Recovery, Buildreihenfolge |
| Release-Validator | PASS | Revisionen, Labels, Verträge, Hygiene, Paketgrenzen |
| Worker-v0082-Manifest | PASS | vollständiges `SHA256SUMS`, unveränderter ZIP-Hash |

Der vollständige Worker-`36/36`-Lauf und die abschließende ZIP-/Manifest-
Reproduktion werden zusätzlich im finalen Testprotokoll des Gesamtpakets
festgehalten.

## Paket- und Worker-Grenze

Das Control-Plane-Archiv enthält weiterhin keinen Worker-Quellbaum, kein
Worker-Image und keinen Worker-Lifecycle. Das Gesamtpaket führt das separat
korrigierte Worker-v0082-ZIP unverändert mit:

```text
SHA-256 f6bc082cc89deefd570df4718387c09ed38c5184968316dc6a56b94667e071a0
```

Die drei entfernten unreferenzierten Altdateien bleiben:

```text
_config/config_data/device_state_data_v7_cross_fn-01_.json
_config/config_data/device_state_data_v7_node_fn-01_.json
_config/config_data/device_state_data_v7_node_fn-02_.json
```

## Nicht lokal ausführbare r8-Live-Gates

| Gate | Status | Grund |
| --- | --- | --- |
| gepinntes Validation-Image bauen | NOT_RUN | kein Docker-CLI/-Daemon in der Analyseumgebung |
| CPython-3.14.6t-/Master-/Proxy-/PostgreSQL-Images bauen | NOT_RUN | keine Containerumgebung |
| dedizierten Docker-Host purgen | NOT_RUN | kein freigegebener Zielhost |
| zehnstufiger r8-Fresh-Rollout | TARGET_GATE_R8 | Referenzhost erforderlich |
| live MQTT-mTLS/ACL/Persistence | TARGET_GATE_R8 | Brokercontainer erforderlich |
| live PostgreSQL Primary/Standby/TLS | TARGET_GATE_R8 | DB-Container erforderlich |
| Worker `192.168.0.80` installieren/starten | TARGET_GATE_R8 | externes LAN-Ziel |
| fn-01-Admission/Node-Konvergenz | TARGET_GATE_R8 | externer Worker |
| Telemetrie-Fortschrittsgate | TARGET_GATE_R8 | Ziel-DB/Worker |
| r8-Core-E2E gegen `192.168.0.3:5020` | TARGET_GATE_R8 | Worker-/Modbus-Ziel |
| Full-Lab-Cross-Worker | NOT_RUN | externe Proof-/Launcher-Infrastruktur |

## Verbindliche Zielhost-Abnahme

Die Rolloutanleitung verlangt in Reihenfolge:

1. Gesamtpaket-, Control-Plane- und Worker-Prüfsummen;
2. kontrollierten Worker-Stopp und dedizierten Docker-Host-Purge;
3. alle zehn Fresh-Rolloutstufen und OCI-Revision `v0016-r8`;
4. Worker-Test, saubere Neuinstallation, neue MQTT-PKI und Preflight;
5. exakte fn-01-Node-Konvergenz;
6. `./test_worker_sensor_telemetry.sh fn-01 45`;
7. `./run_core_e2e_lab.sh` mit E2E-Version `0.16.0-e2e.5`;
8. `status=PASSED`, vollständige Core-Prüfmenge, bestätigte Config-/Aktor-
   Restores und `retained_stream_command_filter_removed=true`.

## Freigabegrenze

r8 ist lokal tief analysiert sowie statisch und funktional geprüft. Ein echter
r8-Live-PASS wird erst nach der obigen Zielhost-Abnahme behauptet. Der reale
r7-Lauf ist Diagnoseevidenz, aber keine r8-Freigabe.

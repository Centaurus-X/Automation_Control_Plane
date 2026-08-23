# Automation Control Plane v0015 – Releasevalidierung

Stand: 18. Juli 2026  
Release: `0.15.0` / `v0015-r3`

## 1. Urteil

| Ebene | Ergebnis |
|---|---|
| Archiv- und Quellstruktur | `PASSED` |
| Host-Syntax, AST, JSON und Shell | `PASSED` |
| externer Host-venv-Bootstrap, Pfad- und Dependency-Regressionen | `PASSED` |
| Komponenten-Unit-/Contracttests | `PASSED` |
| direkter TLS-WSS-Integrationstest mit `websockets.serve` | `PASSED` |
| E2E-v0015-Selftests und Simulator-/Launcher-Selftests | `PASSED` |
| Auto-Admission-Regression | `PASSED` |
| Checksummen, ZIP-CRC und exakter ZIP-Baum | `PASSED` |
| Docker-/Compose-Build und Container-Runtime | `NOT_RUN` – Docker nicht installiert |
| reales PostgreSQL-/MQTT-/fn-01-/Modbus-Live-E2E | `NOT_RUN` – kein Ziel-Lab |
| `full-lab` Cross-Worker-Simulation | `NOT_RUN` – externe Voraussetzungen fehlen |
| positiver Cross-Site-Test | `NOT_APPLICABLE` – nicht Teil von v0015 |

`NOT_RUN` ist kein fachlicher Fehler des statisch geprüften Artefakts, aber
auch kein Produktionsnachweis. Die Zielhost-Gates der Rolloutanleitung bleiben
verbindlich. Das ZIP ist damit als geprüftes Codeartefakt auslieferbar, jedoch
noch nicht für eine konkrete Produktionsinstanz betrieblich freigegeben.

## 2. Ausgeführte Gates

| Gate | Ergebnis |
|---|---|
| `mqtt_broker/test_static.sh` | PASS |
| `postgres_system/test_static.sh` | PASS |
| `master_node/test.sh` | PASS; Code-Standards für 83 Python-Dateien und alle Master-Contracttests |
| `proxy_gateway/test.sh` | PASS; Code-Standards für 60 Python-Dateien und alle Proxy-Tests |
| `integration_test/e2e_v0015/test_static.sh` | PASS |
| `tools/build_master_v2_fixture.py --check` | PASS |
| `tools/test_release_packaging.py` | PASS |
| `tools/test_host_validation_environment.py` | PASS; kein Netzwerk erforderlich |
| Proxy-Offline-Repairprobe mit lokalem Wheelhouse | PASS unter Host-CPython 3.12; Zielcontainer bleibt `NOT_RUN` |
| `tools/test_lifecycle.sh` | PASS |
| `tools/test_image_staging.sh` | PASS |
| `tools/test_python_runtime_compatibility.sh` | PASS (statische Runtimeverträge) |
| `tools/test_runtime_hotfixes.sh` | PASS |
| `tools/test_upgrade_state.sh` | PASS |
| `tools/test_rollout_modes.sh` | PASS |
| `tools/test_extended_recovery.sh` | PASS |
| `tools/validate_release.sh` im bereinigten Archivbaum | PASS |
| `tools/verify_checksums.sh` | PASS |
| `tools/build_release_archive.py` inklusive ZIP-Selbstprüfung | PASS |

## 3. Proxy-Testabdeckung

Der Proxy-Testlauf enthält unter anderem:

- echten TLS-Handshake und echte Clientverbindung gegen
  `websockets.serve`;
- exakte Pfade, fremde Origin, fehlende Auth und Query-Rejection;
- Control-Command/Accepted/Result-Korrelation und vollständige 23-Action-
  Registry;
- Vorabnormalisierung produktionsnaher PostgreSQL-Skalare im WSS-
  `users.list`-Reply sowie fail-closed `internal_error` ohne 1011-Abbruch;
- keine Ausgabe eines unbekannten Objektwerts oder `repr()` in der sicheren
  JSON-Pfad-/Typdiagnose und erfolgreicher Folgecommand auf demselben Socket;
- Action-, Site- und Resource-RBAC sowie korrektes `DENY`-Audit;
- Session/CSRF und explizit leere API-Token-Scopes;
- atomare lokale Idempotenz und fail-closed Recovery eines verlassenen Claim;
- persistierte Operation-Owner-Bindung für Live- und Replay-Events, wobei die
  MQTT-Produzentenidentität separat erhalten bleibt und fehlende oder
  mehrdeutige Owner fail-closed bleiben;
- Event-RBAC, Default-Deny, Replay-Snapshot, Generation-Fencing und
  stale-event refill sowie vollständiges Unregister bei Task-Cancellation;
- echte TLS-WebSocket-Gates für Open-Retention-Gap (`1013`), Replaypagination,
  langsame Telemetrie, Critical-Overflow, parallelen Filterwechsel und das
  harte Connection-Limit; diese absichtlich isolierten Gates werden nicht als
  Live-Cluster-Ereignisse ausgegeben;
- Same-Key-Coalescing, Telemetrie- und Critical-Overflow ohne stillen Drop;
- MQTT-Erstpublish-Fehler als durable `PENDING`, Retry und exakte ACK-
  Korrelation;
- keine ACK-Auslieferung bei fehlgeschlagener DB-Persistenz;
- Session-Rotation an der absoluten Deadline;
- getrennte WSS-/HTTPS-Readiness, HTTP-only-Uvicorn und Forwarded-Header-
  Policy;
- Prometheus-Metriken ohne Secret-Leak;
- SHA-256-verifiziertes Image-Wheelhouse und ausschließlich indexfreie,
  erneut validierte Proxy-Dependency-Reparatur im Runtime-Volume.

Die Datenbank- und Dispatcherports verwenden in den Hosttests kontrollierte
Fakes. Der Live-Nachweis erfolgt deshalb zusätzlich auf dem Zielsystem.

## 4. E2E-Selftestabdeckung

Der Offline-/Selftest bestätigt:

- versionsgepinntes `websockets==16.0` und echten Client/Server-
  Frameaustausch;
- geschlossene JSON-Schemas und exakt 23 eindeutige Actions;
- keine Klassen, Decorators, Lambdas oder Typannotationen im E2E-Code;
- rekursive Secret-Redaktion;
- TLS-Verifikation ohne `CERT_NONE` oder Insecure-Schalter;
- fn-02-Protokollaktor-Selftest;
- kanonisch signierte Ed25519-Broker-Proofs einschließlich Tamper-,
  Wrong-Signer- und Wrong-Pin-Negativfällen, ohne privaten Schlüssel im
  Releasebaum;
- isolierte Term-/Epoch-, Deduplizierungs-, TTL- und echte TLS-
  Connection-Limit-Gates (`XWK-004` bis `XWK-006`, `PAR-002`);
- externer Worker-Launcher-Selftest mit argv-Ausführung und Archivhash;
- Auto-Admission-Termregression.

Diese Selftests erzeugen ausdrücklich keinen Live-E2E-PASS.

## 5. Reproduzierbarkeit und Hygiene

Der Release-Builder:

1. verwirft `secrets`, `test-pki`, Runtime-, Ergebnis-, Backup-, Cache- und
   Virtualenv-Verzeichnisse sowie jedes eingeschleuste `pyvenv.cfg`;
2. verwirft Private Keys, PEMs, Bytecode und Logs im Archivbaum;
3. erzeugt Komponenten-, E2E- und Root-`SHA256SUMS` neu;
4. validiert den exakten Manifestbaum;
5. schreibt ein ZIP mit festem Timestamp, stabiler Sortierung und erhaltenen
   Unix-Modi;
6. prüft CRC, Pfadtraversal und die exakte Dateimenge nach dem Build.

Der äußere ZIP-SHA-256 ist naturgemäß nicht im ZIP selbst enthalten und wird
beim Build sowie bei der Übergabe separat ausgegeben.

## 6. Verbindliche offene Live-Gates

Vor Produktionsfreigabe müssen auf dem Zielhost mindestens die folgenden
Stufen ausgeführt werden. Die externe Validation-Umgebung wird dabei vor dem
ersten Host-Gate automatisch außerhalb des Releasebaums vorbereitet und
validiert:

```bash
./rollout.sh --fresh --profile lab_v2
./rollout.sh test-running
```

Erst nach diesem Fresh-Rollout werden der dedizierte Lab-Testprincipal, der
externe Worker, die aktuelle CA und die getrennten Live-Log-Captures aus den
Abschnitten 7, 8, 10 und 14 vorbereitet. Solange diese Follower laufen:

```bash
E2E_PROFILE=core E2E_CONFIRM_MUTATIONS=1 ./rollout.sh test-e2e
```

Zusätzlich sind das finale Proxyimage unter CPython 3.14.6, die TLS-/SAN-
Verifikation auf 8443/8444, PostgreSQL-Transaktionen, MQTT-mTLS/ACL,
fn-01-Konvergenz, Modbus-Readback sowie alle Restores zu bestätigen.

`full-lab` darf nur mit fester `E2E_RUN_ID`, frischer autoritativer
Broker-Reservation, einem vorab über einen separaten Vertrauenskanal gepinnten
Ed25519-Oracle-Key-Hash, mTLS-gebundenem Foreign-Site-Publish-/Subscribe-DENY,
lokalem Runner-/Simulatorlock, privater fn-02-PKI, externem
Worker-v0082-Launcher und einem nach dem Stop neu signierten
Broker-Release-Proof ausgeführt werden. Reservation und Release müssen exakt
an Run, Site, Node, Client-ID, Zertifikatsidentität, Reservation-ID, Quelle,
Ablauf und denselben Signer gebunden sein. Cleanup verlangt zusätzlich
MAINTENANCE, einen stale Heartbeat, `online=false`, freigegebene Broker-
Reservation sowie ruhige Queues/Outbox. Fehlt eine Voraussetzung, ist
`BLOCKED` das korrekte Ergebnis.

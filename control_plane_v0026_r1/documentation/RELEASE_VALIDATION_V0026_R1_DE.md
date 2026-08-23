# Release-Validierung - Automation Control Plane v0026-r1

Stand: 15. August 2026

> Dieses Dokument trennt erfolgreich ausgeführte Host-/Paketprüfungen strikt
> von Live-Gates, die Docker, Broker, Datenbank, Netzwerk, Worker oder Hardware
> benötigen. Nicht ausführbare Prüfungen werden nicht als PASS umetikettiert.

## Release-Identität

| Feld | Wert |
|---|---|
| Release-Verzeichnis | `automation_control_plane_release_v0026_r1` |
| Bundle | `0.26.0 / v0026-r1` |
| Master / Proxy | `0.26.0 / v0026-r1` |
| MCP-Komponente | `0.1.0 / v0001-r1` |
| aktiver E2E | `e2e_v0025 / 0.25.0-e2e.3` |
| externer Worker-Vertrag | `automation_system_v0101 / 0.101.0` |
| Erstellungsumgebung | Linux, Python 3.13.5, kein Docker/Podman |

## Verifizierte Eingänge

| Eingang | SHA-256 | Ergebnis |
|---|---|---|
| Control Plane v0025-r1 | `1e4396d25a0d2a896a139d46188d7e1d90c4c5580a5e82ab7a50a5082fb7473d` | Upload-Sidecar PASS |
| MCP Controller v0001-r1 | `16c42b77b9583ced7287a8c3138567f124e63864b9e9afdd962570fd911c202b` | Upload-Sidecar PASS |

Für beide ZIPs bestanden CRC, Pfadnormalisierung, Traversal-/Absolutpfad-Gate,
reguläre Dateitypen, Symlink-, Duplikat- und Verschlüsselungsprüfung. Die
ursprünglichen internen SHA-256-Manifeste bestanden vor der Änderung.

## Additive Bestandserhaltung

- Control Plane: `482/482` Baseline-Dateien im v0026-Baum vorhanden;
- MCP Controller: `84/84` Baseline-Dateien im integrierten Komponentenbaum
  vorhanden;
- keine Ausführungsmodi der Baseline-Dateien verändert;
- historische Release-Dokumente und der qualifizierte E2E-v0025-Vertrag
  unverändert weiter enthalten;
- Änderungen beschränken sich auf Versions-/Integrationsflächen, PKI/ACL,
  Master-Allowlist, Lifecycle, Rollout, Administration, Tests, SBOM und
  aktuelle Dokumentation.

## Ausgeführte statische und funktionale Gates

| Prüfbereich | Ergebnis |
|---|---|
| vollständiger Dateibaum: reguläre Dateien, UTF-8 und Formatparser | PASS |
| 234 Python-Dateien plus 7 eingebettete Python-Heredocs | PASS |
| 0 Klassen, 0 Decorators, 0 Lambdas, 0 `shell=True`, 0 bare `except` | PASS |
| 129 Shellskripte per `bash -n` und Ausführungsmodus | PASS |
| JSON, YAML, TOML, SVG/XML und SBOM-Parsing | PASS |
| Master-Testdateien | `40/40 PASS` |
| Proxy-Testdateien | `17/17 PASS` |
| MCP-Standalone-/Referenzsuite | `20/20 PASS` |
| MCP-Cross-Bundle-Integration | PASS |
| Administration-Orchestrator | `27/27 PASS` |
| Rollout-/Bootstrap-/SSH-/Worker-Installer-Gates | PASS |
| File-Recovery, WAL-Projektion und Core-E2E-Ergebnisprüfung | PASS |
| Lifecycle, Image-Staging, Runtime-Kompatibilität und Upgrade-State | PASS |
| Rollout-Modi und Extended-Recovery | PASS |
| E2E-v0025-Static-Gates | PASS |
| Root-/Komponenten-/E2E-SHA-256-Manifeste | PASS |
| Release-Builder, ZIP-CRC, Pfadsicherheit, Modi und Secret-Ausschluss | PASS |

Die MCP-Integration besitzt ein eigenes bundleweites Gate. Dieses verifiziert
unter anderem die isolierte MQTT-Identität, Master-Allowlist, Allow-/Deny-ACL,
Test-/Produktions-PKI-Verträge, externes Auth-Dokument, Root-Lifecycle,
Administration-Registry und die Reihenfolge:

```text
database -> mqtt -> master -> proxy -> mcp_controller_server -> worker
```

## Worker-Cross-Artifact-Gate

Das separate `automation_system_v0101.zip` und sein Sidecar waren in diesem
Merge-Auftrag nicht vorhanden. Deshalb konnte der gepinnte Worker-ZIP-Vertrag
nicht erneut ausgeführt werden.

Der Release-Builder bleibt standardmäßig fail-closed und fordert beide Dateien.
Das finale Paket darf ohne sie nur mit
`--skip-external-worker-contract` erzeugt werden. Dieser Override wird
seinerseits abgewiesen, sobald Worker-ZIP **oder** Sidecar vorhanden ist. Damit
kann ein verfügbares Artefakt nicht still an der strengen Prüfung vorbeigeführt
werden. Der Worker bleibt für Fresh-/Upgrade-Rollouts zwingend erforderlich.

## Secret- und Sicherheitsgrenzen

- MCP-Bearer-Token nur aus externer `credentials.json`;
- atomare Auth-Datei mit Modus `0600`;
- `CHANGE_ME_*`-Token werden fail-closed abgewiesen;
- private Keys, Zertifikate, Auth-Dateien und `runtime-secrets/` sind nicht im
  ZIP;
- nur `mcp_controller_server/secrets/README.md` bleibt als sichere
  Betriebsdokumentation erhalten;
- MCP besitzt keinen PostgreSQL-Zugang und keinen direkten Worker-Pfad;
- HTTP bindet standardmäßig auf `127.0.0.1:8080`;
- externer HTTP-Zugriff erfordert einen authentifizierten TLS-Reverse-Proxy.

## Nicht ausgeführte Live-Gates

Die Erstellungsumgebung besitzt kein Docker/Podman und keinen realen Broker,
PostgreSQL-Cluster, Worker, Modbus-/S7-Ziel oder externen TLS-Endpunkt. Deshalb
bleiben folgende Punkte offen:

- Docker-Image-Builds und Compose-Start;
- MQTT-mTLS, ACL-Allow/Deny, Delivery und Persistenz live;
- PostgreSQL Primary/Standby, TLS, Rollen, Migration und Replikation live;
- Master-, Proxy- und MCP-Health/Readiness live;
- HTTPS/WSS und MCP-HTTP über den vorgesehenen TLS-Pfad;
- Worker-SSH-Installation, Admission, Heartbeat und Telemetrie;
- Modbus-/S7-/Hardware-Kommunikation;
- vollständiges Core- und File-E2E einschließlich Restart-/Sequenz-Recovery;
- reales Backup/Restore unter persistentem Laufzustand.

## Verbindliches Zielhost-Gate

```bash
./tools/verify_checksums.sh
CONTROL_PLANE_VALIDATION_ACTIVE=/release ./test_static_all.sh
./rollout_automation.sh \
  --release v0026-r1 \
  --archive "$HOME/automation_control_plane_release_v0026_r1.zip" \
  --worker-archive "$HOME/automation_system_v0101.zip" \
  --fresh
./test_running_all.sh
./test_worker_auto_admission.sh fn-01 180
./test_worker_sensor_telemetry.sh fn-01 60
./test_e2e.sh
./test_extended_all.sh
```

Betriebsbereit ist das System erst bei Exit-Code `0`,
`summary.json.status=PASSED`, `summary.json.stage=complete` und vollständig
reproduzierter Live-Abnahme. Das Dateiinventar liegt unter
`documentation/automation_control_plane_v0026_r1_file_inventory.csv`.

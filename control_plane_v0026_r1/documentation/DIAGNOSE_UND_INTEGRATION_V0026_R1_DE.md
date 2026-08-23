# Tiefe Diagnose und Merge-Bericht - v0026-r1

Stand: 15. August 2026

## 1. Auftrag

Zusammenzuführen waren:

1. `automation_control_plane_release_v0025_r1` als bestehendes Bundle mit
   Administration, Orchestration, MQTT, PostgreSQL, Master, Proxy und externem
   Worker-Vertrag;
2. `mcp_controller_server_release_v0001_r1` als später fertiggestellte,
   eigenständige MCP-Komponente.

Ziel war eine additive Integration ohne Verlust vorhandener Features oder
Authority-/Safety-Invarianten.

## 2. Verifizierte Quellen

| Quelle | ZIP-Einträge | Dateien | SHA-256 / Sidecar |
|---|---:|---:|---|
| Control Plane v0025-r1 | 545 | 482 | `1e4396d25a0d2a896a139d46188d7e1d90c4c5580a5e82ab7a50a5082fb7473d` - PASS |
| MCP Controller v0001-r1 | 84 | 84 | `16c42b77b9583ced7287a8c3138567f124e63864b9e9afdd962570fd911c202b` - PASS |

Beide Archive wurden vor der Extraktion auf CRC, Pfad-Traversal, absolute
Pfade, Backslashes, Duplikate, Symlinks, fremde Dateitypen und Verschlüsselung
geprüft. Die internen Manifeste der Control Plane und die MCP-Release-Struktur
waren gültig.

### 2.1 Nachweis der additiven Bestandserhaltung

Ein Pfad-für-Pfad-Vergleich der extrahierten Eingänge gegen den v0026-Baum
bestätigt:

- `482/482` Dateien der Control-Plane-v0025-Baseline vorhanden;
- `84/84` Dateien des MCP-v0001-Standalone-Releases vorhanden;
- `0` fehlende Baseline-Dateien;
- `0` unbeabsichtigte Änderungen an Unix-Ausführungsmodi.

Byteänderungen betreffen ausschließlich die für v0026 erforderlichen
Integrationsflächen und aktuelle Dokumentation. Historische Dokumente sowie der
aktive, qualifizierte `e2e_v0025`-Vertrag bleiben erhalten. Der sichere
Standalone-Hinweis `mcp_controller_server/secrets/README.md` bleibt paketiert;
reales Secret-Material unter diesem Pfad und `runtime-secrets/` wird weiterhin
fail-closed vom Release-Builder ausgeschlossen.

## 3. Ausgangsdiagnose

### 3.1 Control Plane

Der vorhandene Stack war funktional und fail-closed, aber die
Administration definierte eine starre Fünfer-Reihenfolge:

```text
database -> mqtt -> master -> proxy -> worker
```

Root-Lifecycle, Deployments, Image-Staging, Status, Logs, Stop, Testmatrix,
Release-Builder, SBOM und Dokumentation kannten keine zusätzliche Komponente.

### 3.2 MCP Controller

Die MCP-Komponente war für den Standalone-Betrieb vollständig implementiert:

- Streamable HTTP und stdio;
- Bearer/RBAC/Site-Scopes;
- MQTT-mTLS und `automation.control.v1`;
- read-only Tools und Prepare-Approve-Commit;
- lokale SQLite-Idempotenz und append-only Audit;
- Lifecycle, Backup, Rollback, Health und 20 dependency-freie Tests.

Die Standalone-Dokumentation beschrieb deshalb noch eine v0025-Lücke und eine
optionale Kompatibilitätsidentität `proxy-01`. Diese Aussagen waren für das neue
Bundle nicht ausreichend.

## 4. Merge-Entscheidungen

### 4.1 Eigenständige Komponente statt Quellcode-Vermischung

Der komplette MCP-Quellbaum wurde unter `mcp_controller_server/` erhalten. Es
gibt keine Imports in Master, Proxy oder Worker. Die Integration erfolgt über
bestehende Verträge, Deployment- und Orchestration-Hooks.

### 4.2 Dedizierte MQTT-Identität

Die gemeinsame Identität `proxy-01` wurde für das Bundle verworfen. Verwendet
wird ausschließlich:

```text
urn:automation:site-main:proxy:mcp-controller-01
```

Damit sind Zertifikat, Revocation, ACL, ACK-Pfad und Audit eindeutig. Die
Master-Allowlist und Broker-ACL wurden additiv erweitert.

### 4.3 Sechste Orchestrator-Komponente

Die zentrale Reihenfolge lautet jetzt:

```text
database -> mqtt -> master -> proxy -> mcp_controller_server -> worker
```

Inventory, Registry, Release-Manifest, Planvalidierung, Operation-Steps,
Rollback-Reihenfolge, Tests und Dokumentation wurden konsistent erweitert.

### 4.4 Externe Auth-Datei

Ein MCP-Token darf nicht in Compose, Releasebaum oder Logs liegen. Deshalb
wurde ein eigener atomarer Auth-Generator ergänzt und in den autonomen Rollout
eingebunden. Der Rollout lehnt Auth-Ziele innerhalb des Releasebaums ab.

### 4.5 Versionsmodell

Bundle, Master und Proxy wurden auf `0.26.0 / v0026-r1` angehoben. Der MCP
Controller bleibt `0.1.0 / v0001-r1`; seine Metadaten dokumentieren die
Bundle-Bindung. MQTT, PostgreSQL, Worker und der qualifizierte E2E-Vertrag
bleiben unverändert.

## 5. Geänderte Integrationsflächen

- Root-Version, Release-Revision, OCI-Labels und SBOM;
- Master-Allowlist;
- MQTT-ACL, Test-PKI, PKI-Verification und Live-Handoff;
- Root-Lifecycle, Lab-/Production-Deployment, Status, Logs und Stop;
- statische, laufende und erweiterte Testketten;
- Rollout-Konfiguration, Credentials, CLI und externe Auth-Erzeugung;
- Administration-Orchestrator, Komponentenadapter, Inventory, Registry und
  Release-Manifest;
- Release-Builder, Transient-/Secret-Filter und Manifestprüfung;
- Root-, Master-, Proxy-, Administration-, E2E- und MCP-Dokumentation.

## 6. Erhaltene Invarianten

- Worker bleibt finale Authority für Node-Konfiguration und Hardware.
- Master führt keine SEM-, Safety-, Rule- oder Feldbuslogik aus.
- MCP besitzt keinen DB- oder Worker-Direktzugriff.
- `config_scope_operations_v1` wird unverändert transportiert.
- Authority-Epoch, Scope-Revision, Execution-Epoch und Fingerprint bleiben die
  Konsistenzgrenze.
- Worker-ZIP bleibt extern und exakt gepinnt.
- Datenbank wird bei Rollback nicht blind restored.
- bestehende Canary-/Batch-, Lock-, Fencing-, Audit-, Evidence-, Resume- und
  Rollback-Mechanismen bleiben aktiv.

## 7. Zusätzliche Sicherheitsdiagnose

- HTTP-Default bleibt Loopback;
- MQTT-TLS ist verpflichtend, unsicherer Transport fail-closed;
- MCP-ACL besitzt keine Worker-/Broadcast-Schreibrechte;
- Runtime-Container ist read-only, ohne Capabilities und mit
  `no-new-privileges`;
- Auth-Token wird auf 32 bis 4096 druckbare Nicht-Leerzeichen begrenzt und `CHANGE_ME_*` wird fail-closed abgewiesen;
- Auth-/PKI-Dateien werden nur extern gelesen und transient als `0400`
  bereitgestellt;
- Release-Packaging verwirft `runtime-secrets`, `.test-tmp`, private Keys,
  lokale Credentials, Logs, VCS- und Runtime-Verzeichnisse;
- Cross-Komponenten-Test prüft Master, ACL, Orchestrator, Lifecycle und
  Secret-Hygiene gemeinsam.

## 8. Teststatus

Die finalen, tatsächlich ausgeführten Tests, Laufzeiten und Umgebungsgrenzen
stehen in `documentation/RELEASE_VALIDATION_V0026_R1_DE.md`. Unabhängig davon
bleiben auf dem Zielsystem zwingend: Docker-Build, Compose-Start, MQTT-mTLS,
PostgreSQL Primary/Standby, HTTPS/WSS, MCP-Readiness, Worker-SSH,
Worker-Admission, Telemetrie, Modbus und vollständiges Core-/File-E2E.

## 9. Ergebnis

Das Merging ist technisch konsistent möglich und in v0026 additiv umgesetzt.
Der MCP Controller ist kein loses Sidecar mehr, sondern eine releasegebundene,
administrierbare und orchestrierbare Komponente. Die bestehende Authority- und
Safety-Architektur wird nicht aufgeweicht.

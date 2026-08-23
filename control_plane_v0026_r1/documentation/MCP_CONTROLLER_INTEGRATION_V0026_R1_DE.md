# MCP-Controller-Integration - Automation Control Plane v0026-r1

## 1. Ziel und Systemgrenze

Der `mcp_controller_server 0.1.0 / v0001-r1` ist in v0026 eine eigene
Control-Plane-Komponente. Er stellt MCP über Streamable HTTP und stdio bereit,
kommuniziert mit der Plattform aber ausschließlich über den bestehenden
`automation.control.v1`-Vertrag auf MQTTS.

```text
MCP Client
  -> Bearer / Principal / Site-RBAC
  -> mcp_controller_server
  -> MQTTS mit eigener Identität
  -> Master Registry / Relay / Mirror / Audit
  -> owning Worker / SEM / Safety / Authority
  -> Hardware
```

Nicht zulässig sind:

- direkter PostgreSQL-Zugriff;
- direkte Worker-Verbindungen;
- frei wählbare MQTT-Topics;
- Shell-/Terminalzugriff;
- Umgehung von SEM, Safety oder `node_local`-Authority.

Die lokale SQLite-Datei des MCP-Servers speichert ausschließlich Approval,
Idempotenz und Commitzustand. Das append-only Auditlog ist ebenfalls lokaler
Komponentenzustand und keine Plattform-Datenbank.

## 2. Identität und Transport

### 2.1 Dedizierte Identität

```text
service:     proxy
instance_id: mcp-controller-01
certificate: urn:automation:site-main:proxy:mcp-controller-01
```

`service=proxy` bleibt wegen des bestehenden, versionsstabilen
Control-Envelopes unverändert. Die eigene `instance_id` trennt die Komponente
vom normalen Proxy `proxy-01`.

### 2.2 Broker-ACL

Der MCP-Principal darf:

```text
WRITE automation/v1/site-main/master/master-01/commands/#
READ  automation/v1/site-main/proxy/mcp-controller-01/acks/#
READ  automation/v1/site-main/events/+/master-01
```

Er darf insbesondere nicht auf Worker-Command-, Worker-ACK- oder
Broadcast-Command-Topics schreiben. Der Master besitzt zusätzlich die
Schreibberechtigung für den eigenen MCP-ACK-Pfad.

Implementierung:

- `mqtt_broker/config/acl.conf`;
- `mqtt_broker/scripts/generate_test_pki.sh`;
- `mqtt_broker/scripts/verify_pki.sh`;
- `mqtt_broker/scripts/handoff_live_pki.sh`;
- `mqtt_broker/test_static.sh` und `mqtt_broker/test.sh`.

### 2.3 Master-Allowlist

`master_node/master_node/_config/application/master_config.json` enthält sowohl
`proxy-01` als auch `mcp-controller-01`. Die bestehende fail-closed Prüfung im
Master bleibt unverändert; nur die zweite erlaubte Instanz wird additiv
konfiguriert.

## 3. Authentifizierung und Secret-Lifecycle

### 3.1 Credential-Quelle

```text
credentials.json:
  mcp_controller.local_agent.token
```

`tools/prepare_mcp_auth.py` liest den Wert über den bestehenden
`credential_reader`, validiert Länge und Zeichenklasse und schreibt atomar:

```text
$HOME/.config/automation-control-plane/mcp_auth.json
```

Eigenschaften:

- Zielverzeichnis `0700`;
- Auth-Datei `0600`;
- atomarer `os.replace`;
- `fsync` vor Veröffentlichung;
- keine Token-Ausgabe;
- keine Ausgabe in `summary.json`, Audit oder Evidence;
- Pfad innerhalb des Releasebaums wird vom Rollout abgewiesen.

### 3.2 Container-Secrets

Die vier externen Quellen sind:

```text
MCP_MQTT_CA_SOURCE
MCP_MQTT_CERT_SOURCE
MCP_MQTT_KEY_SOURCE
MCP_AUTH_SOURCE
```

`mcp_controller_server/tools/prepare_runtime_secrets.py` kopiert sie mit einem
isolierten Docker-Helfer nach `runtime-secrets/`, setzt UID/GID `10001` und
Modus `0400`. Das Verzeichnis ist transient, wird vom Release-Builder verworfen
und darf im finalen ZIP nicht existieren.

## 4. Deployment

Root-Lifecycle und Komponentenskripte unterstützen:

```text
preflight  backup  apply  ready
status     test    restart rollback
logs       stop
```

Wichtige Dateien:

- `mcp_controller_server/compose.yaml`;
- `mcp_controller_server/scripts/*.sh`;
- Root-Wrapper `mcp_controller_server/*.sh`;
- `scripts/control_plane_lifecycle.sh`;
- `deploy_test_lab.sh` und `deploy_production.sh`;
- `status_all.sh`, `logs_all.sh`, `stop_all.sh`;
- `test_static_all.sh`, `test_running_all.sh`, `test_extended_all.sh`.

Der Container ist read-only, besitzt `no-new-privileges`, verwirft alle
Capabilities, nutzt ein begrenztes `/tmp` und bindet standardmäßig nur an
`127.0.0.1:8080`.

## 5. Administration und Orchestration

### 5.1 Komponentenreihenfolge

```text
database -> mqtt -> master -> proxy -> mcp_controller_server -> worker
```

Diese Reihenfolge ist konsistent in:

- `administration/orchestrate.py`;
- `administration/component_adapter.py`;
- `administration/component_registry.test-lab.json`;
- `administration/component_registry.example.json`;
- `administration/release_manifest.json`;
- `administration/release_manifest.example.json`.

Der Worker hängt damit weiterhin vom vollständigen Control-Plane-Stack ab; der
MCP Controller wird nach den vorhandenen öffentlichen und internen
Control-Komponenten gestartet.

### 5.2 Adapterabbildung

| Operation | MCP-Umsetzung |
|---|---|
| `preflight` | Pfade, Profil, Port, Site, Zertifikatsquellen und Auth prüfen |
| `backup` | SQLite Online-Backup und vollständige Audit-Zeilen sichern |
| `apply` | Runtime-Secrets stage, Image bauen/verwenden, Compose convergieren |
| `ready` | HTTP-Readiness; standardmäßig MQTT-Verbindung verpflichtend |
| `status` | Compose- und Anwendungssnapshot |
| `test` | MCP-Vertrag, Auth, MQTT-Grenze und Runtime-Probes |
| `restart` | kontrollierter Compose-Restart mit operation-id-Fence |
| `rollback` | nur auf explizit gepinntes vorheriges Image |

Die vorhandenen Mechanismen bleiben erhalten: Plan-ID aus SHA-256,
Environment-/Target-Locks, monotones Fencing, dauerhafte Idempotency-Keys,
atomare Checkpoints, append-only Audit, Evidence, Redaction, `--resume` und
Reverse-Order-Rollback.

### 5.3 Inventory- und Manifest-Bindung

Der Control-Plane-Target `control-plane-01` enthält die Komponente
`mcp_controller_server`. Die Config-Bindung
`mcp-controller-config-v1` verweist auf
`mcp_controller_server/config/controller_config.json`.

Das Release-Manifest pinnt zusätzlich:

- `mcp_controller_server/RELEASE_METADATA.json`;
- `mcp_controller_server/SHA256SUMS`;
- `mcp_controller_server/test_all.sh`;
- die Tests `mcp-static-contract`, `mcp-mqtt-boundary` und `mcp-readiness`.

## 6. Rollout-Automation

`rollout_automation.sh` erweitert die vorhandene Logik additiv um:

- Konfigurationswerte für Bind-Adresse, Port, Source-Instance, Auth-Pfad,
  Credential-Key, Client-ID und Subject;
- CLI-Option `--mcp-auth-file`;
- externe Auth-Erzeugung vor jeder Mutation;
- Export der MCP-Umgebung an den Root-Lifecycle;
- Test-PKI mit dediziertem MCP-Zertifikat;
- MCP-Deployment nach Proxy;
- Readiness und Image-/Revision-Prüfung in der Gesamtabnahme.

Fresh, Reuse und Upgrade behalten ihre bisherigen Worker- und
Persistenzsemantiken.

## 7. Monitoring und Abnahme

MCP stellt bereit:

```text
GET /health/live
GET /health/ready
POST /mcp
```

`ready` ist fail-closed und verlangt standardmäßig eine aktive MQTT-Verbindung.
`test_running_all.sh` prüft Image `automation-mcp-controller-server:0.1.0`, OCI
Revision `v0001-r1`, Container-Health und Anwendung-Readiness.

Statische Cross-Komponenten-Prüfung:

```bash
python3 tools/test_mcp_bundle_integration.py
```

Sie validiert Versionen, Metadata-Bindung, Master-Allowlist, ACL, Compose-
Hardening, Orchestrator-Reihenfolge, Rollout-/Lifecycle-Hooks und Secret-Hygiene.

## 8. Rollback und Recovery

MCP ist reversibel, aber ein Rollback ist nur zulässig, wenn
`MCP_CONTROLLER_ROLLBACK_IMAGE` auf ein vorhandenes, unveränderliches Image
zeigt. Vorher wird der lokale Approval-/Audit-Zustand gesichert. Ein
Datenbank-Rollback der Plattform bleibt weiterhin `manual_recovery`; der MCP-
Rollback verändert keine PostgreSQL-Daten und keine Worker-Konfiguration.

## 9. Produktionsbedingungen

Vor Produktionsfreigabe zusätzlich erforderlich:

- eigenes Produktionszertifikat und Revocation-Prozess für MCP;
- Token-Rotation und Secret-Store statt statischer Hostdatei;
- TLS-Reverse-Proxy oder vollständig internes Loopback-Modell;
- zentrale Log-/Alert-Anbindung ohne Secret-Leakage;
- getestetes Backup/Restore des MCP-State-Volumes;
- reproduzierter Live-MQTT-, Master-, Worker- und Hardware-E2E-Lauf.

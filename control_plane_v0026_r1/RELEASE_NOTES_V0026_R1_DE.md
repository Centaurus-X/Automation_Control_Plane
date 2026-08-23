# Release Notes - Automation Control Plane 0.26.0 / v0026-r1

## Zweck

`v0026-r1` führt das bisher separate
`mcp_controller_server 0.1.0 / v0001-r1` additiv mit der Automation Control
Plane `v0025-r1` zusammen. Die MCP-Komponente bleibt ein eigenständiger
Service, wird aber vollständig durch Release, Deployment, Administration,
Orchestration, Monitoring, Tests und Dokumentation verwaltet.

## Eingangsartefakte

| Artefakt | verifizierter SHA-256 |
|---|---|
| `automation_control_plane_release_v0025_r1.zip` | `1e4396d25a0d2a896a139d46188d7e1d90c4c5580a5e82ab7a50a5082fb7473d` |
| `mcp_controller_server_release_v0001_r1.zip` | `16c42b77b9583ced7287a8c3138567f124e63864b9e9afdd962570fd911c202b` |

Beide Upload-Sidecars wurden vor der Extraktion erfolgreich geprüft. Die
Archive enthielten keine Traversalpfade, absoluten Pfade, Symlinks,
Duplikate oder verschlüsselten Einträge.

## Versionsstand

| Komponente | Version / Revision |
|---|---|
| Bundle | `0.26.0 / v0026-r1` |
| Master | `0.26.0 / v0026-r1` |
| Proxy | `0.26.0 / v0026-r1` |
| MCP Controller Server | `0.1.0 / v0001-r1` |
| MQTT / PostgreSQL | `0.14.0` |
| aktiver E2E-Vertrag | `e2e_v0025 / 0.25.0-e2e.3` |
| externer Worker | `automation_system_v0101 / 0.101.0` |

Die MCP-Komponente wird nicht künstlich auf `0.26.0` umnummeriert. Ihre eigene
SemVer- und Revisionsidentität bleibt erhalten; Metadaten binden sie zusätzlich
an das v0026-Bundle.

## Neue Integration

### Release und Deployment

- `mcp_controller_server/` ist Bestandteil des Control-Plane-Archivs.
- Root-Lifecycle, Fresh-/Reuse-/Upgrade-Rollout, Status, Logs, Stop, statische
  Tests, laufende Tests und Extended Tests berücksichtigen die Komponente.
- Docker-Image und OCI-Labels bleiben `0.1.0 / v0001-r1`.
- HTTP bindet standardmäßig an `127.0.0.1:8080`.
- Runtime-Secrets werden außerhalb des unveränderlichen Releasebaums erzeugt
  und nicht paketiert.

### MQTT und Master

- dedizierte mTLS-Identität:
  `urn:automation:site-main:proxy:mcp-controller-01`;
- eigene Broker-ACL für Master-Control-Kommandos, eigenen ACK-Pfad und
  Master-Events;
- explizite Verweigerung von Worker-/Broadcast-Kommandotopics;
- Master-Allowlist um `mcp-controller-01` erweitert;
- Test-PKI, PKI-Handoff und Live-PKI-Prüfung um die MCP-Identität ergänzt.

### Administration und Orchestration

Die feste Reihenfolge wurde kontrolliert erweitert:

```text
database -> mqtt -> master -> proxy -> mcp_controller_server -> worker
```

Der bestehende Planvertrag bleibt unverändert: immutable kanonische JSON-Pläne,
SHA-256-Plan-ID, Locks, monotones Fencing, Checkpoints, Audit, Evidence, Resume,
Canary/Batch und Reverse-Order-Rollback. Der zentrale Adapter bildet für MCP
`preflight`, `backup`, `apply`, `ready`, `status`, `test`, `restart` und
`rollback` ab.

### Authentifizierung

`tools/prepare_mcp_auth.py` erzeugt aus dem geschützten Credential-Key
`mcp_controller.local_agent.token` atomar ein externes `0600`-Auth-Dokument.
Das Token muss 32 bis 4096 Zeichen lang sein, darf keine Leer-/Kontrollzeichen
enthalten, darf nicht mit `CHANGE_ME_` beginnen und wird nie in Logs oder
Evidence ausgegeben.

## Kompatibilität und unveränderte Invarianten

- Worker bleibt finale Node-Authority im Modus `node_local`.
- Master bleibt Registry/Relay/Mirror/Audit und führt keine Hardwarelogik aus.
- MCP besitzt keinen PostgreSQL-Zugang und keinen direkten Worker-Pfad.
- `config_scope_operations_v1`, Authority-Epoch, Scope-Revision,
  Execution-Epoch und Fingerprint bleiben unverändert.
- Worker v0101 bleibt ein separates, externes Release-Artefakt.
- Der qualifizierte E2E-Vertrag `e2e_v0025` bleibt aktiv.
- Historische Dokumente und frühere Release Notes bleiben erhalten.

## Upgrade-Hinweise

1. neue v0026-ZIP- und Worker-v0101-Sidecars prüfen;
2. `rollout_automation.json` um MCP-Bind-/Port-/Identitätswerte ergänzen;
3. in `credentials.json` ein starkes MCP-Bearer-Token setzen;
4. Produktions-PKI um das dedizierte MCP-Zertifikat ergänzen;
5. Port `8080` nur lokal binden oder durch einen authentifizierten
   TLS-Reverse-Proxy veröffentlichen;
6. Fresh oder kontrolliertes Upgrade ausführen;
7. MCP-Readiness, MQTT-Grenze und vollständige bestehende E2E-Abnahme
   reproduzieren.

## Verifikation

Die genaue, reproduzierbare Testmatrix und die in der Erstellungsumgebung nicht
verfügbaren Live-Gates stehen in
`documentation/RELEASE_VALIDATION_V0026_R1_DE.md`.

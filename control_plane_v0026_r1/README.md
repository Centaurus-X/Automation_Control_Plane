# Automation Control Plane 0.26.0 / v0026-r1

Eigenständiges, reproduzierbares Control-Plane-Release für Authority V5 mit
vollständig integrierter MCP-Controller-Komponente. Der Worker bleibt bewusst
ein separates, exakt gepinntes Archiv `automation_system_v0101`; Worker-Code
wird nicht in das Control-Plane-ZIP kopiert.

## Releasegrenzen

| Bestandteil | Version / Revision |
|---|---|
| Control Plane | `0.26.0 / v0026-r1` |
| Master | `0.26.0 / v0026-r1` |
| Proxy | `0.26.0 / v0026-r1` |
| MCP Controller Server | `0.1.0 / v0001-r1` |
| MQTT Broker | `0.14.0` |
| PostgreSQL | `0.14.0` |
| qualifizierter E2E-Vertrag | `e2e_v0025 / 0.25.0-e2e.3` |
| externer Worker | `automation_system_v0101 / 0.101.0` |

Die MCP-Komponente behält ihre eigene Komponenten-Version. Ihre
`RELEASE_METADATA.json` bindet sie zusätzlich eindeutig an das Bundle
`0.26.0 / v0026-r1`.

## Architektur und Authority

```text
Normale Clients -> Proxy -> MQTT -> Master -> owning Worker -> Hardware
MCP Clients     -> MCP Controller Server -> MQTTS -> Master -> owning Worker
                                      \-> lokaler Approval-/Audit-Zustand

Dauerhafte Plattformdaten: PostgreSQL
Finale Node-Authority:      Worker im Modus node_local
```

| Verantwortung | Besitzer |
|---|---|
| Node-Config, SEM/Safety, Scope-Revision und Hardware | Worker |
| Registry, Relay, Mirror und Audit | Master |
| Benutzer-Authentifizierung, RBAC, HTTPS/WSS | Proxy |
| MCP-Protokoll, Bearer/RBAC, Prepare-Approve-Commit | MCP Controller Server |
| dauerhafte Control-Plane-Persistenz | PostgreSQL |
| mTLS/QoS-1-Transport | Mosquitto |

Der MCP Controller besitzt **keinen** Datenbankzugang und spricht **keinen**
Worker direkt an. Er verwendet ausschließlich `automation.control.v1` über
MQTTS. Der Master akzeptiert dafür die dedizierte logische Quellinstanz
`mcp-controller-01`; die Broker-Identität lautet:

```text
urn:automation:site-main:proxy:mcp-controller-01
```

`source.service=proxy` bleibt dabei ein unveränderter Bestandteil des bestehenden
Control-Vertrags. Die dedizierte `source.instance_id` trennt ACL, Zertifikat,
Revocation, ACK-Pfad und Audit-Zuordnung vom normalen Proxy `proxy-01`.

## Kanonischer Proposal-Fluss

Alle verteilten Config-Proposals verwenden weiterhin exakt
`config_scope_operations_v1`. Proxy, MCP Controller und Master dürfen die
Operationsliste nicht semantisch umschreiben. Nur der owning Worker prüft SEM,
Safety und Authority und vergibt die nächste Scope-Revision.

Contract Authority `2.0` und Type Schema `2.0.0` bleiben byte-identisch in
Worker, Master und Proxy. Node-Speicherung bleibt V9, Cross V8; V5 ist nur ein
expliziter Importpfad.

## Administration und Orchestration

Die verbindliche Komponentenreihenfolge ist:

```text
database -> mqtt -> master -> proxy -> mcp_controller_server -> worker
```

`administration/orchestrate.py` und `administration/component_adapter.py`
behandeln den MCP Controller als vollwertige, reversible Komponente. Unterstützt
werden `preflight`, `backup`, `apply`, `ready`, `status`, `test`, `restart` und
`rollback`; immutable Pläne, SHA-256-Bindung, Locks, Fencing, Checkpoints,
Evidence, Resume und Reverse-Order-Rollback bleiben unverändert erhalten.

Der autonome Vollinstaller `rollout_automation.sh` erzeugt das externe
MCP-Auth-Dokument aus geschützten Credentials, stellt die dedizierte Test-PKI
bereit, deployt den MCP Controller nach Proxy und vor Worker und nimmt seine
Readiness in die Gesamtabnahme auf.

## Verzeichnisstruktur

```text
administration/          immutable Pläne und operativer Komponentenadapter
integration_test/        qualifizierter e2e_v0025-Vertrag
master_node/             Registry, Relay, Mirror und Audit
mcp_controller_server/   eigenständiger MCP-Server 0.1.0/v0001-r1
mqtt_broker/             mTLS-Broker, ACL und Test-PKI
postgres_system/         Datenbank, TLS, Replikation und Backup
proxy_gateway/           Auth, RBAC, HTTPS/WSS und Client-API
runtime/                 Validation-Container und Python-Runtime
tools/                   statische, Release-, Integritäts- und Archiv-Gates
```

## Externe Konfiguration und Secrets

Standardpfade:

```text
$HOME/.config/automation-control-plane/rollout_automation.json
$HOME/.config/automation-control-plane/credentials.json
$HOME/.config/automation-control-plane/mcp_auth.json
```

Das MCP-Bearer-Token liegt nur in `credentials.json`. Der Installer erzeugt
daraus atomar eine Datei mit Modus `0600`; Platzhalter mit `CHANGE_ME_`
werden fail-closed abgewiesen. Token, private Keys, Zertifikate und
`runtime-secrets/` werden nicht in das Release-Archiv aufgenommen. Nur
`mcp_controller_server/secrets/README.md` bleibt als sichere
Betriebsdokumentation paketiert. HTTP bindet
standardmäßig nur an `127.0.0.1:8080`. Externer Zugriff benötigt einen
authentifizierten TLS-Reverse-Proxy.

## Validierung

Auf einem Host mit der qualifizierten Validation-Runtime:

```sh
CONTROL_PLANE_VALIDATION_ACTIVE=/release sh test_static_all.sh
sh tools/verify_checksums.sh
```

Ein vollständiger Zielhost-Rollout muss zusätzlich Docker-/Compose-Builds,
MQTT-mTLS, PostgreSQL Primary/Standby, Master, Proxy, MCP-Readiness, Worker-SSH,
Worker-Admission, Telemetrie sowie Core-/File-E2E reproduzieren.

Der Release-Builder bleibt standardmäßig streng und verlangt das benachbarte
`automation_system_v0101.zip` samt Sidecar, um den vollständigen
Cross-Artifact-Vertrag zu prüfen.

## Fresh-Rollout

```sh
sh ./rollout_automation.sh \
  --release v0026-r1 \
  --archive "$HOME/automation_control_plane_release_v0026_r1.zip" \
  --worker-archive "$HOME/automation_system_v0101.zip" \
  --fresh
```

Auf einem ausschließlich dafür vorgesehenen Laborhost kann zusätzlich
`--purge-host` verwendet werden. Auf gemeinsam genutzten Hosts ist diese Option
verboten.

## Aktuelle Dokumentation

- `RELEASE_NOTES_V0026_R1_DE.md`
- `ROLLOUT_TEST_ANLEITUNG_AUTOMATION_CONTROL_PLANE_V0026_R1_DE.md`
- `documentation/MCP_CONTROLLER_INTEGRATION_V0026_R1_DE.md`
- `documentation/DIAGNOSE_UND_INTEGRATION_V0026_R1_DE.md`
- `documentation/RELEASE_VALIDATION_V0026_R1_DE.md`
- `ROLLOUT_AUTOMATION_ANLEITUNG_DE.md`

Dokumente früherer Releases bleiben als unveränderlicher Audit- und
Migrationsnachweis erhalten.

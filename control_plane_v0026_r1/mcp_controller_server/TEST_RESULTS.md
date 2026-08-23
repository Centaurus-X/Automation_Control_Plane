# Testprotokoll - MCP Controller Server und v0026-Integration

Stand: 2026-08-15  
Komponente: `mcp_controller_server`  
Komponentenversion: `0.1.0 / v0001-r1`  
Integrationsbundle: `0.26.0 / v0026-r1`

## Verifizierte Merge-Eingänge

| Artefakt | SHA-256 | Ergebnis |
|---|---|---|
| Control Plane v0025-r1 | `1e4396d25a0d2a896a139d46188d7e1d90c4c5580a5e82ab7a50a5082fb7473d` | Sidecar, CRC und sichere ZIP-Struktur PASS |
| MCP Controller v0001-r1 | `16c42b77b9583ced7287a8c3138567f124e63864b9e9afdd962570fd911c202b` | Sidecar, CRC und sichere ZIP-Struktur PASS |

Alle 84 Dateien des Standalone-MCP-Releases bleiben im integrierten
Komponentenbaum vorhanden. Der Fachkern unter `src/`, die Protokollverträge und
die ursprünglichen Tests wurden nicht durch einen alternativen Stack ersetzt;
Deployment-, PKI-, ACL-, Orchestrierungs- und Dokumentationsflächen wurden
additiv erweitert.

## Standalone-Komponentengates

- Functional-Python-AST-Gate: PASS;
- Release-Struktur und MQTT-only-Plattformgrenze: PASS;
- Konfiguration und fail-closed Defaults: PASS;
- 20 dependency-freie funktionale Tests: PASS;
- Referenzvergleich gegen Master-Topics, Envelope, Payloads und den
  byteidentischen Lightweight-MQTT-Client: PASS;
- ASGI HTTP, MCP-Metadaten, Header-Bindung, Origin und Bearer: PASS;
- Legacy-MCP-Initialize/stdio: PASS;
- MQTT-ACK-/Identity-/Retain-/Topic-Prüfungen: PASS;
- externe Freigabe, Digest, Idempotenz, Concurrent-Commit-Fence und
  Crash-Retry: PASS;
- Audit-Symlinkabwehr und fail-closed Mutation bei Audit-Ausfall: PASS;
- Online-SQLite-Backup, Audit-Complete-Lines und private Dateimodi: PASS;
- Secret-Permissions und isolierter Non-root-Staging-Vertrag: PASS;
- Shell-Syntax und Compose-YAML: PASS;
- Python-Wheel-Build ohne Netzwerk/Dependencies sowie Wheel-CRC: PASS.

## Cross-Bundle-Gates v0026-r1

- dedizierte Brokeridentität
  `urn:automation:site-main:proxy:mcp-controller-01`: PASS;
- Master-Allowlist für `mcp-controller-01`: PASS;
- Broker-ACL nur für Master-Control, eigenen ACK-Pfad und Master-Events;
  Worker-/Broadcast-Kommandotopics verweigert: PASS;
- Test-PKI, Produktions-PKI-Handoff und Zertifikatsidentität: PASS;
- Root-Lifecycle für Deploy, Ready, Status, Logs, Test, Restart, Backup,
  Rollback und Stop: PASS;
- externe Auth-Datei, Modus `0600`, Token-Redaction und Verbot von
  `CHANGE_ME_*`: PASS;
- Komponentenreihenfolge
  `database -> mqtt -> master -> proxy -> mcp_controller_server -> worker`:
  PASS;
- Component-Adapter mit `preflight`, `backup`, `apply`, `ready`, `status`,
  `test`, `restart` und `rollback`: PASS;
- Administration-Orchestrator 27/27 Szenarien einschließlich MCP: PASS;
- bundleweites Integrationsgate `tools/test_mcp_bundle_integration.py`: PASS;
- Secret-Hygiene: nur `secrets/README.md` wird dokumentarisch paketiert;
  Auth, Schlüssel, Zertifikate und `runtime-secrets/` bleiben ausgeschlossen:
  PASS.

## Historische Standalone-Evidenz

Das ursprüngliche MCP-Release dokumentiert einen separaten Test gegen
`automation_system_v0101` mit Worker-SHA
`c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467`.
Dieses Ergebnis bleibt Provenienz des Standalone-Releases, wurde im aktuellen
Merge-Lauf aber nicht unabhängig reproduziert, weil das Worker-ZIP nicht als
Eingang bereitgestellt wurde.

## Nicht in dieser Umgebung ausführbar

- echter Docker-Image-Build und Compose-Start;
- Live-MQTT-mTLS-Verbindung gegen den Broker;
- Live-Master-/Worker-E2E;
- MCP-HTTP über den vorgesehenen produktiven TLS-Reverse-Proxy;
- reale SQLite-/Audit-Rollback-Probe bei laufendem Container.

Diese Prüfungen bleiben durch Preflight, Healthcheck, Readiness und die
bundleweiten Running-/Extended-Gates verbindlich. Readiness fällt ohne
MQTT-Verbindung geschlossen aus.

# Changelog

## Bundle-Integration — 2026-08-15

- additive Einbindung in Automation Control Plane `0.26.0 / v0026-r1`
- dedizierte MQTT-mTLS-Identität `mcp-controller-01` mit enger ACL
- Aufnahme in Master-Allowlist, Test-PKI und PKI-Handoff
- Integration in Compose-Lifecycle, Status, Logs, Static-/Running-/Extended-Gates
- zentrale Orchestrierung nach Proxy und vor Worker einschließlich
  Preflight, Backup, Apply, Ready, Status, Test, Restart und Rollback
- extern erzeugtes Bearer-Auth-Dokument mit restriktiven Dateirechten
- bundleweite Integritäts-, Secret-Hygiene- und Cross-Component-Vertragstests

## 0.1.0 — 2026-08-12

- erste eigenständige MCP-to-MQTT-Komponente
- modernes stateless MCP und Legacy-Kompatibilität
- fester Read-/Change-Toolkatalog ohne Raw MQTT, Terminal oder DB-Zugriff
- MQTT-mTLS und strikte Application-ACK-Korrelation
- externer Prepare–Approve–Commit-Workflow mit idempotentem Recovery
- begrenzte Event- und Sensorcaches
- rootless Compose-Deployment und vollständiger Lifecycle-Adapter
- tiefe Architektur-, Security-, Deployment-, Interface- und Betriebsdokumentation
- dependency-freie Test- und reproduzierbare Release-Pipeline

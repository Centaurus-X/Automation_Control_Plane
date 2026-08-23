# Proxy Gateway 0.26.0 / v0026-r1

Der Proxy bildet die öffentliche Auth-, RBAC-, HTTPS-/WSS- und
Transportgrenze. Im Standardmodus `node_local` akzeptiert er Config-Proposals
ausschließlich als `config_scope_operations_v1`, prüft Zielscope und erlaubte
Sections und leitet die validierte Operationsliste ohne Mapping an den Master
weiter.

Legacy `changes`, Path-/Selector-Payloads, clientseitige Proposal-IDs und
node-lokale Rollback-/Diff-Dekrete werden am Boundary fail-closed abgewiesen.
Der Master relayed; der owning Worker entscheidet durch SEM und Authority-
Fencing.

```sh
./test.sh
./deploy.sh
./status.sh
```

Die lokale Suite umfasst 17 Testdateien einschließlich TLS/WSS, Auth/RBAC,
Proposal-Negativfällen, Recovery, Migrationen und PostgreSQL-Transaktionen.

Der eigenständige MCP Controller ist keine Erweiterung dieses HTTP-/WSS-
Prozesses. Er läuft als separate Komponente, verwendet eine eigene Bearer-
Grenze und die MQTT-Instanz `mcp-controller-01`. Dadurch bleiben bestehende
Proxy-Clients, Sessions, CSRF-, Origin- und API-Verträge unverändert.


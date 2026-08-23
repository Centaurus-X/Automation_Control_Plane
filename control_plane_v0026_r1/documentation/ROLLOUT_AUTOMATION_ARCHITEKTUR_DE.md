# Rollout-Automationsarchitektur v0026-r1

```text
standalone/älteres rollout_automation.sh
        │
        ├─ Release-Metadaten aller ZIPs/Ordner lesen
        ├─ numerisch neuesten oder gepinnten Kandidaten wählen
        ├─ getrennte ZIP-Sidecars, sichere Struktur und Manifeste prüfen
        ├─ revisionsspezifisch idempotent installieren
        └─ ausgewähltes rollout_automation.sh re-exec
                         │
                         ├─ externe Konfiguration/Credentials
                         ├─ externes MCP-Auth-Dokument atomar erzeugen
                         ├─ SSH-ControlMaster mit Auto-Reconnect
                         ├─ verankertes Worker-v0101-Paket separat installieren
                         ├─ Control-Plane Fresh/Reuse/Upgrade
                         ├─ MCP-Test-/Produktions-PKI und dedizierte ACL prüfen
                         ├─ MCP Controller deployen und Readiness abnehmen
                         ├─ Admin-Bootstrap und allowlist-gebundene Admission
                         ├─ MQTT-PKI Stream/Installation
                         ├─ Fresh/Upgrade: Worker Authority-State prüfen/starten
                         ├─ Reuse: aktive Identität/PID ohne Neustart
                         ├─ Worker-Head ↔ Registry/Mirror exakt konvergieren
                         ├─ PID-Aktor-524- und Neustart-Sequenz-Gate
                         ├─ Core-E2E (WSS/Commands/Worker-Commit/Restore)
                         ├─ JSON-File-E2E (File-WAL/SEM/Commit/Mirror/Restore)
                         └─ Diagnose + credential-freies summary.json
```

Der Installer verwendet keine dauerhafte Backup-Abhängigkeit. Er prüft
Archivhash, Manifesthash, ZIP-Typen, Pfade und Größen vor einer recoverablen
Installation. Frühere Worker- und Releaseverzeichnisse werden unter
`$HOME/__OLD__` erhalten; die numerische Metadatenordnung bestimmt den aktiven
Control-Plane-Stand.

Lokale Worker-Dateiänderungen und externe Client-Proposals enden in derselben
node-lokalen Authority-Engine. Der Worker materialisiert stabile entity-keyed
Operations gegen einen unveränderlichen Baseline-Snapshot, führt SEM/Safety aus
und persistiert Commit, Journal und Mirror-Outbox atomar. Proxy und Master
validieren, autorisieren und relayen externe Operations unverändert; sie
mappen sie nicht und vergeben keine Node-Revision. Result, Registry-Tupel,
Mirror-Fingerprint und Runtime-Kennung bestätigen die abschließende
Konvergenz.

Die operative Komponentenreihenfolge ist:

```text
database -> mqtt -> master -> proxy -> mcp_controller_server -> worker
```

Der MCP Controller wird über den zentralen Component-Adapter mit denselben
Plan-, Hash-, Lock-, Fencing-, Checkpoint-, Audit-, Evidence-, Resume- und
Rollback-Verträgen verwaltet wie die bestehenden Komponenten. Sein lokaler
Approval-/Audit-Zustand wird vor Änderungen gesichert; Plattformdatenbanken
werden weiterhin nicht blind automatisch zurückgerollt.

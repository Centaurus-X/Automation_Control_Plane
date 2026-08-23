# Betrieb und Administration


## Zentrale Administration und Orchestration

Im v0026-Bundle ist die Komponente als reguläres Orchestrierungsziel eingebunden.
Der zentrale Plan bindet Registry, Inventory, Release-Manifest, Adapter und
Artefakthashes unveränderlich. Die Ausführungsreihenfolge lautet:

```text
database -> mqtt -> master -> proxy -> mcp_controller_server -> worker
```

Für selektive MCP-Operationen wird ein normaler Administration-Plan erzeugt:

```bash
./administration/orchestrate.sh plan \
  --inventory administration/inventory.test-lab.json \
  --registry administration/component_registry.test-lab.json \
  --manifest administration/release_manifest.json \
  --component mcp_controller_server \
  --output "$STATE_ROOT/plans/mcp-controller.json"

./administration/orchestrate.sh apply \
  --plan "$STATE_ROOT/plans/mcp-controller.json"
```

`preflight`, `backup`, `apply`, `ready`, `status`, `test`, `restart` und
`rollback` werden durch `administration/component_adapter.py` auf die unten
beschriebenen Lifecycle-Skripte abgebildet. Locks, Fencing, Checkpoints, Audit,
Evidence, Resume und Reverse-Order-Rollback bleiben damit dieselben wie für die
bestehenden Control-Plane-Komponenten.

## Lifecycle

| Aktion | Befehl | Aussage |
|---|---|---|
| Preflight | `./scripts/preflight.sh` | Secrets vorhanden, Compose gültig, Konfiguration fail-closed |
| Deploy/Upgrade | `./scripts/deploy.sh` | Image bauen, idempotent konvergieren, Readiness beweisen |
| Status | `./scripts/status.sh` | Container- und Healthzustand |
| Test | `./scripts/test.sh` | vollständige Release-Tests plus Live-Readiness |
| Backup | `./scripts/backup.sh` | Online-SQLite-Snapshot und vollständige Auditzeilen |
| Restart | `./scripts/restart.sh` | kontrollierter Neustart plus Readiness |
| Logs | `./scripts/logs.sh` | letzte 200 Zeilen und Follow |
| Stop | `./scripts/stop.sh` | Service stoppen, State-Volume behalten |
| Rollback | `MCP_CONTROLLER_ROLLBACK_IMAGE=... ./scripts/rollback.sh` | explizit vorhandenes altes Image starten |

Alle mutierenden Control-Plane-Adapteraktionen werden mit Fencing-Token und
Idempotency-Key lokal protokolliert. Ein bereits erfolgreich abgeschlossener Key
wird ohne erneute Wirkung beantwortet.

## Health

- `GET /health/live`: Prozessruntime ist weder failed noch stopped.
- `GET /health/ready`: Runtime läuft und – im Default – MQTT ist verbunden.

Die Endpoints enthalten keine Secrets und benötigen keine Bearer-Authentifizierung.
Sie sind nur über die konfigurierte Portbindung erreichbar. Docker markiert den
Container erst nach erfolgreichem `/health/ready` als healthy.

## Bridge-Diagnose

`automation_connection_status` liefert:

- Bridge-State und MQTT-Verbindungsstatus
- Queue-Tiefe und offene Requests
- Anzahl Publish-Erfolge/-Fehler, ACKs, unkorrelierter ACKs und ungültiger Frames
- Event-/Sensorcache-Größe und Gesamtzähler
- geheimnisfreien Transport-Snapshot und letzten neutralen Fehler

Bei `ready=false` zuerst Broker-Erreichbarkeit, CA, Servername, Zertifikatszeit,
ACL-Identität, Master-Instanz und Site prüfen. Der Server fällt bei erforderlichem
MQTT nicht auf Direktzugriff oder Offline-Schreibbetrieb zurück.

## Freigaben

Plan prüfen:

```bash
./scripts/approval_status.sh <plan-id>
```

Exakten Digest freigeben:

```bash
./scripts/approve.sh <plan-id> <payload-sha256> operator:alice
```

Die Freigabe gibt die gespeicherte Payload absichtlich nicht aus. Der Operator
muss Ziel, Änderungsbeschreibung und Digest über einen getrennten, vertrauenswürdigen
Prozess verifizieren. Default-TTL ist 300 Sekunden.

## Backup

`scripts/backup.sh` legt im State-Volume einen Zeitstempelordner an:

```text
/app/state/backups/backup-YYYYMMDDTHHMMSSZ/
  controller.sqlite3
  audit.jsonl
  manifest.json
```

SQLite wird über die Online-Backup-API konsistent kopiert. Vom Auditlog werden
nur vollständig abgeschlossene Zeilen übernommen. Das Manifest enthält SHA-256
beider Dateien. Backupverzeichnisse werden nie überschrieben.

Backups im selben Volume schützen nicht gegen Volume-/Hostverlust. Sie müssen
nach dem Lauf über eine etablierte, verschlüsselte Backupstrecke exportiert und
mit Retention versehen werden.

## Recovery

State-Recovery ist absichtlich kein automatischer Rollback-Schritt:

1. Service stoppen.
2. Backupmanifest und SHA-256 offline verifizieren.
3. aktuelles Volume separat sichern.
4. `controller.sqlite3` und `audit.jsonl` durch die verifizierten Dateien
   ersetzen; Besitz UID/GID 10001 und Modus 0600 setzen.
5. Service starten und Readiness prüfen.
6. einen bekannten Planstatus und `automation_connection_status` lesen.

Eine Wiederherstellung kann bereits abgeschlossene Planzustände zurücksetzen.
Da MQTT-Plan-ID und Idempotency-Key stabil sind, darf sie nur unter kontrollierter
Master-Inbox-/Operationsprüfung erfolgen.

## Kapazität und Skalierung

Defaults:

- 256 offene MQTT-Requests
- 10.000 Freigabepläne; bei Kapazitätsdruck werden nur terminale/abgelaufene
  Altpläne entfernt, aktive Pläne niemals verdrängt
- 1.024 eingehende MQTT-Nachrichten
- 4.096 Transport-Outbound-Nachrichten
- 5.000 Events und 5.000 Sensor-Identitäten im RAM
- 4 MiB maximales MQTT-Paket
- 1 MiB maximaler MCP-Request

Die erste Version ist single-active pro State-Volume. Für horizontale Skalierung
werden ein gemeinsamer transaktionaler Freigabespeicher und eine eindeutige
MQTT-Identität pro Replikat benötigt; beides ist nicht Teil von 0.1.0.

## Log- und Audit-Retention

Uvicorn-Access-Logs sind deaktiviert, um Authorization-Header und Nutzlasten
nicht versehentlich aufzunehmen. Container-stdout sollte vom Host rotiert werden.
`audit.jsonl` benötigt eine eigene Größen-/Zeit-Retention nach vorherigem Export
in ein unveränderliches Ziel. Eine abgeschnittene lokale Datei darf nur bei
gestopptem Service und nach gesichertem Export ersetzt werden.

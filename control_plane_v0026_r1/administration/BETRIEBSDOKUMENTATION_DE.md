# Betriebsdokumentation – Administration-Orchestrator

## Zweck und Grenzen

`administration/orchestrate.py` liegt additiv über den bestehenden Systemen.
Es ändert weder den nativen MQTT/Master-Config-Diff noch den lokalen
JSON-File-WAL-Pfad. Der primäre Node-Storage-Vertrag bleibt V9; V8 bleibt der
getrennte Cross-Shard und V5 ausschließlich expliziter Legacy-Import.

Der Standard `component_adapters` rollt bei Updates und Wartung nur die im Plan ausgewählten
Komponenten auf den ausgewählten Targets aus. `component_adapter.py` verwendet
für Database, MQTT, Master, Proxy und MCP Controller die vorhandenen Deploy-,
Status- und Testskripte. Beim Worker werden das exakt gehashte ZIP, der vorhandene
recoverable Installer und `worker_remote_lifecycle.sh` verwendet. Das alte
Gesamtrollout bleibt als expliziter `--legacy-full-once`-Fallback erhalten.
Ein erster/fresh Host besitzt noch kein belegbares Rollback-Ziel und wird daher
einmal über diesen Legacy-Bootstrap provisioniert; der Adaptermodus erfindet
für einen solchen Host absichtlich kein Schein-Backup.

Zugangsdaten stehen nicht in Inventory, Registry, Manifest, Plan oder Audit.
SSH-Authentifizierung muss über Agent, Schlüssel oder eine bereits etablierte
externe SSH-Sitzung funktionieren. Der Adapter startet SSH absichtlich im
Batch-Modus und speichert keine Passwörter.

## Dateien

- `orchestrate.py`: deterministischer Plan, Scope, Locks, Apply, Operations,
  Resume und Rollback;
- `component_adapter.py`: operative lokale Docker-Compose- und Remote-Worker-
  Lifecycle-Grenze;
- `component_registry.test-lab.json`: anwendbares Adapterprofil plus retained
  Legacy-Fallback;
- `component_registry.legacy.example.json`: separates genau-einmal-Legacy-
  Beispiel;
- `inventory.test-lab.json`: credential-freie Target- und Canary-Daten;
- `release_manifest.json`: Release-, Worker- und Artefakt-Hashes;
- `tests/`: lokale, mutationsfreie Funktionsprüfungen mit Fake-Adapter.

## Unveränderlicher Release- und State-Vertrag

Jeder Plan bindet:

1. den SHA-256 der Root-Datei `SHA256SUMS`;
2. einen kanonischen Digest aller darin deklarierten Release-Dateien;
3. jede einzelne Datei durch erneute Hashprüfung;
4. Manifest, Registry, Inventory, Config-Dateien, Adapter und externe
   Artefakte.

Eine fehlende, zusätzliche geänderte oder symbolisch verlinkte Datei im
Root-Manifest stoppt `plan` beziehungsweise das Drift-Gate vor einer Mutation.
Nach finalen Release-Änderungen muss deshalb zuerst das offizielle
`SHA256SUMS` neu gebaut werden.

`state_dir` und `--output` müssen außerhalb von `release_root` liegen. Dies ist
nicht nur eine Empfehlung, sondern wird fail-closed validiert. Ein Plan kann
seinen signierten Lock-/Checkpoint-Root später nicht per `--state-dir` umgehen.

## Pläne erstellen

Externer Zielpfad:

```sh
STATE_ROOT="$HOME/.local/state/automation-control-plane/administration/test-lab"
mkdir -p "$STATE_ROOT/plans"
```

Vollständiger Adapter-Update-Rollout auf bereits provisionierten Targets:

```sh
sh administration/orchestrate.sh plan \
  --inventory administration/inventory.test-lab.json \
  --registry administration/component_registry.test-lab.json \
  --manifest administration/release_manifest.json \
  --output "$STATE_ROOT/plans/full.json"
```

Nur Proxy:

```sh
sh administration/orchestrate.sh plan \
  --inventory administration/inventory.test-lab.json \
  --registry administration/component_registry.test-lab.json \
  --manifest administration/release_manifest.json \
  --component proxy \
  --output "$STATE_ROOT/plans/proxy.json"
```

Master und Proxy gemeinsam:

```sh
sh administration/orchestrate.sh plan \
  --inventory administration/inventory.test-lab.json \
  --registry administration/component_registry.test-lab.json \
  --manifest administration/release_manifest.json \
  --component master,proxy \
  --output "$STATE_ROOT/plans/master-proxy.json"
```

Nur MCP Controller Server:

```sh
sh administration/orchestrate.sh plan \
  --inventory administration/inventory.test-lab.json \
  --registry administration/component_registry.test-lab.json \
  --manifest administration/release_manifest.json \
  --component mcp_controller_server \
  --target control-plane-01 \
  --output "$STATE_ROOT/plans/mcp.json"
```

Genau ein Worker:

```sh
sh administration/orchestrate.sh plan \
  --inventory administration/inventory.test-lab.json \
  --registry administration/component_registry.test-lab.json \
  --manifest administration/release_manifest.json \
  --component worker \
  --target worker-fn-01 \
  --worker-canary-count 1 \
  --worker-batch-size 2 \
  --output "$STATE_ROOT/plans/worker-fn-01.json"
```

Mehrfach verwendete `--component`/`--target`-Optionen und kommagetrennte Werte
werden normalisiert und stabil sortiert. Canary und Batches werden nur aus den
tatsächlich ausgewählten Worker-Targets neu gebildet. Jeder Canary und jedes
Batch muss Readiness und Komponententest bestehen, bevor der nächste Schritt
beginnt. Die Ausführung innerhalb eines Batches bleibt bewusst seriell.
Die feste Reihenfolge ist
`database -> mqtt -> master -> proxy -> mcp_controller_server -> worker`.
Die Registry bindet dazu `rollout_strategy=serial` für Control-Plane-
Komponenten und `rollout_strategy=canary_batches` für Worker; Canary-Anzahl und
Batchgröße stammen aus dem Inventory oder den explizit signierten Plan-Overrides.

Ein Target-Filter ohne Komponentenfilter wählt nur die Komponenten dieses
Targets. Ein Komponentenfilter ohne Target-Filter wählt alle passenden
Targets. Leere Schnitte und unbekannte Namen werden abgelehnt.

## Apply, Status, Test und Restart

```sh
PLAN="$STATE_ROOT/plans/master-proxy.json"

sh administration/orchestrate.sh apply --plan "$PLAN"
sh administration/orchestrate.sh status --plan "$PLAN"
sh administration/orchestrate.sh status --plan "$PLAN" --live
sh administration/orchestrate.sh test --plan "$PLAN"
sh administration/orchestrate.sh restart --plan "$PLAN" \
  --operation-id maintenance-20260722-01
```

`status` ohne `--live` liest nur den lokalen Checkpoint. Mit `--live` wird der
Statusadapter auf jedem geplanten Target ausgeführt. `test` verlangt einen
vollständig erfolgreichen Apply desselben Plans. `restart` führt pro selektiver
Komponente den echten Restart aus und prüft anschließend Readiness; beim Worker
zusätzlich Admission und fortschreitende Telemetrie.
Die explizite `--operation-id` ist die Idempotenzgrenze: derselbe Wert setzt
einen ergebnisoffenen Aufruf sicher fort beziehungsweise wird nach belegtem
Erfolg zum No-op; ein neuer Wert autorisiert bewusst einen neuen Restart.

Vor jedem Adapterprozess werden Environment- und nur die ausgewählten
Target-Locks erworben. Fencing-Token und Idempotency-Key werden als Argumente
und Umgebungsvariablen weitergegeben. Der Component-Adapter registriert jede
Mutation zusätzlich durable. Ein bereits erfolgreich ausgeführter
Idempotency-Key wird nicht erneut mutiert.

Unterbrochene idempotente Schritte können fortgesetzt werden:

```sh
sh administration/orchestrate.sh apply --plan "$PLAN" --resume
```

## Sicherer Wechsel auf einen neuen Plan

Ein neuer immutable Plan verwendet denselben stabilen Environment-State. Ein
vorhandener Vorgänger wird nie still überschrieben. Nach complete, rolled_back
oder failed_rolled_back ist der Wechsel explizit möglich:

    sh administration/orchestrate.sh apply --plan "$NEW_PLAN" --archive-terminal

Der Vorgänger wird unter state_dir/__OLD__/<plan_id> inklusive Checkpoint,
Audit, Evidenz, Backup-Manifest und Handoff-Beleg archiviert. Environment- und
Target-Locks werden vorher erworben; Fencing-Counter und Adapter-Idempotenz
bleiben aktiv. failed, manual_recovery und failed_manual_recovery werden nicht
archiviert und verlangen zuerst eine externe Recovery-Entscheidung.

## Rollback

```sh
sh administration/orchestrate.sh rollback --plan "$PLAN"
```

Rollback läuft in umgekehrter Reihenfolge und nur, wenn Backup, explizite
Reversibilität und Rollback-Adapter belegt sind. Danach müssen Readiness und
Komponententest erneut erfolgreich sein.

- MQTT, Master, Proxy und MCP Controller sichern vor Apply die exakten
  Container-Image-IDs in
  externem State und unter einem planspezifischen lokalen Backup-Tag.
- Der MCP Controller sichert zusätzlich seinen SQLite-Approval-State und das
  append-only Auditlog. Rollback ist nur mit explizit gepinntem vorherigem
  Image zulässig und verändert weder PostgreSQL noch Worker-Konfiguration.
- Worker sichert den vorherigen Packagebaum remote außerhalb des Releases,
  stoppt kontrolliert, stellt nur diesen belegten Baum wieder her und startet
  anschließend über den vorhandenen Lifecycle.
- Database erstellt ein Backup, ist aber absichtlich `reversible: false`.
  Ein blindes Restore könnte neuere Produktionsdaten vernichten; ein Restore
  bleibt ein separat freizugebender Recovery-Vorgang nach Expand/Contract-
  Prüfung.
- Fehlt ein Beleg, wird nichts geraten: der Checkpoint wechselt zu
  `manual_recovery`.

## Worker-Voraussetzungen

Der Worker-Adapter ist für kontrollierte Updates einer bereits provisionierten
Node gedacht. Vor jedem Apply verifiziert er CA, Zertifikatskette,
Target-Identität, Ablaufzeit, Schlüsselpaar und Dateirechte. Anschließend
überträgt er die MQTT-Identität erneut als kurzlebiges Einmal-Staging, bevor der
laufende Worker gestoppt wird. Der Lifecycle konsumiert und entfernt dieses
Staging nach erfolgreicher Installation. Das Test-Lab verwendet ausschließlich
die markierte `mqtt_broker/test-pki`; Produktion muss mit
`adapter.pki_source_path` auf ein absolutes, extern verwaltetes Verzeichnis mit
`ca.crt`, `mqtt.crt` und `mqtt.key` zeigen. Das Worker-ZIP wird vor Transfer
gehasht, sein internes `SHA256SUMS` gebunden und remote erneut durch
`install_worker_release.py` geprüft. Commissioned V9-Konfiguration wird gemäß
`config_drift_policy=preserve` erhalten.

Für einen komplett neuen Host einschließlich Identitätsbootstrap wird einmalig
der retained Legacy-Fallback verwendet:

```sh
sh administration/orchestrate.sh plan \
  --inventory administration/inventory.test-lab.json \
  --registry administration/component_registry.test-lab.json \
  --manifest administration/release_manifest.json \
  --legacy-full-once \
  --output "$STATE_ROOT/plans/bootstrap-full.json"
```

Der Legacy-Modus akzeptiert keine partielle Komponenten- oder Target-Auswahl,
plant genau einen nicht-idempotenten Full-Apply und wiederholt einen
ergebnisoffenen Aufruf nach Prozessabbruch niemals automatisch.

## MCP-Voraussetzungen

Das Testprofil verwendet die markierte Labor-PKI unter
`mqtt_broker/test-pki/clients/mcp-controller/`. Produktion muss vollständige,
absolute Quellen für CA, MCP-Zertifikat und privaten Schlüssel sowie eine
externe Auth-Datei angeben. Teilmengen der Produktions-PKI werden fail-closed
abgewiesen. `deployment_profile` ist ausschließlich `test` oder `production`.

Vor jeder Mutation prüft der Adapter:

- Site und Source-Instance `mcp-controller-01`;
- Bind-Adresse und freien HTTP-Port;
- reguläre, nicht verlinkte Auth-/PKI-Dateien;
- Zertifikatsidentität und Schlüsselpaar;
- Auth-Dateirechte;
- vorhandenes Compose-Netz `automation_control`;
- unveränderte Release-/Manifest-Hashes.

Die HTTP-Schnittstelle bindet standardmäßig an Loopback. Ein externer Zugriff
ist nur hinter einem authentifizierten TLS-Reverse-Proxy zulässig.

## Audit und Evidenz

Im externen `state_dir` entstehen:

- `checkpoint.json`: atomarer Fortschritts-/Resume-Zustand;
- `audit.jsonl`: append-only, fsync und redigiert;
- `evidence/*.json`: Command, Exitcode, Dauer, stdout/stderr und Hashes;
- `adapter-state/`: durable Idempotency-/Fencing-Belege;
- `backups/`: planspezifische Rollback-Manifeste;
- `locks/`: Environment-/Target-Locks und monotone Fencing-Counter.

Secrets und URI-Credentials werden bereits beim Dokument-Import abgelehnt und
in Prozessausgaben vor Persistenz redigiert.

## Releaseuebergreifendes PostgreSQL-Runtime-Material

Vor dem Datenbank-Apply prueft der Adapter die aktuell gemounteten Rollen-
Secrets gegen den laufenden Primary und uebergibt sie atomar in das vom neuen
Release ausgeschlossene `postgres_system/secrets/`-Runtime-Verzeichnis. Dadurch
bleiben persistente PostgreSQL-Rollen und separat ausgerollte Master-/Proxy-
Komponenten konsistent. Fehlende, leere oder symbolisch verlinkte Quellen sowie
ein fehlgeschlagener TLS-Login stoppen den Plan vor dem Datenbank-Deploy. Bei
einem Fresh-Rollout ohne vorhandenen Primary wird kein Handoff ausgefuehrt.

Der Datenbank-Test ist ein eigener Adapterprozess. Wenn ein verifizierter
`runtime-tls-handoff` vorhanden ist, bindet auch dieser Prozess dieselben
`POSTGRES_*_SOURCE`-Dateien vor `test.sh`. Damit bleiben Apply und der im Test
enthaltene erneute Test-Deploy in derselben aktiven PostgreSQL-TLS-Domain.

## Lokale Verifikation

```sh
PYTHONDONTWRITEBYTECODE=1 python3 administration/component_adapter.py --self-test
PYTHONDONTWRITEBYTECODE=1 python3 administration/tests/test_orchestrator.py
PYTHONDONTWRITEBYTECODE=1 python3 tools/test_administration_release_set.py \
  --require-worker-artifact
```

Der letzte Test ist absichtlich ein finaler Packaging-Gate: Er wird erst grün,
wenn Worker-ZIP, Legacy-Hook-Hashes und Root-`SHA256SUMS` exakt zum fertigen
Release passen.

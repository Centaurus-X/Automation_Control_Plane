# Additive Administration-Orchestrierung

Diese Schicht ist jetzt ein operatives, vom Laufzeitsystem getrenntes
Rollout-System. Sie plant und sperrt eine exakte Komponenten-/Target-Auswahl,
führt die vorhandenen komponentenspezifischen Lifecycle-Hooks aus und speichert
Audit, Evidenz, Plan und Checkpoint ausschließlich außerhalb des unveränderlichen
Releasebaums.

Der Adaptermodus ist der Standard für Updates/Wartung bereits provisionierter
Targets. Ein erster/fresh Bootstrap ohne belegbares Rollback-Ziel verwendet den
retained `--legacy-full-once`-Pfad.

Wesentliche Garantien:

- kanonischer Plan mit Root-`SHA256SUMS` und vollständigem Release-Digest;
- partielle Auswahl über `--component` und `--target`;
- Database → MQTT → Master → Proxy → MCP Controller Server → Worker, mit
  Worker-Canary und Batches;
- operative Adapterpfade für `apply`, Live-`status`, `test`, `restart` und
  belegtes `rollback`;
- begrenztes Readiness-Wait für transiente starting-Zustände; status bleibt
  eine sofortige fail-closed Momentaufnahme;
- expliziter --archive-terminal-Planwechsel nur nach complete oder sauberem
  Rollback; manual recovery bleibt gesperrt und Locks/Fencing bleiben erhalten;
- Datenbank-Backup-Verifikation am realen sicheren /backup-Mount des laufenden
  Containers, unabhängig vom Zielrelease-Verzeichnis;
- Environment-/Target-Locks, monotone Fencing-Tokens, Idempotency-Keys,
  atomare Checkpoints und redigiertes Audit;
- exakter Worker-ZIP-Transfer und Wiederverwendung des verifizierten
  Worker-Installers/Lifecycle-Skripts; die geplante Worker-MQTT-Identität wird
  vor jedem Apply verifiziert und als kurzlebiges Einmal-Staging übertragen,
  bevor der laufende Worker gestoppt wird;
- Datenbank-Backup, aber kein blindes automatisches Datenbank-Restore;
- eigenständiger MCP-Lifecycle mit externer Auth-/PKI-Quelle, lokalem
  State-Backup, Readiness und explizit gepinntem Image-Rollback;
- expliziter `--legacy-full-once`-Fallback ohne partielle Auswahl.

Schnellbeispiel für genau einen Worker:

```sh
STATE_ROOT="$HOME/.local/state/automation-control-plane/administration/test-lab"
mkdir -p "$STATE_ROOT/plans"

sh administration/orchestrate.sh plan \
  --inventory administration/inventory.test-lab.json \
  --registry administration/component_registry.test-lab.json \
  --manifest administration/release_manifest.json \
  --component worker \
  --target worker-fn-01 \
  --output "$STATE_ROOT/plans/worker-fn-01.json"

sh administration/orchestrate.sh apply \
  --plan "$STATE_ROOT/plans/worker-fn-01.json"
sh administration/orchestrate.sh status \
  --plan "$STATE_ROOT/plans/worker-fn-01.json" --live
sh administration/orchestrate.sh restart \
  --plan "$STATE_ROOT/plans/worker-fn-01.json" \
  --operation-id maintenance-20260722-01
```

Die vollständige Bedienung, Rollback-Grenzen und Produktionsvoraussetzungen
stehen in [BETRIEBSDOKUMENTATION_DE.md](BETRIEBSDOKUMENTATION_DE.md).

Selektives MCP-Beispiel:

```sh
sh administration/orchestrate.sh plan \
  --inventory administration/inventory.test-lab.json \
  --registry administration/component_registry.test-lab.json \
  --manifest administration/release_manifest.json \
  --component mcp_controller_server \
  --target control-plane-01 \
  --output "$STATE_ROOT/plans/mcp.json"

sh administration/orchestrate.sh status \
  --plan "$STATE_ROOT/plans/mcp.json" --live
```

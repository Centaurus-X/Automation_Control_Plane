# Betriebsanleitung

## 1. Lokaler Schnellstart

Voraussetzung ist Python 3.12 oder neuer.

```sh
./start_orchestration.sh
./status_orchestration.sh
```

Der erste Start erzeugt unter XDG-Standardpfaden:

- `~/.config/automation-fleet/fleet.json`;
- Inventory, Desired State, Policy und Release Catalog;
- Admin-, Operator-, Approver-, Auditor- und Viewer-Token mit Modus `0600`;
- `~/.local/state/automation-fleet/` für Journal und Evidence.

Die UI ist standardmäßig nur auf `http://127.0.0.1:9080` erreichbar. Den
ausgegebenen rollenbezogenen Tokeninhalt in die Loginmaske eingeben. Operator
planen und führen aus; Approver genehmigen mit einer getrennten Identität;
Auditoren lesen Audit und Evidence. Beenden und Status:

```sh
./stop_orchestration.sh
./status_orchestration.sh
```

## 2. Konfiguration

Vor einem Rollout die extern kopierten Dateien bearbeiten, nicht die Beispiele
im Releasebaum. Wesentliche Felder:

- `inventory.example.json`: echte kubeconfig-Kontexte, Namespaces und Wellen;
- `desired_state.example.json`: nur gewünschte Komponenten aktivieren und alle
  Image-Digests/SecretRefs setzen;
- `policy.example.json`: Approval und Sicherheitsgrenzen beibehalten;
- `fleet.json`: Pfade, Listen-Adresse und Observe-Timeout.

Die Null-Digests sind beabsichtigte Sperren. Ein aktiviertes Image mit
Placeholder-Digest wird abgewiesen.

Vor dem Öffnen des HTTP-Listeners validiert der Dienst Konfiguration, Katalog,
Descriptoren, Desired State und Policy und prüft die bestehende Audit-Hashkette.
Ein ungültiger oder manipulierter Zustand führt zu einem fehlgeschlagenen Start.

## 3. CLI-Workflow

```sh
CONFIG="$HOME/.config/automation-fleet/fleet.json"
PLAN=/tmp/automation-plan-v0028-r1.json

./kubernetes_orchestration/fleetctl.sh --config "$CONFIG" validate
./kubernetes_orchestration/fleetctl.sh --config "$CONFIG" \
  plan --output "$PLAN"
PLAN_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan_id"])' "$PLAN")"
./kubernetes_orchestration/fleetctl.sh --config "$CONFIG" \
  approve --plan-id "$PLAN_ID" --principal local-operator
FLEET_ALLOW_MUTATIONS=1 \
  ./kubernetes_orchestration/fleetctl.sh --config "$CONFIG" \
  apply --plan "$PLAN"
```

`validate` und `plan` verändern kein Cluster. `apply` ist ohne die explizite
Umgebungsvariable gesperrt. Ein abgebrochener Plan kann mit demselben Planfile
idempotent fortgesetzt werden.

## 4. Web-Workflow

1. mit Operator-Token anmelden;
2. Desired State prüfen/bearbeiten;
3. Plan erzeugen und vollständige Plan-ID prüfen;
4. abmelden und mit einer anderen Approver-Identität exakt freigeben;
5. weitere Wellen jeweils separat freigeben;
6. mit Operator-Identität nur auf einem bewusst mit Schreibrechten gestarteten
   Server anwenden, bei Bedarf pausieren und idempotent fortsetzen;
7. Status, Agenten, Ressourcen, Audit und Evidence live verfolgen.

Apply und Rollback werden in einem separaten Prozess ausgeführt. Die UI bleibt
für Statusabfragen verfügbar und aktualisiert alle zehn Sekunden.

Ein im Zielcluster laufender Fleet-Pod darf sein eigenes Deployment nicht
aktualisieren: Der Planner weist `automation-fleet` für `context=in-cluster`
ab, weil ein `Recreate` den ausführenden Helm-Prozess beenden würde. Installation
und Upgrade des Fleet-Dienstes erfolgen aus einem externen Managementkontext.

## 5. Image-Builds

### Control Plane

```sh
./kubernetes_orchestration/scripts/build_control_plane_images.sh \
  --registry registry.example.invalid/automation \
  --master-base registry.example.invalid/python-free-threaded@sha256:... \
  --proxy-base docker.io/library/python@sha256:... \
  --mcp-base docker.io/library/python@sha256:... \
  --output /secure/control-plane-images.lock.json
```

Der Build nutzt `buildx`, SBOM/Provenance und Push. Das Ergebnis enthält die
tatsächlichen Registry-Manifestdigests. MQTT und OpenTelemetry werden als
ebenfalls digest-gepinnte Upstream-Images konfiguriert.

### Separate Runtime

```sh
AUTOMATION_RUNTIME_BASE_IMAGE='docker.io/library/python@sha256:...'
export AUTOMATION_RUNTIME_BASE_IMAGE
./kubernetes_orchestration/scripts/build_runtime_image.sh \
  --archive /secure/automation_system_v0101.zip \
  --sidecar /secure/automation_system_v0101.zip.sha256 \
  --image registry.example.invalid/automation/system:v0101
```

Das Archiv bleibt außerhalb des Control-Plane-Releases. Erst der kontrollierte
Image-Build verifiziert den exakt gebundenen äußeren Digest, das interne
Manifest und jede enthaltene Datei und extrahiert es.

Die unveränderten v0101-Profile sind identitätsgebunden: `worker_simulation`
und `standalone` verwenden `site-main/fn-01`, `worker_fn02` verwendet
`site-main/fn-02`. Der Kubernetes-Planner und das Chart lehnen abweichende
Site-/Node-Kombinationen ab, statt wirkungslose Umgebungsvariablen vorzutäuschen.
`worker_fn02` ist nur mit `kubernetes_soft_rt` zulässig; Hard-RT bleibt Linux.

### Fleet Image

Der Fleet-Build erwartet ein digest-gepinntes Python-Basisimage sowie lokal
bereitgestellte, separat SHA-256-geprüfte `kubectl`- und Helm-Binaries. Details
stehen in `scripts/build_fleet_image.sh`.

## 6. Kubernetes Secrets

Die Charts erzeugen keine geheimen Werte. Erforderliche Schlüssel:

| SecretRef | erforderliche Keys |
|---|---|
| MQTT TLS | `ca.crt`, `tls.crt`, `tls.key`, `health.crt`, `health.key` |
| Master DB | `app-password`, `migrator-password`, `ca.crt` |
| Master MQTT | `ca.crt`, `tls.crt`, `tls.key` |
| Master Initial Config | `initial_config_v9.json` |
| Proxy DB | `app-password`, `migrator-password`, `ca.crt` |
| Proxy MQTT | `ca.crt`, `tls.crt`, `tls.key` |
| Proxy HTTPS/WSS | je Secret `tls.crt`, `tls.key` |
| Proxy Security | `password-pepper`, `token-pepper`, `binding-pepper` |
| MCP MQTT | `ca.crt`, `tls.crt`, `tls.key` |
| MCP Auth | `mcp_auth.json` |
| Worker MQTT | `mqtt_ca.crt`, `tls.crt`, `tls.key` |
| Fleet Auth | `admin.token`, `operator.token`, `approver.token`, `auditor.token`, `viewer.token` |
| Cluster Agent | `agent.token` (pro Target eindeutig) |

Secrets niemals als `--set`-Wert, Desired-State-Feld oder Git-Datei übergeben.

## 7. Cluster-Agent und GitOps

Ein nicht direkt erreichbares Workload-Cluster wird zunächst aus einem externen
Managementkontext mit `automation-cluster-agent` gebootstrapped. Danach erhält
sein Inventareintrag `context=in-cluster`, `execution_mode=agent` und
`write_owner=helm`. Das per-Target-Token wird zentral in
`agent_registry` und im Zielcluster als Secret referenziert. Die UI muss den
Agenten als `ONLINE` zeigen, bevor eine Welle freigegeben wird. Der Agent benötigt
keinen Ingress; Egress zu Kubernetes API, DNS und Fleet HTTPS genügt.

Für GitOps wird je Target genau `gitops_argocd` oder `gitops_flux` als Write
Owner gesetzt. `worktree`, Repositorypfad, Branch, Remote und Reconciler-Name
sind explizit zu binden. Vor Apply muss der Worktree sauber sein. Der Provider
committet nur `manifest.yaml`, `kustomization.yaml` und digestgebundene Metadaten,
optional pusht er den gebundenen Branch und wartet anschließend auf den echten
Reconciler-Status. Direkte Helm-Mutationen sind für dasselbe Target damit
ausgeschlossen.

## 8. Observability

Das Observability-Chart kann `ServiceMonitor`, fünf Fleet-Alertklassen und das
Dashboard `Automation Fleet Overview` bereitstellen. Optional sendet der
OpenTelemetry Collector per authentifiziertem Prometheus Remote Write an eine
zentrale Instanz. Die Autorisierung wird ausschließlich über ein bestehendes
Secret (`authorization`) injiziert. Relevante Alarme betreffen unerreichbare
Targets, degradierte Targets, Offline-Agenten, fehlgeschlagene/manuelle
Recovery-Pläne, lange ausstehende Wellenfreigaben und eine ungültige Auditkette.

## 9. Reiner Helm-Betrieb

Jeder Unterordner in `deploy/helm/` ist ein eigenständiges Chart. Das
Umbrella-Chart ist bereits offline vendored. Vor jeder Installation:

Ein einzelnes Chart startet mit `enabled: true`. `enabled: false` rendert auch
bei direkter Chart-Nutzung keine Ressourcen und kann daher nicht als Umgehung
der Placeholder-Digest-Sperre verwendet werden.

```sh
helm lint --strict CHART --values production-values.yaml
helm template RELEASE CHART --values production-values.yaml > rendered.yaml
```

Die anschließend verwendeten Values müssen dieselben Digests und SecretRefs
enthalten, die im genehmigten Plan angezeigt werden.

## 10. Backup, Rollback und Recovery

Vor Master-/Proxy-Migrationen ist ein anwendungskonsistentes PostgreSQL-Backup
außerhalb dieses Layers verpflichtend. PVC-Snapshots ersetzen kein DB-Backup.
Ein Rollback benötigt eine beobachtete frühere Helm-Revision. Initiale
Installationen und Datenbankschema-Recovery werden absichtlich nicht blind
automatisiert. Bei `MANUAL_RECOVERY` keine weiteren Mutationen auslösen, sondern
Evidence, Helm-History, Migration-Job und DB-Backup gemeinsam auswerten.

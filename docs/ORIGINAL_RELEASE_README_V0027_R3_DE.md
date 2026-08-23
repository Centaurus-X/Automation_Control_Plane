# Automation Control Plane 0.27.3 / v0027-r3

Dieses Release ergänzt die unveränderte Control Plane `v0026-r1` um den
eigenständigen Kubernetes-Orchestration- und Administrationsdienst
`kubernetes_orchestration`.

## Driftfreie Releasegrenze

```text
automation_control_plane_release_v0027_r3/
├── control_plane_v0026_r1/      byte-identische Baseline
└── kubernetes_orchestration/    neuer additiver Management-Layer
```

`automation_system_v0101.zip` bleibt ein zweites, separates Releaseartefakt.
Es wird nicht in dieses ZIP eingebettet. Der neue Layer verifiziert das externe
Archiv und kann daraus ein digest-gepinntes Soft-RT-/Simulations-Image bauen.
Der Linux-/systemd-Betrieb des Workers bleibt vollständig erhalten und ist der
bevorzugte Betriebsmodus für Hardware- und Echtzeitprofile.

## Schnellstart der Web-UI

```sh
./start_orchestration.sh
./status_orchestration.sh
```

Die Initialisierung legt Konfiguration, Zustand und getrennte Admin-, Operator-,
Approver-, Auditor- und Viewer-Token außerhalb
des Releasebaums ab. Die UI bindet standardmäßig ausschließlich an
`127.0.0.1:9080`. Kubernetes-Mutationen bleiben bis zur expliziten Aktivierung
`FLEET_ALLOW_MUTATIONS=1` gesperrt.

## Kubernetes-Rollout

```sh
./kubernetes_orchestration/scripts/setup.sh
CONFIG="$HOME/.config/automation-fleet/fleet.json"
PLAN=/tmp/automation-plan.json
./kubernetes_orchestration/fleetctl.sh --config "$CONFIG" validate
./kubernetes_orchestration/fleetctl.sh --config "$CONFIG" \
  plan --output "$PLAN"
PLAN_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["plan_id"])' "$PLAN")"
./kubernetes_orchestration/fleetctl.sh --config "$CONFIG" \
  approve --plan-id "$PLAN_ID"
FLEET_ALLOW_MUTATIONS=1 \
  ./kubernetes_orchestration/fleetctl.sh --config "$CONFIG" \
  apply --plan "$PLAN"
```

Die Komponenten-Charts unter `kubernetes_orchestration/deploy/helm/` sind
einzeln installierbar. Das Umbrella-Chart ist ausschließlich eine optionale
Komposition. Kubernetes bleibt Deployment-Owner; Worker-Authority, SEM,
Safety, Scope-Revision und Hardware-Fencing bleiben unverändert im Worker.

R3 ergänzt den Layer um erklärbares Placement, neun journalisierte Saga-
Schritte, Vier-Augen- und Wellenfreigaben, Pause/Resume, outbound-only
Cluster-Agenten mit zweistufigem Lease-Fencing, Argo-CD-/Flux-Handoff,
Evidence-Export sowie erweiterte Metriken, Alerts und Dashboard. Diese
Funktionen verändern weder die erhaltene Control Plane noch das separate
Runtime-Archiv.

## Verifikation

```sh
./test_release.sh
sha256sum -c SHA256SUMS
```

Die vollständige Diagnose, Betriebsanleitung, Sicherheitsgrenzen und die
explizit extern auszuführenden Docker-/Helm-/Cluster-Gates befinden sich unter
`kubernetes_orchestration/documentation/`.

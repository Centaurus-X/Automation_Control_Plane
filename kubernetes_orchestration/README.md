# Kubernetes Orchestration Point 0.28.0 / v0028-r1

Der Orchestration Point ist eine additive, eigenständig betreibbare Management-
Komponente über der unveränderten Automation Control Plane und dem weiterhin
separaten Automation System.

Er stellt bereit:

- deterministische, SHA-256-gebundene Multi-Cluster-Pläne;
- typisierte Helm-, Argo-CD- und Flux-Operationen ohne frei eingebbare Shellkommandos;
- deterministische capability-/labelbasierte Placement-Entscheidungen mit Ausschlussgründen;
- outbound-only Cluster-Agenten mit Kubernetes-Lease, zentraler Lease-Epoche und Fencing;
- namespace-begrenzte Kubernetes-Beobachtung und Administration;
- idempotente Wave-/Saga-Ausführung, Pause/Resume, Fencing, SQLite-Journal,
  Audit-Hashkette und exportierbare Evidence-Bundles;
- einzelne Helm-Charts sowie ein optionales Umbrella-Chart;
- einen getrennten Deploymentpfad für `automation_system_v0101.zip`;
- Live-Inventar, Pod-/Deployment-/StatefulSet-/Job-/PVC-Zustand und Metriken;
- eine responsive Web-UI für Health, Placement, Agenten, Planvorschau,
  Vier-Augen-/Wellenfreigabe, Rollout, Pause, Audit und Evidence;
- Standalone-, systemd-, Compose- und Kubernetes-Betriebsartefakte.

## Schnellstart

```sh
./scripts/setup.sh
./scripts/start.sh
./scripts/status.sh
```

Danach ist die UI unter `http://127.0.0.1:9080` erreichbar. Das beim Setup
erzeugte Token liegt mit Modus `0600` im ausgegebenen Konfigurationsverzeichnis.

## Sicherheitsdefault

Der Dienst startet read-only. Schreibende Kubernetes-Operationen benötigen:

1. getrennte Operator- und Approver-Identitäten (oder einen nicht selbst
   genehmigenden Admin),
2. einen deterministischen Plan,
3. genau einen Write Owner (`helm`, `gitops_argocd` oder `gitops_flux`),
4. `FLEET_ALLOW_MUTATIONS=1`,
5. die namespace-begrenzten Rechte des Zielclusters beziehungsweise eines
   authentisierten per-Target-Agenten.

Es gibt weder Terminalzugriff noch `pods/exec`, dynamischen Python-Code,
`shell=True`, CRDs, mutierende Admission-Webhooks oder automatische
ClusterRoleBindings.

## Verzeichnisstruktur

```text
config/                 Inventar, Katalog und Desired-State-Beispiele
schemas/                versionierte JSON-Schemas
component_descriptors/  provider-neutrale Komponentenverträge
src/automation_fleet/   Planner, Provider, Journal, API und Collector
web/                    eigenständige Web-Oberfläche
deploy/                 Helm, Compose, systemd und Image-Builds
scripts/                Setup, Lifecycle, Tests und Runtime-Image-Build
tests/                  funktionale und Security-Regressionen
tools/                  Integritäts-, Audit- und Releasewerkzeuge
documentation/          Diagnose, Architektur, Betrieb und Security
```

## Reiner Kubernetes-Modus

Alle Komponenten-Charts können ohne Fleet API installiert werden. Der Fleet
Layer ist keine Laufzeitabhängigkeit und besitzt keine Domain-Authority.

## Managed-Fleet-Modus

`fleetctl` oder die Web-UI erzeugen einen immutable Plan und führen ihn in
stabiler Reihenfolge aus. Ein Fehler stoppt spätere Wellen. Bereits laufende
Workloads bleiben Kubernetes-eigene Ressourcen; ein Ausfall des Fleet-Dienstes
stoppt weder Control Plane noch Worker.

Die vollständige Inbetriebnahme, Image-Builds, Secret-Verträge und reale
Cluster-Abnahme stehen in `documentation/BETRIEBSANLEITUNG_DE.md` und
`documentation/KUBERNETES_TARGET_ACCEPTANCE_DE.md`.

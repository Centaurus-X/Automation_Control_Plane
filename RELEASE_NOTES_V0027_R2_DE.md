# Release Notes – automation_control_plane_release_v0027_r2

## Ergebnis

`v0027-r2` ergänzt die vollständig erhaltene Control Plane `v0026-r1` um den
eigenständigen **Automation Fleet Kubernetes Orchestration Point**. Die
Anwendungslogik der bestehenden Control Plane wurde nicht verändert. Das
Worker-Release `automation_system_v0101` bleibt ein zweites, separates Archiv.

## Neue additive Funktionen

- eigenständige Fleet API und responsive Web-UI auf Port `9080`;
- deterministische, SHA-256-gebundene Pläne für mehrere Kubernetes-Cluster;
- explizite Write-Ownership, Rollout-Wellen und Stop-on-failure;
- exakte Planfreigabe, idempotente Schritte, lokales Fencing und SQLite-Journal;
- manipulationsprüfbare Audit-Hashkette und Evidence pro Schritt;
- typisierte `kubectl`-/Helm-Aufrufe ohne Shell-Interpretation;
- Live-Beobachtung von Deployments, StatefulSets, DaemonSets, Pods, Jobs, PVCs
  und Services;
- Prometheus-Metriken und optionale OpenTelemetry-/Prometheus-Operator-Artefakte;
- acht einzeln installierbare Helm-Charts und ein offline-vendorisiertes
  Umbrella-Chart;
- gehärtete Image-Buildpfade für Master, Proxy, MCP Controller, Fleet API und
  die separat gelieferte Runtime;
- systemd-, Compose- und lokaler Standalone-Betrieb des Fleet-Dienstes;
- vollständiger Release-Checksum- und File-Audit-Workflow.

## Unveränderte Grenzen

- `control_plane_v0026_r1/` ist byteidentisch zur gelieferten Baseline.
- Die Worker-Authority, SEM/Safety, Scope-Revisionen und Hardware-I/O verbleiben
  im Worker.
- Kubernetes ist Deployment-Provider, nicht Echtzeitregler oder Domain-Owner.
- PostgreSQL wird im Kubernetes-Layer ausschließlich als externer/verwalteter
  Dienst integriert; es wird keine naive Datenbank-HA-Lösung erfunden.
- Hard-RT-Profile sind in Kubernetes fail-closed abgewiesen.

## Inbetriebnahme

Siehe
`kubernetes_orchestration/documentation/BETRIEBSANLEITUNG_DE.md` und
`kubernetes_orchestration/documentation/KUBERNETES_TARGET_ACCEPTANCE_DE.md`.

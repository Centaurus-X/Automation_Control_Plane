# Release Notes – automation_control_plane_release_v0027_r3

## Ergebnis

R3 schließt die bei der Vollanalyse von v0027-r2 verbliebenen Integrationslücken
im eigenständigen Kubernetes-Orchestration Point. Die 588 Dateien der
v0026-r1-Control-Plane bleiben byteidentisch; `automation_system_v0101` bleibt
ein separat digestgebundenes Archiv.

## Neue additive Funktionen

- Placement per Target-ID oder Selector mit Capabilities, Labels, Failure
  Domains, stabiler Rangfolge und vollständigen Ausschlussgründen;
- Plan v2 mit neun Saga-Schritten von Discovery bis Verify;
- direkter Helm-, Argo-CD-, Flux- und outbound Agent-Pfad unter exakt einem
  Write Owner pro Target;
- Cluster-Agent mit Kubernetes-Leader-Lease, zentraler Lease-Epoche,
  per-Target-HMAC, Task-Fencing und signierten Livebeobachtungen;
- getrennte Operator-, Approver-, Auditor-, Viewer- und Admin-Rechte,
  technisches Vier-Augen-Prinzip und Approval pro Welle;
- Pause/Resume und idempotentes Fortsetzen eines unveränderten Plans;
- digestgeprüfter Evidence-Bundle-Export über API und Web-UI;
- Agenten-/Placement-/Risiko-/Wellenansichten in der Web-UI;
- erweiterte Prometheus-Metriken und Alerts, Grafana-Dashboard sowie optionales
  authentifiziertes Remote Write;
- eigenständiges Cluster-Agent-Chart; zehn Charts einschließlich Umbrella.

## Bewusst unveränderte Grenzen

- keine Änderung an Control-Plane-Anwendungslogik oder Runtime-Code;
- keine Verlagerung von SEM, Safety, Shard- oder Hardware-Authority;
- kein unqualifiziertes Kubernetes-Hard-RT;
- kein automatischer Datenbankschema-Rollback;
- keine cluster-scoped RBAC-, CRD-, Admission- oder Terminalfunktion.

Die reale Produktionsabnahme mit Registry, Helm, kubectl, Kubernetes, CNI,
Storage, PKI, PostgreSQL und GitOps-Controller bleibt ein explizites externes
Gate gemäß `KUBERNETES_TARGET_ACCEPTANCE_DE.md`.

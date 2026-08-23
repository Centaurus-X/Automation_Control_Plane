# Tiefe Diagnose und Implementierung v0027-r3

## Auftrag und unveränderliche Grenze

Untersucht wurden das vollständige v0027-r2-Release, sein internes
`SHA256SUMS`, alle 588 Dateien der eingebetteten v0026-r1-Control-Plane, alle
Dateien des additiven Kubernetes-Layers, die externe Architekturplanung sowie
die Digestbindung des separaten `automation_system_v0101`-Archivs. Die
Control-Plane v0026-r1 wurde nicht als Quellbasis fortgeschrieben: Ihr
Dateisatz, ihr internes Manifest und jedes einzelne Byte bleiben unverändert.
Das Runtime-Archiv bleibt extern und ist nicht im Control-Plane-ZIP enthalten.

Die Änderungen liegen ausschließlich unter `kubernetes_orchestration/` sowie
in Release-Metadaten und Dokumentation des äußeren Wrappers. Der Layer bleibt
ein eigenständiger Dienst ohne Domain-, SEM-, Safety- oder Hardware-Authority.

## Prüfmethode

Die Diagnose kombinierte:

1. äußere und interne SHA-256-Prüfung beider Eingangsarchive;
2. vollständigen Dateisatzvergleich der erhaltenen Control-Plane;
3. Parsing aller Python-, JSON-, JavaScript-, Shell- und statischen YAML-Dateien;
4. Quelltextprüfung von Planner, Validation, Journal, Executor, Provider,
   API, Auth, Collector, UI, Charts, Build-/Releasewerkzeugen und Tests;
5. Abgleich der Descriptor-DAGs mit dem Release Catalog;
6. Abgleich mit dem Multicluster-/RT-Architekturplan;
7. funktionale Negativ- und Positivtests für Ownership, Secrets, Fencing,
   Agenten, GitOps, Approval, Wellen, Resume, Rollback und Evidence;
8. erneute Driftprüfung und Release-Verifikation aus dem finalen Baum.

## Befund v0027-r2

v0027-r2 enthielt bereits einen belastbaren additiven MVP: eigenständige UI/API,
deterministischen Helm-Planer, Journal, Hashkette, Runtime-Digestbindung, acht
Komponentencharts plus Umbrella-Chart sowie lokale Tests. Gegen die vollständige
Zielarchitektur bestanden jedoch konkrete Integrationslücken:

| Befund | Auswirkung | R3-Behebung |
|---|---|---|
| Executor rief trotz geplantem Register nur den Helm-Provider direkt auf | GitOps war nicht Ende-zu-Ende aktiv | funktionales Provider-Register im Executor und providerbezogener Rollback |
| nur explizite Target-IDs | keine erklärbare Placement-Entscheidung | deterministische Selector-, Capability-, Label- und Failure-Domain-Auswahl mit Ausschlussgründen |
| fünf grobe Schritte | Discover, Diff und Backup fehlten als Evidence-Grenzen | neun unveränderliche Saga-Schritte |
| keine remote Cluster-Agent-Laufzeit | nicht direkt erreichbare Cluster waren nicht verwaltbar | outbound-only Agent, lokale Kubernetes-Lease, zentrale Epoche, signierte Tasks/Resultate |
| ein Admin konnte planen und genehmigen | kein technisches Vier-Augen-Prinzip | Operator-/Approver-Trennung, Creator-Bindung und Wellenfreigaben |
| keine Pause-/Resume-Zustände | operative Wellenkontrolle unvollständig | `PAUSE_REQUESTED`, `PAUSED`, idempotentes Resume und Approval-Gate pro Welle |
| keine typisierte GitOps-Reconciliation | Owner-Vertrag nur dokumentiert | Flux-/Argo-CD-Worktree-Handoff, Commit/Push, echte Reconciler-Health und Git-Revert |
| Evidence nur als interne Step-Datei | kein komfortabler Abnahmeexport | digestgeprüftes Plan-/Status-/Step-/Audit-Bundle über API/UI |
| minimale Observability | Agenten-, Audit- und Rolloutfehler nicht abgedeckt | neue Metriken, fünf zusätzliche Regeln, Grafana-Dashboard und optionales Remote Write |
| UI ohne Agenten/Placement/Wellen | Administration blieb CLI-lastig | Rollenbewusste Agenten-, Risiko-, Placement-, Wellen-, Pause- und Evidence-Ansichten |
| Katalog enthielt verkürzte Dependency-Gruppen | Descriptor-/Katalogdrift | vollständige fünf Dependency-Gruppen und neue Placement Contracts |

## Implementierter Orchestrierungsvertrag

Ein Plan v2 bindet Inventory, Desired State, Policy, Catalog, jeden Descriptor,
jeden Chart-Tree, effektive Values, Write Owner, Execution Mode, Field Manager,
Placement-Begründung, Wellen und Risikoübersicht per SHA-256. Die Operationen
lauten stabil:

`discover → validate_target → render → diff → preflight → backup → apply → wait_ready → verify`

Pro Target gilt genau ein mutierender Pfad:

- `helm` + `direct`: typisierte lokale Helm-/kubectl-Prozesse;
- `helm` + `agent`: derselbe Provider im Zielcluster über signierten Task;
- `gitops_argocd`: ausschließlich Git-Handoff und Argo-CD-Health;
- `gitops_flux`: ausschließlich Git-Handoff und Flux-Health;
- `observe_only`: Monitoring, keine Aufnahme in einen mutierenden Plan.

Agent-Resultate werden erst akzeptiert, wenn Target, Plan, Step,
Idempotency-Key, Fencing-Token, Lease-Epoche, HMAC und Result-Digest stimmen.
Der lokale Agent-Leader wird über `coordination.k8s.io/v1` gewählt; die Fleet
Plane vergibt zusätzlich eine monotone Epoche gegen Split Brain.

## Laufzeit- und RT-Grenze

`automation_system_v0101` bleibt ein zweites Archiv. Sein äußerer Digest
`c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467`
und das interne Manifest sind fest gebunden. Das Kubernetes-Chart akzeptiert
nur die bereits vorhandenen Soft-RT-/Simulationsprofile und die unveränderten
Site-/Node-Identitäten. Der harte RT-Regelpfad, C-Core, HAL und finale lokale
Aktuator-Authority werden nicht in Kubernetes oder den Fleet-Dienst verlagert.
Eine solche Runtime-Änderung wäre kein additiver Orchestration Layer und bleibt
deshalb bewusst außerhalb dieses Releases.

## Sicherheitsbewertung

- Keine freie Shell, kein `shell=True`, kein dynamisches Python, keine
  benutzerdefinierten Klassen/Decorator/Lambdas im Python-Layer.
- Keine Inline-Secrets in Plan, Inventory, Desired State, Catalog oder Evidence.
- Keine ClusterRoles, ClusterRoleBindings, CRDs, Admission-Webhooks oder
  `pods/exec`-Rechte.
- Browsermutationen bleiben Same-Origin-/CSRF-gebunden; Sitzungen sind
  HttpOnly/SameSite und rollenbezogen.
- Tokenfiles werden mit Modus `0600` geprüft; Agenten-Token sind per Target
  getrennt und können zusätzlich an ein mTLS-Subject gebunden werden.
- Datenbankrollback bleibt verboten; fehlende sichere Provider-Referenzen
  enden in `MANUAL_RECOVERY`.

## Verbleibende externe Produktions-Gates

Die Software- und Releaseprüfungen sind lokal ausführbar. Eine
Produktionsfreigabe verlangt zusätzlich eine reale Zielumgebung mit Docker/
OCI-Registry, Helm, `kubectl`, Kubernetes API, CNI, StorageClass, PKI,
PostgreSQL, Argo CD/Flux und mindestens zwei Agent-Replikaten. Diese Gates sind
in `KUBERNETES_TARGET_ACCEPTANCE_DE.md` normativ aufgeführt. Nicht vorhandene
Tools oder Cluster werden niemals als bestanden ausgegeben.

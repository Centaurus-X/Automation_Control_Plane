# Kubernetes-Orchestration – Integrationsstatus v0028-r1

## Vollständig im Paket enthalten

| Bereich | Status | Bemerkung |
|---|---|---|
| Preserved Control Plane v0026-r1 | PASS | 588 Dateien byteidentisch erhalten |
| Automation Fleet | PASS | Planner, Placement, Saga, Journal, Approval, Evidence |
| Direkter Helm-Provider | PASS | typisierte Argumente, Digestbindung, Rollbackgrenze |
| Argo CD / Flux | PASS | eindeutiger Git-Write-Owner und Reconciler-Status |
| Outbound Cluster-Agent | PASS | Lease, Epoche, HMAC, Fencing und signierte Resultate |
| Helm-Einzelcharts | PASS | neun unabhängig installierbare Charts |
| Helm-Umbrella-Chart | PASS | vendorte Dependencies und `global`-Schema-Kompatibilität |
| Proxy / Master / MQTT / MCP | BEIBEHALTEN | unveränderte Komponenten- und Kommunikationsgrenzen |
| PostgreSQL-Integration | BEIBEHALTEN | externe, explizite Datenbankgrenze |
| Automation Runtime | EXTERN GEBUNDEN | v0101-Digest; nicht eingebettet |
| Observability | PASS | Metriken, Regeln, Dashboard und Collector |

## Authority

Fleet plant, genehmigt, deployt und beobachtet. Kubernetes verwaltet die
Workloads. Master koordiniert Registry, Relay, Mirror und Audit. Proxy bildet
die Client-/RBAC-Grenze. MQTT transportiert Nachrichten. Der owning Worker
behält SEM, Safety, Hardware-I/O und Aktuatorautorität. Der Patch ändert diese
Aufteilung nicht.

## Offene Zielabnahme

Die normative Abnahme in
`KUBERNETES_TARGET_ACCEPTANCE_DE.md` bleibt für reale Cluster verpflichtend.
Insbesondere sind produktive Images, Registry, PKI, CNI, Storage,
PostgreSQL-Backup/Restore, GitOps-Reconciler und Failure-Injection nicht durch
ein lokales Release-Paket ersetzbar.

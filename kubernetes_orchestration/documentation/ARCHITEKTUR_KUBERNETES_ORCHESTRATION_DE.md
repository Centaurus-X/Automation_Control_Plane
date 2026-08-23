# Architektur des Kubernetes Orchestration Point

## Systemgrenzen

```text
Web UI / fleetctl
        |
Fleet API + Planner + Placement + Journal + Evidence
        |
Direct Helm | GitOps Handoff | Outbound Cluster Agent
        |
Kubernetes API und namespace-scoped Ressourcen
        |
MQTT / Master / Proxy / MCP / Soft-RT-Worker

Separate Grenze: automation_system_v0101.zip
Bevorzugter Hardware-/RT-Pfad: Linux/systemd
```

Der Fleet-Dienst ist Management Plane. Er ist weder Bestandteil des lokalen
Regelpfads noch Authority für Node-Shards, SEM, Safety oder Hardware-I/O.

## Datenfluss

1. Inventory registriert Cluster, Failure Domain, Kontext, Namespace, Site,
   Welle, Capabilities, Execution Mode und genau einen Write Owner.
2. Desired State wählt explizite Targets oder einen deterministischen Selector.
3. Der Katalog bindet Charts, Descriptoren, Timeouts und Dependencies.
4. Die Policy erzwingt Approval, verbietet automatisches DB-Rollback und
   Kubernetes-Hard-RT.
5. Der Planner erzeugt einen kanonischen Plan und dessen SHA-256-ID.
6. Eine vom Planer verschiedene Identität bindet Approval exakt an diese ID.
7. Jede weitere Welle benötigt bei aktivierter Policy eine eigene Freigabe.
8. Der Executor journalisiert Discover, Target Validation, Render, Diff,
   Preflight, Backup, Apply, Wait und Verify als Saga-Schritte und Evidence.
9. Collector und UI lesen direkte oder vom Agenten signierte Live-Zustände zurück.

## Komponenten-DAG

| Komponente | harte Deployment-Dependencies | weiche Runtime-Dependencies |
|---|---|---|
| PostgreSQL-Integration | keine | keine |
| MQTT | keine | keine |
| Master | PostgreSQL, MQTT | keine |
| Proxy | PostgreSQL, MQTT | Master |
| MCP Controller | Master, MQTT | Master |
| automation_system | keine | MQTT, Master |
| Fleet | keine | keine |
| Cluster Agent | keine | keine |
| Observability | keine | Master, MQTT, Proxy |

Weiche Dependencies blockieren Standalone-Szenarien nicht. Harte Dependencies
müssen im selben Desired State aktiviert sein.

Das externe v0101-Archiv bleibt unverändert und definiert seine Kubernetes-
fähigen Identitäten selbst. Deshalb bindet der additive Layer die Profile
`worker_simulation`/`standalone` an `site-main/fn-01` und `worker_fn02` an
`site-main/fn-02`. Eine andere Site-/Node-Identität erfordert ein eigenständig
versioniertes Runtime-Archiv; sie wird nicht durch eine wirkungslose
Kubernetes-Umgebungsvariable simuliert.

## Write Ownership

Pro Target gilt exakt ein Write Owner. `helm` mutiert direkt oder über den
outbound-only Agenten. `gitops_argocd` und `gitops_flux` schreiben ausschließlich
in den gebundenen Git-Worktree; die jeweilige Reconciler-Resource muss anschließend
`Synced/Healthy` beziehungsweise `Ready` melden. `observe_only` wird überwacht,
kann aber nicht in einen Deploymentplan aufgenommen werden. Ein Owner-Wechsel
ist kein stilles Umschalten, sondern ein separat zu genehmigender Inventarwechsel.

## Outbound Cluster Agent

Der Agent öffnet keinen eingehenden Dienst. Zwei oder mehr Replikate konkurrieren
per namespaced `coordination.k8s.io/v1` Lease; nur der lokale Leader pollt die
Fleet API. Die Fleet API vergibt zusätzlich eine monotone Lease-Epoche. Tasks,
Ergebnisse und Heartbeats sind per-Target HMAC-signiert und an Plan-ID,
Step-ID, Idempotency-Key sowie Fencing-Token gebunden. Eine alte Replik oder
abgelaufene Epoche kann keinen Task abschließen.

## Persistenz und Rollback

- Master, Proxy, MCP, Worker und Fleet besitzen RWO-PVCs mit
  `helm.sh/resource-policy: keep`.
- MQTT verwendet ein StatefulSet mit persistentem Datenvolume.
- RWO-Workloads sind Single Replica und verwenden `Recreate`, um Multi-Attach-
  Deadlocks zu vermeiden.
- Migrationen laufen vor Install/Upgrade als Hook Jobs.
- PostgreSQL-Daten und Schemata werden nicht automatisch zurückgerollt.
- Ein initialer Install ohne frühere Helm-Revision kann nicht als automatischer
  Rollback ausgegeben werden und führt fail-closed zu manueller Recovery.

## Beobachtung

Der Collector liest ausschließlich gelabelte Ressourcen. Agent-Targets liefern
dieselbe normalisierte Beobachtung signiert über ihren ausgehenden Kanal. Die Healthbewertung
prüft unter anderem Pod-Ready, Deployment-/StatefulSet-Replikate, DaemonSet-
Scheduling, Job-Erfolg und PVC-Bindung. Leere, ungültige und nicht erreichbare
Targets werden getrennt erkannt. `/metrics` exportiert Fleet- und Planstatus.

# Runbook: Cluster-Agenten und GitOps

## Agent offline

1. In der UI Target, Holder, Lease-Epoche und letzten Beobachtungszeitpunkt prüfen.
2. Im Zielnamespace Deployment, Pods und Lease lesen; keine Lease manuell löschen,
   solange ein gesunder Holder aktiv ist.
3. DNS-/HTTPS-Egress zur Fleet API, Kubernetes-API-Egress und Token-Secret prüfen.
4. Stimmen Target-ID, Registry-Eintrag und Agent-Konfiguration nicht exakt
   überein, Konfiguration korrigieren; Token niemals in Logs kopieren.
5. Nach Lease-Ablauf muss genau eine Replik eine höhere zentrale Epoche erhalten.
   Ein Resultat der alten Epoche muss abgelehnt werden.
6. Erst wenn die UI `ONLINE` und eine aktuelle signierte Beobachtung zeigt, die
   betroffene Rollout-Welle freigeben.

## Agent-Task fehlgeschlagen

1. Planstatus und Evidence-Bundle exportieren.
2. `step_id`, `fencing_token`, `idempotency_key` und Agent-Epoche vergleichen.
3. Bei `rbac_denied` die namespaced Role gegen den konkreten Chartvertrag prüfen;
   keine ClusterRole als Schnellkorrektur anlegen.
4. Bei Timeout keine parallele Direktmutation starten. Agent und Lease
   stabilisieren und denselben unveränderten Plan idempotent fortsetzen.
5. Bei Datenbankkomponenten Backup-ID und manuelles Recovery-Verfahren prüfen.

## GitOps dirty oder Push fehlgeschlagen

1. Den gebundenen Worktree und Branch prüfen; fremde Änderungen nicht automatisch
   verwerfen.
2. Ownership-Konflikt klären, Worktree kontrolliert bereinigen und einen neuen
   Plan erzeugen, falls sich Inventory oder Desired State geändert haben.
3. Remote-/Branch-Protection und Git-Identität prüfen.
4. Ein fehlgeschlagener Push ist kein erfolgreicher Apply und darf nicht durch
   direkten Helm-Apply auf demselben Target umgangen werden.

## Reconciler nicht Ready

- Argo CD muss gleichzeitig `Synced` und `Healthy` melden.
- Flux muss eine `Ready=True`-Condition und eine zum aktuellen
  `metadata.generation` passende `observedGeneration` melden.
- Reconciliation-Events, Repositoryzugriff, Health Checks und das erzeugte
  `fleet-metadata.json` prüfen.
- Bei Rollback wird der tatsächlich angewendete Fleet-Commit per Git-Revert
  kompensiert; Datenbankschemata werden nicht automatisch zurückgedreht.

# Kubernetes-Zielsystem-Acceptance

Diese Checkliste ist vor Produktion verpflichtend. Sie qualifiziert die reale
Cluster-, Storage-, CNI-, Registry-, PKI- und Datenbankumgebung.

## Vorbedingungen

- unterstützte Kubernetes-Version und funktionierende API-Kompatibilität;
- Helm 3 und `kubectl` als verifizierte Binaries;
- StorageClass mit nachgewiesenem RWO-, Snapshot- und Recovery-Verhalten;
- NetworkPolicy-fähiges CNI;
- erreichbare OCI-Registry mit unveränderlichen Digests;
- externe PostgreSQL-Instanz mit TLS, Backup und Restore-Test;
- ausgestellte mTLS-Zertifikate mit den erwarteten URN-Identitäten;
- getrennte Test-, Staging- und Produktionsnamespaces.

## Statische Gates

```sh
./test_release.sh
sha256sum -c SHA256SUMS
helm lint --strict kubernetes_orchestration/deploy/helm/automation-control-plane \
  --values production-values.yaml
helm template automation \
  kubernetes_orchestration/deploy/helm/automation-control-plane \
  --values production-values.yaml > /secure/rendered.yaml
```

Gerenderte Manifeste zusätzlich mit den organisationsinternen
Schema-/Policy-Tools prüfen. Kein Image darf einen Tag ohne Digest verwenden.

## Frische Installation

1. Namespace und existierende Secrets vorbereiten.
2. Fleet-Plan in read-only erzeugen und Plan-ID archivieren.
3. RBAC-Negativtest: `pods/exec`, Secrets-Lesen und clusterweite Operationen
   müssen verweigert werden.
4. Approval dokumentieren und Mutation kurzzeitig aktivieren.
5. Welle für Welle anwenden.
6. Readiness, Metriken, Auditkette und Evidence prüfen.
7. Domain-E2E für Proxy → Master → MQTT → Worker durchführen.

## Fehler- und Recovery-Tests

- ein Cluster offline, danach Rejoin und idempotentes Resume;
- fehlerhaftes Image und fehlschlagender Migration Job;
- API-Server-Unterbrechung während Render, Apply und Verify;
- PVC Pending, Node-Ausfall und Multi-Attach-Negativtest;
- CNI-/DNS-/PostgreSQL-/MQTT-Unterbrechung;
- Fleet-Dienst-Ausfall während laufender Workloads;
- Rollback auf eine vorhandene Helm-Revision;
- initialer Install ohne Revision muss manuelle Recovery melden;
- PostgreSQL Restore aus dem dokumentierten Backup;
- Zertifikatsrotation und abgelaufenes Zertifikat;
- falscher Write Owner und fehlende harte Dependency müssen vor Apply scheitern.
- Agent-Leaderverlust, Lease-Übernahme und Ablehnung einer alten Lease-Epoche;
- manipulierte Agent-Signatur und falsch gebundene Task-/Result-Identität;
- Argo-CD/Flux nicht Ready, Git-Worktree dirty und fehlgeschlagener Push;
- Vier-Augen-Negativtest sowie fehlende Wellenfreigabe;
- Pause zwischen zwei Saga-Schritten und idempotentes Resume;
- Remote-Write-Unterbrechung ohne Einfluss auf Automation oder Rolloutzustand.

## Runtime- und Safety-Nachweise

- separaten Linux/systemd-Worker ohne Fleet/Cluster-Netz betreiben;
- laufende lokale Automation bei Fleet-, Master- und MQTT-Ausfall nachweisen;
- Safe-State bei Worker-/Hardware-I/O-Ausfall testen;
- Runtime-Archivdigest im Image, im Plan und in der Podannotation vergleichen;
- kein Kubernetes-Hard-RT-Profil akzeptieren;
- Soft-RT/Simulation nur auf ausdrücklich dafür markierten Nodes einsetzen.

## Abnahmeprotokoll

Zu archivieren sind Plan-ID, Inventory-/Policy-Digest, Image-Lock, gerenderte
Manifeste, Helm-History, Testzeiten, Cluster-/CNI-/Storage-Versionen, Evidence,
Audit-Head, DB-Backup-ID und Ergebnis jedes Negativtests. Erst ein vollständig
grünes Protokoll erlaubt die Produktionsfreigabe.

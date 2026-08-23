# Sicherheitsmodell

## Standardzustand

- Dienst bindet lokal an Loopback;
- Kubernetes-Mutationen sind deaktiviert;
- Viewer, Auditor, Operator, Approver und Admin besitzen getrennte Tokens;
- Tokenfiles müssen gruppen-/weltunzugänglich sein;
- UI-Sitzungen sind signiert, kurzlebig, HttpOnly und SameSite=Strict;
- Mutation über Browser benötigt CSRF-Token und gleiche Origin;
- keine beliebige Shell, kein SQL, kein SSH und kein `pods/exec`;
- keine Inline-Secrets in Inventory, Desired State, Katalog oder Plan;
- keine cluster-scoped RBAC-Objekte, CRDs oder Admission-Webhooks.
- Vier-Augen-Freigabe verhindert die Genehmigung durch die Plan-Erstelleridentität;
- Agenten besitzen per-Target-Token, optional gebundene mTLS-Subjects und
  monotones Lease-/Fencing;
- Evidence-Export prüft jeden Step-Digest sowie die vollständige Audit-Hashkette.

## Berechtigungen

Der in Kubernetes laufende Fleet-Dienst und jeder Cluster-Agent erhalten
ausschließlich eine Role im
Zielnamespace. Pods/Events sind read-only. Helm-verwaltete namespaced Objekte
können gelesen und geschrieben werden. Helm speichert Releasezustand in
ConfigMaps; daher besitzt der ServiceAccount keinen API-Lesezugriff auf
Kubernetes-Secrets. Der Cluster-Agent verwendet für Helm bewusst den Secret-
Storage im eigenen Namespace; sein Secret-Zugriff ist damit auf diesen Namespace
begrenzt. Kubernetes verhindert zusätzlich RBAC-Privilege-Escalation
beim Erzeugen von Roles/RoleBindings.

Ein externer Fleet-Dienst verwendet die Rechte des gewählten kubeconfig-
Kontexts. Der Preflight prüft jede für das konkrete Chart benötigte Ressource
und die Verben `get`, `list`, `create`, `update`, `patch`, `delete`.

## Supply Chain

- bestehender Releasebaum ist manifestgebunden und unverändert;
- Charts und Descriptoren gehen mit Tree-/File-Digest in jeden Plan ein;
- alle Workload-Images werden als `repository@sha256:digest` gerendert;
- Image-Bases müssen digest-gepinnt sein;
- Runtime-ZIP, Runtime-Manifeste und Tool-Binaries werden vor Build geprüft;
- Release-ZIP besitzt ein vollständiges `SHA256SUMS` und externes Sidecar.

## Container-Härtung

Alle Workloads setzen Non-root, Seccomp RuntimeDefault,
`allowPrivilegeEscalation: false`, read-only Root-Filesystem und Capability Drop
ALL. ServiceAccount-Tokens sind außer beim Fleet-Dienst deaktiviert. Flüchtige
Schreibpfade sind begrenzte `emptyDir`-/tmpfs-Volumes. Persistente Volumes werden
nur an die dafür vorgesehenen Applikationspfade gemountet.

Die strikten v0101-/MCP-MQTT-Key- und Fleet-Tokenverträge verlangen Modus `0600`.
Nicht-root Init-Container kopieren diese Dateien deshalb aus einem nur
gruppenlesbaren Kubernetes-Secret in ein speicherbasiertes `emptyDir`, setzen
den exakten Zielmodus und geben dem Hauptcontainer nur das gestagte Volume.
Die bestehenden Permission-Prüfungen der Anwendungen werden nicht gelockert.

## Trust Boundaries

Der Fleet-Dienst darf Deploymentzustand verwalten, erhält aber keine
Node-/Hardware-Authority. Worker-Entscheidungen werden nicht im Fleet-Dienst
rekonstruiert. Ein Ausfall oder eine Kompromittierung der Management Plane darf
keine neue lokale Aktuator-Authority erzeugen. Hard-RT ist ohne separate
Node-/Kernel-/HIL-Qualifikation gesperrt.

## Bekannte operative Risiken

- Ein Namespace-Writer ist eine privilegierte Rolle innerhalb dieses
  Namespaces; der Managementzugang muss separat geschützt werden.
- Die einfache Tokenauthentifizierung ist für lokalen/isolierten Betrieb. Für
  organisationsweite Exposition muss ein TLS-Reverse-Proxy mit OIDC und
  externem Identity-Lifecycle vorgeschaltet werden; die internen Plan-/Approve-/
  Execute-Rechte bleiben getrennt.
- Lokales Fencing schützt ein gemeinsames Journal. Mehrere unabhängige Fleet-
  Instanzen mit getrennten State-Verzeichnissen sind nicht zulässig.
- PostgreSQL-Migrationen können irreversibel sein; Backups und manuelle
  Recovery bleiben zwingend.

# Tiefe Diagnose und Implementierung v0027-r2

## 1. Auftrag und überprüfte Eingaben

Untersucht wurden die vollständigen bereitgestellten Releaseartefakte und der
Architekturplan:

| Eingabe | SHA-256 | Ergebnis |
|---|---|---|
| `automation_control_plane_release_v0026_r1(2).zip` | `ccddf6ed4c959bf17cad4041c68b683eccc0110702c6ae46987b220d07d2fb03` | ZIP und Sidecar gültig |
| `automation_system_v0101(8).zip` | `c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467` | ZIP, Sidecar und internes Manifest gültig |
| Architekturplan | `865c061bd73b2fddc756a91dee6c5b96058e8559f733bd0b9be10e8fdfac3509` | 2.126 Zeilen vollständig ausgewertet |

Die beiden ursprünglichen ZIPs bestanden den strukturellen ZIP-Test. Beide
mitgelieferten Sidecars stimmen exakt mit den Archiven überein.

## 2. Vollständige Bestandsanalyse

Die Baseline umfasst zusammen **780 Dateien**:

| Bereich | Dateien | Bytes | Python-Dateien | Python-Zeilen |
|---|---:|---:|---:|---:|
| Control Plane v0026-r1 | 588 | 5.061.676 | 234 | 82.130 |
| Runtime v0101 | 192 | 3.040.635 | 129 | 73.828 |
| Gesamt | 780 | 8.102.311 | 363 | 155.958 |

Zusätzlich wurden 141 Dokumentationsdateien und 135 Shellskripte klassifiziert.
Alle JSON-Dateien der Baseline waren parsebar, alle 135 Shellskripte bestanden
`bash -n`. Die AST-Prüfung bestätigte den funktionalen Codestandard: keine
benutzerdefinierten Klassen, Lambdas oder Decorators, kein `shell=True` und kein
`eval`. Ein einzelner kontrollierter `exec`-Aufruf liegt ausschließlich in
einem Proxy-Testloader der unveränderten Baseline.

Die internen Manifeste wurden vollständig nachgerechnet:

- Control Plane: 587 Manifestzeilen, Manifest-SHA-256
  `451759c6e8b1f73636e65626c06ff79dc70a1fd9b3b0e9828d7f0902c5c01240`;
- Runtime: 191 Manifestzeilen, Manifest-SHA-256
  `699c29f9cf58b1bd6bd7407e879b3b5b3f41ad978343037a1dc33a75ec25026d`.

Der maschinenlesbare Audit im fertigen Release listet jede einzelne Datei mit
Pfad, Größe, SHA-256 und Kategorie in `FILE_AUDIT_V0027_R2.json`.

## 3. Diagnose der bestehenden Architektur

### 3.1 Control Plane

Die vorhandene Control Plane besitzt bereits sauber getrennte Bausteine:

- PostgreSQL-Paket mit Migrationen, Rollen und TLS-Verträgen;
- Mosquitto-Broker mit gegenseitigem TLS und identitätsgebundenen ACLs;
- Master mit Registry, Relay, Konfigurationsprojektion und PostgreSQL-Journal;
- Proxy mit HTTPS/WSS, Authentifizierung, RBAC und Datenbankmigration;
- MCP Controller als separater, MQTT-gebundener Administrationsdienst;
- bestehende Compose-/SSH-/Rollout-Administration und umfangreiche Tests.

Diese Komponenten sind funktional und besitzen etablierte Start-, Secret- und
Persistenzverträge. Eine Veränderung innerhalb dieser Verzeichnisse hätte
unnötigen Drift und ein hohes Regressionsrisiko erzeugt. Deshalb ist die
komplette Baseline unter `control_plane_v0026_r1/` unverändert eingebettet und
wird bei jedem Release-Test byteweise gegen ihr Originalmanifest geprüft.

### 3.2 Runtime

Die Runtime ist lokal autonom. Scheduling, Automation, SEM/Safety,
Node-Authority und Hardware-I/O verbleiben dort. Das ist eine technische
Sicherheitsgrenze: Ein Ausfall des Fleet-Dienstes, des Masters, des Brokers oder
eines Clusters darf einen bereits laufenden lokalen Regelpfad nicht stoppen.

Die Runtime wird daher nicht in das neue Control-Plane-ZIP aufgenommen. Der
Kubernetes-Build akzeptiert nur das externe v0101-Archiv mit dem exakt
gebundenen Archivdigest und prüft zusätzlich Pfadsicherheit, Symlinkfreiheit,
Größenlimits, Version und jede interne Manifestdatei. Linux/systemd bleibt der
bevorzugte Produktionsmodus für Hardware und Echtzeit. Kubernetes erlaubt nur
`container_simulation` und `kubernetes_soft_rt`.

Die Codeanalyse zeigte außerdem, dass v0101 die Identität aus den vorhandenen
Profilen und nicht aus beliebigen Site-/Node-Umgebungsvariablen liest. Deshalb
bindet der neue Layer `worker_simulation`/`standalone` an `site-main/fn-01` und
`worker_fn02` an `site-main/fn-02`. Abweichungen scheitern bereits im Planner
und im Helm-Schema; das bestehende Runtime-Archiv wird dafür nicht verändert.

### 3.3 Wesentliche Integrationsrisiken und ihre Behandlung

| Risiko | Diagnose | Umsetzung |
|---|---|---|
| Drift im Bestand | Änderungen könnten bewährte Verträge brechen | unverändertes, manifestgebundenes Baseline-Verzeichnis |
| lineare Deploy-Reihenfolge | Standalone-Komponenten würden unnötig gekoppelt | getypter Dependency-DAG mit harter Fehlermeldung bei fehlender Dependency |
| mehrere Writer | Helm/GitOps/Fleet könnten gegeneinander arbeiten | genau ein `write_owner` pro Target; Observe-only ist nicht mutierbar |
| ungebundene Artefakte | Tags und veränderte Charts wären nicht reproduzierbar | Image-, Chart-, Descriptor-, Inventory-, Policy- und Desired-Digests im Plan |
| Geheimnisse im Desired State | UI/Plan/Audit könnten Secrets leaken | ausschließlich Secret-Referenzen; rekursiver Leak-Scanner |
| unkontrolliertes UI | Terminal/SQL/`kubectl exec` wäre eine neue Angriffsfläche | ausschließlich typisierte Desired-State-Operationen; kein Terminal-Endpunkt |
| Datenbank-Rollback | Schema-Rückrollen ist nicht allgemein sicher | externe DB, Migration Jobs, kein automatisches DB-Rollback |
| RWO-PVC und RollingUpdate | Multi-Attach kann Rollouts blockieren | `Recreate` für Single-Replica-Workloads mit RWO-State |
| Migration vor NetworkPolicy | Pre-install Hook könnte ungeschützt laufen | eigene vorgezogene Hook-NetworkPolicy |
| überbreite MQTT-ACL | Wildcard könnte Masterrechte erweitern | exakte Rekonstruktion der bestehenden Topic-Rechte |
| Hard-RT in Kubernetes | Scheduler-/CNI-/Kubelet-Grenzen sind unqualifiziert | Schema- und Policy-Gate lehnen Hard-RT ab |
| Nicht-root Secret-Modi | direkte Secret-Volumes erfüllen die vorhandenen `0600`-Prüfungen nicht zuverlässig | flüchtiges nicht-root Staging für Worker-Key und Fleet-Token, ohne Gate-Lockerung |

## 4. Implementierter Orchestration Point

Der neue Layer liegt ausschließlich unter `kubernetes_orchestration/` und kann
unabhängig gestartet, aktualisiert und beendet werden.

### 4.1 Planner

Der Planner ist eine reine Funktion:

```text
Inventory v2 + Release Catalog + Component Descriptors
+ Desired State v1 + Policy Snapshot + Chart Trees
= immutable Execution Plan
```

Er enthält keine Zeitstempel oder Zufallswerte. Targets werden stabil nach
Welle, Site, Cluster und Target sortiert. Komponenten folgen einem stabilen
topologischen DAG. Jede effektive Target-Konfiguration wird gehasht; die
`siteId` wird an die Site des Targets gebunden. Fehlende harte Dependencies,
unbekannte Targets, Observe-only-Mutationen, Placeholder-Digests, Inline-Secrets
und nicht qualifizierte RT-Profile werden vor der Ausführung abgewiesen.

### 4.2 Executor und Kubernetes-Provider

Jeder Plan durchläuft pro Komponente und Target:

```text
preflight -> render -> apply -> wait_ready -> verify
```

Die Aufrufe sind Argumentlisten an `subprocess.run`; eine Shell wird nicht
verwendet. Vor dem Rollout werden Chart-Drift, Clientzugriff, Namespacezugriff,
Helm-Lint und die tatsächlich benötigten Kubernetes-Rechte geprüft. Helm setzt
mit `--atomic`, `--wait`, begrenzter History und Timeout um. Anschließend werden
Workload-Rollouts und der Zustand von Pods, Controllern, Jobs und PVCs geprüft.

SQLite läuft mit WAL und `synchronous=FULL`. Schritte sind über
Idempotency-Keys gebunden. Pro Target wird ein monotones lokales Fencing-Token
geführt und bei jedem Claim gegen den aktuellen Wert geprüft. Evidence-Dateien
sind atomar geschrieben und selbst gehasht. Der Audit-Log bindet Vorgängerhash,
Ereignis, Payload und Mikrosekundenzeit in einer verifizierbaren Hashkette.

### 4.3 Web-UI und API

Die UI ist ein eigenständiger Dienst und keine Erweiterung des Domain-Proxys.
Sie bietet:

- Environment- und Multi-Cluster-Übersicht;
- live beobachtete Kubernetes-Ressourcen und Readiness;
- Desired-State-Editor;
- deterministische Planvorschau mit exakter Plan-ID;
- Approval, asynchrones Apply, Live-Status und kontrollierten Rollback;
- Planjournal und Audit-Hashkette.

Der Server startet read-only. Mutationen benötigen zusätzlich
`FLEET_ALLOW_MUTATIONS=1`. Browser-Sitzungen sind HMAC-signiert, HttpOnly,
SameSite=Strict und CSRF-/Origin-gebunden. CSP, Frame-Blockade und MIME-Schutz
sind aktiv. Bearer-Token bleiben für CLI/Automation verfügbar.

### 4.4 Helm und Images

Die Charts sind einzeln nutzbar; das Umbrella-Chart enthält offline vendored
Dependencies. Kein Chart erzeugt CRDs, Admission-Webhooks, ClusterRoles oder
ClusterRoleBindings. Der in-cluster Fleet-Dienst verwendet namespace-scoped
RBAC und den Helm-ConfigMap-Treiber, sodass er keine Kubernetes-Secrets über die
API lesen muss. Applikations-Secrets werden ausschließlich als vorhandene
Secret-Namen referenziert.

Master, Proxy und MCP Controller werden aus den unveränderten v0026-Quellen
gebaut. Die additiven Proxy-/MCP-Dockerfiles ersetzen lediglich bewegliche
Python-Basetags durch verpflichtende Digest-Referenzen; Anwendungscode wird
nicht verändert. Der Build erzeugt nach Push ein Image-Lock mit Registry-
Manifestdigests.

## 5. Verifikation

In der Buildumgebung wurden erfolgreich ausgeführt:

- vollständige Manifest- und Byteidentitätsprüfung der Control Plane;
- alle 54 Tests der separaten Runtime v0101;
- Runtime-Archiv-, ZIP-Sicherheits- und internes Manifest-Gate;
- JSON-, AST-, JavaScript- und Shell-Syntaxprüfungen;
- Planner-, Dependency-, Secret-, RT-, Ownership- und Digest-Tests;
- Journal-, Approval-, Idempotency-, Fencing- und Auditketten-Tests;
- Kubernetes-/Helm-Providervertrag mit typisierten Fake-Binaries;
- asynchroner WSGI-Rollout inklusive Live-Status;
- vollständiger Standalone-Service-Lifecycle;
- Release-Manifest, File Audit und reproduzierbarer ZIP-Build.

Die ursprüngliche Control-Plane-Static-Suite lief bis zu den Master-Tests grün;
der Proxy-Abschnitt benötigt lokal `argon2`, und die offizielle Hülle verlangt
Docker. Diese Werkzeuge sowie Helm, `kubectl` und ein echter Kubernetes-Cluster
standen in der Buildumgebung nicht zur Verfügung. Deshalb werden keine
erfundenen Live-Cluster-Ergebnisse behauptet. Die verpflichtenden Zielsystem-
Gates sind vollständig in `KUBERNETES_TARGET_ACCEPTANCE_DE.md` beschrieben.

## 6. Ergebnis und verbleibende Betriebsgrenzen

Der additive Code ist releasefähig und fail-closed. Eine konkrete Produktion
ist erst nach folgenden installationsspezifischen Nachweisen freigegeben:

1. Images bauen, pushen und deren Registry-Digests in Desired State übernehmen;
2. echte PKI-/Secret-Objekte und eine TLS-gesicherte externe PostgreSQL-Instanz
   bereitstellen;
3. Helm-Lint/Template und RBAC-Negativtests mit der Zielversion ausführen;
4. frischen Cluster, N-1-Upgrade, Migration-Fehler, PVC-Ausfall und Rollback-
   Recovery testen;
5. Domain-E2E und Worker-Safe-State unter Cluster-/Fleet-Ausfall nachweisen.

Diese Gates sind Zielumgebungsqualifikation, keine nachträgliche Änderung am
Releasecode.

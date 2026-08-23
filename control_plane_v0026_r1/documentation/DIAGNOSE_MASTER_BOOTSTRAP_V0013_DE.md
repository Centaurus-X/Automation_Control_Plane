# Diagnose: Master-Bootstrap und Node-Synchronisierung in v0013

Stand: 16. Juli 2026

Ergänzung vom 17. Juli 2026: Die anschließend auf dem Zielhost beobachtete
Stage-8-Aktivierungskollision und ihre Korrektur in `v0013-r2` sind vollständig
in `DIAGNOSE_MASTER_STAGE8_ACTIVATION_V0013_R2_DE.md` dokumentiert.

## 1. Beobachtete Fehlerkette

Der fehlerhafte v0012-Lauf zeigte gleichzeitig:

- einen als gesund gemeldeten Master und funktionierende MQTTS-Verbindungen,
- `global_revision=0`, keinen globalen Fingerprint und `scope_count=0`,
- einen online sichtbaren Worker `fn-01`, der in `PENDING` blieb,
- keinen Node-Shard-Rollout und deshalb einen Abbruch des E2E-Tests vor WSS
  und Aktoroperation.

Damit waren Modbus, Admin-Login und die grundlegende TLS-Verbindung in diesem
Lauf nicht die Ursache. Der Worker erreichte den Broker und der Master sah
seine Nachrichten, besaß aber keine autoritative Konfiguration, gegen die er
die Worker-Identität und ihren Node-Scope prüfen konnte.

## 2. Technische Ursache

Der v0012-Bootstrap wurde nur direkt beim Prozessstart versucht. Wenn der erste
Lease-Versuch `standby` ergab, aktivierte ein späterer Leader-Übergang lediglich
den Worker-MQTT-Ingress. Er erzeugte beziehungsweise lud die Master-v2-
Konfiguration nicht erneut. Zusätzlich konnte `/health/ready` ohne globale
Revision, Fingerprint oder Node-Scope trotzdem `ready=true` liefern.

Die Auto-Admission arbeitet absichtlich fail-closed. `_configured_worker()`
liest die autoritative Konfiguration. Bei leerer Konfiguration ist `fn-01`
nicht konfiguriert; ein gültiger brokergebundener, nicht-retainierter mTLS-
Heartbeat darf dann keine Freigabe erzwingen. Registrierungen aktualisierten
somit nur den sichtbaren Node-Datensatz und `last_seen_at`, während der Status
korrekt `PENDING` blieb.

## 3. Korrektur in v0013

Der Master führt vor jeder Aktivierung des Leader-Worker-Ingresses jetzt eine
idempotente Autoritätsprüfung durch:

1. aktuellen PostgreSQL-Konfigurationsstand lesen;
2. einen passenden Heap wiederverwenden oder den vollständigen Zustand laden;
3. bei leerem Store die geprüfte `initial_config_v2.json` atomar als Revision 1
   materialisieren;
4. globale Revision und Fingerprint validieren;
5. alle für aktivierte Worker erforderlichen `node:*`-Scopes validieren;
6. erst danach den Worker-Ingress öffnen.

Ein Standby lädt einen inzwischen neueren dauerhaften Zustand nach. Dadurch
übernimmt ein später promovierter Master keinen veralteten Heap. Fehlschläge
bleiben sichtbar in `runtime.bootstrap` und setzen `config_ready=false`.

Die Readiness verlangt nun gleichzeitig:

- laufenden Prozess,
- erreichbare Datenbank,
- erforderliche MQTT-Verbindungen,
- `global_revision >= 1`,
- einen zum globalen Config-Inhalt passenden Fingerprint,
- gültige Node-Scope-Heads für alle aktivierten Worker.

`test_running_all.sh` ruft zusätzlich den semantischen
`master_node/test_runtime_state.sh`-Probe auf. Ein leerer Heap kann damit nicht
mehr als erfolgreicher Fresh-Rollout durchgehen.

## 4. Node-Sync ist unabhängig von Cross-Site

Ja: Die normale Synchronisierung muss ohne Cross-Site funktionieren. v0013
erzwingt diese Trennung ausdrücklich:

- Für jeden aktivierten Worker wird immer `node:<node-id>` materialisiert.
- Ein `cross:<node-id>.default` entsteht nur, wenn aktive Cross-Regeln für den
  Owner vorhanden sind.
- Der v0082-Basispfad ist `distributed.config.apply` (Node Full).
- Fehlende Cross-Capabilities blockieren oder verzögern ausschließlich den
  betreffenden Cross-Rollout; der Node-Rollout wird weiterhin gestaged.
- Readiness verlangt Cross-Scopes nur, wenn der Betreiber sie explizit über
  `MASTER_EXPECTED_CROSS_SCOPES` als Testziel vorgibt.

Damit kann ein Worker seine lokale Revision zunächst unabhängig besitzen und
anschließend den autoritativen Node-Shard vom Master übernehmen. Cross-Site ist
eine zusätzliche, getrennt aktivierbare Replikations- und Ausführungsgrenze.

## 5. Regressionsnachweise

Neue Tests decken ab:

- Start als Standby und spätere Leader-Promotion mit Seed;
- Reload eines neueren PostgreSQL-Zustands vor Leader-Aktivierung;
- fail-closed Verhalten bei unvollständigem Seed;
- Materialisierung ausschließlich von Node-Scopes bei deaktiviertem Cross;
- v0082-Node-Full trotz fehlender Cross-Runtime-Capability;
- gleichzeitige Node-/Cross-Rollouts, bei denen Cross retryt, Node aber sendet;
- Ablehnung der früheren falschen Readiness mit Revision 0 und leerem Heap.

Zusätzlich wurden die getrennt gelieferte Worker-v0082-Codebasis und ihre
MQTT-, Distributed-Runtime-, Config-Sync-, Simulation-, Cross-Containment- und
Identity-Preflight-Tests gegen den Vertrag geprüft.

## 6. Nicht veränderte Grenzen

- Der Worker bleibt ein getrenntes Paket und wird nativ mit
  `AUTOMATION_PROFILE=worker_simulation ./run_worker.sh` gestartet.
- Der aktive virtuelle Controller des fn-01-Lab-Shards ist ausdrücklich auf
  den bekannten nativen Simulator-Endpunkt `192.168.0.3:5020` festgelegt.
  Damit überschreibt ein erfolgreicher Master-Sync den funktionierenden
  Worker-Endpunkt nicht mit einem nur in einem Docker-Netz auflösbaren Alias.
- WSS wird im externen E2E-Test erst nach erfolgreicher Worker-Admission und
  Node-Konvergenz erreicht. Ein Abbruch in Schritt 3 ist daher kein WSS-Test.
- Die vollständige siebenstufige Online-Scope-Migration bleibt `BLOCKED`; ihre
  unsicheren Topologieänderungen werden weiter fail-closed abgelehnt.

# Tiefendiagnose und Korrekturbericht v0014

> Hinweis für R13: Dieses Dokument beschreibt den historischen v0014-Stand.
> Das ausführbare Paket liegt weiterhin im unveränderten R8 beziehungsweise im
> lokalen `__OLD__`-Archiv, ist aber nicht Bestandteil des aktiven R13-Releases.

Stand: 17. Juli 2026  
Release: `automation_control_plane_release_v0014` / `0.14.0` / `v0014-r1`

## Ergebnis

Der ursprüngliche E2E-Abbruch wurde bis zur verursachenden Konfigurationskante
lokalisiert: `deploy_test_lab.sh` setzte den effektiven MQTT-Paketgrenzwert des
Masters auf 8192 Byte, obwohl Master-Basiskonfiguration und Broker 4 MiB und der
Worker v0082 1 MiB erlauben. Der reale fn-01-Node-Shard passt in Worker und
Broker, nicht aber in den überschriebenen Master-Grenzwert. Die Planung brach
deshalb vor dem Outbox-Eintrag und vor MQTT mit `packet_limit` ab.

v0014 setzt den Lab-Default auf `4194304`, misst für Fehlerdiagnosen die
tatsächlich serialisierte äußere `distributed.config.apply`-Hülle und bewahrt
den fail-closed-Fall für wirklich zu große Pakete. Der reale Fixture-Test
ermittelt 19.804 Byte kanonische Node-Konfiguration und 20.722 Byte äußere
Hülle: 8192 schlägt erwartungsgemäß fehl, 4 MiB besteht.

Die Vollanalyse fand fünf weitere voneinander unabhängige Release-Risiken, die
minimal-invasiv korrigiert wurden:

1. Ein abgelehnter Leader-Role-Handler ließ den Cluster-Orchestrator trotzdem
   Leader-Wartung ausführen. Wartung und Recovery laufen nun nur bei
   `authority_services_ready=true`.
2. Eine frische Control-Plane-Datenbank begann erneut bei Authority-Term 1.
   Ein Worker mit persistiertem höheren Term konnte neue Rollouts deshalb als
   stale ablehnen. Der Term erhält jetzt beim Fresh-Start einen zeitbasierten
   monotonen Floor; bestehende DB-Terme bleiben maßgeblich und steigen weiter.
3. Rollout-IDs bestanden nur aus Term, Scope und Revision. Nach einem Fresh-
   Deploy konnte dieselbe ID für anderen Inhalt wiederverwendet werden. Der
   Ziel-Fingerprint ist nun Teil der stabilen Rollout-ID.
4. Bereits terminale Operationsergebnisse wurden als Duplicate akzeptiert,
   bevor Quelle, Term, Revision und Resultat geprüft waren. Alle Fences werden
   nun zuerst validiert; nur ein inhaltsidentisches Ergebnis ist idempotent.
   Events und Outbox-Korrelation stammen ausschließlich aus dem persistierten
   Datensatz.
5. Asynchrone Worker-ACKs konnten in einem kleinen Zeitfenster nach Ablauf des
   Leader-Leases noch Outbox-, Operations- oder Rolloutzustand ändern. Die
   Mutationen prüfen den Lease-/Token-/Term-Fence nun erneut in ihrer eigenen
   PostgreSQL-Transaktion.
6. Der Archiv-Builder erkannte `backups`, aber nicht die tatsächlich benutzten
   Runtime-Verzeichnisse namens `backup`. Singular und Plural werden nun
   abgewiesen und durch einen Sentinel-Regressionsfall geprüft.

## Kausalkette des ursprünglichen E2E-Fehlers

```text
Lab-Override 8192 Byte
  -> effective_limit = min(8192, Worker 1048576)
  -> signierte Full-Hülle > effective_limit
  -> Rollout retry_wait / packet_limit, attempts=0
  -> kein Outbox-Datensatz und kein MQTT-Kommando
  -> kein Worker-ACK und kein Replica-Commit
  -> Worker bleibt DEGRADED
  -> E2E stoppt korrekt vor Aktor-/Modbus-Schreiben
```

Der Fehler lag nicht bei Modbus, TLS, Clock-Skew oder einem korrupten
PostgreSQL-Snapshot. Der bestätigte Modbus-Zielendpunkt bleibt
`192.168.0.3:5020`.

## Geänderte Codepfade

| Bereich | Minimaler Patch | Absicherung |
| --- | --- | --- |
| Lab-Deployment | Paketdefault 8192 -> 4194304 | reale Fixture-Hüllengröße und Negativgrenze |
| Rollout-Planung | reale Größen- und Grenzdetails im Fehler | Master-Unit-Test für 8 KiB und 4 MiB |
| Rollout-Identität | Fingerprint in stabiler Message-ID | Retry-Stabilität und Payload-Eindeutigkeit |
| Authority | Fresh-Term-Floor, DB-Term bleibt obere Grenze | Fresh-DB-Fencing-Test |
| Leader-Promotion | Wartung nur nach akzeptiertem Role-Handler | Thread-Regressionsfall ohne Publish/Expiry |
| Operation Result | Source/Term/Revision/Result vor Duplicate | fünf negative Fencing-Szenarien |
| MQTT ACL | Worker darf gezielt Master-Sensor-Facts lesen | positive mTLS-ACL-Probe für beide Worker |
| Worker-Readiness | echte Node-Revision/Fingerprint-Konvergenz | laufendes SQL-Gate und Diagnose-Skript |
| E2E | direkt unter `integration_test/e2e_v0014` | Offline-Selbsttest, Checksummen, Shell/AST |
| E2E-Sicherheit | Restore im inneren `finally` | Erfolg-/Fehler-Selbsttests |
| Modbus-Grenze | erwarteten Host/Port fail-closed vergleichen | negativer Endpoint-Selbsttest |
| Packaging | `backup`/`backups` und Transients ablehnen | Sentinel plus entpackter Archiv-Neutest |

## Architekturprüfung

- Der Master bleibt reine Config-, Registry-, Routing- und
  Koordinationsautorität. Er enthält keine RuleEngine, FSM, Automation,
  Controller- oder Fieldbus-Ausführung.
- PostgreSQL bleibt durable Wahrheit. Der Heap ist nur ein atomar ersetzter
  Read-Snapshot.
- Ein PostgreSQL-Lease, Authority-Term und Lease-Token fencen den aktiven
  Master. Die v0014-Ergänzung verhindert jede Leader-Wartung, solange der
  zugehörige Service-Start nicht bestätigt ist.
- Worker kommunizieren ausschließlich über mTLS-MQTT und erhalten nur ihren
  Node-Shard sowie optional einen capability-gestützten Cross-Shard.
- Node-Konvergenz hängt nicht von Cross-Site-Unterstützung ab. Ein v0082-Worker
  ohne Cross-Runtime kann den Node-Full-Shard korrekt anwenden.
- Hardware schreibt ausschließlich der Owner-Worker. Der Master erzeugt und
  korreliert Intents, führt aber keine Hardwareoperation selbst aus.
- Control Plane und Worker bleiben getrennte Releasepakete. Dieses Archiv
  enthält weder Worker-ZIP noch Worker-Dockerfile, Worker-Entrypoint oder
  Worker-Lifecycle.

Das Entity-/Scope-Schema ist gegenwärtig bewusst Single-Site: mehrere
Config-v2-Tabellen besitzen keinen `site_id`-Schlüssel. Das Paket konfiguriert
genau `site_main`. Ein Multi-Site-Betrieb in derselben Datenbank ist daher
nicht freigegeben und würde eine eigene Schema-Migration benötigen.

Die vollständige verteilte Online-Verschiebung ausführbarer Entities
`staged -> READY -> activate -> retire` bleibt eine dokumentierte
Spezifikationsgrenze. Unsichere Moves/Removals werden fail-closed abgelehnt.

## Integrierter E2E v0014

Der Test prüft in dieser Reihenfolge:

1. HTTPS-Readiness und Proxy-MQTT-Verbindung,
2. authentifizierte Admin-Session,
3. `APPROVED`, online, `ACTIVE` und exakte Node-Shard-Konvergenz,
4. Master-Konfiguration und eindeutige Hardware-Ownership,
5. erwarteten Modbus-Endpoint `192.168.0.3:5020`,
6. authentifizierten WSS-Live-Stream,
7. Sensor-Liveereignis vor dem Schreiben,
8. Proxy -> Master-Outbox -> Worker -> Modbus-Aktoroperation,
9. terminalen API-Zustand, WSS-Resultat und Hardware-Readback,
10. Sensor-Liveereignis nach dem Schreiben und bestätigten Restore.

Der E2E verlässt sich nicht allein auf ein WSS-Event: der persistierte
Operation-API-Zustand ist die Wahrheit, WSS ist eine zusätzliche
Delivery-Assertion. Nach jedem begonnenen Primärversuch wird der konfigurierte
Restore vor Collector-/Session-Cleanup versucht. Ein Restore-Fehler lässt
einen sonst erfolgreichen Test fehlschlagen; bei einem bereits bestehenden
Primärfehler bleibt dieser erhalten und der Restore-Fehler wird zusätzlich
protokolliert.

Aufruf nach Control-Plane-Rollout, Worker-Start und Admin-Anlage:

```bash
export AUTOMATION_ADMIN_PASSWORD='ein-passwort-mit-mindestens-14-zeichen'
./rollout.sh test-e2e
```

Bei einem Konvergenzfehler liefert `./diagnose_worker_sync.sh fn-01` Scope,
Registry-Anker, Rolloutdetail und Outboxzustand ohne Daten zu verändern.

## Worker-v0082-Grenze

Der Worker wurde nicht verändert und wird nicht ausgeliefert. Die
Cross-Version-Vertragsprüfung bestätigt, dass v0082
`distributed.config.apply` und den Node-Fingerprint unterstützt; ein v0083-
Upgrade ist für den korrigierten Node-Full-Pfad nicht erforderlich.

Das separat angelieferte v0082-Eingangsarchiv besitzt bereits vor diesem
Release fünf Abweichungen in seinem eigenen `SHA256SUMS` (zwei Config-Shards,
Standardswerkzeug und zwei Systemmodule). Wegen der ausdrücklich geforderten
Unverändertheit wurde daran nichts korrigiert. Vor einer formalen
Gesamtabnahme muss der Betreiber dieses externe Paket gegen seine autorisierte
v0082-Quelle verifizieren.

## Aussagegrenze

Alle statischen, funktionalen, Paket-, Manifest- und Offline-E2E-Gates werden
vor dem ZIP-Build lokal ausgeführt. Im Erstellungsumfeld steht kein Docker-
Daemon und kein Zugriff auf die realen Hosts zur Verfügung. Daher sind der
Containerbuild, mTLS-Brokerlauf, PostgreSQL-Replikation, der externe Worker,
WSS über das reale Netz und Modbus-Hardware-Readback hier `NOT_RUN` und müssen
auf dem Zielsystem ausgeführt werden. Ein nicht ausgeführtes Real-Gate wird
nicht als PASS bezeichnet.

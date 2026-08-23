# Vollständiger Code-Audit und Patchbericht v0002

## 1. Prüfgegenstand und Methode

Geprüft wurden das vollständige Control-Plane-Release 0.1.0, die verbindliche
Code-/Architekturspezifikation, der Master-/Proxy-Implementierungsplan, der
separate Prüfbericht mit Patch-Empfehlungen, die Mosquitto-v0002-Spezifikation
und die unveränderte Worker-Codebasis als Kompatibilitätsreferenz.

Die Analyse umfasste jede Source-, Config-, SQL-, Compose-, Shell-, Test- und
Dokumentationsdatei. Automatisiert wurden Python-AST, Imports, Syntax,
Contract-Kopien, SQL-Migrationssplit, JSON/YAML, Shellsyntax, PKI, Dateirechte
und Release-Hygiene geprüft. Änderungen betreffen ausschließlich die Control
Plane; kein Worker-File wurde übernommen oder verändert.

## 2. Architektur in einem Satz

Der Proxy ist die Access Plane, Mosquitto die technische mTLS-/ACL-Trust-
Boundary, der Master die einzige aktive fachliche Authority, PostgreSQL die
persistente Fencing-/State-Wahrheit und der unveränderte Worker bleibt alleiniger
Hardware-/Feldbus-Owner.

```text
Web/API --HTTPS/WSS--> Proxy --MQTTS--> Mosquitto --MQTTS--> Master
                                              |                 |
                                              +----> Worker      +--> PostgreSQL
                                                      |          Proxy --> PostgreSQL
                                                      +--> Modbus/Hardware
```

PostgreSQL befindet sich im internen Netz `automation_database`. Nur Master und
Proxy sind dort Teilnehmer. Broker, Worker und Webclients besitzen weder
DB-Zugang noch DB-Credentials. Das getrennte Netz `automation_control` verbindet
Broker, Master und Proxy; Worker greifen nur über den veröffentlichten
MQTTS-Port 8883 zu.

## 3. Master-Node – Funktionsanalyse

### Composition und Lifecycle

`master_main.py` lädt die JSON-Konfiguration, installiert Signalhandler und
startet `master_runtime`. Der Runtime-Composition-Root erzeugt ausschließlich
Context-/Capability-Dictionaries und verdrahtet Store, Authority, Registry,
Ownership, Operationen, Worker-Kompatibilitätsadapter, Control-MQTT, globale
Automation, Orchestrator und Metrics. Der Start ist gestuft; bei Fehlern werden
bereits gestartete Komponenten rückwärts gestoppt und der DB-Pool geschlossen.

### Authority und Config

`authority_store_postgresql.py` sperrt die Site-Authority in serialisierbaren
Transaktionen. Leader-ID, Lease-Token, Ablauf und monotoner Authority-Term
fencen konkurrierende Writes. `config_authority.py` verlangt `base_revision`,
erlaubt nur geschlossene Pfade, validiert die vollständige Kopie und schreibt
Snapshot, Commit, Change-Journal und Rollout atomar. Rollback erzeugt eine neue
immutable Revision; der Revisionszähler bewegt sich nie rückwärts.

### Registry und Ownership

`node_registry.py` trennt Registrierung, Admission, Heartbeat sowie Desired/
Observed State. Unbekannte Worker werden nicht automatisch aktiv. Ownership
bindet Controller an Site, Node, Authority-Term, Config-Revision, Lease-Token
und Ablauf. Fehlende Heartbeats, Maintenance oder Quarantäne lösen Leases.

### Operationen und Hardwaregrenze

`operation_management.py` persistiert vor dem Publish eine deterministische,
idempotente Operation. Ziel, Principal, Owner, Term, Revision und Expiry werden
gebunden. `operation_store_postgresql.py` erzwingt erlaubte Statusübergänge.
`CONFIRMED` wird nur für ein Resultat des gespeicherten Owners mit Hardware-
Readback vergeben. Ein Authority-Wechsel markiert unsichere alte Dispatch-
Fenster konservativ als `UNCERTAIN`, statt einen möglicherweise doppelten
Hardware-Write zu wiederholen.

### Worker-Kompatibilität und Automation

`legacy_worker_mqtt_interface.py` implementiert den vorhandenen
`automation.mqtt.v1`-/`automation.distributed.v1`-Vertrag, ohne Hardwarecode zu
importieren. `coordinator_service.py` übersetzt Registrierung, Heartbeat,
Config- und Actuator-Nachrichten in die neuen Stores. Globale Rule-/FSM-/
Automation-Funktionen verwenden nur hardwarefreie Bibliothekskopien, begrenzte
Queues und einen kleinen Executor. Nur der DB-gefencete Leader startet Worker-
MQTT und globale Berechnung; ein Standby stoppt beide.

### Interfaces

- Control-MQTT akzeptiert nur geschlossene Topics/Envelopes zugelassener Proxies.
- Retained, abgelaufene, falsch adressierte oder inkonsistente Commands werden
  verworfen.
- Metrics/Health veröffentlichen secret-freie Zustände auf Port 9090.
- Die Admin-Schnittstelle kennt nur registrierte Domain-Commands, keine Shell.

## 4. Proxy-Gateway – Funktionsanalyse

### Composition und HTTPS/WSS

`gateway_main.py` validiert HTTPS-TLS, startet die Background-Capabilities und
betreibt einen einzelnen begrenzten Uvicorn/ASGI-Prozess. `https_interface.py`
erzwingt UTF-8/JSON, Bodylimits, CSRF für Cookie-Mutationen, Origin-Allowlist,
Security Header und feste Routen. Benutzer können niemals MQTT-Topics oder
Datei-/Shellpfade frei wählen.

### Identität und Autorisierung

`auth_management.py` nutzt Argon2id plus externe Pepper. Session- und API-
Tokenwerte werden zufällig erzeugt und nur als HMAC-Hash gespeichert. Die
Entscheidung in `access_policy.py` kombiniert Permission, Site-Scope,
Resource-Scope und fachliche Policy; Default ist Deny. Login-Limits,
Sessionrotation, CSRF und Audit sind serverseitig.

### Command Gateway

`command_gateway.py` bildet erlaubte HTTP-Domainaktionen auf feste
`automation.control.v1`-Events ab. `mqtt_gateway_interface.py` persistiert das
vollständige Envelope vor dem Publish, wiederholt dieselbe Message-ID und prüft
ACK-Topic, Responder, Causation, Correlation, Eventtyp und Principal gegen den
gespeicherten Request. Idempotency ist benutzer-/site-/eventbezogen.

### WSS und Backpressure

`event_stream.py` und `websocket_interface.py` erlauben nur logische Resource-
Filter. Jede Queue ist begrenzt. Telemetrie darf zum neuesten Wert koalesziert
werden; kritische ACK-/Operation-/Rollout-Events führen bei vollem Puffer zur
kontrollierten Trennung. Der DB-Replay-Cache unterstützt `last_event_id`.

## 5. PostgreSQL – Funktionsanalyse

PostgreSQL 18.4 läuft als eigenständiges Primary-/Standby-Paket. TLS-only HBA,
SCRAM, getrennte Schemas, App-/Migratorrollen und ein internes Netz bilden die
Zugriffsgrenze. Migrationen sind versioniert und idempotent. Constraints,
Foreign Keys, Row Locks und append-only Trigger schützen Authority, Revisionen,
Operationsevents und Audit. Retention-Jobs begrenzen volatile Historien.

Primary- und Standby-Daten, TLS-Runtime und WAL-Archiv sind getrennte benannte
Volumes. `container-entrypoint.sh` repariert beim Start Owner/Mode für `/backup`
und `/archive`. Streaming-Replikation, physischer Slot, Container-Recreate,
logischer Dump, Base Backup, `pg_verifybackup` und isolierter Restore besitzen
ausführbare Tests. Promotion verlangt die explizite Bestätigung, dass der alte
Primary extern gefencet wurde.

## 6. Mosquitto – Funktionsanalyse

Das neue Paket nutzt den offiziellen Mosquitto-2.1.2-Alpine-Container. Die
Konfiguration besitzt genau einen Listener auf 8883, verlangt mTLS, deaktiviert
Anonymous, begrenzt QoS auf 1, Paket-/Queue-/Connectiongrößen und persistiert in
einem benannten Volume. Der Zertifikat-CN wird zum ACL-Benutzer.

Die Test-PKI erzeugt getrennte Server-, Health-, Master-, Proxy- und Worker-
Schlüssel. Sie ist markiert, idempotent, wird nie ungefragt über unmarkiertes
Material geschrieben und ist aus dem Release ausgeschlossen. Produktion
installiert nur extern gelieferte Dateien und verweigert Testmarker.

Die ACL bildet nicht nur Rollen, sondern konkrete Site-/Instanzpfade ab. Der
Health-Client hat ausschließlich `$SYS/broker/uptime`. Der Live-Test enthält
positive und negative Authorization-Probes sowie Restart/Persistenz und prüft,
dass 1883 geschlossen bleibt.

## 7. Deployment- und Updateverhalten

`rollout.sh` ist der zentrale Einstieg. `test` und `production` prüfen zuerst
Werkzeuge und Releaseinvarianten. Ein atomarer Lock verhindert parallele
Rollouts. Anschließend sucht der Lifecycle über Compose-Projektlabels nach
vorhandenen Containern, stoppt sie mit Grace Period, killt verbleibende Prozesse
und entfernt die alten Container. Volumes werden nie automatisch gelöscht.

Der feste Startgraph lautet Broker → PostgreSQL → Master → Proxy. Jedes
Einzelpaket verwendet zusätzlich `--force-recreate --remove-orphans`, sodass
auch direkte Deployments sicher das aktuelle Image/Config anwenden. Nach dem
Start laufen Health, Replikation, wiederholte Migrationen, DB-Transaktionsprobes
und im Lab Broker-Securitytests.

## 8. Befunde und Patchstatus

| Priorität | Befund aus 0.1.0 | Status in v0002 |
|---|---|---|
| P0 | Psycopg `transaction(isolation_level=...)` ist ungültig | behoben in Master und Proxy |
| P0 | keine echte Isolation-/Rollback-Probe | zwei Containerprobes plus je sechs Unitchecks ergänzt |
| P1 | Backup-/WAL-Pfade möglicherweise nicht schreibbar | Owner/Mode im Entrypoint plus Tests ergänzt |
| P1 | Lab deaktiviert MQTT-TLS und nutzt 1883 | vollständig auf mTLS/8883 umgestellt |
| P1 | Brokerpaket fehlt | eigenständiges Mosquitto-Paket ergänzt |
| P1 | Gesamtrollout ersetzt laufende Version nicht sicher | Lock, Stop, Kill-Fallback, Remove, Force-Recreate ergänzt |
| P1 | automatisches physisches Master-Failover | bewusst nicht als fertig behauptet |
| P1 | instanzübergreifendes Proxy-Command-Takeover | bewusst nicht als fertig behauptet |

## 9. Psycopg-P0 im Detail

Psycopg 3.3.4 akzeptiert an `Connection.transaction()` nur Savepoint- und
Rollbackoptionen, kein Isolation-Level. Die korrigierte Funktion normalisiert
den gewünschten Wert und schlägt ihn in einer geschlossenen Map nach:

```text
READ COMMITTED   -> SET TRANSACTION ISOLATION LEVEL READ COMMITTED
REPEATABLE READ  -> SET TRANSACTION ISOLATION LEVEL REPEATABLE READ
SERIALIZABLE     -> SET TRANSACTION ISOLATION LEVEL SERIALIZABLE
```

Andere Werte lösen `ValueError` aus. Der Transaction-Context wird parameterlos
geöffnet; die Isolation ist die erste Anweisung, danach folgen transaktionale
Lock-/Statement-Timeouts. Serialization Failure und Deadlock behalten den
begrenzten exponentiellen Retry. Damit sind sowohl API-Fehler als auch SQL-
Injection über den Isolationswert geschlossen.

## 10. Bewusste Grenzen und Risiken

1. Das Paket ist kein vollständig automatisches HA-System. Die DB-Lease fenced
   Master-Writes, aber Proxy/Worker routen auf die physische ID `master-01`.
   Ein `master-02` übernimmt diese Topics nicht automatisch.
2. Offene Proxy-Commands sind noch instanzgebunden; es existiert keine
   `FOR UPDATE SKIP LOCKED` Claim-Lease für einen zweiten Proxy.
3. Mosquitto ist ein einzelner persistenter Broker, kein Broker-Cluster.
4. Primary und Standby auf demselben Host dienen nur dem Lab. Produktion
   benötigt getrennte Failure Domains und externes Fencing/Orchestrierung.
5. Der unveränderte Worker reicht die authentifizierte MQTT-Peeridentität nicht
   bis zum Payload-Handler durch. Die ACL verhindert fremde Topicpfade, kann auf
   einem gemeinsam adressierten Coordinator-Commandtopic jedoch keine
   Payload-Source kryptografisch umschreiben. Vor einer Multi-Worker-
   Produktionsfreigabe ist die geplante minimale Peer-Identity-Bindung nötig.
6. Docker/LAN/Worker/Modbus konnten im Erstellungsumfeld nicht live laufen. Die
   zugehörigen Gates sind implementiert, aber müssen auf dem Zielnetz grün sein.

## 11. Releaseentscheidung

Der P0-Laufzeitblocker und die für v0002 geforderten additiven Control-Plane-
Funktionen sind implementiert. Das Paket ist ein konsistenter Release Candidate
für den vollständigen Lab-Test. Produktionsfreigabe setzt die dokumentierten
Docker-/LAN-Gates, sichere Produktions-PKI, Off-host-Backups und eine bewusste
Entscheidung zu den verbleibenden HA-/Worker-Identity-Grenzen voraus.

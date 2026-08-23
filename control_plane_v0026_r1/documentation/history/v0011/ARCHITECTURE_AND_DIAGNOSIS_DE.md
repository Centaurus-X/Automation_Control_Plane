# Architektur und vollständige Systemdiagnose v0010

## 1. Ergebnis der Bestandsanalyse

Das gelieferte Worker-Archiv enthält 119 Dateien und rund 46.000 relevante
Code-/Config-/Testzeilen. Der bestehende Worker ist kein einfacher
Modbus-Client, sondern eine ereignisgetriebene Device Plane mit zentralem
Config-Writer, Thread-Management, generischen Rule-/FSM-/Automation-Bausteinen,
Controller-Ownern, MQTT und einem persistenten Distributed-Journal.

Der Worker-Quellcode wurde für dieses Release nicht verändert. Alle 18
vorhandenen ausführbaren Tests sowie die 41 Prüfungen des Generic Data Center
liefen erfolgreich. `compileall` war ebenfalls erfolgreich.

### Auffälligkeiten im gelieferten Release

1. `SHA256SUMS` ist veraltet. Fünf gelieferte Dateien stimmen nicht mit der
   Manifestdatei überein: `app_config.yaml`, `state_event_management.py`,
   `thread_management.py`, `mqtt_interface.py` und
   `bootstrap_runtime_interface.py`. Das ZIP selbst ist strukturell intakt.
2. Die Worker-App verwendet `site-main`, die normalisierte Device-Config dagegen
   `site_main`. MQTT-Routing und Master verwenden in diesem Release einheitlich
   `site_main`.
3. MQTT und `distributed_runtime` sind im Worker-Basisprofil absichtlich
   deaktiviert. Ohne reine Konfigurationsänderung kann er sich nicht am neuen
   Master registrieren.
4. Der vorhandene Default `coordinator_node_id=fn-01` kollidiert mit dem Worker
   `fn-01`. Der externe Master muss eine eigene logische Coordinator-ID, hier
   `master-01`, erhalten.
5. Das Worker-Testprofil erreicht den Modbus-Simulator unter
   `192.168.0.3:5020`, nicht unter dem normalen Port 502.
6. Die alte Instanz `192.168.0.31` wird nicht mehr vorausgesetzt. v0003 enthält
   einen eigenen Mosquitto-2.1.2-Broker für `192.168.0.82`, ausschließlich auf
   MQTTS/8883 mit mTLS, getrennten Identitäten und geschlossener ACL.
7. Der Worker reicht die authentifizierte TLS-Peer-URI nicht bis zum
   MQTT-Payload-Handler durch. Bis zur minimalen späteren Contract-Erweiterung
   muss der Broker die Zertifikatsidentität technisch an Client-ID und Topic
   binden.
8. Der Worker erfüllt seine eigene funktionale Architektur weitgehend, enthält
   aber weiterhin geerbte Type-Annotations in einem Automation-Modul sowie
   zahlreiche sehr lange/verschachtelte Funktionen. Diese wurden bewusst nicht
   invasiv geändert. Die Master-Kopie des benötigten hardwarefreien Kerns wurde
   dagegen von verbotenen Annotationen bereinigt.

## 2. Funktionsweise des bestehenden Workers

### Start und Composition

`sync_xserver_main.py` lädt das Profil und ruft den Runtime-Bootstrap auf. Der
Bootstrap erzeugt Context-Dictionaries und verbindet die Komponenten über
kleine Capability-Dictionaries. Es gibt keine globale Master-Weblogik.

```text
Worker Main
  -> Bootstrap Runtime
  -> Generic Data Center / Config-Heap
  -> State Event Management (einziger Config-Writer)
  -> Thread Management / begrenzter Job-Pool
  -> RuleEngine / FSM / Automation
  -> Controller- und Connection-Owner
  -> Feldbus / Modbus TCP
```

### Config und State Event Management

Die normalisierte JSON-Config wird im Heap gehalten und vor Load/Save mit der
generischen Type Library geprüft. Das State Event Management ist die zentrale
Single-Writer-Grenze. Änderungen werden als Operation verarbeitet, geprüft,
atomar committed und anschließend an Verbraucher ausgerollt. Runtime-Sensorwerte
liegen getrennt von der Config in einem Value Store.

### Concurrency

Thread Management besitzt die Lifecycle- und Queue-Verantwortung. Schwere
Rule-/FSM-/Automation-Berechnungen werden als begrenzte Jobs ausgeführt.
Controller und Connections besitzen ihren jeweiligen I/O-State selbst. Dadurch
bleibt Hardware-I/O aus dem Config-Writer heraus.

### Device Plane

Der Worker ist alleiniger Hardware-Owner. Er pollt Sensoren, schreibt
Aktuatoren, führt Safety-/Revision-Prüfungen aus und bestätigt einen Remote-Write
erst nach dem konfigurierten Hardware-Readback. Das Simulationsprofil aktiviert
nur den Controller mit UUID `62671112-766c-59ed-b0ff-30a49d48f747` gegen
`192.168.0.3:5020`.

### Distributed Runtime

Der vorhandene `distributed_coordinator` kann bereits als Worker betrieben
werden. Er publiziert Registrierung, Heartbeat, Config-Proposals und
Aktuatorrouting als bestehende `automation.mqtt.v1`-Commands. Inbox, Outbox und
Aktuatorzustände werden lokal in SQLite journalisiert. Das neue Master-Paket
verwendet exakt diese Contract-Fläche und verschiebt keine Hardwarelogik.

## 3. Zielarchitektur

```text
Client / ERP
    |
    | HTTPS / WSS
    v
Proxy-Gateway (Access Plane)
    |
    | automation.control.v1 über MQTTS
    v
Mosquitto (eigenständige Trust Boundary)
    |
    v
Master-Node (Control Plane, eine aktive Authority)
    |
    | bestehendes automation.mqtt.v1 / automation.distributed.v1
    v
Worker fn-01 (Device Plane)
    |
    | Modbus TCP
    v
Testserver 192.168.0.3:5020

Proxy + Master -> internes TLS-PostgreSQL
Worker/Broker  -X-> PostgreSQL
```

### Warum vier eigenständige Pakete

- PostgreSQL hat einen eigenen Lifecycle, persistente Volumes und
  Recovery-Verfahren.
- Mosquitto authentifiziert technische Clients und erzwingt Topic-ACLs, erhält
  aber weder DB-Credentials noch Zugriff auf das Datenbanknetz.
- Der Master darf nie von Web-Session- oder Passwortlogik abhängig werden.
- Der Proxy darf nie Feldbus- oder Worker-Topics frei ansprechen.
- Die Pakete können unabhängig gebaut, migriert, gestoppt und aktualisiert
  werden.

## 4. Master-Node intern

### Authority und HA

`authority_state` ist die Fencing-Wahrheit. Eine serialisierbare Transaktion
sperrt die Site-Zeile. Nur eine Instanz mit passendem `leader_instance_id`,
`lease_token`, aktivem `lease_until` und aktuellem `authority_term` darf
schreiben. Ein neuer Leader erhöht den Term. Standbys halten nur Control-MQTT
und Read-Pfade aktiv; Worker-MQTT und globale Automation werden beim Rollenwechsel
gestartet oder gestoppt.

### Config-Authority

Ein Proposal enthält immer `base_revision`. Der Master wendet nur erlaubte
Pfade auf eine Kopie an, prüft die vollständige Worker-Config und committed
Snapshot, Commit, Change-Journal, Authority-Revision und Rollout in einer
Transaktion. Ein Rollback bewegt den Revisionszeiger nie rückwärts, sondern
erzeugt aus einem historischen Snapshot eine neue immutable Revision.

### Registry und Ownership

Unbekannte Worker werden `PENDING`. Erst explizite Admission erlaubt
Heartbeats und Ownership. Jede Controller-Lease bindet Site, Controller,
Owner-Node, Term, Config-Revision, Token und Ablaufzeit. Fehlender Heartbeat,
Maintenance oder Quarantäne lösen Ownership. Bei einem Routingwechsel wird
keine noch aktive fremde Lease überschrieben.

### Aktuatorjournal

Vor MQTT wird eine Operation journalisiert. Sie bindet Principal,
Idempotency-Key, Controller, Aktuator, Owner, Lease, Term, Revision und Expiry.
Der Worker erhält eine deterministische Operation-ID. Der Master akzeptiert ein
Resultat nur vom gespeicherten Owner mit passendem Term und passender
Operationsrevision. `CONFIRMED` kommt ausschließlich aus dem Worker-Readback.
Nach Termwechsel werden alte Dispatch-Fenster konservativ `UNCERTAIN` und nicht
automatisch als neuer Hardware-Write wiederholt.

### Globale Automation

Der Master importiert nur hardwarefreie Rule-/FSM-/Automation-Funktionen. Ein
gebundener Ingress und ein begrenzter Executor verarbeiten Remote-Sensorwerte.
Resultierende Aktuatoraufgaben laufen durch dasselbe persistente
Ownership-/Operation-/Outbox-Verfahren wie Proxy-Requests.

## 5. Proxy-Gateway intern

### Authentifizierung und Autorisierung

Passwörter werden mit Argon2id und externem Pepper gespeichert. Sessions und
API-Tokens sind zufällig, rotierbar und nur als HMAC-SHA-256-Hash in PostgreSQL
vorhanden. Jede Entscheidung kombiniert Permission, Site-Scope,
Resource-Scope und fachliche Policy. Der Master prüft Permission/Site/Resource
erneut gegen die vom mTLS-authentifizierten Proxy behauptete Principal-Struktur.

### HTTPS/WSS

JSON, UTF-8, Content-Type und Größe werden am Rand geprüft. Cookie-Mutationen
verlangen CSRF. Origin-Allowlist, HSTS, CSP, No-Sniff, Frame-, Referrer- und
Permissions-Policy sind zentral. Blockierende DB-, Argon2- und MQTT-Aufrufe
werden in einen begrenzten Threadpool ausgelagert und blockieren den ASGI-Loop
nicht.

WSS-Clients abonnieren `resource_type` und `resource_pattern`. `/`, `#` und `+`
sind verboten. Jede Queue ist begrenzt. Telemetrie wird bei Überlast auf den
neuesten Wert koalesziert; kritische ACK-/Operations-/Rollout-Events führen bei
voller Queue zur kontrollierten Trennung statt zum stillen Verlust. Der
PostgreSQL-Replay-Cache ermöglicht `last_event_id`.

### Command-Korrelation

Vor MQTT wird das vollständige Envelope persistiert. Dasselbe Message-ID wird
bis zum Ablauf sicher wiederholt. ACK-Topic, Master-Identität, Causation-ID,
Correlation-ID, Command-Typ und Principal müssen zum gespeicherten Request
passen. Idempotency ist user-/site-/eventbezogen und funktioniert damit auch
über mehrere Proxy-Instanzen.

## 6. PostgreSQL

Master und Proxy besitzen getrennte Schemas, App-Rollen und Migratorrollen.
Alle Transaktionen haben Lock-/Statement-Timeouts; Serialisierungs- und
Deadlockfehler werden begrenzt wiederholt. Config und Audit sind append-only.
Hochvolumige Heartbeat-, Inbox-/Outbox-, Replay-, Session- und Limiterdaten
haben begrenzte Retention-Jobs.

Primary und Standby verwenden TLS und SCRAM. Named Volumes überleben Container-
Recreate. Streaming-Replikation, Base Backup, WAL-Archiv, logischer Dump und
isolierter Restore-Test sind getrennte Betriebsfunktionen.

Der v0.1.0-Blocker in `run_transaction()` ist korrigiert: Psycopg 3 akzeptiert
kein `isolation_level` an `Connection.transaction()`. v0003 öffnet zuerst einen
parameterlosen Transaction-Context und setzt danach als erste SQL-Anweisung
eine ausschließlich aus einer Whitelist gewählte `SET TRANSACTION ISOLATION
LEVEL ...`-Anweisung. Dynamische SQL-Injektion über den Isolationswert ist
ausgeschlossen. Unit- und echte Containerprobes prüfen Isolation und Rollback.

Die Container-Entrypoint bereitet `/backup` sowie beim Primary `/archive`
explizit mit PostgreSQL-Owner und restriktiven Rechten vor. Damit sind die im
Prüfbericht beanstandeten Host-Bind-/WAL-Volume-Rechte deterministisch.

## 7. MQTT-Broker

Mosquitto ist ein viertes unabhängiges Compose-Projekt. Das Serverzertifikat
enthält den internen Namen `mqtt-broker`; Master und Proxy verbinden sich über
Docker-DNS und mTLS. Externe Worker erreichen denselben Listener über die
Hostadresse `192.168.0.82`, prüfen aber weiterhin den TLS-Namen `mqtt-broker`.

`use_identity_as_username` bindet den Zertifikat-CN an vier exakt definierte
ACL-Rollen. Proxy kann keine Worker-Commands senden, Worker `fn-01` kann nicht
in fremde Nodepfade schreiben, und der Health-Client darf nur Broker-Uptime
lesen. Daten liegen in einem benannten Volume; Queues, Paketgröße, Keepalive und
QoS sind begrenzt. Der Broker ist persistent, aber kein HA-Cluster.

## 8. Bekannte, bewusst nicht vorgetäuschte Grenzen

- Das aktuelle Ausführungsumfeld besitzt weder Docker noch Zugriff auf das
  private Lab-Netz. PostgreSQL-Replikation, echte MQTTS-Verbindungen und der
  Modbus-Testserver konnten hier nicht live ausgeführt werden. Dafür liegen
  ausführbare Container-/Recovery-/Integration-Skripte bei.
- `192.168.0.31` wird nicht verändert. Der neue Broker wird im Lab zusammen mit
  den übrigen Control-Plane-Diensten auf `192.168.0.82` ausgerollt.
- Vollständige Payload-zu-TLS-SAN-Bindung des unveränderten Workers ist erst
  möglich, wenn Worker-ID, source-gebundenes Topic und Payload gemeinsam
  validiert werden. Die aktuelle Broker-ACL genügt für den isolierten
  Ein-Worker-Pilot, nicht für eine Multi-Worker-Produktionsfreigabe.
- Der Worker stoppt seine lokalen Controller derzeit nicht allein aufgrund
  einer Master-Desired-State-Nachricht. Der Master verhindert neue Remote-
  Operationen durch Ownership-Entzug. Vollständiges lokales Drain benötigt die
  später vorgesehene minimale Worker-Anpassung.
- Automatischer PostgreSQL- oder Master-Failover ohne externes Fencing/
  Orchestrierung ist laut Spezifikation nicht Bestandteil des ersten Releases.
- Die Authority-Lease verhindert zwei aktive schreibende Master. Das Routing
  bleibt jedoch auf `master-01` gebunden; automatische Übernahme durch
  `master-02` ist nicht implementiert. Ebenso besitzt die Proxy-Outbox noch
  keine instanzübergreifende Claim-Lease. Diese P1-Funktionen sind deshalb kein
  behauptetes Produktions-HA-Feature.

## 9. Von v0002 übernommene Basis

- ausschließlich Control Plane geändert; Worker-Archiv und Worker-Code bleiben
  unberührt
- Psycopg-P0 zentral und identisch in Master/Proxy korrigiert
- sechs Regressionstests pro App und echte DB-Probes ergänzt
- PostgreSQL Backup-/WAL-Rechte und Force-Recreate-Lifecycle korrigiert
- eigenständiges Mosquitto-Paket mit PKI, ACL, Persistenz, Backup und Tests
- alle Labpfade auf MQTTS/8883 umgestellt
- idempotenter Root-Rollout mit Lock, geordnetem Stop, Kill-Fallback,
  Container-Replacement, festem Startgraph und abschließenden Probes
- Produktionspfad prüft Zertifikatsketten, Identitäten, SANs, Schlüsselpaare,
  Restlaufzeit und einen gepinnten Broker-Image-Digest vor dem Stop

## 10. Additiver v0003-Patchumfang

- keine Worker-, Master-Protokoll-, MQTT-ACL- oder SQL-Schemaänderung
- serverseitige Rollen-Grant-Prüfung gegen verwaltete Site und die effektive
  Permission-Teilmenge des Grantors
- API-Token-Principals werden bei jeder Verwendung mit dem aktuellen Benutzer-
  Zugriff geschnitten; aktuelle Resource-Scopes bleiben erhalten
- deploybare, normalisierte und fail-closed `PROXY_ORIGIN_ALLOWLIST`
- 15 gezielte neue Unit-/Securitychecks
- realer PostgreSQL-Security-Probe mit tatsächlichem Tokenhash, Rollenabfrage,
  Resource-Scope, Rollen-/Site-Entzug und garantierter Datenbereinigung
- v0003-Releasevalidator prüft Versionskonsistenz und Vorhandensein aller neuen
  Security-Gates

## 11. Additiver v0004-Runtime-Patch

- File-Secrets bleiben hostseitig restriktiv und werden nach jeder Erzeugung
  auf Host-UID, jeweilige Runtime-GID und `0640` normalisiert.
- Die vier Pakete enthalten den Helper jeweils selbst und bleiben separat
  ausrollbar.
- Der Broker-Healthcheck und alle positiven MQTT-Testclients verwenden
  explizite Client-IDs; die Sicherheitsvorgabe gegen leere IDs bleibt aktiv.
- Der gehärtete MQTT-Tools-Container läuft für PKI-Tests unter Host-UID/GID und
  benötigt trotz `cap_drop: ALL` keine zusätzliche Dateisystem-Capability.
- Fehlende Container werden durch `test-running` sofort gemeldet. Der
  Gesamtrollout zeigt seine acht Stufen und die konkrete Abbruchstufe an.
- Worker-Code, MQTT-Topics, Payload-Verträge und SQL-Schemata bleiben
  unverändert.

## 12. Minimal-invasiver v0005-ACL-Testpatch

- Der Zielhost bestätigte einen gesunden Mosquitto-Broker und eine korrekte
  mTLS-Identität, der Rollout stoppte jedoch in Stufe 4 mit
  `Expected MQTT authorization failure did not occur`.
- Ursache war kein nachgewiesener ACL-Durchbruch: Mosquitto verwarf den
  unzulässigen Publish brokerseitig, während das MQTT-v5-CLI den negativen
  PUBACK nicht zuverlässig als von Null verschiedenen Prozessstatus abbildete.
- Der Deny-Test verwendet nun einen autorisierten Subscriber auf einem
  eindeutigen Topic. Ein retained Readiness-Marker beweist zuerst die aktive
  Subscription; danach folgen verbotener Publish und autorisierter
  Kontroll-Publish in definierter Reihenfolge.
- Erwartet werden exakt Readiness-Marker und Kontroll-Payload. Jede tatsächliche
  Zustellung des verbotenen Payloads ist weiterhin ein harter Security-Fehler.
- ACL-Datei, Brokerkonfiguration, Zertifikatsidentitäten, Topics, Master,
  Proxy, PostgreSQL und Worker bleiben funktional unverändert.

## 13. Minimal-invasiver v0006-Proxy- und Upgrade-Patch

- Der v0005-Zielhostlauf bestätigte MQTT, PostgreSQL Primary/Standby,
  Streaming-Replikation, Master-Migration, Proxy-Migration sowie einen gesunden
  Master mit aktiven DB- und MQTT-Verbindungen.
- Der Proxy-Prozess verband sich ebenfalls erfolgreich mit PostgreSQL und MQTT,
  aber Uvicorn klassifizierte das über `functools.partial` gebundene ASGI3-
  Callable fälschlich als ASGI2. Jeder Healthcheck führte deshalb zu
  `gateway_asgi() missing 2 required positional arguments` und HTTP 500.
- `gateway_main` setzt Uvicorns Interface nun explizit auf `asgi3`. Der
  Adapter, seine Security-Header und die Konfiguration werden durch zwei neue
  Regressionchecks geprüft.
- Vor einem Versionsupgrade importiert der aggregierte Rollout ausschließlich
  allowlist-basierte Runtime-Secrets, Test-PKI und TLS-Material aus dem vorherigen
  Release. Docker-Datenvolumes selbst werden weiterhin nicht kopiert oder
  gelöscht.
- Ein vorhandenes PostgreSQL-Datenvolume ohne passendes Credential-Set ist ein
  Pre-Stop-Fehler. Widersprüchliche Zieldateien werden fail-closed abgelehnt.
- Worker, Topics, Payloads, SQL-Schemata, Broker-ACL, Master- und Proxy-
  Geschäftslogik bleiben unverändert.

## 14. Minimal-invasiver v0007-Rollout-Patch

- Die Zielhostliste zeigte weiterhin ausschließlich v0005-Images. Damit ist
  bewiesen, dass der ASGI3-Patch nicht gestartet worden war; der wiederholte
  Trace stammte aus dem alten Container und nicht aus v0006-Quellcode.
- Der v0006-Preflight war vor Stufe 1 beendet worden. Ursache war die
  Auswertung eines explizit leeren State-Quellparameters mit Bash-`:-`: Eine
  noch exportierte `CONTROL_PLANE_STATE_SOURCE` wurde dadurch ungewollt in den
  Fresh-Test übernommen.
- Explizit leer und „Parameter nicht angegeben“ sind nun getrennte Zustände.
  Der Regressionstest setzt absichtlich eine störende Umgebungsvariable und
  prüft, dass Fresh trotzdem keinen Import ausführt.
- Lab-Rollouts sind standardmäßig `--fresh`: Images werden vorab gebaut,
  danach werden ausschließlich die vier projektgelabelten Container, ihre
  benannten Volumes, die beiden Control-Plane-Netze und generierter State des
  aktuellen Releasebaums entfernt.
- `--upgrade` ist explizit, generisch und verlangt einen vorhandenen
  `CONTROL_PLANE_STATE_SOURCE`; Volumes bleiben erhalten. Produktion verlangt
  immer eine Modusangabe und für destruktives Fresh eine zweite Bestätigung.
- Die Abschlussabnahme verifiziert die tatsächlich gestarteten Image-Strings.
  Alte, aber gesunde Images können nicht mehr als neuer Rollout akzeptiert
  werden.
- Uvicorn bleibt als ASGI-Server erhalten. Ein aiohttp-Wechsel würde das
  Starlette-/ASGI-Anwendungsmodell ersetzen und wäre weder additiv noch
  minimal-invasiv.

## 15. Minimal-invasiver v0008-Recovery- und Runtime-Patch

- Der v0007-Zielhostlauf bestätigte alle fünf laufenden Container, korrekte
  Image-Versionen, Healthchecks, Streaming-Replikation, Migrationen und
  Datenbank-Sicherheitsprobes. Auch der erzwungene PostgreSQL-Recreate und das
  physische Backup einschließlich `pg_verifybackup` waren erfolgreich.
- `test_extended_all.sh` endete dennoch mit Status ungleich Null, weil der
  Container den Host-Bind-Mount `postgres_system/backup` auf UID 999 und 0700
  gesetzt hatte. Der nachfolgende hostseitige `find` durfte das Verzeichnis
  nicht traversieren. Isolierter Restore, MQTT-Backup und zweite
  Abschlussabnahme waren deshalb nicht mehr erreicht worden.
- v0008 übergibt die Deployment-UID explizit an Primary und Standby. Das
  Backup-Verzeichnis bleibt Host-eigen, erhält die schreibfähige
  `postgres`-Gruppe und Setgid. Nach erfolgreicher Verifikation normalisiert
  `backup.sh` Artefakte auf hostlesbare, nicht öffentliche Rechte und prüft
  diese Invariante sofort.
- `restore_test.sh` entdeckt Backups unabhängig von Hostrechten innerhalb des
  Primary-Containers. Der bereits manuell bestätigte Sentinel-SQL-Fix ist im
  Release verankert. Ein neues Regression-Gate prüft alle drei Pfade.
- Mosquitto erhält eine aus read-only Quellen erzeugte Runtime-Konfiguration
  mit festen Eigentümern und Rechten. Die 2.1-Deprecations werden entfernt,
  ohne Listener, mTLS, ACL, Topics oder Persistenzmodell zu verändern.
- Der Proxy bleibt auf Starlette/Uvicorn und explizitem ASGI3. Nur der von
  Uvicorn empfohlene `websockets-sansio`-Adapter ersetzt die veraltete Auswahl;
  Routing und Geschäftslogik bleiben unverändert.

## 16. Minimal-invasiver v0009 Worker-Auto-Admission-Patch

- Der Worker sendete bereits alle 15 Sekunden einen generischen, frischen
  Heartbeat auf seinem eigenen Node-Topic. Der Master abonnierte diesen Topic
  bisher nicht. Verteilte Registrierungen wurden deshalb zwar persistent als
  `PENDING` erfasst, konnten aber ohne manuelle Proxy-Aktion nie bestätigt
  werden. Der Worker erhielt keinen erfolgreichen Registrierungs-ACK und
  wechselte folglich nicht in den verteilten Heartbeat-Zustand.
- Der Master liest jetzt `nodes/+/heartbeat`. Mosquitto bindet über
  `use_identity_as_username` und eine exakte Worker-ACL das Zertifikat von
  `fn-01` an `nodes/fn-01/heartbeat`. Die Anwendung akzeptiert ausschließlich
  nicht-retained, zeitlich gültige Heartbeats, bei denen Site, Topic-Node und
  Envelope-Node übereinstimmen.
- Dieser Besitznachweis ist flüchtig. Auto-Admission erfolgt nur für einen in
  der aktuellen Authority-Konfiguration aktivierten Worker, nur auf dem
  DB-gefenceten Leader und nur bei der kanonischen Zertifikatsidentität
  `urn:automation:<site>:worker:<node>`.
- `PENDING` wird idempotent zu `APPROVED`. Bereits `DENIED` oder `QUARANTINED`
  gesetzte Nodes werden niemals automatisch reaktiviert. Nach einem
  Master-Neustart ist ein neuer Live-Heartbeat erforderlich.
- Der Worker-Code, sein MQTT-Protokoll, Feldbus-/Modbuspfade, SQL-Schema,
  Proxy-API und manuelle Admin-Freigabe bleiben unverändert.

## 17. v0010: hierarchische lokale und globale Automation-Authority

- Worker v0076 bleibt für seine Controller lokaler Feldbus-, RuleEngine-, FSM-,
  Safety- und Automation-Owner. Diese Funktion ist nicht von der Erreichbarkeit
  des Masters abhängig.
- Registrierung und Heartbeat transportieren
  `local_automation_authority=true`. Der Master persistiert die Capability und
  berechnet daraus eine controllerbezogene Policy.
- Regeln, deren Sensorabhängigkeiten und Aktorziele vollständig zu demselben
  lokal autoritativen Worker gehören, laufen am Master nur beobachtend. Regeln
  über mehrere Worker bleiben globale Master-Regeln.
- Der interne Master-Aktorrouter blockiert als zweite Sicherung jeden
  automatisch erzeugten Auftrag an einen lokal autoritativen Controller.
  Benutzeraufträge vom Proxy verwenden weiterhin den expliziten, auditierten
  Operationspfad.
- Die Authority wird nicht aus `online` oder einem kurzlebigen Ownership-Lease
  abgeleitet. Die zuletzt von einem zugelassenen Worker publizierte Capability
  bleibt bei einer Unterbrechung bestehen. Das verhindert automatisches
  Split-Brain-Failover.
- Konfiguration bleibt global revisionsgeführt. Lokale Worker-Proposals werden
  vom Master serialisiert; identische Änderungen sind No-op. Bei einer
  veralteten Basisrevision wird rebasiert und `master_wins` angewendet. Eine
  unkontrollierte Zeitstempel-LWW-Regel existiert nicht.

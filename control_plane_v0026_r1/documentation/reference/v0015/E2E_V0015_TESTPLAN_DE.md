# E2E v0015 – ausführbare Testspezifikation

Status: verbindliche Spezifikation für den **noch zu implementierenden**
Running-E2E im späteren Control-Plane-Release v0015. In diesem
Entwicklungsplan ist nur der lokale WSS-/TLS-Referenz-Smoke ausführbar; ein
Live-PASS gegen Master, Broker, Worker und Modbus wird nicht behauptet.

## 1. Testurteil und Grenzen

Der E2E v0015 muss die reale Laborkette über das neue primäre WSS-Control-Gate
verifizieren:

```text
E2E-Client
  ⇄ WSS/TLS Proxy :8443
  ⇄ Proxy Core
  ⇄ MQTTS/mTLS Mosquitto ⇄ Master Authority
  ⇄ MQTTS/mTLS Mosquitto ⇄ Worker fn-01
  ⇄ Modbus-Simulator
```

Zusätzlich simuliert ein zweiter MQTT-Protokollaktor `fn-02` den bestehenden
Workervertrag. Das beweist Cross-Worker-Routing im Standort `site_main`, aber
keinen echten zweiten Worker und keinen zweiten Standort.

## 2. Testmodi

| Modus | Zweck | Externe Änderung |
|---|---|---|
| `static` | Code-/Paket-/Schema-/Architekturgates | keine |
| `running` | Health, DB, MQTT, Migrationen, Authority | nur lesend |
| `wss-core` | Auth, Paths, Commands, Events, Replay | reversible Testoperationen |
| `wss-cross-sim` | fn-02-Protokoll und Cross-Worker | vorhandene Lab-PKI; beim ersten Fresh-Lauf dokumentierte Fixture-Inbetriebnahme, danach reversible Konfigurationsänderungen |
| `parallel-smoke` | begrenzte AsyncIO-Parallelität | nur Testclients; keine Dauerlast |

Der Ergebnisreport unterscheidet zwei Profile:

* `core`: WSS-/fn-01-Kette; Cross-Simulation ist explizit `SKIPPED`;
* `full-lab`: alle Core-Gates plus fn-01-Cross-Testlauncher und fn-02-
  Protokollsimulator; dieses Profil ist das vollständige Labor-Releasegate.

Die spätere `rollout.sh`-Schnittstelle soll einfach bleiben:

```bash
./rollout.sh test-static
./rollout.sh test-running
./rollout.sh test-e2e

E2E_ENABLE_FN02_SIMULATION=1 ./rollout.sh test-e2e
```

Der Cross-Simulationsmodus ist opt-in, solange fn-02-PKI und Cross-Fixture nur
in der Testumgebung existieren. Das Release-Gate der vollständigen
Laborabnahme aktiviert `profile=full-lab` ausdrücklich. Ein `core`-PASS darf
niemals als Cross-Worker-PASS ausgegeben werden.

## 3. Vorbedingungen

* alle Control-Plane-Services healthy;
* Master besitzt eine aktive Lease mit Term > 0;
* Proxy meldet WSS und HTTPS separat ready;
* fn-01 ist `APPROVED`, `ACTIVE`, heartbeat-fresh und exakt Node-konvergent;
* Modbus-Simulator `192.168.0.3:5020` ist erreichbar;
* WSS-CA und SAN-Name beziehungsweise SAN-IP werden vom E2E validiert;
* Testprincipal hat nur die für den Test nötigen Scopes;
* Ausgangsconfig, Scope-Fingerprints und Aktuatorwerte werden vor jeder
  Mutation gesichert;
* kein anderer Operator verändert während des Mutationsfensters dieselbe
  Configrevision.
* für `full-lab` ist der externe fn-01-Cross-Testlauncher verfügbar, darf den
  normalen Worker kontrolliert stoppen/starten und hat eine validierte
  Recovery-Datei; fehlt diese Freigabe, ist der Cross-Modus `BLOCKED`.
* `full-lab` läuft ausschließlich in einer dedizierten Lab-Instanz, in der die
  fn-02-Fixture-Identität niemals an einen realen Worker provisioniert wird.
  Der Runner hält einen exklusiven, laufzeitweiten fn-02-Simulator-Lock und
  weist vor dem MQTT-Connect über Master-Registry **und** Broker-Sessionstatus
  nach, dass keine andere fn-02-Session und kein frischer fn-02-Heartbeat
  existiert. Zulässig ist nur die bekannte stale/offline Fixture-Baseline;
  fehlende Broker-Introspektion, ein Lockkonflikt oder ein aktiver fn-02 führt
  zu `BLOCKED`.

Fehlt eine Vorbedingung, endet der Test als `BLOCKED`, nicht als scheinbares
fachliches `FAILED`. Ein fehlgeschlagener Restore ist hingegen immer `FAILED`
und wird deutlich ausgegeben.

## 4. Testfallmatrix

| ID | Test | Erwartung |
|---|---|---|
| ST-001 | kein `WebSocketRoute` im Proxy | PASS |
| ST-002 | kein `websockets-sansio` und kein Uvicorn-WSS | PASS |
| ST-003 | `websockets.serve` bindet genau 8443 | PASS |
| ST-004 | Uvicorn `ws="none"`, HTTPS 8444 | PASS |
| ST-005 | JSON-Schemas und SVG parsebar | PASS |
| ST-006 | Worker-v0082-Referenzhash unverändert | PASS |
| TLS-001 | CA + SAN + Hostname auf 8443 | PASS |
| TLS-002 | CA + SAN + Hostname auf 8444 | PASS |
| TLS-003 | falsche CA/Hostname | Handshake FAIL |
| SEC-001 | fehlende Auth | HTTP 401/Handshake FAIL |
| SEC-002 | falscher Bearer | FAIL |
| SEC-003 | falsche oder fehlende Origin | FAIL |
| SEC-004 | Session-Mutation ohne/falsches CSRF | bestehender v0014-Code `authorization_denied` |
| SEC-005 | fehlender Action-Scope | `authorization_denied` |
| SEC-006 | Token/Secret im Queryparameter | Pfad FAIL; nichts im Log |
| SEC-007 | Token/Session nach Handshake widerrufen | spätestens im Revalidierungsintervall Close 1008; kein weiteres Event/Command |
| SEC-008 | API-Token mit explizit leeren Rechten/Sites | bleibt leer; erbt niemals versehentlich Principal-Rechte |
| SEC-009 | eindeutige Bearer-/Cookie-/CSRF-/Passwort-/Token-Canaries | in keinem Proxy-, Protocol- oder E2E-Log auffindbar |
| PTH-001 | `/ws/v1/events` | akzeptiert Subscription |
| PTH-002 | `/ws/v1/control` | sendet `connection.ready` |
| PTH-003 | unbekannter Pfad/Query | 404/400 |
| PTH-004 | Upgrade auf Uvicorn :8444 | nicht als WSS akzeptiert |
| RD-001 | `nodes.list` über WSS | fn-01 sichtbar und online |
| RD-002 | `config.get` über WSS | Revision/Fingerprint korrekt |
| RD-003 | `runtime.status` über WSS | Proxy/MQTT/Master bereit |
| EVT-001 | Sensorfilter | nur autorisierte Sensoren |
| EVT-001a | leere Filterliste | alle, aber ausschließlich autorisierte Site-/Resource-Events |
| EVT-002 | Node-/Operationfilter | nur passende Resources |
| EVT-003 | Filterupdate | atomar wirksam |
| EVT-004 | Reconnect + Replaycursor | kritische Lücke geschlossen |
| EVT-004a | mehr Records als eine Replayseite | Pagination bis zur atomaren High-Watermark |
| EVT-004b | Cursor vor Retention-Floor | `replay.resync_required`; niemals still zu Live springen |
| EVT-005 | langsamer Telemetrieclient | Coalescing statt unbounded Wachstum |
| EVT-006 | kritischer Queueoverflow | Close 1013, Replay möglich |
| EVT-007 | Filterupdate bei parallel geplantem Event | alte Subscription-Generation wird verworfen |
| CFG-001 | reversible `config.propose` | Master-Commit + Rollout |
| CFG-002 | fn-01-Konvergenz | Revision und Fingerprint exakt |
| CFG-003 | identischer Idempotency-Retry | identisches Ergebnis, kein Doppelcommit |
| CFG-004 | Key mit anderem Payload | bestehender v0014-Code `conflict` |
| CFG-005 | stale Base-Revision | `stale_revision` |
| CFG-006 | Config-Restore | kanonische Nutzlast/Fingerprints semantisch wie Ausgang; Revision monoton höher |
| ACT-001 | Ausgangswert lesen; `actuator.request = not original` über WSS | echte Zustandsänderung, `CONFIRMED` nach Readback |
| ACT-002 | Resultat über `/events` | gleiche Operation-ID und Resultat |
| ACT-003 | Sensor vor/nach Schaltung | beide live und owner-validiert |
| ACT-004 | Restore auf exakt `original` über WSS | `CONFIRMED` nach Readback |
| ADM-001 | Node-Term 0, Master-Term aktiv | Admission-Skript PASS |
| ADM-002 | isoliertes SQL-/Shell-Fixture mit abgelaufener Master-Lease | Admission-Skript FAIL; aktive `site_main`-Lease bleibt unberührt |
| SIM-001 | fn-02 mTLS register/heartbeat | zugelassen, fresh |
| SIM-002a | initialer Full-Apply | exakter Scope-Fingerprint |
| SIM-002b | kleine Folgemutation erzwingt Patch | Base-/Target-Revision und Fingerprint exakt |
| SIM-002c | isolierte vorhandene Chunk-/Orchestrator-Tests mit 64-KiB Paketlimit und 192-KiB Hard-Chunklimit | deterministisches In-Memory Encode/Reassemble sowie Planung; **keine** Restart-Persistenz und kein Running-E2E-PASS-Kriterium |
| XWK-000 | externer fn-01-Testlauncher, Overlay-Preflight, Cross-Runtime und Hash vor/nach | Cross aktiv; Worker-v0082-Paket/App-Config unverändert; andernfalls `BLOCKED`/Restore-`FAILED` |
| XWK-001 | fn-01 Cross-Shard | exakt konvergent |
| XWK-002 | Sensor 3 ausschließlich über erlaubten fn-01-Modbus-Simulatorpfad kontrolliert unter/über Threshold 75 setzen | genau ein gefenceter Intent |
| XWK-003 | fn-02 simuliertes Execute/Readback | `simulation=true`, `CONFIRMED` |
| XWK-004 | isolierter Master-Protokolltest: falscher Term/Epoch | fail-closed; keine gefälschte fn-01-Identität im Live-Broker |
| XWK-005 | isolierter Master-Protokolltest: doppelter Intent | dedupliziert |
| XWK-006 | isolierter Master-Protokolltest: abgelaufene TTL | nicht ausgeführt |
| XWK-007 | Cross-Restore/Cleanup | Config und fn-01-Testlauncher restauriert; fn-02 `MAINTENANCE`, Heartbeat stale/observed offline; dokumentierter Lab-Fixture-Residualzustand |
| PAR-001 | 128 parallele Clients | keine Leaks/Timeouts außerhalb Grenzwert |
| PAR-002 | temporäre Proxy-Instanz mit `max_connections=2` | dritter Client erhält kontrolliertes 503 statt Ressourcenwachstum |
| XST-001 | `site_other` Envelope/Topic gegen `site_main` Fixture | mTLS-/ACL-/Envelope-Isolation lehnt ab; kein vollständiger Cross-Site-Funktionsnachweis |
| HTTP-001 | sekundärer REST-Lesepfad | Parität mit WSS |
| HTTP-002 | sekundärer REST-Mutationspfad | weiterhin funktionsfähig und gleich geschützt |

## 5. fn-02-Protokollsimulator

### 5.1 Erlaubte Fähigkeiten

Der Simulator ist ein kurzlebiger eigener Testprozess. Nach einem erfolgreichen
v0014-Test-Lab-Rollout verwendet er die dort provisionierte feste fn-02-
Lab-mTLS-Identität und minimalen festen Topics; nur Prozess und MQTT-Session
sind kurzlebig. Er implementiert nur die bereits existierenden
`automation.distributed.v1`-Envelopes:

* Registration/Capabilities;
* Heartbeat mit Replica-Revisions/Fingerprints;
* Full- und Patch-Apply, jeweils nur wenn der Test den passenden
  deterministischen Trigger tatsächlich erzeugt;
* ACK/NACK mit stabiler Message-ID;
* Actuator-Execute und deterministischer In-Memory-Readback;
* Inbox-Deduplizierung;
* Coordinator-Term, Execution-Epoch, Target, Revision und TTL;
* `simulation=true` in Capability, Log und Resultat.

### 5.2 Verbotene Abkürzungen

Der Simulator darf nicht:

* direkt in PostgreSQL Node-/Rollout-/Operationstabellen schreiben;
* Master-Autorisierung umgehen;
* interne Proxy-Funktionen importieren;
* beliebige MQTT-Topics abonnieren;
* ein Hardware-Readback vortäuschen, ohne es als Simulation zu markieren;
* den realen Worker-v0082-Ordner ändern;
* nach Testende einen aktiven Simulatorprozess, frische Heartbeats oder
  Config-Overlays zurücklassen. Die feste Lab-PKI bleibt provisioniert, wird
  jedoch von keinem Prozess mehr genutzt. Der dokumentierte
  stale/`MAINTENANCE` Fixture- und Audit-Datensatz darf und muss mangels
  Delete-/Unapprove-API bestehen bleiben.

### 5.3 Deterministischer Zustand

Der Simulator besitzt nur einen kleinen Dictionary-Zustand:

```json
{
  "node_id": "fn-02",
  "simulation": true,
  "authority_term": 0,
  "execution_epoch": 0,
  "node_scope_revision": 0,
  "node_fingerprint": "",
  "actuators": {}
}
```

Scope-Payloads werden mit derselben kanonischen Fingerprint-Funktion wie der
Protokollvertrag geprüft. Die Logik wird nicht lokal neu erfunden; das spätere
Testmodul muss die freigegebene gemeinsame Canonicalization verwenden.

### 5.4 fn-01-Cross-Testlauncher ohne Worker-Paketänderung

Der freigegebene Worker v0082 hat im Profil `worker_simulation`
`cross_runtime.enabled=false`; `run_worker.sh` besitzt keinen externen
`config_path`-Schalter. Ein Master-Config-Overlay allein kann die lokale
Cross-Runtime deshalb **nicht** aktivieren. Ohne die folgende Vorbedingung ist
der Cross-Modus korrekt als `BLOCKED` zu beenden.

Der spätere E2E liefert einen externen `worker_cross_test_launcher.py`, aber
keinen geänderten Worker. Der Launcher:

1. verifiziert den freigegebenen Worker-v0082-Archivhash und den Hash der
   originalen `_config/application/app_config.yaml`;
2. stoppt den normalen fn-01-Prozess kontrolliert und legt eine Recovery-Datei
   mit Startkommando, PID-/Servicezustand und Originalhashen an;
3. kopiert `app_config.yaml` in ein privates temporäres Verzeichnis und setzt
   ausschließlich
   `profiles.worker_simulation.cross_runtime.enabled=true`;
4. ruft `validate_worker_environment(config_path=<overlay>,
   active_profile="worker_simulation")` auf und prüft zusätzlich in der
   aufgelösten Config `cross_runtime.enabled is True`;
5. startet v0082 über einen externen kleinen Wrapper. Dieser setzt vor dem
   Runtime-Import
   `bootstrap_system_interface.CONTEXT["options"]["default_config_path"]`
   auf den absoluten Overlaypfad und ruft danach
   `src.sync_xserver_main.main()` auf;
6. wartet auf fn-01 Node- **und** Cross-Shard-Konvergenz, bevor das
   Korrelationsfenster beginnt;
7. stoppt im `finally` den Testprozess, entfernt den privaten Overlay, startet
   den normalen Worker mit seinem ursprünglichen Kommando und verifiziert
   erneut App-Config- und Worker-Archivhash.

Der Wrapper verändert keine Worker-Quelldatei und kein ausgeliefertes
Worker-Paket. Normale Laufzeitjournale und vom Master replizierte Configdaten
bleiben davon getrennte, erwartete Laufzeitdaten. Schlägt Stop, Preflight,
Start oder Restore fehl, lautet das Ergebnis `FAILED` beziehungsweise vor der
ersten Mutation `BLOCKED`; der E2E darf nicht heimlich mit deaktivierter
Cross-Runtime weiterlaufen.

### 5.5 Deterministischer Cross-Trigger

Der Cross-Test schreibt niemals direkt in Master- oder Worker-Datenbanken. Er
setzt den Rohwert für Sensor 3 über den dafür freigegebenen
Modbus-Simulator-Testkanal zunächst auf 74. Diese Baseline darf abhängig vom
Originalwert selbst einen fallenden/`else_do`-Pfad auslösen. Der Test wartet
deshalb, bis alle daraus entstandenen Operationen terminal und Outbox/Queues
ruhig sind, und erfasst **danach** einen neuen Event-High-Watermark. Erst das
anschließende kontrollierte Setzen auf 76 öffnet das Messfenster und darf dort
genau einen steigenden Cross-Intent erzeugen. Der Test korreliert Source-Event,
Cross-Scope/-Epoch, Remote-Intent und fn-02-Resultat. Danach wird das
Messfenster geschlossen; im `finally` wird der ursprüngliche Simulatorwert
außerhalb dieses Fensters wiederhergestellt und jede dabei mögliche
Restore-/`else_do`-Operation separat bis zum Terminalzustand drainiert.

Die exakte v0014-Lab-Fixture lautet:

| Element | Wert |
|---|---|
| Source-Node | `fn-01` |
| Source-Controller | `62671112-766c-59ed-b0ff-30a49d48f747` |
| Source-Sensor | ID `3`, `read_holding_registers`, Adresse `3`, Unit `1` |
| Simulator-Encoding | ein unsigned 16-bit Holding-Register, kein Scaling-Profil |
| Simulator-Endpunkt | `192.168.0.3:5020` |
| Trigger | Rule `3`, `>= 75.0`; schreibe zuerst `74`, danach `76` |
| Cross-Scope | `cross:fn-01.default`, Owner `fn-01` |
| Target-Node | `fn-02` |
| Target-Controller | `d11032fa-33db-56de-a4ed-7e16cca3ded7` |
| Target-Aktuator | ID `4`, `write_register`, Adresse `544` |
| Target-State | State `14`; Rule `3` setzt `value_id` von `2` auf `1` |

Der Test liest Register 3 zuerst, speichert den ursprünglichen unsigned-
16-bit-Wert und verwendet einen versionskompatiblen `pymodbus`-Client mit
`device_id=1` beziehungsweise dem von der installierten Version verlangten
äquivalenten Unit-Keyword. Ein PASS erfordert die korrelierte Kette
Source-Event → Cross-Fact → gefenceter Intent → fn-02-Simulationsreadback →
WSS-Operationsergebnis. Genau ein steigender Thresholdwechsel darf **im neu
gesetzten Messfenster** gezählt werden; Baseline und Restore sind getrennte,
vollständig korrelierte Phasen.

### 5.6 fn-02-PKI, ACL und deterministische Rolloutmodi

Der v0014-Test-Lab-Rollout erzeugt die feste Lab-Identität
`urn:automation:site_main:worker:fn-02`, das Clientmaterial unter
`mqtt_broker/test-pki/clients/worker-fn-02/` und exakt auf fn-02 begrenzte
ACL-Zeilen. Diese Dateien sind keine statische Produktions-PKI im
Quellarchiv. Der Simulator darf ausschließlich diese nach dem Lab-Rollout
provisionierte **Lab-Fixture** nutzen. Er erzeugt keine zweite CA, ändert
keine Produktions-ACL und kopiert keinen CA-Private-Key. Vor Start werden
URI-SAN, ClientAuth-EKU, Chain und die erwarteten fn-02-Topics geprüft.

Diese Identität darf niemals parallel von einem zweiten Prozess oder einem
realen fn-02 genutzt werden. Noch vor dem Zertifikatsladen erwirbt der Runner
atomar einen exklusiven Lock für `site_main/fn-02` (beispielsweise einen über
die gesamte Testdauer auf derselben PostgreSQL-Verbindung gehaltenen Advisory
Lock). Danach prüft er erneut:

1. kein fremder E2E-Runner hält den Lock;
2. die Master-Registry zeigt keinen frischen Heartbeat und keinen aktiven
   beobachteten fn-02-Zustand, ausgenommen die dokumentierte stale/offline
   Fixture-Baseline;
3. der Broker führt weder die feste fn-02-Client-ID noch eine andere Session
   mit der fn-02-Zertifikatsidentität;
4. während des Laufs tritt kein Duplicate-Client-Disconnect oder unerwarteter
   Sessionwechsel auf.

Kann der Broker diese Exklusivität nicht autoritativ ausweisen, startet der
Simulator nicht und der Test endet `BLOCKED`. Der Runner darf einen aktiven
Client niemals durch einen Connect mit derselben Client-ID verdrängen.

Der Simulator meldet `automation.distributed.v2`, `contracts_version=1.1`,
`node_full`, `node_patch`, `cross_runtime`, `cross_full`, `cross_patch`,
`cross_fact_consumer` und `remote_actuator`. Die Capability
`chunk_reassembly` bleibt bis zu einem eigenen Persist-/Restart-Test `false`;
die verbotene Capability `master_automation_execution` bleibt ebenfalls
`false`.

Die Rolloutmodi werden so erzwungen:

1. Revisionsloser Simulatorstart (`node/cross revision=0`) → Full-Apply.
2. Danach wird ausschließlich das zur Laufzeit anhand `node_id=fn-02`
   gefundene Feld `nodes.<index>.description` um eine kurze Run-ID ergänzt →
   Patch mit exakter Base-/Targetrevision; Restore ist ein weiterer Patch.
3. Chunk ist kein Live-E2E-Trick: Die unveränderten v0014-Tests
   `test_chunk_transport_v2.py` und `test_rollout_orchestrator_v2.py` laufen
   zusätzlich mit dem dort definierten 65536-Byte-Limit. Sie beweisen nur
   deterministisches In-Memory Encode/Reassemble, Fehlererkennung und
   Orchestratorplanung, nicht Persistenz über Simulator-/Prozessrestart.
   Maßgebliche Hard-Limits bleiben 4 MiB Packet, 192 KiB Chunk, 32 MiB Payload
   und maximal 512 Chunks. Erst ein eigener Encode → Persist → Restart →
   Reassemble-Test erlaubt Capability und Running-Gate.

Die vorhandene Broker-ACL erlaubt fn-02 seine festen Status-, Heartbeat-,
Registration-, Config-State-, Event-, Command- und ACK-Pfade. Negative
Fencingtests dürfen diese Identität nicht als fn-01 ausgeben. Deshalb laufen
falscher Epoch/Term, Duplicate und TTL isoliert gegen Master-Core und
Contracts; der Live-Broker erhält keine test-only Wildcard-ACL.

### 5.7 Grenze der Cross-Site-Simulation

fn-01 und fn-02 gehören beide zu `site_main`; dies ist ein echter
Cross-**Worker**-, aber kein zweiter Standort. `XST-001` simuliert nur die
negative Standortisolation mit `site_other` und beweist, dass mTLS/ACL und
Envelope-Validator fail-closed bleiben. Ein positiver Cross-Site-Datenpfad
benötigt eine zweite Site-Authority beziehungsweise eine versionierte
Federation und liegt außerhalb des minimal-invasiven Proxy-v0015-Umbaus. Der
Ergebnisreport muss deshalb weiterhin `cross_site=false` ausgeben.

## 6. Cleanup-Transaktion

Vor jeder Mutation schreibt der Test eine Recovery-Datei mit:

* Testlauf-ID und Startzeit;
* Ausgangsrevisionen und Fingerprints;
* ursprünglichem Aktuatorwert;
* Config-Snapshot beziehungsweise inverser, validierter Änderung;
* verwendeter fn-02-Fixture-Identität, vorherigem Registry-/Desired-Zustand
  und Kennzeichen, ob die Fixture in diesem Lauf erstmals registriert wurde;
* Cross-Overlay-ID und Kennung des gehaltenen fn-02-Simulator-Locks.

Die Datei wird mit `open(..., O_CREAT|O_EXCL, 0600)` in einem privaten
Testverzeichnis erzeugt, atomar ersetzt und enthält niemals Private Keys,
Passwörter, Bearer-, Session-, CSRF- oder API-Tokenklartext.

Cleanup läuft in umgekehrter Reihenfolge:

1. Aktuator auf Ausgangswert und Readback bestätigen;
2. Cross-Config restaurieren und fn-01-Konvergenz bestätigen;
3. fn-02 über den Master auf den gültigen gewünschten Zustand
   `MAINTENANCE` setzen;
4. Simulator stoppen und frischen Heartbeat auslaufen lassen;
5. bei bereits vorhandener Fixture den vorherigen gültigen Desired-Zustand
   wiederherstellen. Wurde fn-02 nach einem Fresh-Rollout erstmals
   registriert/approved, bleibt mangels offizieller Delete-/Unapprove-API
   bewusst eine `APPROVED`/`MAINTENANCE`, heartbeat-stale und observed-offline
   Lab-Registryzeile zurück; der Report markiert
   `fixture_bootstrap_performed=true` und `expected_residual=true`;
6. fn-02-Lab-Fixture-Zertifikat unverändert und mit Modus 0600/0644 im
   geschützten PKI-Verzeichnis belassen; die gemeinsam von fn-01 genutzte
   Test-CA wird beim Cleanup **nicht** rotiert. Simulator stoppen plus
   auslaufender Heartbeat entzieht die aktive Laufzeit;
7. den fn-02-Simulator-Lock erst nach gestopptem Simulator, geschlossener
   Broker-Session und bestätigtem stale Heartbeat freigeben;
8. Recovery-Datei erst nach bestätigtem Endzustand und erfolgreicher
   Lockfreigabe als abgeschlossen markieren.

Ein Signalhandler darf Cleanup anfordern, aber die eigentliche asynchrone
Bereinigung bleibt im Haupt-Lifecycle. Ein zweiter Lauf erkennt eine offene
Recovery-Datei und führt zuerst Recovery statt neuer Mutationen aus.

Der erste `full-lab`-Lauf nach `--fresh` ist damit eine dokumentierte
einmalige Fixture-Kommissionierung und kein exakt zustandsloser Test. Alle
Folgeläufe sind gegenüber dieser stale/`MAINTENANCE`-Lab-Baseline reversibel.
Direktes Löschen aus PostgreSQL ist ausdrücklich kein Cleanup.

## 7. Ergebnisformat

Der E2E schreibt Log und JSON-Zusammenfassung. Minimalfelder:

```json
{
  "version": "0.15.0-e2e.0",
  "profile": "full-lab",
  "status": "PASSED",
  "tls_verification": true,
  "transport": "wss-primary",
  "wss_server": "websockets",
  "https_websocket_enabled": false,
  "worker_fn01": {"real": true, "converged": true},
  "worker_fn02": {
    "simulation": true,
    "enabled": true,
    "executed": true,
    "converged": true,
    "exclusive_lock_acquired": true,
    "preexisting_active_session": false,
    "broker_session_exclusive": true,
    "exclusive_lock_released": true,
    "fixture_bootstrap_performed": false,
    "expected_residual": true
  },
  "cross_worker": {
    "enabled": true,
    "executed": true,
    "skipped": false,
    "tested": true,
    "cross_site": false
  },
  "config_restore_confirmed": true,
  "actuator_restore_confirmed": true,
  "checks": {}
}
```

Ein PASS ist nur erlaubt, wenn alle aktivierten Checks `true` sind und beide
Restores bestätigt wurden. Configrevisionen bleiben monoton; „restauriert“
bedeutet semantisch identische kanonische Nutzlast/Fingerprints der betroffenen
Scopes bei einer neuen höheren Revision. Audit-, History- und erlaubte
Fixture-Node-Datensätze bleiben erwartungsgemäß erhalten. `cross_site` bleibt
zwingend `false`.

Im Profil `core` müssen `worker_fn02.enabled=false`,
`cross_worker.executed=false`, `cross_worker.skipped=true` und
`cross_worker.tested=false` lauten. Nur `full-lab` darf die oben gezeigte
Cross-Worker-Belegung als Releasegate verlangen.

## 8. Nichtfunktionale Folgetests

Nach erfolgreichem E2E separat ausführen:

* 10K-Connection-/TLS-Handshake-Benchmark;
* Event-Fanout bei realistischer Sensorfrequenz;
* 24–72 Stunden Soak;
* Proxy-HA mit zwei Instanzen;
* Master-/PostgreSQL-Failover während laufender Operation;
* Broker-/Netzwerkpartition und Wiederanlauf;
* echte fn-02-Hardware und Cross-Site.

Diese Tests dürfen nicht durch den 128-Client-Smoke ersetzt oder als bereits
bestanden dargestellt werden.

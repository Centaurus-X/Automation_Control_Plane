# Automation Control Plane v0015 – tiefe Analyse, Diagnose und Releaseentscheidung

Stand: 18. Juli 2026  
Release: `0.15.0`  
Revision: `v0015-r3`  
E2E-Paket: `0.15.0-e2e.0`

## 1. Ergebnis

v0015 ist eine gezielte Erweiterung des Proxy-Gateways und des externen
Abnahmesystems. Der primäre WSS-Pfad wird nicht mehr von Uvicorn oder
Starlette bedient. Ein direkter, TLS-geschützter `websockets.serve`-Listener
besitzt Port 8443 und die zwei festen Pfade `/ws/v1/events` und
`/ws/v1/control`. Uvicorn bleibt als HTTP-only-ASGI-Server auf Port 8444 aktiv
und startet zwingend mit `ws="none"`.

Der WSS-Pfad besitzt keine zweite Businesslogik. HTTPS und WSS verwenden
denselben Dispatcher, dieselbe Authentisierung, RBAC-, Site- und
Resource-Scope-Prüfung, dieselben Rate Limits, Audits, Idempotenzregeln und
die persistente MQTT-/Master-Korrelation.

Der neue E2E v0015 verwendet das öffentliche Python-Paket `websockets` und
prüft Command-, Result-, Event-, Replay-, Origin-, Auth-, TLS- und
Recovery-Verträge. Das optionale `full-lab`-Profil verwendet einen kurzlebigen
fn-02-Protokollsimulator und einen Launcher für einen separat bereitgestellten,
unveränderten Worker v0082. Worker-Code und Worker-Archive sind nicht Teil des
Control-Plane-Releases.

Die Host-Gates sind bestanden. Ein Container-/Live-Lab-PASS wird nicht
behauptet, weil in der Erstellungsumgebung kein Docker vorhanden war. Die
Produktionsfreigabe bleibt deshalb an die in der Rolloutanleitung genannten
Zielhost-Gates gebunden.

## 2. Analysierte Eingaben

| Eingabe | SHA-256 | Verwendung |
|---|---|---|
| v0015-Entwicklungsplan | `d48b832b8df47b6c5473f0e7e34f18a7d8a76c2b1e10bb103c8a837bccaec668` | normative v0015-Anforderungen, Referenzmodule, Schemas, Testplan |
| v0014-Release | `e85b171633c9a7e01b212bcced788e0f6d0c995048e7fdcbb704523ee3498077` | geprüfte Baseline |
| Code-/Architekturspezifikation | `28e5a0df52154c9974972339d3b217b4290fcbe21630905d4548a86180885dad` | unveränderte Architekturgrenzen |
| v0014-Rolloutanleitung | `a1b26b5b064d2e981e4578792a716dc076e491d0dc6c04c49de6f7c9680af009` | Upgrade-, Rollback- und Betriebsbaseline |

Beide ZIP-Eingaben wurden vor der Analyse auf CRC-Fehler, absolute Pfade,
Pfadtraversal und symbolische Links geprüft. Die internen Checksummen des
v0015-Entwicklungsplans waren gültig. Die mitgelieferten Referenzmodultests
liefen mit `websockets 16.0` und `cryptography 46.0.0` erfolgreich.

Die normativen v0015-Dokumente und Protokollschemas sind unverändert unter
`documentation/reference/v0015/` enthalten. Sie sind Planungsquellen und kein
nachträglich als grün deklarierter Releasebericht.

## 3. Diagnose des Entwicklungsplans

### 3.1 Eindeutige Kernanforderungen

Der Plan fordert konsistent:

1. WSS direkt über das öffentliche `websockets`-Modul;
2. keinen Uvicorn-/Starlette-WSS-Pfad;
3. einen gemeinsamen, allowlist-basierten Client-Command-Dispatcher;
4. exakt getrennte WSS- und HTTPS-Listener;
5. atomaren Replay-/Live-Handoff mit High-Watermark und Generation-Fencing;
6. bounded Queues, Coalescing und fail-closed Critical-Overflow;
7. einen WSS-primary E2E mit allen 23 Actions und sicheren Restores;
8. eine Cross-Worker-Simulation ohne gebündelte Worker-Implementierung;
9. ehrliche Zustände `PASSED`, `FAILED`, `BLOCKED` und `SKIPPED`.

### 3.2 Aufgelöste Unschärfen

| Planstelle | Diagnose | Umsetzung |
|---|---|---|
| „Cross-Site-Simulation“ versus fn-01/fn-02 in `site_main` | Der geplante positive Test ist technisch Cross-Worker, nicht Cross-Site. | Jeder Report setzt `cross_site=false`; fremde Site/Origin wird nur negativ geprüft. |
| fn-02 Distributed v1/v2 | Äußerer MQTT-Vertrag bleibt `automation.mqtt.v1`; Capability und Full/Patch/Aktor-Payload können `automation.distributed.v2` sein. | Simulator deklariert v1-Fallback und v2-Fähigkeit, ohne den äußeren Brokervertrag umzubenennen. |
| „virtueller Worker“ | Ein vollständiger Worker im Control-Plane-Archiv würde die Architekturgrenze verletzen. | fn-02 implementiert nur den notwendigen Protokollaktor; fn-01 wird extern über einen argv-basierten Launcher gestartet. |
| Broker-Session-Exklusivität | Mosquitto-`$SYS`-Summen sind keine autoritative per-Client-/Zertifikatsauskunft. | `full-lab` verlangt eine frische externe, für die gesamte Laufzeit reservierte Session-Identität, mTLS-ACL-Negativproben und nach Stop einen frischen Release-Proof; andernfalls `BLOCKED` beziehungsweise beim Cleanup `FAILED`. |
| Referenz-WSS-Server | Das Referenzmodul ist ein Transportmuster, kein Drop-in für Auth, RBAC, Replay und Lifecycle der Baseline. | Der Releaseadapter wurde gegen die existierenden funktionalen Capabilities verdrahtet und separat getestet. |

### 3.3 Bewusst verbleibende Nachweisgrenzen

- kein positiver Cross-Site-Nachweis;
- keine reale fn-02-Hardware, Safety, FSM oder lokale Automation;
- kein Proxy-HA-/Failover-, Soak- oder 10.000-Connection-Nachweis;
- kein Live-Fault-Injection-Nachweis für Broker-, DB- oder Netzpartitionen;
- keine Aussage über den free-threaded CPython-3.14.6t-Master beziehungsweise
  den normalen CPython-3.14.6-Proxy im finalen Container ohne Zielhostlauf;
- die bestehende Master-v0014-Schnittstelle projiziert für `sensor.value` eine
  neue WSS-Event-ID und exportiert die ursprüngliche interne Cross-Fact-ID
  nicht. Der E2E bindet deshalb Payload, Worker-Provenienz, Cross-Scope,
  Fingerprint, Epoch, zeitliche Reihenfolge und den exakten Remote-Intent,
  behauptet aber keine nicht beobachtbare Message-ID-Kante;
- TLS-Handshakefehler vor Übergabe einer Verbindung an `websockets` sind
  unterhalb der Applikationsschicht und müssen durch TLS-Ingress-/Socket-
  Telemetrie ergänzt werden.

## 4. Implementierte Architektur

| Bereich | v0015-Umsetzung |
|---|---|
| Listener | direktes WSS 8443; HTTP-only Uvicorn 8444; gemeinsamer AsyncIO-Lifecycle |
| Start/Stop | beide Listener müssen gemeinsam starten; Readiness erst nach DB, MQTT, WSS und HTTPS; Stop-before-start wird nicht verloren; neue Commands werden atomar gegen Shutdown gesperrt und bereits zugelassene Mutationen vor dem Store-Close vollständig beendet |
| TLS | TLS ist für WSS obligatorisch; exakte HTTPS-Origin-Allowlist; Lab erzeugt CA-signiertes Leaf mit SAN; Produktion verlangt externe PKI |
| Control | streng validiertes `command.request`; 23 erlaubte Actions; korrelierte `command.accepted`, `command.result` oder `command.rejected` |
| Events | logische Filter statt MQTT-Topics; Default-Deny unbekannter Eventfamilien; Resource-RBAC; paginierter Replay |
| Backpressure | bounded Response-/Live-/Coalescing-Queues; gleiche Telemetrie wird latest-value coalesced; neuer unterschiedlicher Overflow trennt 1013 statt still zu verwerfen |
| Persistenz | Command vor MQTT-Publish persistiert; fehlgeschlagener Erstpublish bleibt `PENDING` und retryfähig; ACK wird erst nach erfolgreicher DB-Aktualisierung ausgeliefert; Operation-Events werden über die persistierte Operation-ID an den ursprünglichen Request-Owner gebunden |
| Idempotenz | atomarer DB-Claim; abgebrochener lokaler `PENDING`-Claim wird nach 15 Minuten fail-closed als Recovery-required markiert, nie automatisch erneut ausgeführt |
| Audit | Action-/Resource-RBAC-Preflight vor `ALLOW`; jede RBAC-Ablehnung besitzt einen `DENY`-Audit |
| Observability | Event-/Control-Clients, Queue-/Coalescing-Tiefe, kumulative Overflow-Counter, Handshakegründe, Command-Ergebnis/Latenz, Replay/Lag, Fanout-Latenz, Tasks und Clean-Close-Zähler |

## 5. Minimal-invasive Änderungssurface

Master, Mosquitto, PostgreSQL, gemeinsame Contracts und der Python-3.14t-
Runtime-Layer bleiben fachlich auf dem v0014-Stand. Geändert wurden:

- Proxy-Gateway-Source, Konfiguration, Compose, Migration, Tests, SBOM und
  Dokumentation;
- rootweite Versions-, Rollout-, Packaging- und Validierungsskripte;
- das neue, getrennte `integration_test/e2e_v0015/`;
- ausschließlich der Auto-Admission-Test und seine statische Regression,
  nicht die Master-Admission-Implementierung.

Die additive Proxy-Migration legt die lokale Idempotenzregistry an und ergänzt
an der bestehenden Command-Tabelle die persistierte Operation-ID samt Index.
Damit kann Live- und Replay-Fanout Operationsevents an den ursprünglichen
Request-Owner binden. Sie löscht oder benennt keine v0014-Tabelle um und
bleibt für einen Image-Rollback lesbar.

## 6. Wichtige zusätzlich diagnostizierte und behobene Fehler

### 6.1 Host-`ModuleNotFoundError` versus Docker

Der gemeldete Fehler in `test_async_websockets_server.py` war kein Fehler des
direkten WSS-Servers und kein fehlender Requirement-Pin.
`proxy_gateway/requirements.txt` enthielt bereits `websockets==16.0`. Der
konkrete Ablauf war:

```text
rollout.sh
  -> deploy_test_lab.sh
  -> test_static_all.sh                 (vor Image-Staging)
  -> proxy_gateway/test.sh
  -> Host-python3 test_async_websockets_server.py
  -> ModuleNotFoundError: websockets
```

Das Dockerfile installiert seine Requirements vor den Proxytests. Dieser
Container-Build wurde im Fehlerlauf wegen `set -Eeuo pipefail` gar nicht
erreicht. Host- und Container-Interpreter teilen keine Site-Packages. v0014
hatte denselben Requirement-Pin, aber noch keinen direkten Hostimport im
Proxy-WSS-Test; die alte Provisionierungslücke blieb dadurch unsichtbar.

Korrekturstand `v0015-r3` startet alle vollständigen Host-, direkten Proxy-
und E2E-Gates über `tools/run_in_host_validation_env.sh`. Das Tool erzeugt
außerhalb des Releasebaums eine nach Host-Python und Manifesten
gefingerprintete venv, installiert alle drei Komponenten-Requirements in
einem Resolverlauf und prüft exakte Distributionen, Imports sowie `pip check`.
Es installiert nie global, führt kein ungepinntes Pip-Upgrade aus und lehnt
Release-interne, symlinkumgangene, fremde oder unsichere Zielpfade ab. Ein
explizites Wheelhouse erzwingt `--no-index` ohne Online-Fallback.

Das Proxy-Image besitzt zusätzlich ein während des Builds erzeugtes lokales
Wheelhouse mit SHA-256-Manifest. Der non-root Entrypoint prüft Wheelhouse,
direkte Pins, Imports und transitive Konsistenz vor Migration und Gateway.
Nur wenn die installierte Image-Schicht beschädigt ist, wird eine geprüfte
Ersatzschicht offline im bereits beschreibbaren `proxy_runtime`-Volume
aufgebaut. Ein manipulierter oder unvollständiger Bestand führt zum
fail-closed Startabbruch; Runtime-PyPI-Zugriff findet nicht statt.

### 6.2 Weitere behobene Fehler

- Der alte Event-Core erlaubte unbekannte Eventfamilien implizit. v0015 ist
  dafür Default-Deny.
- Ein voller Coalescing-Bereich verwarf unterschiedliche Telemetrie still.
  v0015 trennt den Client fail-closed und behält Same-Key-Latest-Coalescing.
- Stale Live-Records wurden aus der Queue genommen, aber nicht als konsumiert
  bestätigt. Dadurch konnte der Coalescing-Nachschub stecken bleiben.
- Ein initial fehlgeschlagener MQTT-Publish wurde trotz durablem Retry als
  `not_ready` dargestellt. Er wird nun korrekt als akzeptiert/`PENDING`
  gemeldet.
- Ein ACK konnte vor erfolgreicher Persistenz an lokale Waiter gelangen. Das
  ist nun ausgeschlossen.
- `operation.get` prüfte Resource-Scopes nicht am Operation-Identifier.
- Session- und WSS-Cookie-Namen konnten divergieren; die Konfiguration wird
  nun fail-closed validiert.
- Session-Rotation begrenzt die neue Sliding-Expiry auf die bestehende
  absolute Frist.
- `proxy_headers=False` verhindert das Vertrauen ungeprüfter Forwarded-
  Header am direkt exponierten Listener.
- Der Auto-Admission-Test interpretierte `node.authority_term=0` als fehlende
  Master-Authority. Er liest nun den aktiven, nicht abgelaufenen Site-Master-
  Lease-Term aus `master_control.authority_state`.

### 6.3 Feldbefund r2: WSS-Reply mit PostgreSQL-nativem Skalartyp

Zwei identische Zielhostläufe von `v0015-r2` erreichten den direkten WSS-
Listener, bestanden TLS, Origin, Authentisierung, Event-Subscription,
Auto-Admission und Node-Konvergenz und scheiterten reproduzierbar beim ersten
Live-Read-Command. Der Writer beendete die Verbindung mit `1011`, weil die
alte JSON-Grenze einen nativen Rückgabewert des PostgreSQL-/Dispatcherpfads
erst beim asynchronen Senden als „unsupported JSON value type“ erkannte.
Damit war `command.accepted` potentiell bereits eingereiht und die eigentliche
Typ-/Feldposition blieb im Log unsichtbar.

`v0015-r3` normalisiert Antworten vor dem Queueing in einen abgelösten,
begrenzten JSON-Baum. Neben UUID, Datum/Zeit und Decimal werden auch
PostgreSQL-nahe Zeitintervalle, IP-Address/Network/Interface sowie Bytes,
Bytearray und Memoryview deterministisch abgebildet. Ungeordnete Container,
nicht endliche Zahlen und unbekannte Objekte bleiben verboten; es gibt
bewusst kein `default=str`. Eine Ablehnung nennt nur JSON-Pfad und Python-
Typ, niemals `repr()` oder den Objektwert.

Die komplette `command.accepted`/`command.result`-Sequenz wird vor dem ersten
Queueeintrag einschließlich Größenlimit geprüft. Ein weiterhin unbekannter
Backendtyp erzeugt genau ein `command.rejected` mit `internal_error`; die WSS-
Verbindung bleibt offen und nimmt den nächsten gültigen Command an. Der echte
TLS-Integrationstest deckt nun `users.list` mit produktionsnahen
Datenbankskalaren, den Fail-closed-Fall und die Wiederverwendung derselben
Verbindung ab. Der korrigierte Zielhost-Live-E2E bleibt vor einer konkreten
Produktionsfreigabe erneut auszuführen.

## 7. E2E v0015

Das `core`-Profil prüft den realen direkten WSS-Pfad, TLS/Origin/Auth,
Command-/Result-Korrelation, die vollständige 23-Action-Matrix, Event-Live-
Delivery, Ressourcenfilter, Multi-Page-Replay, Generationen, HTTPS-Leseparität, reversible Config- und
Aktoränderungen sowie ihre semantisch bestätigten Restores.

Retention-Floor, Slow-Client-Coalescing, Critical-Overflow,
Filterupdate-Race und das harte Connection-Limit werden absichtlich in einer
isolierten Instanz des echten TLS-Produktions-WSS-Servers erzeugt. Diese Gates
sind Teil der verpflichtenden Komponentenprüfung, werden aber im Live-Report
nicht als auf dem Zielcluster provozierte Ereignisse ausgegeben.

Hardwarewrites sind zusätzlich an drei fail-closed Bedingungen gebunden: ein
explizites `site_controller_map`-Owner-Mapping, eine explizite Aktor-/Sensor-
Fixture und der freigegebene Modbus-Lab-Endpunkt `192.168.0.3:5020` (oder ein
bewusst per CLI ersetzter dedizierter Testendpunkt). Reservierte Node-IDs der
Negativproben müssen vorab nachweislich fehlen.

Nicht reversible Actions werden mit sicheren, nicht existierenden oder
schema-invaliden Targets ausgeführt und müssen tatsächlich
`command.rejected` liefern. Ein `command.accepted` gilt dort als Testfehler.

`full-lab` ergänzt:

- eine frische, kanonisch Ed25519-signierte Broker-Session-Reservation über
  die gesamte Laufzeit; der erwartete Public-Key-SHA-256 muss getrennt vom
  Proof über einen vertrauenswürdigen Deploymentkanal gepinnt sein;
- lokale Runner-/fn-02-Locks vor Zertifikatladen sowie einen frischen
  signierten Broker-Release-Proof nach Disconnect, exakt an Run, Site, Node,
  Client, Zertifikatsidentität, Reservation-ID, Quelle, Ablauf und denselben
  Signer gebunden;
- den externen Worker-v0082-Launcher mit festem Archivhash;
- fn-02 Full/Patch/Actuator-Protokollkonvergenz;
- einen gleichen-Site Cross-Worker-Trigger mit `simulation=true`;
- exakte Cross- und Target-Node-Fences, die terminale Master-Operation, die im
  fn-02-Heartbeat beobachtete Command-ID und genau einen Traceeintrag im
  isolierten 74→76-Messfenster;
- getrennte Baseline-, Mess- und Restore-Barrieren: relevante Operation
  `CONFIRMED`, Master-/Proxy-MQTT-Queues und QoS1-Zähler null, stabiler
  Outbox-Publishzähler und ein bestätigter fn-02-Drainmarker;
- Worker-, Config-, Aktor- und Sensor-Recovery in `finally`;
- einen dokumentierten fn-02-Residualzustand erst nach `MAINTENANCE`, stale
  Heartbeat, `online=false` und signiert bestätigter Broker-Freigabe.

Ein offenes Recovery-Journal blockiert einen neuen mutierenden Lauf. Secrets,
Cookies, CSRF-Werte, Tokens und Private Keys werden nicht in Report oder
Recovery-Journal geschrieben.

Beim nächsten Lauf mit identischem Profil-/Site-/Node-/Fixture-Binding wird
ein offenes Journal zuerst automatisch abgearbeitet. Es persistiert neben
globalen Config-Ankern auch die ursprünglichen Node-Replica-Fingerprints und
Token-Erstellungsverpflichtungen, sodass ein verlorener WSS-Reply weder einen
unbemerkten Doppelcommit noch ein nicht widerrufenes Testcredential erzeugen
darf. Erst das profilabhängige Vollständigkeitsgate über Pflicht-Check-IDs,
Restores, TLS- und Cross-/Lock-Invarianten kann den Gesamtstatus auf `PASSED`
setzen. Die Secret-Canary-Suche läuft auch nach einem Primärfehler; getrennte
Proxy-, Protocol- und E2E-Logquellen sind für `SEC-009` verpflichtend. Der
Runner verlangt frische, nicht leere, unterschiedliche und lesbare externe
Capture-Dateien und schützt sie bis zum Endscan gegen Austausch. Deren
Prozess-/Collector-Provenienz bleibt ein operatives Gate und wird nicht als
kryptographisch bewiesen ausgegeben.

## 8. Releaseentscheidung

Die Code-, Struktur-, Security-, Unit-, Selftest- und Packaging-Gates sind für
das erzeugte Archiv bestanden. Das v0015-ZIP ist damit als Codeartefakt
auslieferbar. Dies ist ausdrücklich noch keine betriebliche Freigabe einer
konkreten Installation.

Die betriebliche Produktionsfreigabe ist konditional: Docker-/Compose-Build,
Python-3.14t-Containerprobe, reale PostgreSQL-/MQTT-mTLS-Gates, ein realer
fn-01-/Modbus-Core-E2E und – falls beansprucht – `full-lab` müssen auf dem
Zielsystem erfolgreich sein. Ein dortiges `BLOCKED` oder `NOT_RUN` darf nicht
in `PASSED` umetikettiert werden.

Die verbindliche Durchführung steht in
`ROLLOUT_TEST_ANLEITUNG_AUTOMATION_CONTROL_PLANE_V0015_DE.md`.

# Automation Control Plane v0015 – Implementierungsvorschrift

Dokumentversion: `1.0`  
Zielrelease: `automation_control_plane_release_v0015`  
Ausgangsbasis: `automation_control_plane_release_v0014`  
Worker: `automation_system_v0082`, unverändert  
Status: normativer Entwicklungs- und Abnahmeplan

## 1. Kurzurteil

Die v0014-Architektur wird nicht neu entworfen. Sie wird am externen
Proxy-Transport gezielt erweitert:

1. Das Python-Paket `websockets` übernimmt als primärer, direkt auf `asyncio`
   laufender Server den gesamten WSS-Verkehr.
2. `/ws/v1/events` bleibt der abonnierbare Event-Ausgang.
3. `/ws/v1/control` wird der primäre Eingang für fachliche Lese- und
   Schreibbefehle.
4. Starlette/Uvicorn bleibt als sekundäre HTTPS/REST-Schnittstelle bestehen,
   wird jedoch explizit ohne WebSocket-Unterstützung gestartet.
5. Beide Transporte rufen dieselben vorhandenen Proxy-Core-Funktionen auf.
   Authentisierung, RBAC, Idempotenz, Audit, MQTT-Korrelation und Master-
   Authority werden nicht dupliziert.
6. Master, Mosquitto, PostgreSQL und Worker v0082 benötigen für diesen Umbau
   keine fachliche Änderung.

Das ist die minimal-invasive und architektonisch saubere Lösung. Die in diesem
Paket enthaltenen Python-Dateien sind geprüfte Referenzmodule. Sie sind noch
nicht das vollständige produktive v0015-Release: Die in Kapitel 8 beschriebenen
Adapter an den realen v0014-Proxy und der vollständige Running-System-E2E müssen
vor der Freigabe umgesetzt werden.

## 2. Verbindlicher Scope

### 2.1 In Scope

* direkter TLS-WSS-Listener mit `import websockets` und
  `websockets.serve(...)`;
* getrennte Pfade für Events und Control auf demselben WSS-Port;
* WSS als primärer fachlicher Command-/Control-Transport;
* HTTPS/REST als kompatibler sekundärer Transport;
* ein gemeinsamer Dispatcher für REST und WSS;
* bestehende Proxy-Authentisierung, CSRF, RBAC, Idempotenz, Audit und
  MQTT-Korrelation;
* begrenzte Queues, Backpressure, Replay und Telemetrie-Coalescing;
* TLS-/SAN-Konzept und explizite Lab-PKI-Werkzeuge;
* E2E v0015 für Events, Commands, Config-Mutation, Actuation, Restore,
  Negativfälle und Cross-Worker-Simulation;
* Korrektur des Auto-Admission-Fehlalarms;
* aktualisierte Rollout-, Health- und Abnahme-Gates.

### 2.2 Nicht in Scope

* Änderungen am Worker-Paket v0082;
* eine zweite reale Worker-Implementierung;
* echter Cross-Site-Betrieb;
* neue Master-, Broker- oder Datenbank-Fachlogik;
* beliebige MQTT-Topic-Auswahl durch Clients;
* ein allgemeines Remote-Shell-Protokoll;
* Produktions-PKI-Erzeugung im Proxy-Container;
* der Nachweis von 10.000 gleichzeitigen Verbindungen allein durch den
  funktionalen E2E.

Der Begriff „Command-Line-Gate“ wird in v0015 als fachliches Command-/Control-
Gateway verstanden. `terminal.execute` darf ausschließlich die bereits
vorhandene serverseitige Allowlist und RBAC-Prüfung verwenden. Niemals dürfen
beliebige Shellstrings oder Programme direkt aus einem WSS-Payload ausgeführt
werden.

## 3. Auswertung des Ist-Zustands v0014

v0014 besitzt bereits die richtigen fachlichen Bausteine:

* Authentisierung mit Session und API-Token;
* Default-Deny-RBAC und Site-/Resource-Scopes;
* CSRF-Schutz für Session-basierte Mutationen;
* Command-Gateway mit Idempotenz, Korrelation und Audit;
* MQTTS-Verbindung zum Broker und fachliche Master-Commands;
* Event-Stream, Replay und Clientfilter;
* HTTPS-Routen für Nodes, Config, Rollback, Aktuatoren und Operationen.

Der vorhandene WSS-Pfad ist jedoch direkt in Starlette eingebunden:

* `https_interface.py` importiert `WebSocketRoute`;
* `websocket_interface.py` arbeitet gegen Starlette-WebSocket-Objekte;
* `gateway_main.py` startet Uvicorn mit `ws="websockets-sansio"`;
* WSS kann nur Subscriptions empfangen, keine fachlichen Commands.

Damit liegt der Änderungsbedarf ausschließlich in der Transportintegration des
Proxy Gateways. Es gibt keinen Grund, Authority, Sharding oder Worker-Runtime
für v0015 umzubauen.

### 3.1 Diagnose der beiden hochgeladenen Ausgangsmodule

Das ursprüngliche `async_websockets_server.py` war nicht direkt integrierbar.
Unter anderem fehlten verbindliche Pfade, Authentisierung, Origin-Prüfung,
RBAC, bounded Queues, Replay, Audit, Idempotenz und ein sicherer Lifecycle.
Client-IDs und Topics waren zu frei wählbar; Startup-Fehler und Cancellation
wurden nicht zuverlässig behandelt.

Das ursprüngliche `generate_ssl_certs.py` war ebenfalls nicht releasefähig:
zwei aufgerufene Funktionen fehlten, der Import löste Ausführung aus, eine
CLI fehlte, SAN-Ermittlung war nicht deterministisch und sichere Key-Rechte
waren nicht garantiert. Außerdem war ein selbstsigniertes Mehrzweck-Leaf kein
sauberes Server-PKI-Modell.

Die beiliegenden Referenzmodule beheben diese Punkte. Sie bleiben bewusst
funktional, ohne Klassen, Type-Hints, Decorators, Dataclasses oder Lambdas und
folgen der bereitgestellten `CODE_AND_ARCHITECTURE_SPEZIFICATION`.

## 4. Bindende Zielarchitektur

### 4.1 Listener und Ports

```text
0.0.0.0:8443  TLS/WSS  direktes websockets.serve
  ├── /ws/v1/events
  └── /ws/v1/control

0.0.0.0:8444  TLS/HTTPS  Starlette über Uvicorn
  └── /api/v1/*, /metrics, /api/v1/health/*
```

Zwei unabhängige Server können nicht gleichzeitig denselben Socket
`IP:Port` besitzen. Deshalb muss Uvicorn intern auf 8444 wechseln, wenn der
direkte WSS-Server 8443 übernimmt. `SO_REUSEPORT` ist hierfür ausdrücklich
verboten, weil Verbindungen dann zufällig beim falschen Protokollserver landen
könnten.

Soll extern zwingend ein einziger Port 443 oder 8443 sichtbar sein, ist ein
L7-Reverse-Proxy davor zulässig:

```text
/ws/v1/*  → Proxy-Container:8443
/*        → Proxy-Container:8444
```

Dieser Reverse-Proxy ist optional und nicht Bestandteil des v0015-Kerns.

### 4.2 Prozess- und Zustandsmodell

Empfohlen ist weiterhin **ein Proxy-Prozess** mit einem gemeinsamen Runtime-
Context:

* eine MQTT-Verbindung;
* ein Identity-/RBAC-Kontext;
* ein Command-Gateway;
* ein Event-Stream und Replay-Store;
* ein `asyncio`-Loop;
* zwei Listener: direktes WSS und HTTP-only Uvicorn.

Dadurch gibt es keine doppelte MQTT-Subscription, keinen zweiten In-Memory-
Korrelationszustand und keine unnötige IPC-Schicht. Ein späteres horizontales
Scaling darf mehrere vollständige Proxy-Instanzen einsetzen, muss dann aber
Sessions, Idempotenz, Audit und Replay vollständig über PostgreSQL teilen.

### 4.3 Kommunikationspfad

```text
Clientcommand: Client ──WSS──► Proxy ──MQTTS──► Broker ──MQTTS──► Master
Workerdispatch: Master ──MQTTS──► Broker ──MQTTS──► Worker
Events/Result: Worker/Master ──MQTTS──► Broker ──MQTTS──► Proxy ──WSS──► Client
```

Proxy, Master und Worker verwenden denselben Mosquitto-Broker. Der Proxy
spricht nie direkt mit einem Worker. Der Master bleibt globale Config-
Authority und Routinginstanz. Der Worker bleibt Besitzer der lokalen
Automation und Hardwareausführung.

Diagramme:

* [Systemkommunikation als Mermaid](architecture/01_system_communication_v0015.mmd)
  und [SVG](architecture/01_system_communication_v0015.svg)
* [Proxy-Architektur als Mermaid](architecture/02_proxy_node_architecture_v0015.mmd)
  und [SVG](architecture/02_proxy_node_architecture_v0015.svg)
* [E2E-/Cross-Worker-Sequenz als Mermaid](architecture/03_e2e_wss_cross_worker_v0015.mmd)
  und [SVG](architecture/03_e2e_wss_cross_worker_v0015.svg)

## 5. Direktes WSS-Gateway

### 5.1 Bibliotheksgrenze

Im WSS-Transport ist ausschließlich der öffentliche AsyncIO-Server des
Pakets `websockets` zulässig:

```python
import websockets

async with websockets.serve(...):
    ...
```

Unzulässig sind dort:

* Starlette `WebSocket` und `WebSocketRoute`;
* Uvicorn-WebSocket-Backends;
* direkte SansIO-Imports oder selbst implementiertes RFC-6455-Framing;
* ein zweiter fachlicher Command-Dispatcher;
* ein frei wählbarer MQTT-Topic-Passthrough.

„Kein SansIO“ bedeutet als Projektgrenze: Der Anwendungscode importiert oder
konfiguriert keine SansIO-Schnittstelle. Interne Implementierungsdetails des
freigegebenen Drittanbieterpakets `websockets` werden nicht nachgebaut.

### 5.2 Endpoint `/ws/v1/events`

Der Endpoint akzeptiert nach erfolgreichem TLS-Handshake und Authentisierung
als erste Nachricht genau eine logische Subscription:

```json
{
  "protocol": "automation.proxy.websocket.v1",
  "type": "subscribe",
  "request_id": "sub-001",
  "last_event_id": 1204,
  "filters": [
    {"resource_type": "sensor", "resource_pattern": "62671112-*:1"},
    {"resource_type": "operation", "resource_pattern": "*"},
    {"resource_type": "node", "resource_pattern": "fn-*"}
  ]
}
```

Der Client darf danach nur Filterupdates senden. Freie Broker-Topics,
`#`, `+`, Pfadsegmente und Queryparameter sind verboten. Jede Nachricht wird
erneut gegen Principal, Site und Resource-Scope autorisiert.

Der Server liefert:

* `subscription.accepted` oder `subscription.rejected`;
* autorisierte Live-Events;
* persistierte Replay-Events nach `last_event_id`;
* kritische `operation.result`, `command.ack`, `config.rollout` und
  `security.event` ohne stilles Verwerfen.

Bei langsamen Clients werden unkritische Sensorereignisse pro Resource auf den
neuesten Wert zusammengeführt. Ist die Queue durch kritische Ereignisse voll,
wird mit Close-Code 1013 getrennt; der Client setzt danach mit Replay fort.

### 5.3 Endpoint `/ws/v1/control`

Nach dem Handshake sendet der Server `connection.ready`. Eine Anfrage besitzt
immer diese Form:

```json
{
  "protocol": "automation.proxy.websocket.v1",
  "type": "command.request",
  "request_id": "abc-123",
  "action": "config.propose",
  "idempotency_key": "unique-key",
  "ttl_s": 30,
  "csrf_token": "required-for-session-mutation",
  "payload": {}
}
```

Antworten sind über `request_id` korreliert:

```json
{
  "protocol": "automation.proxy.websocket.v1",
  "type": "command.accepted",
  "request_id": "abc-123",
  "action": "config.propose",
  "operation_id": "operation-id",
  "state": "PENDING"
}
```

```json
{
  "protocol": "automation.proxy.websocket.v1",
  "type": "command.result",
  "request_id": "abc-123",
  "action": "config.propose",
  "result": {"ok": true}
}
```

oder deterministisch:

```json
{
  "protocol": "automation.proxy.websocket.v1",
  "type": "command.rejected",
  "request_id": "abc-123",
  "action": "config.propose",
  "code": "stale_revision",
  "message": "base revision is no longer current"
}
```

Das Transportergebnis bedeutet nicht automatisch, dass eine verteilte
Operation abgeschlossen ist. Länger laufende Operationen werden nach
`command.accepted` über `/ws/v1/events` bis `CONFIRMED`, `FAILED`, `EXPIRED`
oder `CANCELLED` verfolgt.

### 5.4 Parität zwischen WSS und HTTPS

Alle post-authentisierten v0014-Anwendungs- und Adminfunktionen verwenden
denselben Dispatcher. Identity-Aktionen bleiben lokal im Proxy; Domain-
Commands werden über feste Master-Actions geroutet:

| WSS-Action | Sekundäre HTTPS-Funktion | Berechtigung/Regel |
|---|---|---|
| `users.list` / `user.create` | `GET/POST /api/v1/users` | `user.manage`, Create mutierend |
| `roles.list` / `permissions.list` | Rollen/Permissions lesen | `role.manage` |
| `api_tokens.list/create/revoke` | API-Token-Routen | eigener Principal, CSRF für Mutation |
| `sites.list` | `GET /api/v1/sites` | Site-Scopes des Principals |
| `nodes.list` | `GET /api/v1/nodes` | Node-Read-Scope |
| `node.get` | `GET /api/v1/nodes/{node_id}` | Node-Read-/Resource-Scope |
| `runtime.status` | Runtime-/Health-Status | Read-Scope |
| `config.get` | `GET /api/v1/config` | Config-Read-Scope |
| `config.history` | Config-History | Config-Read-Scope |
| `config.diff` | Config-Diff | Config-Read-Scope |
| `config.propose` | Config-Proposal | Mutation, CSRF bei Session, Idempotenz |
| `config.rollback` | Config-Rollback | Mutation, Admin/RBAC, Idempotenz |
| `actuator.request` | Aktuator-Request | Ownership/Safety, TTL, Idempotenz |
| `operation.get` | Operation lesen | Resource-Scope |
| `audit.list` | `GET /api/v1/audit` | `audit.read` |
| `node.drain` | Node drain | Admin/RBAC, Idempotenz |
| `node.state.set` | Sollzustand setzen | Admin/RBAC, Idempotenz |
| `node.admission.approve` | Admission | Admin/RBAC, Idempotenz |
| `terminal.execute` | erlaubte Terminalaktion | strikte serverseitige Allowlist |

Login, Logout, Session-Introspektion, Health und Metrics bleiben in v0015
bewusst HTTPS-only. Tokenverwaltung nach erfolgreichem Handshake wird dagegen
gespiegelt. Damit ist HTTPS für native Bearer-Clients sekundär, aber für den
Browser-Login weiterhin ein notwendiger Bootstrap. „Optional“ darf nicht als
„für jeden Client vollständig entfernbar“ dokumentiert werden.

Die verbindlichen Felder und Limits pro Action stehen in
[`protocol/ACTION_PAYLOAD_CONTRACTS_DE.md`](protocol/ACTION_PAYLOAD_CONTRACTS_DE.md).
Top-Level-`idempotency_key`, `ttl_s`, `csrf_token` und Payload werden im Adapter
explizit in den vorhandenen v0014-Aufruf überführt; sie dürfen nicht verloren
gehen oder nochmals unabhängig validiert werden.

Browser können im nativen WebSocket-API-Handshake keinen beliebigen
`Authorization`-Header setzen. Deshalb gelten zwei Authentisierungspfade:

* Browser: bestehende Secure-/HttpOnly-Session-Cookies plus exakte Origin-
  Allowlist; mutierende Nachrichten tragen zusätzlich ein CSRF-Token.
* Native Clients/E2E: `Authorization: Bearer <api-token>` im Handshake.

Tokens in URL-Queryparametern sind verboten. Falls später ein vollständig
REST-freier Browser-Bootstrap gefordert wird, ist ein kurzlebiges, einmaliges
WSS-Ticket ein eigener, sicherheitsgeprüfter Folgeschritt.

Langzeitverbindungen dürfen den Handshake-Principal nicht unbegrenzt cachen.
Der Proxy prüft spätestens alle 30 Sekunden Session/API-Token, Ablauf,
Revocation, Rollen-/Policy-Version und Site-Scope erneut. Mutationen erzwingen
zusätzlich eine unmittelbare Revalidierung. Nach maximal 30 Minuten wird der
Socket mit Code 1008 geschlossen und neu authentisiert. Ein widerrufener
Principal erhält nach der wirksamen Revalidierung weder weitere Commands noch
Events.

Der Event-Core autorisiert fail-closed pro Eventtyp:

| Eventfamilie | Mindestberechtigung plus Scope |
|---|---|
| Sensor-/Node-Events | `node.read`, passende Site und Resource |
| Operations-/Commandresultate | `operation.read` beziehungsweise eigener Subjectbezug; fremde Ergebnisse nur mit Auditrecht |
| Config-Rollout | `config.read`, passende Site/Scope |
| Security-/Audit-Events | `audit.read` |

Ein Callback-Ergebnis gilt nur bei explizitem `ok=true` beziehungsweise
booleschem `True` als erlaubt; beliebige truthy Dictionaries dürfen niemals
autorisieren.

Eine leere Filterliste bleibt aus Kompatibilitätsgründen zulässig und bedeutet
„alle für diesen Principal autorisierten Events der Site“. Sie umgeht niemals
Event-RBAC oder Resource-Scope. Nichtleere Filter schränken diese bereits
autorisierte Menge zusätzlich ein.

Wird ein L7-Reverse-Proxy eingesetzt, müssen HTTPS und WSS dieselbe
vertrauensgebundene Client-IP-Regel verwenden, weil v0014 Sessions an
Source-IP und User-Agent bindet. Entweder sind Forwarded-Header auf beiden
Listenern deaktiviert, sodass beide die L7-Adresse sehen, oder beide werten sie
nur von exakt allowlisteten Proxy-CIDRs identisch aus. Ungeprüftes
`X-Forwarded-For` ist verboten. Der E2E muss den tatsächlich vorgesehenen
Produktionspfad einschließlich L7 testen.

### 5.5 Semantik und Fehlercodes

Jeder mutierende Request benötigt:

* eindeutige `request_id` für Transportkorrelation;
* stabile `idempotency_key` für Retry/Deduplizierung;
* begrenzte `ttl_s`;
* Principal, Site und Resource-Scope aus dem authentisierten Handshake;
* Audit-Eintrag vor beziehungsweise atomar mit der Weiterleitung;
* unveränderte bestehende Master-Fencing- und Authority-Prüfung.

v0015 erfindet keine umbenannten Parallelcodes. Der gemeinsame Dispatcher
reicht die bestehenden v0014-Codes unverändert an REST und WSS weiter, unter
anderem `authentication_required`, `authorization_denied`, `invalid_payload`,
`conflict`, `stale_revision`, `not_found`, `not_ready`, `expired`,
`not_config_authority` und `internal_error`. WSS-spezifisch kommen nur
Transportzustände wie `authentication_expired` und
`replay_resync_required` hinzu. Eine spätere Codeumbenennung wäre ein eigener,
versionierter und für beide Transporte atomarer Vertragsschritt.

Die JSON-Schemas in `protocol/` sind Release-Artefakte. Ein Schemawechsel
erfordert eine Protokollversion und Kompatibilitätstest; stilles Akzeptieren
unbekannter Felder ist verboten.

## 6. AsyncIO, Parallelität und Backpressure

### 6.1 Concurrency-Modell

* Ein Eventloop bedient viele Socketverbindungen ohne Thread pro Verbindung.
* Jede Verbindung besitzt exakt einen Reader und einen Writer.
* Der WSS-Transport ruft kurze asynchrone Adapter auf. Blocking DB-/Legacy-
  Aufrufe werden dort mit echten DB-/Broker-Timeouts kontrolliert ausgelagert.
* `wait_for(to_thread(...))` darf keinen mutierenden Command scheinbar
  abbrechen: Der Thread würde weiterlaufen und könnte später noch committen.
* MQTT publiziert weiterhin in den bestehenden Event-Core; nur dieser Core
  übergibt loop-owned bounded Live-Queues an den WSS-Transport.
* Mutierende Shared States sind durch Lock, Single-Writer-Queue oder
  unveränderlichen Snapshot geschützt; es gibt keine GIL-Annahme.
* Commands eines einzelnen Control-Sockets laufen bewusst in Empfangsreihenfolge.
  Mehrere Clients werden durch ein global begrenztes Semaphore parallel
  verarbeitet.

Empfohlene Startwerte:

| Parameter | Wert | Zweck |
|---|---:|---|
| `max_connections` | 4096 | harte erste Kapazitätsgrenze |
| `max_connections_per_source` | 256 | Quell-IP-Fairness im vertrauensgebundenen L7-Modell |
| `max_connections_per_principal` | 128 | verhindert Principal-Monopolisierung |
| `max_message_bytes` | 1 MiB | Schutz gegen große Frames |
| `protocol_queue_maxsize` | 16 | interne `websockets`-Receive-Queue |
| `response_queue_maxsize` | 64 | reine Transportantworten pro Client |
| `event_live_queue_maxsize` | 256 | vom Event-Core besessene Live-Queue |
| `coalesced_max_entries` | 256 | harte Event-Core-Grenze; danach Close/Replay |
| `write_limit_bytes` | 32 KiB | Stream-Backpressure |
| `handshake_concurrency` | 64 | Schutz des Auth-Stores |
| `command_concurrency` | 128 | Schutz des Dispatchers |
| `initial_message_timeout_s` | 5 | Frist für die erste Event-Subscription |
| `handshake_queue_timeout_s` | 1 | begrenztes Warten vor Auth statt unbounded Stau |
| `command_queue_timeout_s` | 1 | begrenztes Warten vor Dispatch statt unbounded Stau |
| `ping_interval_s` / `ping_timeout_s` | 20 / 20 | Dead-peer-Erkennung |
| `send_timeout_s` | 5 | Slow-client-Grenze |
| `principal_revalidate_interval_s` | 30 | Ablauf/Revocation/Policy neu prüfen |
| `max_connection_age_s` | 1800 | erzwungener neuer Handshake |
| `replay_page_size` | 200 | DB-Replayseite |
| `replay_timeout_s` | 60 | Gesamtbudget bis zum Live-Handoff |
| Compression | aus | deterministische CPU-/Memory-Last |

4096 als Konfiguration ist kein 10K-Nachweis. Vor einer Zusage für 10.000
gleichzeitige Clients müssen File-Descriptor-Limits, Kernel-Backlog,
TLS-Handshakes, RAM pro Verbindung, Event-Fanout und PostgreSQL-Replay mit
einem separaten Lasttest gemessen werden.

Replay verwendet einen beim atomaren Subscription-Open erfassten DB-
High-Watermark. Der Event-Core puffert Live-Records bounded, während der
Transport autorisierte Replayseiten bis exakt zu diesem Cursor sendet. Erst
danach werden Live-Records mit höherer Sequenz gesendet. Retention-Lücken oder
Sicherheitslimits liefern `replay.resync_required` und Close 1013 – niemals
einen stillen Sprung. Authz-relevante Filter-/Principal-Updates erhöhen eine
Subscription-Generation; eine semantisch unveränderte periodische
Revalidierung muss dieselbe Generation zurückgeben. Ein Principal-bedingter
Generationswechsel erzwingt `replay.resync_required` und Close 1013. Bereits
geplante Records einer alten Generation werden verworfen.

`open_event_subscription` liefert neben der Live-Queue zwingend ein
loop-owned `asyncio.Event` namens `disconnect_event`. Bei kritischem Overflow
setzt der Event-Core dieses Signal; der Transport schließt deterministisch mit
1013. Response-Queue-Overflow nutzt denselben Close-Pfad. Alle injizierten
Callbacks müssen echte asynchrone Callables sein; synchrone DB-/Legacy-
Funktionen werden bereits beim Serverbau abgelehnt und benötigen
cancellation-sichere Adapter mit eigenen Backend-Timeouts.

Der an `websockets.serve` übergebene Protokolllogger bleibt auf `CRITICAL`,
weil DEBUG-Logging Handshake-Header und vollständige Frames einschließlich
Secrets ausgeben kann. Sanitisierte Applikationsfehler und Metriken verwenden
einen getrennten Logger. Der E2E sucht mit eindeutigen Canary-Werten in allen
Logs und schlägt bei Bearer-, Cookie-, CSRF-, Passwort- oder Tokenfund fehl.

Eine Coalescing-Map ist ebenfalls hart begrenzt. Ist Queue plus Coalescing-
Budget ausgeschöpft, wird der Client getrennt und setzt mit Replay fort. Eine
bounded Queue bei gleichzeitig unbounded Coalescing wäre ausdrücklich kein
gültiger Backpressure-Schutz.

### 6.2 Readiness und Shutdown

Proxy-Readiness ist nur `ready=true`, wenn:

* Datenbank und Migrationen bereit sind;
* MQTT authentisiert und verbunden ist;
* direkter WSS-Listener auf 8443 gebunden ist;
* HTTP-only Uvicorn auf 8444 bereit ist;
* Dispatcher und Event-Stream bereit sind.

Shutdown-Reihenfolge:

1. Readiness auf false;
2. keine neuen Handshakes/Commands akzeptieren;
3. aktive Commands bis zur konfigurierten Frist drainen;
4. WSS-Clients mit Code 1001 schließen;
5. Uvicorn stoppen;
6. MQTT und Datenbank sauber schließen.

Startup- oder Listenerfehler müssen den Prozess mit Exit-Code ungleich null
beenden. Ein halb bereiter Proxy darf niemals healthy gemeldet werden.

## 7. TLS, Zertifikate und Secrets

### 7.1 Produktionsbetrieb

Produktiv werden Zertifikate außerhalb des Containers erzeugt und read-only
als Secret gemountet. Der Proxy erzeugt beim Start keine Schlüssel. Das
Serverzertifikat benötigt:

* `ExtendedKeyUsage = serverAuth`;
* SAN-Einträge für alle tatsächlich verwendeten DNS-Namen und IP-Adressen;
* vollständige CA-Kette;
* zum Zertifikat passenden privaten Schlüssel;
* keine Wildcard- oder automatisch ermittelten Bind-Adressen;
* rechtzeitige Erneuerung und definierte Rotation.

`0.0.0.0`, `::` und Container-IDs sind keine Clientidentitäten und gehören
nicht in SAN. Für das Labor wären zum Beispiel `DNS:proxy.local`,
`DNS:localhost`, `IP:192.168.0.82` und `IP:127.0.0.1` explizit zulässig.

Dateirechte:

```text
private key: 0600
certificate/CA: 0644
secret mounts: read-only
```

TLS 1.2 ist das Minimum; TLS 1.3 wird bevorzugt. Hostname-/SAN-Verifikation
muss im v0015-E2E eingeschaltet sein. `tls_verification=false` ist kein
Release-Gate mehr.

### 7.2 Lab-PKI-Werkzeug

`reference_implementation/proxy_gateway/src/library/generate_ssl_certs.py`
stellt bereit:

* deterministische, explizite SAN-Normalisierung;
* kleine Lab-Root-CA und ein CA-signiertes Server-Leaf;
* Server-Leaf ausschließlich mit `serverAuth`;
* privates Staging, exklusive symlink-sichere Lockdatei und atomarer Replace
  pro Datei; kein falsches Versprechen eines atomaren Mehrdatei-Swaps;
* sichere Schlüsselrechte;
* geprüfte Fullchain-/Manifest-Idempotenz und erzwungene `--rotate`-Semantik;
* Prüfung der einstufigen Lab-CA auf Gültigkeit, KeyUsage, Keygröße und
  Abdeckung der Leaf-Laufzeit;
* das explizite CLI-Subcommand `validate` für genau diese Lab-PKI;
* harte Sperre gegen versehentliche Produktionserzeugung.

Die Erzeugung ist nur mit `lab-generate --test-only` möglich. Das Werkzeug
ist kein Ersatz für Vault, ACME oder eine Unternehmens-PKI und kein allgemeiner
Produktions-Chain-Validator. Intermediate-/Root-Ketten werden durch das externe
PKI-/Deployment-System und einen echten verifizierten TLS-E2E geprüft. Der
Runtime-Loader akzeptiert die extern bereitgestellte Fullchain, erzeugt oder
rotiert sie aber nie.

## 8. Exakte Codeänderungen am Proxy

### 8.1 Neu/ersetzt

1. `proxy_gateway/proxy_gateway/src/interfaces/async_websockets_server.py`
   übernimmt Listener, Handshake, Pfadrouting, Client-Lifecycle,
   Transportantworten und WSS-Envelopes. Eventfilter, Live-Queue, Coalescing,
   RBAC und Replay bleiben Eigentum des Event-Cores.
2. `proxy_gateway/proxy_gateway/src/library/generate_ssl_certs.py` wird als
   explizites Lab-/Validierungswerkzeug integriert; kein Import im normalen
   Produktiv-Startup außer für den SSL-Context-Loader.
3. Verbindlich `src/core/client_command_dispatcher.py`: kleine funktionale
   Zuordnung `action → bestehende Command-Gateway-Funktion`. REST und WSS
   rufen exakt diese Zuordnung auf.

### 8.2 Angepasst

* `src/core/gateway_runtime.py`
  erstellt einen gemeinsamen WSS-Context mit injizierten Async-Adaptern für
  Auth/Revalidierung, Shared Dispatch sowie Open/Page/Update/Close/Consumed
  des Event-Cores. CSRF und Mutationsklassifikation existieren ausschließlich
  im Shared Dispatcher.
* `src/core/event_stream.py`
  erhält atomaren Subscription-Open mit DB-High-Watermark, paginierten Replay-
  Vertrag, harte Live-/Coalescing-Grenzen, Eventtyp-RBAC und monotone
  Subscription-Generationen. MQTT publiziert weiterhin nur hier; der WSS-
  Transport implementiert keinen zweiten Fanout.
* `src/core/command_gateway.py`
  erhält bei allen Domain-Actions optionale, ausschließlich intern erzeugte
  Dispatch-Metadaten (`idempotency_key`, `ttl_s`, `request_id`). Jede
  mutierende Funktion reicht Key und TTL an `_send` weiter; insbesondere gilt
  dies auch für `node.drain`, `node.state.set`, `node.admission.approve` und
  `terminal.execute`. Die Metadaten werden nicht als frei setzbare
  Payloadfelder akzeptiert. Ohne diese kleine Proxy-Änderung wäre die
  behauptete REST/WSS-Parität nicht erfüllt.
* `src/gateway_main.py`
  startet in einem `asyncio`-Lifecycle den direkten WSS-Server und Uvicorn als
  HTTP-only `uvicorn.Server`. Uvicorn erhält `ws="none"` und Port 8444.
* `src/interfaces/https_interface.py`
  entfernt den Import `WebSocketRoute` und die Route `/ws/v1/events`.
  REST-Handler delegieren an denselben Client-Command-Dispatcher.
* `src/interfaces/websocket_interface.py`
  wird nach erfolgreicher Migration entfernt oder zu einem klaren
  Kompatibilitätsmodul ohne Starlette-Abhängigkeit umbenannt. Zwei aktive WSS-
  Implementierungen sind verboten.
* `src/system/bootstrap_system_interface.py`
  validiert Ports, TLS-Dateien, Origin-Allowlist und Readiness beider Listener.
* `_config/application/gateway_config.json`
  übernimmt die Struktur aus
  `reference_implementation/proxy_gateway/gateway_config_v0015.example.json`.
* `compose.yaml`, Docker-Healthcheck, `deploy_test.sh`, `status.sh`, README,
  SBOM und Checksummen werden auf 8443/8444 sowie die direkte WSS-Dependency
  aktualisiert.
* `test/test_asgi_interface.py`
  wird ersetzt: Es muss jetzt statisch beweisen, dass keine `WebSocketRoute`
  existiert, Uvicorn `ws="none"` verwendet und der direkte Server
  `websockets.serve` aufruft.

### 8.3 Unverändert

* Master-Fachcode und Master-Protokolle;
* Mosquitto-ACL-/Topic-Modell, soweit keine neuen Proxy-Topics nötig sind;
* PostgreSQL-Schema, sofern der vorhandene Command-/Replay-Store alle neuen
  Aktionen bereits repräsentiert;
* Worker v0082 inklusive PKI-Installationspfad;
* Modbus-Simulator und Feldbuslogik.

Wenn bei der konkreten Umsetzung ein neues Datenbankfeld nötig wird, muss es
als additive, idempotente Migration eingeführt werden. Eine destruktive
Migration ist für v0015 nicht zulässig.

Für lokale Secret-erzeugende Actions gilt eine Sonderregel: Klartext-API-
Tokens werden nur in der ersten direkten Antwort ausgegeben und niemals im
Idempotency-Store persistiert. Ein Retry liefert Metadaten und
`secret_already_delivered=true`. Für `user.create` speichert die
Konflikterkennung nur einen keyed HMAC der kanonischen Payload; CSRF-, Session-
und Bearerwerte werden nie Teil des Fingerprints.

## 9. Gemeinsamer Dispatcher: wichtigste Invariante

WSS darf HTTPS nicht fachlich kopieren. Beide Adapter erzeugen einen
kanonischen internen Request:

```json
{
  "transport": "wss",
  "site_id": "site_main",
  "proxy_instance_id": "proxy-01",
  "principal": {},
  "request_id": "abc-123",
  "action": "config.propose",
  "idempotency_key": "unique-key",
  "ttl_s": 30,
  "csrf_token": "session-mutation-token",
  "session_token": "server-extracted-cookie-or-null",
  "payload": {}
}
```

Der gemeinsame Dispatcher führt genau einmal aus:

1. Action-Allowlist und Payloadschema;
2. Principal-, Site- und Resource-RBAC;
3. CSRF bei Session-Mutationen;
4. Rate Limit und Größenlimits;
5. Idempotency-Lookup beziehungsweise Konfliktprüfung;
6. Audit und persistente Command-Erstellung;
7. feste Master-Command-Zuordnung;
8. MQTT-Korrelation, Timeout und Ergebnisnormalisierung.

Identity-/Audit-Actions werden durch denselben Dispatcher auf bestehende
lokale Proxy-Stores geroutet; Domain-Actions ausschließlich auf die feste
Master-Command-Registry. Ein Adapter darf nie Actionnamen in MQTT-Topics
übersetzen.

Der Dispatcher muss eine Mutation zuerst dauerhaft identifizieren und
idempotent registrieren. Ein äußerer AsyncIO-Timeout darf einen laufenden
Thread nicht als „fehlgeschlagen“ melden. Kann ein bereits gestarteter Aufruf
nicht sicher abgebrochen werden, lautet die Semantik `accepted/pending` und der
Client verfolgt oder wiederholt mit derselben Idempotency-Key; niemals wird ein
zweiter Befehl mit neuer Identität erzeugt.

REST darf daraus HTTP-Statuscodes bilden; WSS bildet
`command.accepted/result/rejected`. Der fachliche Zustand und Fehlercode
müssen identisch bleiben.

## 10. Auto-Admission-Fehlalarm

### 10.1 Ursache

`test_worker_auto_admission.sh` liest in v0014 `nodes.authority_term`. Der
Worker v0082 liefert in Registrierung/Heartbeat keinen Master-Coordinator-Term,
weshalb dieses Node-Telemetriefeld korrekt `0` bleiben kann. Das Skript
interpretiert `0` jedoch als fehlende Master-Authority, obwohl der aktive Term
in `authority_state.authority_term` steht.

Die beobachteten Zustände

```text
APPROVED | ACTIVE | ACTIVE | term=0 | global=1 | node_revision=1
node_fingerprint == head_fingerprint | heartbeat=fresh
```

beweisen daher Konvergenz; nur das Shell-Gate prüft die falsche Spalte.

### 10.2 Minimaler Fix

Der beiliegende Patch
`patches/test_worker_auto_admission_v0015.patch` ersetzt ausschließlich die
Termquelle:

* `authority_state` über `site_id` per `LEFT JOIN` lesen;
* den Master-Term nur bei aktuell gültiger `lease_until` verwenden, sonst `0`,
  damit die übrige Node-Diagnose trotzdem ausgegeben wird;
* diesen wirksamen `a.authority_term` statt `n.authority_term` auswerten;
* Ausgabe eindeutig als `master-term` beschriften.

Node-Admission, Fingerprints und Revisionen bleiben unverändert. Regression:

* Node-Term 0 + aktiver Master-Term > 0 + exakte Konvergenz → PASS;
* abgelaufene Master-Lease → FAIL;
* fehlender oder nicht frischer Heartbeat → FAIL;
* abweichender Scope-Fingerprint oder Revision → FAIL.

## 11. E2E v0015

### 11.1 Ziel

Der neue E2E muss beweisen, dass fachliche Befehle und Ergebnisse ohne REST-
Commandpfad über das direkte WSS-Gateway laufen. HTTPS darf nur für Bootstrap-
Login oder unabhängige Sekundärpfad-Parität verwendet werden.

Vorgeschlagene Struktur im späteren Release:

```text
integration_test/e2e_v0015/
├── README_E2E_V0015_DE.md
├── VERSION
├── control_plane_wss_e2e_test.py
├── fn02_protocol_simulator.py
├── worker_cross_test_launcher.py
├── run_e2e.sh
├── test_static.sh
├── fixtures/
│   ├── config_mutation.json
│   └── cross_worker_overlay.json
└── schemas/
    ├── action_payloads.schema.json
    ├── command_request.schema.json
    ├── command_response.schema.json
    ├── event_subscription.schema.json
    ├── event_filter_update.schema.json
    ├── event_response.schema.json
    └── event_envelope.schema.json
```

### 11.2 Testphasen

#### Phase 0 – statische Gates

* Python-Syntax und Projekt-Code-Standard;
* keine Starlette/Uvicorn-WSS-Imports;
* kein `WebSocketRoute`, kein `websockets-sansio`;
* `ws="none"` für Uvicorn;
* direkte Verwendung von `websockets.serve`;
* JSON-Schemas, Mermaid und SVG parsebar;
* jeder Schema-`$ref` wird innerhalb des kopierten Schemaordners aufgelöst;
* Secret-Canaries erscheinen in keinem Proxy-/Protocol-/E2E-Log;
* Worker-v0082-Hash unverändert;
* Paketmanifest und Checksummen vollständig.

#### Phase 1 – TLS und Endpoint-Trennung

* verifiziertes CA-/Hostname-/SAN-TLS auf 8443 und 8444;
* Events- und Control-Pfad akzeptiert;
* unbekannter Pfad und Queryparameter werden abgewiesen;
* falsche Origin, fehlender Token, falscher Token und falscher Site-Scope
  werden fail-closed abgewiesen;
* Uvicorn akzeptiert keinen WebSocket-Upgrade.

#### Phase 2 – WSS-Leseoperationen

Über `/ws/v1/control`:

* `nodes.list`;
* `runtime.status`;
* `config.get`;
* `operation.get`.

Jede Antwort muss dieselben Daten und Berechtigungsgrenzen wie der sekundäre
HTTPS-Pfad besitzen.

#### Phase 3 – Subscription und Replay

* authentisierte `/ws/v1/events`-Verbindung;
* Filter für fn-01-Sensoren und Operationen;
* neuer Live-Sensorwert;
* Filterupdate mit Subscription-Generation und parallel eingeplantem Alt-Event;
* absichtlicher Reconnect mit `last_event_id`;
* mehr als eine Replayseite bis zur atomaren High-Watermark;
* Retention-Gap erzeugt `replay.resync_required` statt stillem Überspringen;
* verbotene Resource bleibt unsichtbar.

#### Phase 4 – Config-Mutation vollständig über WSS

1. aktuelle Revision/Fingerprint lesen;
2. reversible, unschädliche Teständerung vorschlagen;
3. `command.accepted` und Master-Commit erhalten;
4. Node-Shard-Rollout und exakte fn-01-Konvergenz beobachten;
5. dieselbe Idempotency-Key/Payload-Kombination erneut senden und dasselbe
   Ergebnis erhalten;
6. gleiche Idempotency-Key mit anderem Payload → Konflikt;
7. absichtlich stale Base-Revision → deterministische Ablehnung;
8. ursprüngliche Config über WSS wiederherstellen;
9. höhere monotone Revision, semantisch identische kanonische Config-/Scope-
   Fingerprints und erneute Worker-Konvergenz prüfen. Die alte Revisionsnummer
   darf und kann nicht wiederhergestellt werden.

Der Test muss den Restore in `finally` ausführen und bei unbestätigtem Restore
hart fehlschlagen.

#### Phase 5 – Aktuator vollständig über WSS

* Live-Sensor-Baseline und ursprünglichen Aktuatorwert empfangen/lesen;
* `actuator.request` mit `not original` über `/control`, damit immer eine
  echte Zustandsänderung geprüft wird;
* Master routet zum Owner fn-01;
* Worker schreibt den Modbus-Wert und liest ihn zurück;
* Operation erreicht `CONFIRMED`;
* `operation.result` erscheint über `/events`;
* neuer Sensorwert wird empfangen;
* Aktuatorwert wird über WSS exakt auf `original` restauriert und erneut
  durch Readback bestätigt.

#### Phase 6 – Parallelitäts-Smoke

* mindestens 128 parallele TLS/WSS-Verbindungen;
* Mischung aus Event-Subscriptions und ungefährlichen Lesecommands;
* begrenzte Handshake-/Command-Concurrency;
* keine unbounded Queue, kein Task-/Client-Leak nach Disconnect;
* kontrollierte Ablehnung bei konfigurierter Grenze.

Dies ist ein Regression-Smoke, kein 10K-Benchmark. Ein gesonderter Last- und
Soak-Test bleibt Release-N+1- beziehungsweise Produktionsreife-Gate.

#### Phase 7 – Cross-Worker-Simulation

Ein zweiter, klar markierter Protokollaktor `fn-02` simuliert ausschließlich:

* die nach dem v0014-Test-Lab-Rollout provisionierte MQTTS-mTLS-Identität
  `urn:automation:site_main:worker:fn-02`;
* Registration und frischen Heartbeat;
* initialen Full-Empfang;
* Patch nur nach deterministischer kleiner Folgemutation;
* `distributed.actuator.execute` mit In-Memory-Readback;
* Operation-Result mit `simulation=true`.

Deterministisches In-Memory-Chunk-Reassembly/Planung sowie negative
Coordinator-Term-, Execution-Epoch-, Deduplizierungs- und TTL-Fälle laufen
als isolierte vorhandene Master-/Orchestrator-Protokolltests. Restart-sichere
Simulator-Chunkpersistenz wird damit nicht behauptet. Diese Fälle dürfen
nicht durch künstliche Wildcard-ACLs oder manipulierte Live-Leases in den
Running-E2E eingeschleust werden.

Er simuliert ausdrücklich **nicht**:

* Worker-RuleEngine, FSM oder lokale Automation;
* echtes Device/Fieldbus-I/O;
* Safety-Logik;
* dauerhafte Worker-Journale unter allen Ausfällen;
* einen zweiten Standort.

Für einen vollständigen Cross-Worker-Pfad reicht ein Master-Cross-Shard allein
nicht: Das freigegebene Profil `worker_simulation` setzt lokal
`cross_runtime.enabled=false`, und `run_worker.sh` hat keinen
`config_path`-Schalter. Der E2E muss deshalb den normalen fn-01 kontrolliert
stoppen und über den externen `worker_cross_test_launcher.py` starten. Dieser
kopiert `app_config.yaml` in ein privates temporäres Verzeichnis, setzt dort
ausschließlich `profiles.worker_simulation.cross_runtime.enabled=true`, führt
`validate_worker_environment(config_path=<overlay>,
active_profile="worker_simulation")` aus und setzt vor
`src.sync_xserver_main.main()` den
`bootstrap_system_interface.CONTEXT["options"]["default_config_path"]` auf
diesen absoluten Overlaypfad. Danach werden Testprozess und Overlay entfernt,
der normale Worker gestartet und App-Config-/Worker-Archivhash erneut
verifiziert. Die v0082-Quellen und das Worker-Paket bleiben byte-identisch.
Ohne gestatteten Stop/Start, erfolgreiche Preflight- und Recovery-Prüfung ist
der Cross-Modus `BLOCKED`.

Die vorhandene Lab-Fixture kann als Ausgangspunkt dienen:

```text
Scope: cross:fn-01.default
Owner/Executor: fn-01
Input: Controller 62671112-766c-59ed-b0ff-30a49d48f747, Sensor 3
Threshold: 75
Target Owner: fn-02
Target: Controller d11032fa-33db-56de-a4ed-7e16cca3ded7,
        Actuator 4, Holding Register 544
```

Der Source-Trigger erfolgt ausschließlich über den erlaubten Modbus-
Simulatorpfad von fn-01. Der Test schreibt zunächst 74 und drainiert jede
dabei mögliche fallende/`else_do`-Operation vollständig. Erst danach setzt er
einen neuen Event-High-Watermark und öffnet das Messfenster für 74 → 76. Dort
darf genau ein steigender Cross-Intent entstehen. Das Messfenster wird nach
dem Resultat geschlossen; Restore auf den ursprünglichen Wert und mögliche
Restore-Operationen werden separat korreliert und drainiert. Direkte Writes in
Master-/Worker-Datenbanken sind verboten.

Der v0014-Test-Lab-Rollout provisioniert die feste fn-02-Lab-mTLS-Identität;
nur Simulatorprozess und MQTT-Session sind kurzlebig. Da v0014 keine
offizielle Node-/Admission-Delete-API besitzt, bleibt nach einer erstmaligen
Registration auf einem Fresh-System bewusst eine `APPROVED`/`MAINTENANCE`,
heartbeat-stale und observed-offline Lab-Registryzeile bestehen. Das Ergebnis
markiert diese einmalige Kommissionierung und den erwarteten Residualzustand.
Bei späteren Läufen wird der vorherige gültige Desired-Zustand restauriert.
Zertifikat und gemeinsam genutzte Lab-Test-CA werden weder gelöscht noch
rotiert. Audit-/History-Datensätze bleiben erwartungsgemäß erhalten; direktes
DB-Löschen und die Behauptung eines „exakt leeren“ Ausgangszustands sind
verboten.

Die feste Identität ist ein exklusives Lab-Fixture und darf niemals parallel
von einem realen oder zweiten fn-02-Prozess genutzt werden. Der `full-lab`-
Runner hält deshalb einen laufzeitweiten fn-02-Simulator-Lock und verifiziert
vor dem Connect sowohl in der Master-Registry als auch im Broker, dass keine
aktive fn-02-Session und kein frischer Heartbeat existiert. Nur die bekannte
stale/offline Fixture-Baseline ist zulässig. Ohne autoritative Broker-
Introspektion, bei Lockkonflikt oder aktivem fn-02 endet der Modus `BLOCKED`;
ein bestehender MQTT-Client darf nie durch eine doppelte Client-ID verdrängt
werden. Der Lock wird erst nach gestopptem Simulator, geschlossener Session
und stale Heartbeat freigegeben.

Der E2E muss beweisen:

1. fn-02 wird registriert, zugelassen und exakt konvergent;
2. fn-01 erhält genau den zugehörigen Cross-Shard;
3. ein kontrollierter Source-Fact auf fn-01 erzeugt den Remote-Intent;
4. Master validiert Term, Epoch, Scope und Target-Ownership;
5. nur fn-02 erhält die Ausführung;
6. simuliertes Readback und finales Resultat korrelieren zur Operation;
7. der Client empfängt das finale Cross-Ergebnis über `/events`;
8. die isolierten Master-Protokolltests weisen falschen Term/Epoch,
   doppelten Intent und abgelaufene TTL nachweislich ab;
9. alle reversiblen Teständerungen werden restauriert; eine beim ersten
   Fresh-Lauf erzeugte stale fn-02-Lab-Registryzeile wird als erwarteter,
   nicht löschbarer Fixture-Residualzustand ausgewiesen.

Das Ergebnis heißt „Cross-Worker-Protokollsimulation bestanden“, nicht
„Cross-Site produktiv abgenommen“.

### 11.3 Abnahmebedingungen

v0015 darf nur freigegeben werden, wenn:

* alle statischen und Unit-Tests grün sind;
* bestehende v0014-Proxy-Tests angepasst und grün sind;
* alle Running-Service-Probes grün sind;
* WSS TLS-Verifikation aktiv ist;
* Config- und Aktuator-Restore bestätigt sind;
* der sekundäre HTTPS-Pfad weiterhin funktioniert;
* Auto-Admission den echten Master-Term prüft;
* Master-/Worker-Containerimages beziehungsweise Worker-v0082-Paket gegenüber
  der freigegebenen Basis unverändert sind;
* keine unerwarteten Queue-, Task-, Connection- oder Command-Reste bleiben;
* Log und maschinenlesbare JSON-Zusammenfassung `PASSED` melden;
* ZIP-Hash, interne Checksummen, SBOM und Manifest konsistent sind.

## 12. Rollout-Strategie

### 12.1 Implementierungsreihenfolge

1. gemeinsamen Dispatcher aus bestehenden REST-Handlern extrahieren;
2. Unit-Tests für Transportparität erstellen;
3. direkten WSS-Server integrieren, zunächst auf temporärem Testport;
4. Events auf das neue Interface umstellen und Replay prüfen;
5. `/control` actions schrittweise an den gemeinsamen Dispatcher anbinden;
6. Uvicorn-WebSocket-Route entfernen und `ws="none"` setzen;
7. finale Portzuordnung 8443/8444 und Healthchecks aktivieren;
8. Auto-Admission-Patch anwenden;
9. E2E v0015 inklusive Restore und fn-02-Simulation ausführen;
10. Releaseartefakte, SBOM, Checksummen und Dokumentation erzeugen.

### 12.2 Rückwärtskompatibilität

* REST-Pfade und Semantik bleiben bestehen; der direkte interne Endpoint
  wechselt jedoch von Port 8443 auf 8444 und ist ohne L7 eine bewusste
  Breaking-URL-Änderung;
* `/ws/v1/events` behält logische Filter und Eventtypen;
* echte externe URL-Kompatibilität besteht nur, wenn ein verbindlich
  konfigurierter L7-Proxy `/ws/v1/* → 8443` und alle übrigen Pfade `→ 8444`
  unter der bisherigen externen Authority veröffentlicht;
* neue `/control`-Funktionalität ist additiv;
* Master- und Worker-Protokoll bleiben unverändert.

Vor Umschaltung ist ein kurzer Dual-Run ausschließlich auf verschiedenen
Ports zulässig. Es darf nie gleichzeitig zwei Eventserver mit derselben
öffentlichen Route geben, weil das Replay-/Connection-Verhalten sonst
uneindeutig wäre.

### 12.3 Rollback

Rollback ist ein Image-/Compose-Rollback auf v0014:

1. v0015 Readiness false und neue Connections sperren;
2. aktive Commands drainen oder eindeutig als pending/expired persistieren;
3. v0015 Proxy stoppen;
4. v0014 Proxy mit der bisherigen Portbelegung starten;
5. Datenbankmigrationen müssen additiv und v0014-kompatibel sein;
6. Health, Login, REST-Command und Event-WSS prüfen.

Config- oder Worker-Rollback ist für diesen Proxy-Transportwechsel nicht
erforderlich. Der Rollback darf keine Worker-v0082-Dateien verändern.

## 13. Observability

Mindestens folgende Metriken werden transportbezogen ergänzt:

* aktive Event-/Control-Verbindungen;
* akzeptierte und abgewiesene Handshakes nach Grund;
* Queue-Tiefe, Coalescing und Critical-Overflow;
* Command-Latenz und Resultat nach Action/Code;
* Replay-Anzahl und Replay-Lag;
* Event-Fanout-Latenz;
* TLS-/Origin-/Auth-Ablehnungen;
* Taskanzahl und sauber geschlossene Connections.

Labels dürfen keine Tokens, Nutzernamen, Request-Payloads oder hochkardinalen
IDs enthalten. Logs enthalten `request_id`, `operation_id`, Action, Site,
anonymisierte Principal-ID und Ergebniscode, aber keine Secrets.

## 14. Sicherheits- und Architekturentscheidungen

| Entscheidung | Begründung |
|---|---|
| zwei logische WSS-Pfade, ein Listener | klare Semantik ohne zweiten TLS-Port |
| WSS primary, REST secondary | effiziente bidirektionale Langzeitverbindung bei kompatibler Fallback-API |
| ein gemeinsamer Dispatcher | keine divergierende Business-/Security-Logik |
| kein Client-MQTT | Broker-ACL und interne Topics bleiben geschützt |
| Master bleibt Authority | Proxy-Transport kann Config nicht eigenmächtig committen |
| bounded Queues + Replay | kontrolliertes Verhalten bei Slow Consumers |
| kein WSS über Uvicorn | erfüllt die direkte `websockets`-Vorgabe |
| fn-02 als explizite Simulation | schneller Cross-Pfad-Test ohne falsche Produktionsbehauptung |
| HTTPS-Identity bleibt | sichere Browser-/Admin-Bootstrap-Kompatibilität |
| externe Produktions-PKI | keine privaten Schlüssel aus App-Autogeneration |

## 15. Offene Nachweise nach v0015

Auch ein vollständig grüner v0015-E2E beweist noch nicht:

* reale fn-02-Hardware und dessen lokale Automation;
* echtes Cross-Site-Routing mit separater Site-PKI und Failure Domain;
* 10.000 Verbindungen, maximale Eventrate oder 24–72 Stunden Soak;
* Proxy-HA, Master-Failover und PostgreSQL-Promotion unter aktiver Operation;
* Produktions-PKI-Rotation ohne Verbindungsverlust;
* alle Netzwerkpartitionen und „uncertain outcome“-Fälle.

Diese Grenzen sind keine Fehler der v0015-Architektur. Sie sind getrennte
Abnahmeklassen und müssen mit realen HA-/Last-/Fault-Injection-Umgebungen
bewiesen werden.

## 16. Definition of Done

Die Entwicklung gilt als vollständig, wenn alle folgenden Punkte abgehakt
sind:

- [ ] Direkter WSS-Server importiert ausschließlich `websockets` für RFC 6455.
- [ ] `/events` und `/control` laufen TLS-verifiziert auf 8443.
- [ ] Uvicorn läuft HTTP-only auf 8444 mit `ws="none"`.
- [ ] Keine `WebSocketRoute` und kein `websockets-sansio` im Proxy-Code.
- [ ] REST und WSS verwenden denselben Dispatcher und dieselben Policies.
- [ ] Alle mutierenden WSS-Aktionen sind idempotent, auditiert und gefenced.
- [ ] Auth-Ablauf, Revocation und Policyänderung schließen bestehende Sockets fail-closed.
- [ ] Eventfilter-Generationen, paginiertes High-Watermark-Replay, Gap-Signal,
      bounded Coalescing und Critical-Event-Verhalten sind getestet.
- [ ] Direkter Betrieb und gegebenenfalls L7-Betrieb verwenden eine konsistente,
      vertrauensgebundene Session-Source-IP-Policy.
- [ ] Alle post-authentisierten v0014-Features besitzen WSS-Actions und
      verbindliche Payloadverträge; Bootstrap/Health/Metrics sind bewusst HTTPS-only.
- [ ] Config-Mutation, Konvergenz und Restore laufen komplett über WSS.
- [ ] Aktuation, Hardware-Readback, Resultat und Restore laufen komplett über WSS.
- [ ] fn-02-Simulation testet Cross-Worker und ist überall als Simulation markiert.
- [ ] Auto-Admission liest den aktiven Master-Term aus `authority_state`.
- [ ] HTTPS-Sekundärpfad und Rollback funktionieren.
- [ ] Worker v0082 bleibt byte-identisch beziehungsweise paketunverändert.
- [ ] Code-Standard, Syntax, Tests, TLS, SVG/JSON, Manifest und Checksummen sind grün.
- [ ] Releasebericht benennt verbleibende Cross-Site-/HA-/Lastgrenzen ehrlich.

Erst nach Erfüllung dieser Liste darf das eigentliche Artefakt
`automation_control_plane_release_v0015.zip` gebaut und als produktionsnahes
Control-Plane-Release bezeichnet werden.

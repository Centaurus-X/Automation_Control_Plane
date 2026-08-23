# Quellenanalyse und Änderungsmatrix v0015

Diese Matrix dokumentiert die konkrete v0014-Ausgangsbasis und verhindert,
dass der WSS-Umbau versehentlich Master- oder Worker-Verantwortung verschiebt.

## Analysierte Quellen

* `automation_control_plane_release_v0014` einschließlich Proxy, Master,
  Rollout-Skripten und E2E v0014;
* `automation_system_v0082` zur Prüfung der Worker-Protokollgrenze;
* `CODE_AND_ARCHITECTURE_SPEZIFICATION(2).md` als Code-/Architekturvertrag;
* hochgeladene Ausgangsmodule `async_websockets_server.py` und
  `generate_ssl_certs.py`;
* v0014-Architekturreport und die bereitgestellten Rollout-/Diagnoselogs.

## Ist-zu-Soll-Matrix Proxy

| v0014-Datei | Ist | v0015-Vorschrift | Risiko/Gate |
|---|---|---|---|
| `src/gateway_main.py` | `uvicorn.run(..., ws="websockets-sansio")` | gemeinsamer AsyncIO-Lifecycle; direkter WSS-Task plus `uvicorn.Server(..., ws="none")` | beide Listener müssen atomar ready/failed sein |
| `src/interfaces/https_interface.py` | importiert und registriert `WebSocketRoute` | sämtliche WebSocket-Routen entfernen; nur HTTP-Routen | AST-Test verbietet `WebSocketRoute` |
| `src/interfaces/websocket_interface.py` | Starlette-Objekte, event-only | durch direkten `async_websockets_server.py` ersetzen | niemals beide Implementierungen aktiv |
| `src/core/gateway_runtime.py` | erzeugt v0014-WebSocket-Context | acht echte Async-Adapter für Auth/Revalidierung, Shared Dispatch und Event-Core-Vertrag inklusive Overflow-`disconnect_event` injizieren | keine zweite Auth-/Commandlogik; synchrone Adapter verboten |
| `src/core/event_stream.py` | Filter, bounded Queue, aber Replay/Live ohne atomaren Handoff | Eigentümer von RBAC, Queue, hart begrenztem Coalescing, High-Watermark/Pagination und Subscription-Generation | Critical Events dürfen nicht still verloren gehen |
| `src/core/command_gateway.py` | fachliche Master-Commands und Korrelation; TTL/Key nicht bei allen Wrappern durchgereicht | minimal um interne Dispatch-Metadaten ergänzen und TTL/Key für jede Mutation an `_send` reichen | WSS darf keine freien Topics erzeugen; Paritätstest prüft TTL/Key |
| `src/core/access_policy.py` | RBAC/Site/Resource | unverändert für REST und WSS verwenden | Paritätstest pro Action |
| `src/core/auth_management.py` | Session/API-Token | Handshake-Adapter; Cookie oder Bearer | keine Query-Tokens |
| `src/interfaces/mqtt_gateway_interface.py` | eine MQTTS-Schnittstelle | unverändert, geteilt | keine zweite Broker-Session pro Listener |
| `src/system/bootstrap_system_interface.py` | ein ASGI-Server | zwei Listener/Ports/TLS-Dateien validieren | Teilbereitschaft ist FAIL |
| `gateway_config.json` | HTTPS und WSS gekoppelt | getrennte Sektionen `wss:8443` und `https:8444` | Portkollision statisch verhindern |
| `compose.yaml` | Proxy veröffentlicht bisherigen kombinierten Port | 8443 WSS und 8444 HTTPS bereitstellen | Firewall/Healthcheck anpassen |
| `requirements.txt`/SBOM | `websockets==16.0` bereits vorhanden | Version halten; Uvicorn/Starlette für HTTP behalten | Lock/SBOM konsistent |
| `test/test_asgi_interface.py` | verlangt SansIO-WSS-Backend | invertieren: Uvicorn `ws=none`, kein WebSocketRoute, direkter Serve | statisches Release-Gate |

## Nicht zu ändernde Grenzen

| Komponente | Begründung |
|---|---|
| Master Config Authority | WSS ist nur ein Proxy-Transportadapter; Commits bleiben term-/revision-fenced im Master |
| Node-/Cross-Shard-Format | Cross-Simulation konsumiert den bestehenden Vertrag und definiert keinen neuen |
| Broker-Topologie | Proxy, Master und Worker bleiben Clients desselben Mosquitto-Brokers |
| Worker v0082 | lokale Automation, MQTT-only, Journale und Feldbus bleiben unverändert |
| Modbus-Simulator | v0015 ändert den Clienttransport, nicht Hardware-I/O |

Wichtige Testgrenze: Im Workerprofil `worker_simulation` ist
`cross_runtime.enabled=false`; `run_worker.sh` bietet keinen externen
Configpfad. Der full-lab-E2E darf deshalb nicht behaupten, ein Master-Overlay
aktiviere die lokale Cross-Runtime. Die Testvorschrift verwendet einen
externen Launcher mit privater App-Config-Kopie und setzt den vorhandenen
`bootstrap_system_interface`-Defaultpfad vor dem Runtime-Import. Originale
Worker-Quellen, App-Config und v0082-Archiv bleiben unverändert und werden vor
und nach dem Test gehasht.

## Defekte der hochgeladenen Ausgangsmodule

### Ausgangsmodul `async_websockets_server.py`

Releaseblocker im Ausgangszustand:

* projektfremde beziehungsweise nicht auflösbare Imports;
* keine verbindliche Authentisierung und Origin-Allowlist;
* kein RBAC-/Resource-Scope vor Event-Fanout;
* beliebige Pfade, Client-IDs, Commands oder Topics;
* keine Idempotenz, TTL, Audit- oder Master-Command-Grenze;
* unbounded oder inkonsistente Queues;
* nicht deterministisches Routing und mehrere mögliche Writer;
* unvollständige Cancellation-/Shutdown-Behandlung;
* Startupfehler konnten zu einem scheinbar laufenden System führen;
* kein Replay-/Slow-consumer-Vertrag.

Die Referenz ersetzt diese Punkte durch exakte Pfade, injizierte Policies,
bounded Queues, einen Reader/Writer je Verbindung, threadsicheren Eventübergang,
WSS-Envelopes, TLS und einen expliziten Lifecycle.

### Ausgangsmodul `generate_ssl_certs.py`

Releaseblocker im Ausgangszustand:

* aufgerufene Builder-Funktionen fehlten;
* Import hatte Seiteneffekte;
* keine robuste CLI und kein validate-only-Pfad;
* SANs wurden nicht strikt und deterministisch aus expliziten Identitäten gebaut;
* Key-Dateirechte waren nicht sicher garantiert;
* kein sauberer CA-/serverAuth-only-Aufbau;
* keine atomische Aktivierung und keine sichere Rotation;
* unvollständige Zertifikats-/Key-/CA-Prüfung.

Die Referenz trennt SSL-Context-Loading von expliziter Lab-PKI-Erzeugung,
validiert SAN/EKU/Key/CA/Zeitraum und schreibt private Schlüssel mit 0600.
Sie staged vollständig und ersetzt danach jede Datei atomar; ein
transaktionaler Multi-Datei-Bundle-Swap wird ausdrücklich nicht behauptet.

## Admission-Bug

| Prüfung | v0014 | v0015 |
|---|---|---|
| Node-Telemetrie-Term | `nodes.authority_term`, kann bei v0082 korrekt 0 sein | nicht als Master-Authority verwenden |
| echte Authority | vom Skript ignoriert | `authority_state.authority_term` |
| Leader-Lease | vom Skript nicht explizit gebunden | `lease_until > clock_timestamp()` |
| Ergebnis bei exakt konvergentem fn-01 | falsches FAIL möglich | PASS bei aktiver Master-Lease |

Der Patch liegt unter `patches/test_worker_auto_admission_v0015.patch` und
wurde gegen das unveränderte v0014-Skript mit `patch --dry-run` geprüft.

## Architekturkonformität

Die Referenzmodule erfüllen die projektspezifischen formalen Regeln:

* funktionaler Kontext über Dictionaries und `functools.partial`;
* keine Klassen, Dataclasses, Type-Hints, `typing`, Decorators oder Lambdas;
* englische Codekommentare;
* explizite Locks und Single-Writer-Queues statt GIL-Annahme;
* Dependency Injection für fachliche Komponenten;
* dritte Bibliothek `websockets` ist für diesen Transport ausdrücklich
  vorgegeben; `cryptography` bleibt auf Lab-/Validierungswerkzeug begrenzt.

Die endgültige Free-threaded-Freigabe erfordert trotzdem einen Lauf unter der
tatsächlichen Python-3.14t-Runtime. Die lokale Referenzprüfung unter Python
3.12 beweist Syntax und Verhalten, aber nicht die vollständige 3.14t-/No-GIL-
Laufzeitkompatibilität aller Drittanbieter.

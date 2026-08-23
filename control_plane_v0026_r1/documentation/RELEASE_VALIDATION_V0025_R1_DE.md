# Release-Validierung – Control Plane v0025-r1

## Identität

| Artefakt | Sollwert |
|---|---|
| Release-Verzeichnis | `automation_control_plane_release_v0025_r1` |
| Version / Revision | `0.25.0 / v0025-r1` |
| aktiver E2E | `e2e_v0025 / 0.25.0-e2e.3` |
| separater Worker | `automation_system_v0101 / 0.101.0` |
| Contract / Type | `2.0 / 2.0.0` |

## Statische und funktionale Nachweise

| Gate | Ergebnis |
|---|---:|
| Master-Testdateien | 40/40 PASS |
| Proxy-Testdateien | 17/17 PASS |
| Worker-Testdateien | 54/54 PASS |
| Release-Code-Standards | PASS |
| funktionaler AST-Style | 0 Klassen, Decorators, Lambdas oder Type Hints |
| Python-AST / JSON / YAML / UTF-8 / Shell | PASS |
| Proposal-/Relay-Grenze | PASS |
| Mirror-/Registry-Monotonie | PASS |
| Cross-Owner-Fencing | PASS |
| deterministische Master-V9-Fixture | PASS |
| Packaging-Sicherheitsgrenzen | PASS |
| Validation-Container-Vertrag | PASS |
| E2E-v0025-Static-Gates | PASS |

## Clean-Room- und Live-Abnahme

Der Lauf `20260726T230950Z-3358171` entfernte vor dem Build die bekannten alten
Container, acht Volumes, zwei Netze und fünf Images. Alle Release-Images wurden
ohne Cache neu gebaut; die Runtime kompilierte CPython 3.14.6t mit
`--disable-gil` aus den gepinnten Quellen.

Bestanden haben:

- Master-, Proxy- und Worker-Suites in ihren qualifizierten Runtimes,
- Mosquitto-mTLS, ACL, Identity, Delivery und Persistenz,
- PostgreSQL Primary/Standby, TLS, Rollen und Streaming-Replikation,
- Master-/Proxy-Migrationen, Transaktions- und Security-Probes,
- fünf gesunde Container ohne Master-Neustart,
- automatische, allowlist-gebundene Admission von `fn-01`,
- dauerhafte Worker-Heartbeats sowie Sensor- und Aktortelemetrie,
- reale Modbus-Lese-/Schreibzyklen gegen `192.168.0.3:5020`,
- exakte Konvergenz des Worker-Heads in Registry und Master-Mirror.

Der Live-Mirror führte `node:fn-01`, Owner und Quelle `fn-01`, Authority- und
Execution-Epoch `0`, Contract/Type `2.0/2.0.0` und das Fingerprint-Präfix
`0767595845b1`. Neun Stabilitätssamples lieferten jeweils HTTP 200, keine
Restarts und rund 56,5 bis 57,0 MB Container-Speicher.

## Im Rollout gefundene und geschlossene Fehler

1. Der Runtime-Health-Snapshot wurde als Funktionsobjekt statt als Ergebnis in
   den JSON-Pfad gegeben. Der Aufruf ist korrigiert und regressiv abgesichert.
2. Admission benötigte eine explizite Bootstrap-Allowlist. `fn-01` ist erlaubt;
   unbekannte Nodes bleiben fail-closed `PENDING`.
3. Readiness wurde authority-mode-sensitiv gemacht und behauptet in
   `node_local` keine Master-Authority.
4. Der kanonische Mirror-Record verlor bei `execution_epoch=0` das Top-Level-
   Feld. Worker, Master und Proxy verwenden nun denselben korrigierten Contract;
   eine Worker-SQLite-Regression beweist den Roundtrip.
5. Der initiale PostgreSQL-Mirror-Upsert verwendete `FOR UPDATE` auf der
   nullable Seite eines `LEFT JOIN`. `FOR UPDATE OF h` sperrt nur den
   Authority-Head; eine funktionale Fake-DB-Regression beweist den ersten
   Snapshot und das exakte SQL-Lock-Scope.
6. Die Node-local-Routingprojektion übernimmt den normativen operativen
   Site-Schlüssel `site-main` für Registry-Leases, ohne historische
   V5-Business-Schlüssel im Worker-Mirror umzuschreiben.
7. Migration 004 erlaubt den semantisch korrekten Registry-Fence
   `config_revision=0` in `node_local`; das Live-Gate bestätigte drei aktive
   Controller-Leases sowie fortlaufende Master- und Proxy-Telemetrie.

## Archivfreigabe

Control Plane und Worker werden getrennt gebaut. Der Control-Plane-Builder
verlangt ZIP und Sidecar des Workers und pinnt dessen finalen ZIP-Hash sowie
den Hash des internen `SHA256SUMS`. Root-, Komponenten- und E2E-Manifeste
müssen den jeweiligen Baum exakt decken; Zugangsdaten und generierter
Runtimezustand sind in beiden Archiven verboten.
## Nachqualifikation des Artefakt-Core-E2E

Der erste vollständige Kandidaten-Rollout bestätigte den sauberen Aufbau,
PostgreSQL-Replikation, `fn-01`-Admission, reale Modbus-/MQTT-Telemetrie und
Worker-Neustart-Recovery. Er stoppte anschließend korrekt an einem veralteten
Cross-Fixture-Pin und an der parallelen Roh-JSON-Fingerprintprüfung. Beide
Ursachen sind geschlossen. Danach bestanden erneut Worker 54/54, Master
40/40, Proxy 17/17, alle Control-Plane-Standards-Gates sowie der vollständige
Linux-E2E-Static- und Selbsttest. Freigabebedingung bleibt ein frischer
Rollout aus den exakt finalen ZIP-/Sidecar-Artefakten.

## Abschlussnachweise für ACK, Schema und Replay

Nach den letzten Korrekturen bestanden erneut:

- Worker: 54/54 Testdateien, sechs Shell-Syntaxgates, zwei Preflights,
- Master: 40/40 Testdateien und 91 geprüfte Python-Dateien,
- Proxy: 17/17 Testdateien,
- die gezielten Proposal-Policy-, Envelope-, Recovery-Schema-, CAS- und
  Replay-Selbsttests.

Der reale Pfad belegte nacheinander zwei unabhängige terminale Fehlerbilder:

1. Der Worker persistierte den Proposal-ACK korrekt, verweigerte aber die noch
   nicht freigegebene Section `processes` mit `path_denied`.
2. Nach dieser Policy-Korrektur hätte der Master das Ergebnis wegen der
   ungültigen prefixed `causation_id=relay:<uuid>` nicht als kanonisches
   Envelope publizieren können. Der Regressionstest baut deshalb das echte
   Envelope für Accepted und Rejected und verlangt die rohe UUID oben sowie
   die Relay-ID ausschließlich im Payload.

Der getrennte Worker-Release ist final gepinnt:

| Worker-Nachweis | SHA-256 |
|---|---|
| `automation_system_v0101.zip` | `c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467` |
| internes `SHA256SUMS` | `699c29f9cf58b1bd6bd7407e879b3b5b3f41ad978343037a1dc33a75ec25026d` |

Die Abschlussfreigabe erfolgt ausschließlich durch einen Fresh-Rollout aus
den nach dieser Dokumentation neu gebauten Control-Plane-ZIP-/Sidecar-Dateien
und dem oben gepinnten separaten Worker-Archiv. Danach müssen Core-E2E,
Config-File-E2E, Neustart/Telemetry und exakte Artefaktprüfung gemeinsam PASS
melden.

## Revision-0-Regressionsgate für `config.diff`

Der Artefakt-Fresh-Lauf bestätigte den vollständigen kanonischen
`config.propose`-Pfad einschließlich Worker-ACK, nativer Operations, exakter
Mirror-Konvergenz und kompensierendem Restore. Der anschließende, bewusst
veraltete `config.diff`-Negativpfad verwendete jedoch die gültige Node-Scope-
Revision `0` in einer Legacy-WSS-Aktion, deren JSON-Vertrag ausschließlich
positive globale Revisionen zulässt. Dadurch stoppte der E2E-Client korrekt
vor dem Transport.

Der Negativtest erzeugt nun einen schema-konformen Probe-Payload (`0 -> 1`
wird `1 -> 2`), erreicht damit die Proxy-Policy und verlangt weiterhin exakt
`unsupported_operation_format`. Die produktive Authority-Semantik wurde nicht
gelockert. Der lokale AST-Test prüft `0 -> 1` und positive Revisionen; der
Linux-E2E-Self-Test validiert zusätzlich das vollständige Request-Envelope.

## Diagnosegate für den expliziten File-Routenvertrag

Der Kandidatenlauf `20260727T092722Z-4136939` ist bewusst **keine** Freigabe:
Er bestand Deployment, Service-Health, Worker-Neustart/Sequenz-Recovery und den
Core-E2E (`0.25.0-e2e.2`, 52 Checks), stoppte jedoch im nachgelagerten
Worker-Dateitest. Die produktiven Belege zeigten für die betroffene Phase
korrekt `commit_kind=action_plan`; allein der E2E-Aufruf hatte mangels
expliziter Route den Prüfer-Default `structural` verwendet.

Der korrigierte Build `0.25.0-e2e.3` besitzt keinen impliziten Routen-Default
mehr. Der Phasenvertrag umfasst genau sechs Tupel aus Phase, Scope und Route;
ein Regressionstest verlangt für jede Aufrufstelle die eindeutige Bindung an
dieses Tupel und verwirft fehlende, leere oder unbekannte Routen. Nach dem
abgebrochenen Kandidatenlauf bestätigte das separate Recovery-Journal den
Zustand `RECOVERED`, den exakten ursprünglichen V9-Fingerprint sowie einen
neuen lokalen Authority-Commit mit Mirror-ACK. Freigabebedingung bleibt ein
vollständiger Fresh-Rollout aus dem neu gebauten ZIP und Sidecar.

## Vollständiger Authority-V5-Artefaktlauf und LF-Nachqualifikation

Der vollständige Lauf `20260727T101132Z-16030` bestand Deployment,
Service-Health, Worker-Install/Restart/Telemetry, den Core-E2E mit 52 Checks
und den Worker-Datei-E2E mit sechs reversiblen Übergängen. Policy-Fallback,
Structural und Runtime wurden jeweils über den expliziten Routenvertrag
committed; sämtliche Mirror-IDs wurden bestätigt und der ursprüngliche
Fingerprint exakt wiederhergestellt.

Eine anschließende Bedienprüfung des optionalen Worker-Service-Managers fand
ein eingebettetes Carriage-Return in dessen Pfadzuweisung. Das Skript und der
Validation-Container-Starter sind auf LF normalisiert. Beide Release-Builder
besitzen nun ein fail-closed CR-Gate mit Regression. Die Worker-Suite umfasst
54/54 Tests; alle ausgelieferten Shellskripte beider Projekte sind LF-only.
Die finale Freigabe verlangt erneut einen Fresh-Rollout aus den nach dieser
Korrektur neu gebauten und exakt gepinnten separaten Archiven.

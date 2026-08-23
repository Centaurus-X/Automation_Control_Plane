# Tiefendiagnose Control Plane Authority V5 – v0025-r1

## Befund

Der Ausgangsstand v0020-r4 war funktional breit abgesichert, koppelte aber
mehrere historische Autoritätsmodelle und Contract-Pakete. Besonders kritisch
waren drei Driftklassen:

1. Master-globales Denken konnte in `node_local` Node-Wahrheit überschreiben.
2. Proxy, Master und Worker konnten Config-Operationen unterschiedlich
   normalisieren oder mappen.
3. Mehrere physische Contract-/Type-Wurzeln ermöglichten abweichende
   Fingerprints und Shards.

## Korrigierter Datenfluss

`Client -> Proxy -> Master Relay -> owning Worker -> SEM -> Authority Commit`

- Der Proxy authentifiziert, autorisiert die adressierten Sections und prüft
  die kanonische Form. Er mappt keine Operation.
- Der Master validiert Envelope, Scope, Target und Form, journalisiert das
  Relay und leitet dieselbe Operationsliste weiter. Er vergibt keine
  Node-Revision.
- Der Worker prüft Principal, Authority-Epoch, Basisrevision, Fingerprint,
  Safety und Ownership; nur er akzeptiert oder verwirft.
- Das Ergebnis wird als exakter Journal-/Mirror-Beweis zurückgeführt und erst
  nach durablem ACK an den Proposal-Ursprung publiziert.

## Fail-closed-Grenzen

- Legacy Path-/Selector-Form am öffentlichen `node_local`-Boundary:
  `unsupported_operation_format`.
- Falscher Principal: `principal_denied`.
- Fremder Scope oder Node: `path_denied`.
- Alte Revision: `stale_revision`.
- Alte Authority- oder Execution-Epoch:
  `authority_epoch_stale` / `execution_epoch_stale`.
- Ungültiger oder nicht lowercase Fingerprint: `invalid_payload`.

## RRMA-Zuständigkeiten

| Funktion | Worker | Master | Proxy |
|---|---:|---:|---:|
| Node-Config entscheiden | ja | nein | nein |
| SEM/Safety ausführen | ja | nein | nein |
| Registry/Owner sequenzieren | nein | ja | nein |
| Mirror/Audit persistieren | Outbox/Quelle | ja | Audit-Rand |
| Auth/RBAC | Principal-Bindung | Relay-Bindung | ja |
| Config-Operations mappen | nein | nein | nein |

## Tiefendiagnose des realen Rollouts

Der Clean-Room-Lauf machte zwei Interoperabilitätsfehler sichtbar, die isolierte
Komponententests zuvor nicht gemeinsam erreicht hatten:

1. Mirror-Requests wurden vom Worker erfolgreich abgeschlossen, der Announce
   scheiterte jedoch, weil der kanonische Record `execution_epoch=0` nicht als
   Top-Level-Feld führte. Der Contract wurde in Worker, Master und Proxy
   byte-identisch ergänzt; die Worker-Regression prüft Record und SQLite-Outbox.
2. Danach akzeptierte der Master den Announce, lehnte aber den ersten Snapshot
   mit `FOR UPDATE cannot be applied to the nullable side of an outer join` ab.
   Der Store sperrt nun ausschließlich den nicht-nullbaren Authority-Head mit
   `FOR UPDATE OF h`; die neue Master-Regression prüft SQL und Erstpersistenz.
3. Das finale Telemetrie-Gate belegte eine getrennte Identitätsgrenze: Der
   gültige V5-Business-Schlüssel `site_main` aus dem unveränderten Worker-Mirror
   wurde als operativer Registry-Schlüssel behandelt, obwohl Transport,
   Admission und ACL normativ `site-main` verwenden. Die routing-only
   Topologieprojektion bindet Lease-Routen nun an die einmalig aufgelöste
   operative Site-ID, ohne den autoritativen Worker-Inhalt zu mutieren.
4. Anschließend verhinderte der historische SQL-Check `config_revision > 0`
   die Registry-Lease in `node_local`, wo die globale Master-Revision korrekt
   `0` bleibt. Migration 004 erweitert ausschließlich diesen Registry-Fence auf
   `config_revision >= 0`; positive Legacy-Revisionen bleiben unverändert.

Zusätzlich wurden der zuvor nicht ausgewertete JSON-Runtime-Snapshot, die
mode-sensitive Readiness und die explizite Admission-Allowlist regressiv
abgesichert. Die abschließende Live-Abfrage zeigte `fn-01` als
`APPROVED/ACTIVE`, fortlaufende Heartbeats und einen exakten Mirror mit
Owner/Quelle `fn-01`, Epochs `0/0`, Contract/Type `2.0/2.0.0` und Fingerprint
`0767595845b1…`.

## Verifikation

- Master: 40/40 Testdateien PASS.
- Proxy: 17/17 Testdateien PASS.
- Worker: 54/54 Testdateien PASS.
- Contract- und Type-Dateien sind zwischen Worker, Master und Proxy
  byte-identisch.
- Die drei alten Contract-Verzeichnisse fehlen; ausführbare Imports verwenden
  ausschließlich `src.library`.
- E2E-v0025-Static-Gates sowie neun Master-Stabilitätssamples bestanden.
- MQTT, PostgreSQL-Replikation, reale Modbus-Telemetrie und Live-Konvergenz
  wurden auf den drei getrennten Testhosts verifiziert.
## Nachtrag aus dem ersten Artefakt-Core-E2E

Der saubere Kandidatenlauf `20260727T013554Z-3573014` bestand Build,
Migration, Replikation, Admission, reale Telemetrie und Worker-Neustart-
Recovery. Anschließend schlossen zwei bewusst harte E2E-Gates weitere
Release-Drifts:

1. Die Cross-V8-Fixture war fachlich korrekt auf die operative Site-ID
   `site-main` aktualisiert worden, ihr gepinnter Quellhash zeigte jedoch noch
   auf den Stand vor dieser Änderung. Der Pin lautet nun
   `9c6e8b9f4128c4b061fcfb9bd2a5a2d0fc9ae2c2f9cfe7c97155bbc6bc74c2f2`;
   der E2E-Selbsttest prüft Datei und Pin vor jeder Netzmutation.
2. `config.get` prüfte einen fremden Roh-JSON-Hash, obwohl ausschließlich die
   Type Authority Config-Fingerprints bilden darf. Gleichzeitig konnte der
   Persistence Contract einen gruppierten Runtime-Snapshot in den Mirror
   übernehmen. `make_scope_snapshot()` kanonisiert nun in Worker, Master und
   Proxy byte-identisch zur Storage-Form. Der E2E delegiert die Prüfung direkt
   an `_generic_type_library.config_fingerprint()` und behält die zusätzliche
   flache V9-/V8-Storage-Prüfung bei.

Damit wurden weder Test noch Vertrag gelockert: Die zentrale Authority wurde
verstärkt und die konkrete Transportdarstellung deterministisch gemacht.

## Terminales Proposal-Ergebnis und Recovery-Invariante

Die persistente Diagnose über Proxy-Replay, Master-Inbox/-Outbox und Worker-
Inbox trennte Transport und Fachentscheidung eindeutig:

- Der Request erreichte den Worker und wurde durable abgeschlossen.
- Ein fachliches `path_denied` für `processes` war die erste Ursache; die
  Worker-Basisrichtlinie führt diese Section nun explizit und testet zugleich
  die fortbestehende Sperre für `credentials`.
- Der Master erzeugte danach für das Client-Ergebnis eine prefixed Relay-ID im
  Top-Level-Feld `causation_id`. Der kanonische Envelope-Vertrag verlangt dort
  eine UUID. Da `_publish_event()` fail-closed arbeitet, blieb das WSS-Event
  aus. `_publish_config_proposal_result()` bindet nun die rohe Proposal-UUID;
  `relay:<uuid>` bleibt unverändert als Payload-Korrelation erhalten.
- Der Proxy publiziert und cached den Event vor dem Waiter-Signal. Damit sind
  Live-Stream, Replay und synchroner Command-ACK in derselben dauerhaften
  Reihenfolge beobachtbar.

Ein absichtlich zurückgesetztes Worker-Journal gegenüber einem bestehenden
Node-local-Mirror wurde mit `stale_revision` verweigert und als `DEGRADED`
gemeldet. Das ist kein Anlass für direkte Datenbankkorrekturen. `RESET=yes`
ist nur zulässig, wenn der Fresh-Rollout Registry und Worker-Baseline gemeinsam
neu aufsetzt. Reuse/Restart erhält das Journal; Recovery verwendet den
expliziten Authority-/Restore-Vertrag.

## Diagnose: Revision `0` trifft Legacy-`config.diff`

Der frische Node-local-Head `0` ist für Mirror, CAS und Proposal korrekt. Er
ist aber kein gültiger Wert für die weiterhin globale Legacy-Aktion
`config.diff`, deren WSS-Schema `from_revision >= 1` und `to_revision >= 1`
fordert. Der frühere E2E-Negativtest vermischte beide Revisionsräume und wurde
daher bereits vom eigenen Client-Schema blockiert.

Die Diagnose führte bewusst zu keiner Vertragslockerung: Der Test normalisiert
nur den Legacy-Probe-Payload auf positive, streng aufsteigende Revisionen und
beweist anschließend die mode-sensitive Proxy-Ablehnung
`unsupported_operation_format`. Die tatsächliche Node-local-Mutation und der
Restore verwenden weiterhin die kanonischen Scope-Revisionen ab `0`.

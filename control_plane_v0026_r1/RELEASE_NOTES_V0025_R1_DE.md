# Release Notes – Control Plane v0025-r1

`automation_control_plane_release_v0025_r1` implementiert den RRMA-Zielzustand
der Authority-V5-Spezifikation. Worker und Control Plane bleiben physisch,
versionell und im Rollout getrennte Projekte.

## Architektur

- Worker: finale Authority seines Node-Shards (`node_local`).
- Master: Registry, Relay, Mirror und Audit; keine Rule-, FSM- oder
  Automation-Ausführung und keine eigenmächtige Node-Revision.
- Proxy: Authentifizierung, RBAC, Transportvalidierung und unverändertes Relay
  kanonischer Operations.
- Cross: Registry-sequenzierter Owner-Accept mit monotoner `execution_epoch`
  sowie Freeze-/Activation-Proof.

## Verträge

- einzige Type Authority: `src/library/_generic_type_library.py`, Version 2.0.0
- einzige Contract Authority: `src/library/_generic_system_contract_library/`,
  Version 2.0
- einziges Proposal-Format: `config_scope_operations_v1`
- Fingerprints und Value-Hashes: lowercase SHA-256-Hex
- operative Topic-, PKI- und Transport-Defaults: `site-main`

Master-Mirror und Registry vergleichen das vollständige monotone Tupel
`(authority_epoch, scope_revision, execution_epoch, fingerprint)`. Transfer und
Restore bleiben bis zum exakten Proof und ACK fail-closed.

## Qualifizierte Korrekturen

- Runtime-Snapshots werden vor der JSON-Ausgabe ausgewertet; eine Regression
  und ein Neun-Sample-Stabilitätsgate decken Speicher und Readiness ab.
- Readiness unterscheidet `node_local` und `master_global` semantisch korrekt.
- Bootstrap-Auto-Admission ist auf die konfigurierte Worker-Allowlist begrenzt.
- Mirror-Records erhalten `execution_epoch` auch beim gültigen Wert `0` in
  allen drei byte-identischen Contract-Kopien.
- Der PostgreSQL-Mirror-Upsert verwendet `FOR UPDATE OF h` und sperrt beim
  `LEFT JOIN` nie die nullable Snapshot-Seite.
- Die routing-only Registry-Projektion bindet historische V5-Business-Sites an
  die einmalig aufgelöste operative Site-ID, ohne Worker-Config zu mutieren.
- Migration 004 erlaubt im `node_local`-Registry-Fence die normative globale
  Revision `0`; Legacy-Revisionen und Worker-Scope-Revisionen bleiben getrennt.
- Ein Proposal-Ergebnis wird erst nach durablem Worker-ACK publiziert.

## Live-Qualifikation

Der Clean-Room-Lauf baute CPython 3.14.6t und sämtliche Images neu. MQTT-mTLS,
ACL, PostgreSQL-Replikation, Security- und Transaktionsprobes bestanden. Fünf
Dienste blieben gesund; `fn-01` wurde automatisch zugelassen, publizierte
reale Modbus-Telemetrie und konvergierte exakt in Registry und Mirror.
## Nachtrag: Artefakt-E2E-Härtung

Der Cross-Fixture-Pin folgt nun dem tatsächlich paketierten `site-main`-Stand
und wird im Selbsttest vor jeder Netzmutation geprüft. Scoped `config.get`
verwendet keine parallele Roh-JSON-Fingerprintlogik mehr. Worker, Master und
Proxy speichern Mirror-Snapshots über den byte-identischen Persistence
Contract ausschließlich in der kanonischen V9-/V8-Storage-Darstellung; der
Fingerprint wird allein von der absoluten Type Authority gebildet.

## Abschlusskorrekturen des vollständigen WSS-/Relay-Pfads

- Der Proxy legt ein korreliertes `config.proposal.result` zuerst in Replay-
  Cache und Event-Stream ab und signalisiert erst danach den internen Waiter.
  Damit kann ein schnelles ACK nicht mehr vor seiner beobachtbaren
  Publikation aus dem Request-Pfad zurückkehren.
- Der Master verwendet für die Top-Level-`causation_id` des Proposal-
  Ergebnisses ausschließlich die rohe Proposal-UUID. Die interne Identität
  `relay:<uuid>` bleibt als `relay_message_id` im Payload; sie wird nicht mehr
  fälschlich in ein UUID-Feld geschrieben.
- Die Worker-Safety-Policy erlaubt gezielt kanonische Vorschläge für
  `processes`, während nicht freigegebene Sections weiterhin `path_denied`
  liefern.
- Revision `0` ist in Fresh-Mirror- und Recovery-Schemas normativ gültig.
  Neue Entities verwenden einen expliziten CAS-Nullwert; Bool-Werte bleiben
  für Revisionen und Hash-Felder unzulässig.
- Schemafehler nennen den tiefsten begrenzten Leaf-Pfad. Das E2E-Pagination-
  Gate erzeugt ausschließlich dauerhaft terminal bestätigte Remote-Aktionen
  und beweist Replay über mehr als 200 Events ohne lokales Audit-Shortcut.

Der finale separate Worker-Pin lautet
`c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467`;
sein internes Manifest ist auf
`699c29f9cf58b1bd6bd7407e879b3b5b3f41ad978343037a1dc33a75ec25026d`
fixiert.

## Node-local Revision-0 im E2E-Negativpfad

- Ein frischer Node-Shard beginnt normativ bei Scope-Revision `0`.
- Die Legacy-Aktion `config.diff` bleibt dagegen ein globaler V1-Lesepfad und
  verlangt im WSS-JSON-Vertrag positive Revisionen. Im `node_local`-Modus muss
  sie unabhängig vom Payload fail-closed mit `unsupported_operation_format`
  abgelehnt werden.
- Der E2E-Negativtest bildet deshalb den frischen Zustand `0 -> 1` auf den
  vertraglich gültigen Probe-Payload `1 -> 2` ab. Der reale kanonische
  Proposal-/ACK-/Restore-Pfad bleibt unverändert `0 -> 1 -> 2`.
- Ein Self-Test baut das echte `command.request`-Envelope und validiert diesen
  Revision-0-Fall vor jeder Live-Ausführung gegen das ausgelieferte Schema.

## Abschluss: einheitliche node-lokale File- und Relay-Authority

- Der Worker-JSON-E2E verwendet für alle sechs reversiblen Übergänge dieselbe
  Worker-lokale SEM-Authority wie der native Relay-Pfad.
- Jeder Übergang beweist einen unveränderlichen Authority-Journal-Eintrag, eine
  eindeutige Mirror-ID und deren exaktes ACK. Ein Master-Outbox- oder
  Master-Commit-Beleg ist ausdrücklich unzulässig.
- Der Core-E2E exportiert den echten `distributed.config.propose`-Inboxbeleg
  samt Operation-ID, Relay-ID, Epoch, Revision, Zielfingerprint,
  Replica-Fingerprint, Operationsformat und Operationsanzahl.
- Die beiden separaten fail-closed Verifier lehnen veraltete Master-first-,
  Native-Patch- und Restore-Beweise ab.

## E2E-Build 0.25.0-e2e.3: expliziter File-Routenvertrag

Der frische Artefaktlauf `20260727T092722Z-4136939` bestand den Core-E2E mit
52 Checks, stoppte danach aber fail-closed im Worker-JSON-E2E. Die Diagnose
ergab keinen Worker-Fehler: WAL, Authority-Journal und Mirror belegten den
korrekten node-lokalen `action_plan`. Im E2E-Aufruf fehlte lediglich die
explizite Erwartung `runtime`; dadurch griff der veraltete Prüfer-Default
`structural`.

- Alle sechs reversiblen File-Phasen binden nun Phase, Authority-Scope und Route
  in einem einzigen unveränderlichen Testvertrag.
- `runtime_candidate` und `runtime_restore` verlangen explizit `action_plan`;
  strukturelle und Policy-Fallback-Phasen verlangen `scope_operations`.
- Ein fehlender oder unbekannter Routenwert wird ohne Default fail-closed
  abgelehnt.
- Der Linux-Static-Gate prüft jede der sechs Aufrufbindungen sowie positive und
  negative Routenfälle. Der Recovery-Pfad stellte nach dem Diagnoseabbruch den
  exakten V9-Baseline-Fingerprint mit neuem Authority-Commit und Mirror-ACK
  wieder her.

## Abschlusskorrektur: LF-gesicherte Auslieferung

- Das optionale Worker-Service-Management und der Validation-Container-Start
  werden ausschließlich mit LF-Zeilenenden ausgeliefert.
- Worker- und Control-Plane-Builder verwerfen jedes Shellskript mit einem
  Carriage-Return fail-closed vor Manifest- und Archiverzeugung.
- Beide Packaging-Grenzen besitzen ausführbare Regressionen; der vollständige
  Control-Plane-Standardslauf prüft zusätzlich sämtliche Shellskripte.

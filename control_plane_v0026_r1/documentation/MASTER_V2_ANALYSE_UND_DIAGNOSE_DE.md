# Master v2 – vollständige Analyse und Diagnose

> Historischer Diagnosebeleg für v0013. Dieses Dokument ist im Release
> v0025-r1 nicht normativ. Verbindlich sind `MASTER_V2_ARCHITEKTUR_DE.md`,
> `DIAGNOSE_AUTHORITY_V5_V0025_R1_DE.md` und die Authority-V5-Spezifikation.
> Insbesondere gelten Aussagen über eine globale Master-Config-Authority nicht
> für den Standardmodus `node_local`.

Stand: 16. Juli 2026  
Release: `automation_control_plane_release_v0013` / `0.13.0`

## 1. Prüfumfang und Quellen

Die Analyse umfasst die vollständigen gelieferten Archive sowie die beiden
verbindlichen Spezifikationen:

- `automation_control_plane_release_v0011`: 326 ZIP-Einträge, Basis für
  Broker, Datenbank, Proxy, Registry, Admission und Deployment;
- `automation_system_v0082`: 150 ZIP-Einträge, Referenz für lokale
  Automation, Worker-/Cross-Threads, Feldbusownership und v1-Protokoll;
- `CODE_AND_ARCHITECTURE_SPEZIFICATION.md`;
- `MASTER_V2_PLANUNGSDOKUMENTATION_DE_v1.1.md`.

Die Quelldokumente liegen unverändert unter `documentation/reference/`. Die in
der Planung genannte unklare Cross-Formulierung ist in der gelieferten
Code-/Architekturspezifikation bereits konsistent: Logik über mehr als einen
Controller ist Cross – unabhängig davon, ob die Controller auf demselben oder
auf unterschiedlichen Nodes liegen.

Geprüft wurden insbesondere:

- alle Python-, Shell-, SQL-, Compose-, JSON- und YAML-Dateien,
- Imports, Composition Roots und Lifecycle-Pfade,
- globale Config, Entity-Roundtrip, Graph und Migrationsmatrix,
- MQTT-Topics, mTLS-Identitäten und ACLs,
- PostgreSQL-Transaktions-/Outboxmodell,
- Proxy-Config-CRUD und Authentisierung,
- Worker-v0082-Registration, Heartbeat, Full, Cross, Proposals,
  Aktoroperationen und SQLite-Journale,
- Fresh-/Upgrade-/Recovery-Skripte und Ergebnisbehauptungen.

## 2. Ausgangsbefund v0011

### 2.1 Falsche Master-Grenze

v0011 war nicht nur Config Authority. Der Master enthielt eine vollständige
globale Ausführungsschicht:

- `_automation_thread.py`,
- `automation_authority.py`,
- `global_automation.py`,
- `_automation_engine.py`,
- `_event_interface.py`,
- `_generic_finite_state_machine.py`,
- `_generic_rule_engine.py`,
- `_state_shard_manager.py`.

Damit bestand ein zweiter logischer Rule-/FSM-/Automation-Owner neben dem
Worker. Der Sensor-Ingest konnte Master-seitige Entscheidungen auslösen. Das
widersprach der Zielgrenze „Master koordiniert, Worker führt aus“ und machte
lokale Autonomie sowie Exactly-one-Writer unnötig schwer beweisbar.

v0013 entfernt diese acht Module, ihre Imports, Lifecycle-Aufrufe und Status-
Behauptungen. Der Architekturtest durchsucht den Master zusätzlich nach
RuleEngine-/FSM-/Automation-Factories und Feldbusabhängigkeiten.

### 2.2 Monolithische Revision statt Scope-Cursor

v0011 nutzte die globale Config-Revision als primären Synchronisationsstand.
Damit konnte eine Änderung an fn-01 einen unveränderten fn-02-Worker künstlich
stale machen. Persistente, unabhängig versionierte Node-/Cross-Snapshots und
lückenlose Scope-Ops fehlten.

v0013 trennt:

- `global_revision` für den kanonischen Commit,
- `scope_revision` und Fingerprint pro Node-/Cross-Scope,
- lokale Worker-Revision und Live-Fingerprint,
- `execution_epoch` für Ausführungsfencing.

### 2.3 Unvollständiger Rolloutpfad

v0011 konnte v0082-kompatible Full-Snapshots verteilen, besaß aber keinen
normativen Scope-Patchpfad und keinen restart-sicheren, limitierten
Chunk-Transport. Ein einfacher String-Split wäre für UTF-8 und Ressourcen-
grenzen falsch gewesen.

v0013 serialisiert zuerst kanonische UTF-8-Bytes, komprimiert deterministisch,
schneidet Bytes, base64-kodiert erst danach und prüft Segment-, Encoded- und
Payload-Hashes. Der Worker persistiert Header/Chunks in SQLite und wendet erst
nach vollständiger Reassembly den unveränderten Full-/Patch-Handler an.

### 2.4 Config und Runtime waren fachlich nicht weit genug getrennt

Die Planung unterscheidet globale kanonische Config, abgeleitete Scope-
Snapshots, Worker-Baseline, lokales Proposal-Overlay, Telemetrie, Outbox und
Operationen. v0011 hatte diese Cursor nicht vollständig getrennt.

v0013 speichert diese Bereiche separat. Sensorwerte werden ausschließlich in
`sensor_latest` behandelt und als Cross-Facts geroutet; sie erzeugen keinen
Config-Commit. Worker-Proposals dürfen nur freigegebene Runtime-Felder der
`process_states` ändern. Topologie, Zertifikate, Hardwareendpunkte und Safety-
Felder sind Master-/Engineering-only.

### 2.5 Testprofil und Paketgrenze

Die v0082-Quelle hatte 36 ausführbare Tests mit In-Process-Doubles. Das ist
wertvoll, ersetzt aber weder Mosquitto-mTLS noch zwei echte Prozessgrenzen,
separate Modbusziele, Master-/Broker-Kill oder Python 3.14t.

Die erste v0013-RC-Fassung ergänzte dafür einen Worker-Test und ein
Worker-/Modbus-Compose-Lab. Das vermischte jedoch die unabhängig entwickelten
Pakete und setzte fälschlich einen Docker-Worker voraus. Korrekturstand r2
zieht die Systemgrenze sauber: Die Control Plane prüft ihre Protokolle,
Shards, ACLs und Admission; der native Worker v0082 und sein Modbuspfad werden
anschließend aus dem separaten Worker-Release extern abgenommen.

## 3. Diagnose des separat gelieferten v0082-Eingangs

Für die ursprüngliche Kompatibilitätsanalyse wurde das separat angelieferte
Worker-Archiv mit folgendem SHA-256 geprüft:

```text
734537e5c40f8ac262cf67565aa7d3f7796623ed597a1aee204e1a0a4c0dc995
```

Sein eigenes `SHA256SUMS` enthält 129 Zeilen. 126 stimmen; exakt drei Dateien
weichen bereits im gelieferten Upstream-Archiv ab:

| Datei | tatsächlicher SHA-256 |
| --- | --- |
| `_tools/_check_code_specific_standards.py` | `bb63eb52a5c9ad0f7705be2dff88a1de9bccb1b918e4679620a6d2ee2b8df474` |
| `src/system/_metrics_system.py` | `fd842ed3bdca32ddd1d6da0b664fc5d24db5e2041c85620a77d96ef13b01cbf3` |
| `src/system/_system_init.py` | `cc1524288731e12c3f862dbc0f5538987f0d7fb586c725adc7f464861674ce9b` |

Diese Abweichung wurde bei der Eingangsdiagnose nicht verschwiegen und das
Upstream-Manifest nicht umgeschrieben. Das Worker-Archiv, sein Manifest und
damalige Testoverlays sind im finalen Control-Plane-r2-ZIP nicht enthalten.
Sie gehören in die getrennte Worker-Codebasis und dürfen den Control-Plane-
Lifecycle nicht beeinflussen.

Beim wiederholten Gesamtlauf zeigte der Upstream-Test
`test_worker_all_local_actuators.py` außerdem einen Capture-Race: Er wartete auf
fertige Sensorpipelines, nicht aber auf das Drain des separaten
Aktor-Capture-Threads, und las dessen Liste ohne Lock. Dieser Befund betrifft
die getrennte Worker-Codebasis. r2 patcht ihn nicht innerhalb der Control
Plane; er muss im eigenen Worker-Release getestet und versioniert werden.

## 4. Globale Config und Regelmigration

Die Legacy-Config besitzt 200 Registry-Entitäten. Das Lab-v2-Fixture ergänzt
den zweiten lokalen Node-Datensatz und besitzt 201 Entitäten. Decompose und
Materialize sind verlustfrei; der globale Fingerprint lautet:

```text
08649e10714e44e6e770c4fe1edd0d49d9843935be539ebb3f869e973c0d46ce
```

Alle 14 Originalrules sind im Report enthalten:

| Status | Rules | Diagnose |
| --- | --- | --- |
| `active_node` | 1, 2, 4, 10, 11 | eindeutiger Single-Controller-/Node-Writer |
| `active_cross` | 3, 6 | writer-sichere Multi-Controller-Logik |
| `disabled_with_reason` | 5, 7, 8, 9 | fachliche Writer-/Prozessmigration nötig |
| `disabled_with_reason` | 13, 15, 17 | Safety-/zeitkritische lokale oder PLC-Interlocks nötig |

Das Lab aktiviert nur die nach Spezifikation zulässige Teilmenge. Produktion
akzeptiert dieses Fixture nicht als Safety-Nachweis.

## 5. Materialisierte Scopes

Die Graphklassifikation löst Rule-, State-, Prozess-, Sensor-, Aktor-, Filter-
und Controllerreferenzen transitiv auf. Sie erzeugt:

| Scope | Owner | Revision 1 Fingerprint | Rules |
| --- | --- | --- | --- |
| `node:fn-01` | fn-01 | `4289985599ea95612528b760d1f10ec01f21f39c1217ff8340e50ffbc82aab33` | 1, 2, 10, 11 |
| `node:fn-02` | fn-02 | `dec20a7c141c8598bac90a0b953e399a3591bbcfd7d6b5c39fb059dfefe0b808` | 4 |
| `cross:fn-01.default` | fn-01 | `9889d65009175d36334fec54c006acc29aaeb951b24d28f492863bd1b8d65462` | 3, 6 |

Node-Shards enthalten nur Controller, deren Hardware-Owner der Node ist, mit
vollständigen Endpunkten. Der Cross-Shard enthält Controller-UUIDs und logische
IDs, aber keine `host`, `port`, `address`, `unit`, `bus_type` oder
`function_type`-Hardwareinformation. fn-02 erhält niemals den Cross-Shard.

## 6. Master-v2-Datenfluss

### 6.1 Commit

Ein Config-Request wird anhand der authentifizierten Transportidentität,
`operation_id`, globaler Base-Revision und Pfadpolicy geprüft. Änderungen
werden auf einer Kopie angewandt, vollständig normalisiert und graphvalidiert.
Der Materializer berechnet nur geänderte Fingerprints und Scope-Ops.

Eine PostgreSQL-Transaktion persistiert Commit, Entity-Versionen, Scope-Heads,
immutable Snapshots, Scope-Ops, Rollouts und Authority-Revision. Erst danach
wird der vorbereitete Heap-Snapshot ersetzt. Ein Crash nach Commit wird durch
DB-Rebuild repariert; ein Crash davor macht keinen Teilzustand sichtbar.

### 6.2 Rollout

Der Worker meldet Protocol-/Contracts-Version, Capabilities, Paketlimit und
getrennte Replica-Anker. Der Master entscheidet unabhängig pro Scope:

- gleicher Fingerprint: kein Traffic,
- lückenlose passende Baseline und ≤500 Ops: Patch,
- sonst Full,
- serialisierte Hülle zu groß: Chunked Full/Patch,
- Patch-Basis-/Value-Hashfehler: durable Full-Fallback.

`converged` entsteht nicht durch Transport-ACK, sondern erst beim folgenden
Heartbeat mit exakter Revision und exaktem Fingerprint.

### 6.3 Proposal

Der Worker committed eine lokale zulässige Action zuerst, persistiert Overlay
und Outbox atomar und sendet anschließend ein scope-gebundenes Proposal. Der
Master prüft Source=Scope-Owner, Base-Scope, Entity-/Feldpolicy und globale
Graphgültigkeit. Er committed maximal einmal. `accepted` bleibt sichtbar, bis
der Worker den exakt im Resultat gespeicherten Scope-Target-Anker meldet; erst
dann wird das Proposal `converged`.

### 6.4 Cross

Der Master routet einen validierten Sensorfakt nur anhand der aktiven
Dependency-Map zum Cross-Owner. Er evaluiert keine Bedingung. Der Cross-Thread
auf fn-01 erzeugt einen epoch-/scope-/term-/TTL-gefenceten logischen Intent.
Der Master löst lediglich den Hardware-Owner auf. Nur fn-02 kanonisiert Host,
Port, Adresse und Funktion aus seinem Node-Shard, schreibt und bestätigt per
Readback.

## 7. Threading und no-GIL

Alle neuen gemeinsamen mutable Zustände haben einen expliziten Writer oder
Lock:

- Config-Commit: PostgreSQL-Transaktion + Authority-Lease;
- Heap: `RLock` und detached Copy-on-replace-Snapshots;
- Worker-Replica/Overlay/Chunk: externe Worker-Verantwortung;
- Coordinator-Status/Statistik: externe Worker-Verantwortung.

Der Control-Plane-Build versucht nicht, den GIL wieder einzuschalten. Der
Master-Build bricht ab, wenn `Py_GIL_DISABLED != 1` oder
`sys._is_gil_enabled()` nicht exakt `False` liefert. Der separat betriebene
Worker muss dieselbe Eigenschaft in seinem eigenen Release nachweisen.

### 7.1 Datenbanktreiber auf `cp314t`

Der Zielhostlauf von Korrekturstand r2 zeigte, dass
`psycopg-binary==3.3.4` kein Wheel für das free-threaded ABI `cp314t`
bereitstellt. Ein Wechsel des gesamten Systems auf GIL-Python wäre mit der
verbindlichen no-GIL-Spezifikation unvereinbar. r3 trennt deshalb nach ABI:
Der Master nutzt Pure-Python-psycopg plus System-`libpq`; der Proxy läuft auf
normalem `cp314` und behält das verfügbare Binary-Wheel. `PSYCOPG_IMPL` und der
Runtime-Dependency-Check machen die Auswahl fail-closed.

Die Hostversion Python 3.14.4 ist davon unabhängig. Sie orchestriert nur den
Rollout; die Container bauen beziehungsweise beziehen ihre festgelegten
Python-3.14.6-Runtimes selbst.

## 8. Security-Befund

- MQTT nutzt ausschließlich Port 8883 und mTLS.
- ACLs binden Master, Proxy, Monitor sowie fn-01/fn-02 an getrennte Identitäten
  und eigene Topicräume.
- Auto-Admission gilt nur im Lab, nur für konfigurierte Nodes und nur nach
  frischem brokergebundenem Heartbeat-Proof.
- Proxy-Config-Änderung verlangt Login, Sessioncookie, CSRF und
  Idempotency-Key.
- Runtime-Secrets, Test-PKI, private Keys, PEM, SQLite-Journale, Resultate,
  `__pycache__` und Worker-Distributionen sind aus dem ZIP ausgeschlossen.
- Produktion verlangt externe PKI, Zertifikatsprüfung, Image-Digest und
  explizite Config; Testfixtures werden nicht still übernommen.

## 9. Verbleibender Blocker

Die normative Online-Scope-Migration ist ein mehrprozessiger Sieben-Phasen-
Vorgang (`staged`, `READY`, Epoch-Commit, Activation-ACK, `active`, `retiring`,
`inactive`). Ein atomarer DB-Commit allein reicht nicht.

v0013 besitzt monotone Execution-Epochen und lehnt alte Remote-Intents ab. Es
implementiert jedoch noch keinen freigegebenen vollständigen
READY-/Activation-/Retire-Transport zwischen zwei laufenden Execution-Ownern.
Deshalb akzeptiert `plan_activations` keine Onlinebewegung oder Entfernung
einer bereits aktiven Rule/State/Process-Entity. Das ist absichtlich
fail-closed und verhindert Doppelwrites, erfüllt aber die verbindlichen
Migrationstests 21.7 noch nicht. Für eine solche Topologieänderung ist derzeit
ein kontrollierter Fresh-/Maintenance-Rollout erforderlich.

Damit ist v0013 für das fest definierte `lab_v2`-System und für stabile
Produktionsshards technisch testbar; es darf nicht als vollständig
spezifikationsabgenommener Online-Migrationsrelease bezeichnet werden.

## 10. Ergebnis

Die zentralen v0011-Befunde – Master-Ausführung, globale statt unabhängiger
Scope-Cursor, fehlender Patch/Chunk-Pfad und unklare Proposal-Konvergenz – sind
im Control-Plane-Code geschlossen. Die lokale Master-Suite (18) ist bestanden.
Der Fresh-Rollout führt die noch auf dem Docker-Zielhost auszuführende reale
Control-Plane-mTLS-/Postgres-/no-GIL-Abnahme aus. Worker-/Modbus-/Cross-Site-
Gates folgen separat und dürfen erst nach realer externer Ausführung als grün
gelten.

Die formale Releaseentscheidung einschließlich nicht ausgeführter Gates steht
in `RELEASE_VALIDATION_V0013_DE.md`.

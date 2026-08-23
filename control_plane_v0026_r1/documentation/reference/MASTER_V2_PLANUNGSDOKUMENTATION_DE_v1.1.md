# Master v2 – Planungs- und Implementierungsdokumentation

> **Historischer Planungsstand:** Für v0025-r1 nicht mehr normativ. Authority
> V5, `node_local`, Contract `2.0`, Type Schema `2.0.0` und der Pfad
> `src/library/` werden durch `CODE_AND_ARCHITECTURE_SPEZIFICATION_V005.md`
> sowie `documentation/MASTER_V2_ARCHITEKTUR_DE.md` festgelegt. Verweise auf
> `automation_contracts` dokumentieren ausschließlich den damaligen Plan.

Version: 1.1  
Stand: 2026-07-16  
Status: verbindlicher Implementierungsvertrag für einen vollständigen Master-v2-Release  
Sprache der Implementierung: Englisch; diese Planungsdokumentation: Deutsch

## 0. Zweck, Geltungsbereich und normative Quellen

Dieses Dokument beschreibt die vollständige Zielarchitektur und den verbindlichen Implementierungsumfang für Master v2. Es ersetzt `MASTER_V2_PLANUNGSDOKUMENTATION_DE.md` Version 1.0. Die Umsetzung erfolgt als **ein gemeinsamer, vollständig getesteter Release**. Interne Arbeitspakete dürfen nacheinander entwickelt werden, sind aber keine einzeln freigabefähigen Produktstufen. Der Release ist erst fertig, wenn Master, Worker-Protokollerweiterungen, Migration, Testprofil und reale Abnahme gemeinsam funktionieren.

Normative Quellen, in absteigender Priorität:

1. `CODE_AND_ARCHITECTURE_SPEZIFICATION.md` – verbindliche Code-, Worker-/Master-, Cross-Scope-, Identity- und Threading-Spezifikation.
2. Dieses Dokument – verbindliche Master-v2-Daten-, Protokoll-, Migrations- und Abnahmespezifikation.
3. Worker `automation_system_v0082` – Referenz für die bestehende lokale Automation, Node-/Cross-Graphpartitionierung, MQTT-Hüllen, Fencing, Journale und Hardware-Ownership.
4. Control Plane `automation_control_plane_release_v0011` – Migrationsbasis für Registry, Admission, Postgres, Outbox, MQTT, Proxy und Deployment.

Bei einem Widerspruch zur `CODE_AND_ARCHITECTURE_SPEZIFICATION.md` ist die Implementierung anzuhalten und die Abweichung vor dem Commit explizit zu korrigieren. Es ist verboten, eine abweichende zweite Shard-, Schema-, Fingerprint- oder Entity-Key-Logik im Master zu implementieren.

Die Begriffe **MUSS**, **DARF NICHT**, **SOLL** und **KANN** sind normativ. Aussagen über v0082 oder v0011 bezeichnen nachprüfbaren Ist-Stand; Aussagen über Master v2 bezeichnen Zielverhalten und sind nicht als bereits implementiert auszugeben.

Bekannte Spezifikationspräzisierung: Die Cross-Scope-Sektion der aktuellen `CODE_AND_ARCHITECTURE_SPEZIFICATION.md` nennt an einer Stelle nur mehrere Controller auf **verschiedenen** Nodes, beschreibt später aber ausdrücklich Cross-Controller auf demselben Node und Cross-Node einheitlich. Für Master v2 gilt die umfassendere, mit v0082 und dem Architekturdiagramm konsistente Regel: **Jede Logik über mehr als einen Controller ist Cross**, auch wenn alle beteiligten Controller demselben Node gehören. Diese einzelne Formulierung ist vor dem Code-Release in der Spezifikationsdatei zu vereinheitlichen.

### 0.1 Ziel

Master v2 ist die globale, persistente und eventual-konsistente Config-Authority eines Multi-Worker-Systems. Er:

- speichert eine kanonische globale Konfiguration,
- validiert und committed Änderungen atomar,
- erzeugt deterministische Node- und optionale Cross-Shards,
- verteilt nur betroffene Scopes vollständig oder differenziell,
- verarbeitet Worker-Proposals bidirektional,
- registriert Worker und Hardware-Ownership,
- routet Remote-Sensorfakten und Remote-Aktorintents,
- auditiert alle fachlich relevanten Entscheidungen,
- führt selbst niemals RuleEngine, FSM, Automation oder Hardware-I/O aus.

Jeder Worker bleibt lokal autonom. Master- oder Broker-Ausfall unterbricht niemals die lokale Node-Regelung. Cross-Koordination darf degradieren; lokale Regelung darf es nicht.

### 0.2 Vollständiger Releaseumfang

Der eine Master-v2-Release MUSS gemeinsam enthalten:

- das gemeinsame `automation_contracts`-Paket,
- den Postgres Entity-/Commit-/Scope-/Rollout-Store,
- den atomaren Master-Commitpfad,
- graphbasierte Node-/Cross-Klassifikation,
- Node- und Cross-Full-Rollout,
- Node- und Cross-Patch-Rollout,
- begrenzten, restart-sicheren Chunk-Transport,
- separate Scope-Revisionen und Replica-Baselines im Worker,
- Worker-Proposal-Ingest, Rebase und Konvergenz,
- Cross-Fact-Routing ohne Master-Automation,
- bestehendes gefencetes Remote-Aktor-Routing,
- gefencete Scope-Migration,
- vollständigen Rückbau der Master-Automation,
- eine idempotente Bestandsmigration,
- ein selbststartendes Lab-Testprofil,
- automatisierte, no-GIL- und reale End-to-End-Abnahmen.

Nicht zulässig ist ein Release, bei dem nur Full-Rollout funktioniert, während Patch, Chunk, Proposal, Cross-Routing oder der Rückbau der Master-Automation als spätere Produktstufe offenbleiben. Feature-Flags dürfen für Kompatibilität und Diagnose existieren; alle oben genannten Pfade müssen vor Freigabe implementiert und getestet sein.

### 0.3 Bewusste Nicht-Ziele

- Master v2 ist keine Echtzeitsteuerung.
- Safety- und zeitkritische Interlocks werden niemals Cross ausgeführt.
- Worker greifen niemals direkt auf Postgres oder Master-internen Speicher zu.
- Cross-Threads lesen und schreiben niemals Feldbus-Hardware.
- Telemetrie ist keine Config und wird nicht in Config-Entitätstabellen gespeichert.
- Abgeleitete Shards sind keine unabhängig editierbaren Wahrheiten.

## 1. Verifizierter Ist-Stand und korrigierte Ausgangslage

### 1.1 Worker v0082

Nachweislich vorhanden:

- vollständige lokale Automation in Standalone und Distributed Worker,
- `distributed.config.apply`,
- `distributed.cross.apply`,
- `distributed.config.propose`,
- `distributed.actuator.route`, `distributed.actuator.execute` und `distributed.actuator.result`,
- Node-/Cross-Fingerprints,
- Cross-Thread, Cross-Fakten-Cache und Cross-I/O-Adapter,
- SQLite Inbox/Outbox und Rebase-Zustände,
- Coordinator-Term-Fencing und Hardware-Readback,
- graphbasierte Shard-Bibliothek `_cross_scope_library.py`.

Für Master v2 noch nicht vorhanden und deshalb Bestandteil dieses Releases:

- `distributed.config.patch`,
- `distributed.cross.patch`,
- `distributed.config.chunk`,
- persistente Chunk-Reassembly,
- getrennte Master-Scope-Revision für den Node-Shard,
- Replica-Baseline plus Pending-Proposal-Overlay,
- Patch-Anwendung auf die Replica-Baseline.

Das lokale MQTT-Kommando `config.patch` ist nicht dasselbe wie `distributed.config.patch` und darf nicht als Implementierungsnachweis verwendet werden.

Der dokumentierte v0082-Teststand umfasst 36 ausführbare Tests unter Python 3.12.13 mit aktivem GIL und In-Process-MQTT-/Feldbus-Doubles. Ein realer Mosquitto-/Modbus-/Python-3.14t-Test ist damit nicht ersetzt.

### 1.2 Control Plane v0011

Wiederverwendbar sind:

- Postgres-Grundzugriff und Migrationen,
- Node-Registry und cert-gebundene Admission,
- MQTT-mTLS-Transport und ACL-Struktur,
- durable Outbox-/Command-Grundlagen,
- Proxy/API/Audit-Grundlagen,
- Coordinator-Term und Worker-Sessiondaten.

Zu ersetzen oder zu entfernen sind:

- der globale Config-Blob als alleinige Replikationsquelle,
- `global_automation.py`,
- `automation_authority.py`,
- `_automation_thread.py` im Master,
- jede Sensor-zu-Automation-Verknüpfung im Master,
- Forward-only-/Master-Ausführungsentscheidungen,
- Site-Semantik in der fachlichen Config-Auflösung.

Transport-Namespaces dürfen aus Kompatibilitätsgründen einen bisherigen Site-/Namespace-Anteil behalten. Dieser ist kein fachlicher Identitätsschlüssel. Fachliche Referenzen werden über `controller_uuid` plus lokale IDs aufgelöst.

### 1.3 Bekannte Regelpartition

Die 14 Ausgangs-Rules sind `1,2,3,4,5,6,7,8,9,10,11,13,15,17`.

- sicher node-lokal partitioniert: `1,2,4,10,11`,
- aktuell writer-sicher Cross partitioniert: `3,6`,
- aktuell fail-closed ausgeschlossen: `5,7,8,9,13,15,17`.

Dies ist keine Mode-Gate-Entscheidung. Rules `5,7,8,9` besitzen gemischte State-/Prozess-Writer. Rules `13,15,17` sind Safety-/zeitkritisch und/oder besitzen gemischte Writer. Abschnitt 8 definiert die verpflichtende Migration.

## 2. Authority-Modell und nicht verhandelbare Invarianten

### 2.1 Drei getrennte Authority-Arten

| Authority | Verantwortung | Darf nicht |
| --- | --- | --- |
| Master Config Authority | kanonische globale Soll-Config, Ownership, Commit, Shards, Rollout, Audit | Automation oder Hardware ausführen |
| Worker Runtime-/Hardware-Authority | letzte gültige lokale Config ausführen, Sensoren lesen, eigene Hardware schreiben, Readback | fremde Hardware direkt schreiben oder globale Config umgehen |
| Cross Execution Owner | eigenen Cross-Shard isoliert ausführen, Fakten bewerten, Intents erzeugen | Feldbus öffnen, Safety-Cross-Regelung ausführen oder fremde Config laden |

„Worker besitzt Hardware-Hoheit“ bedeutet: Nur der Hardware-Owner entscheidet nach lokaler Safety-, Ownership-, Epoch- und Readback-Prüfung über den physischen Write. Es bedeutet nicht, dass der Worker globale Ownership oder Master-only-Felder eigenmächtig verändern darf.

### 2.2 Invarianten

**I1 – Master führt nie aus.** Im Master existieren keine gestartete RuleEngine, FSM, Automation- oder Controller-Threads. Sensorfakten werden nur validiert und geroutet.

**I2 – Lokale Kontrolle bleibt aktiv.** Standalone- und Distributed-Worker betreiben dieselbe vollständige lokale Pipeline. Master-/Broker-Ausfall betrifft nur Synchronisation und Cross-Koordination.

**I3 – Genau ein Hardware-Owner.** Jede physische `controller_uuid` besitzt genau eine aktive Owner-Route zu einem `node_id`. Nur dessen lokaler Controller-Thread darf Hardware schreiben.

**I4 – Genau ein logischer Writer.** Jeder zur Laufzeit mutierbare State, Prozess und jede Cross-Domain besitzt genau einen Execution-Owner. Mehrere Rules dürfen nur dann auf dasselbe Ziel wirken, wenn die bestehende Engine eine explizite deterministische Prioritäts-/Arbitrationsregel besitzt und alle Rules demselben Execution-Scope gehören.

**I5 – Cross ist hardwarefrei.** Cross-Shards enthalten logische `controller_uuid + sensor_id/actuator_id`-Referenzen, aber keine Host-, Port-, Unit-, Bus-, Adresse- oder Write-Funktionsdaten.

**I6 – Ein Worker erhält nur eigene Scopes.** Jeder Worker erhält genau seinen Node-Shard und optional die Cross-Domains, deren `owner_node_id` er ist. Shared-Kataloge werden als benötigte Projektion eingebettet, nicht als Fremd-Shard.

**I7 – Eine Contracts-Codequelle.** Schema, Normalisierung, Entity Keys, Graph, Shard-Bildung, Cross-Stripping, Referenzprüfung und Fingerprint existieren exakt einmal im versionierten `automation_contracts`-Paket.

**I8 – Scope-Konvergenz ist unabhängig.** Node-, Cross- und globale Commit-Revision sind getrennte Begriffe. Eine Änderung an fn-01 erzeugt keinen Config-Rollout für einen unveränderten fn-02-Scope.

**I9 – Persistente Wahrheit, atomarer Heap.** Postgres ist die einzige dauerhafte Master-Wahrheit. Der Heap enthält unveränderliche Snapshots und wird nach erfolgreichem DB-Commit atomar ausgetauscht. Nach einem Crash wird er vollständig aus Postgres rekonstruiert.

**I10 – Idempotenz und Fencing.** Config, Proposals und Aktoroperationen besitzen stabile IDs, TTLs und Coordinator-/Execution-Epochen. At-least-once darf keinen doppelten Commit und keinen doppelten Hardware-Write erzeugen.

**I11 – Fail-closed fachlich, testfreundlich operativ.** Schema-, Ownership-, Writer-, Safety-, Hash- und Fencing-Prüfungen sind immer aktiv. Das Lab-Profil beseitigt ausschließlich manuelle Infrastrukturhürden, nicht fachliche Schutzprüfungen.

**I12 – no-GIL-sicher.** Jeder geteilte mutable Zustand besitzt genau einen Writer oder einen expliziten Lock. Es gibt keine Korrektheitsannahme über den Python-GIL.

**I13 – Keine stillen Datenverluste.** Abgelehnte, stale oder nicht materialisierbare Änderungen werden durable protokolliert und sichtbar beantwortet. Es gibt kein stilles Strippen außer der normativen Cross-Hardware-Projektion.

**I14 – Config und Telemetrie sind getrennt.** Config-Commits sind durable und auditierbar. Sensorfakten sind zeitbehaftete Beobachtungen mit Latest-Value-/Sequenzsemantik und dürfen keine unbeschränkte durable Queue erzeugen.

## 3. Identität, Scopes und Revisionsmodell

### 3.1 Identitäten

- Hardware-Referenz: `controller_uuid + sensor_id` oder `controller_uuid + actuator_id`.
- Hardware-Route: `controller_uuid -> owner_node_id + local_controller_id`.
- Node-Scope: `node:<node_id>`.
- Cross-Scope: `cross:<domain_id>`.
- Default-Cross-Domain für v1.1: `domain_id = <owner_node_id>.default`.
- Rule-/Prozess-Owner: explizites `owner_node_id`; Cross-Entitäten zusätzlich explizites `cross_domain_id` nach Materialisierung.

`controller_uuid` ist global eindeutig. Lokale IDs sind nur innerhalb ihres Controller-/Node-Scopes eindeutig. Eine UUID darf in Node- und Cross-Shard erscheinen: Im Node-Shard als physischer Owner-Eintrag, im Cross-Shard ausschließlich als logische Referenz.

Master v2 verwaltet Scopes generisch. Der Worker-v1.1-Release betreibt entsprechend der vorhandenen Architektur jedoch höchstens **eine** Cross-Domain beziehungsweise einen Cross-Thread pro Node. Ein Node besitzt daher entweder keine Cross-Replica oder genau `cross:<node_id>.default`.

### 3.2 Revisionsbegriffe

| Feld | Bedeutung | Änderung |
| --- | --- | --- |
| `global_revision` | monotone Master-Commitfolge | bei jedem erfolgreichen globalen Commit |
| `node_scope_revision` | Materialisierungsrevision eines Node-Shards | nur wenn dessen Fingerprint sich ändert |
| `cross_scope_revision` | Materialisierungsrevision einer Cross-Domain | nur wenn deren Fingerprint sich ändert |
| `local_config_revision` | interne SEM-Revision des Worker-Livezustands | bei lokalem SEM-Commit oder Rollout |
| `execution_epoch` | Fencing einer logischen Ausführungszuordnung | bei Scope-/Owner-Wechsel |
| `coordinator_term` | Fencing einer Master-Instanz | bei Master-Failover/Leadership-Wechsel |

`local_config_revision` DARF NICHT als Cursor für Master-Diffs benutzt werden.

### 3.3 Persistente Worker-Anker

Der Worker speichert getrennt:

```json
{
  "observed_global_revision": 42,
  "node_replica_scope": "node:fn-01",
  "node_replica_revision": 7,
  "node_replica_fingerprint": "...",
  "cross_replica": {
    "scope": "cross:fn-01.default",
    "revision": 3,
    "fingerprint": "..."
  },
  "live_config_revision": 19,
  "live_config_fingerprint": "...",
  "pending_proposal_count": 2
}
```

Replica-Revision/Fingerprint beschreiben den letzten autoritativen Master-Baseline-Snapshot. Live-Revision/Fingerprint dürfen wegen noch nicht konvergierter Local-first-Proposals davon abweichen.

### 3.4 Replica-Baseline und Local-first-Overlay

Der Worker hält pro Scope:

1. eine persistente, unveränderte Master-Replica-Baseline,
2. eine geordnete Liste pending Local-first-Ops,
3. den daraus materialisierten Livezustand.

Bei einem Master-Rollout:

1. neue Baseline auf Kopie validieren,
2. bereits im Master enthaltene Proposals als konvergiert markieren,
3. verbleibende zulässige Pending-Ops deterministisch rebasen,
4. vollständigen Live-Kandidaten validieren,
5. über SEM atomar austauschen,
6. Baseline, Anker und Proposalzustände in einer lokalen Journaltransaktion aktualisieren.

Bei einem semantisch abgelehnten Proposal wird ein worker-proposable Runtime-Feld deterministisch auf die Master-Baseline zurückgeführt. Master-only-Felder dürfen lokal gar nicht Local-first verändert werden.

## 4. Gemeinsames Contracts-Paket

`automation_contracts` wird aus dem nachweislich funktionierenden v0082-Code extrahiert, ohne dessen Semantik neu zu erfinden:

```text
automation_contracts/
  __init__.py
  json_support.py          canonical_dumps, canonical_fingerprint
  type_library.py          GenericType-/V7-Normprüfung
  entity_registry.py       exakte Entity Keys aller Sektionen
  config_graph.py          transitive Referenz- und Writeranalyse
  shard_library.py         Node-/Cross-Bau und Scope-Validierung
  scope_diff.py            deterministische Entity-/Feld-Ops
  protocol_contracts.py    Kommandos, Pflichtfelder, Limits, Capabilities
```

Alle Module folgen `CODE_AND_ARCHITECTURE_SPEZIFICATION.md`:

- funktional, keine Klassen,
- Context-Dictionaries und Dependency Injection,
- keine Decorators, Dataclasses, Lambdas, `typing` oder Type Hints,
- `functools.partial` für gebundene Handler,
- englische Kommentare und normierter Modulheader,
- Standardbibliothek, außer bereits genehmigten Runtime-Abhängigkeiten,
- Python 3.14t/free-threaded-kompatibel,
- keine lokale Kopie derselben Contract-Logik in Master oder Worker.

Die Contracts-Version wird als `contracts_version` in Registration, Heartbeat, Rollout und Proposal übertragen. Master verteilt nur an kompatible Worker. Im Lab-Release liegen Master und Worker immer als zusammengehöriger Satz vor; dadurch entsteht kein manueller Gate.

## 5. Graphbasierte Klassifikation und Shard-Materialisierung

### 5.1 Kein UUID-Überlappungsheuristik

Cross wird niemals aus dem bloßen Vorkommen derselben UUID in mehreren Dateien abgeleitet. Der Klassifikator löst den vollständigen Abhängigkeitsgraphen auf:

```text
Rule
 ├─ when / until / filter_id ──> Filter ──> SensorRefs ──> Controller
 ├─ state_id / state_ids ──────> ProcessState ──> io_refs ──> Controller
 ├─ p_id / p_ids ──────────────> Prozess ──> ProcessStates ──> Controller
 ├─ parameter state refs ──────> ProcessState
 └─ actions / else_do ─────────> mutierte States/Prozesse/Aktoren
```

Die Implementierung MUSS die transitive Closure der v0082-Graphlogik verwenden. Eine Entity-lokale `len(refs)`-Abkürzung ist verboten.

### 5.2 Deterministischer Klassifikationsablauf

Für jeden globalen Zielzustand:

1. Config normalisieren und GenericType-Schema prüfen.
2. Eindeutige Controller-Owner-Map aus aktiven Owner-Routen bilden.
3. Entity Keys erzeugen und Duplikate ablehnen.
4. Referenzgraph und Reverse-Dependency-Index bilden.
5. Für Rules/Prozesse alle direkten und transitiven Lese- und Schreibreferenzen bestimmen.
6. Mutierte States/Prozesse ermitteln und Writer-Kandidaten gruppieren.
7. Jeden State/Prozess exakt einem Execution-Scope zuweisen.
8. Single-Controller-Logik einem Node-Scope zuweisen.
9. Multi-Controller-Logik – auch innerhalb desselben Nodes – einem expliziten Cross-Scope zuweisen.
10. Cross-Writer-Sicherheit und Safety-Klasse prüfen.
11. Dependency Closure des jeweiligen Shards bilden.
12. Node-Shard mit voller lokaler Hardware, Cross-Shard hardwarefrei materialisieren.
13. beide Shardtypen vollständig validieren.
14. kanonisch serialisieren und fingerprinten.

### 5.3 Scope-Regeln

- Shared Typ-/Template-Entitäten werden in jeden Shard projiziert, der sie benötigt.
- Controller-Hardwarezeilen gehören ausschließlich zum Node-Scope des Hardware-Owners.
- Cross-Controller-Einträge enthalten nur UUID, logische lokale IDs, Labels und zulässige Katalogreferenzen.
- Eine Rule mit mehreren Controllern ist Cross, unabhängig davon, ob die Controller auf demselben oder unterschiedlichen Nodes liegen.
- Eine Rule ohne auflösbare Controller-/State-/Prozessreferenz benötigt einen expliziten Execution-Scope; sonst Reject.
- Eine Safety-/zeitkritische Rule mit Remote- oder Cross-Abhängigkeit wird nicht Cross materialisiert.
- Entitäten mit unbekannter oder mehrdeutiger Ownership werden abgelehnt.
- Die Materialisierung darf Hardwarefelder nur beim normativen Cross-Projektionsschritt entfernen; rohe, bereits als Cross deklarierte Eingaben mit Hardware-Endpunkten werden abgelehnt.

### 5.4 Exakte Entity-Key-Registry

Jede unterstützte Sektion MUSS vor Implementierungsbeginn einen konkreten Schlüssel besitzen. Platzhalter wie „Primärschlüssel“ sind nicht zulässig. Mindestregistry:

```text
rules                 rule:<rule_id>
processes             process:<p_id>
process_states        state:<state_id>
controllers           controller:<controller_uuid>
virtual_controllers   virtual:<controller_uuid>
filters               filter:<filter_id>
sensors               sensor:<controller_uuid>:<sensor_id>
actuators             actuator:<controller_uuid>:<actuator_id>
site_controller_map   route:<controller_uuid>:<routing_role>
nodes                 node:<node_id>
local_node            local-node:<node_id>
sensor_types          sensor-type:<sensor_type_id>
actuator_types        actuator-type:<actuator_type_id>
modbus_functions      modbus-function:<function_type>
scaling_profiles      scaling:<scaling_id>
schema_mappings       schema-mapping:<mapping_id>
process_values        process-value:<value_id>
process_modes         process-mode:<mode_id>
pid_configs           pid:<pid_id>
control_methods       control-method:<method_id>
control_method_templates control-template:<template_id>
parameter_generic_templates parameter-template:<template_id>
timers                timer:<timer_id>
rule_engine           rule-engine:<engine_id>
meta                  meta:root
```

Falls die reale GenericType-Struktur einen anderen konkreten Feldnamen verwendet, wird die Registry einmalig daran angepasst und durch Roundtrip-Tests festgeschrieben. Unbekannte Sektionen oder fehlende Keys führen fail-closed zu einem Importfehler.

### 5.5 Roundtrip-Invariante

Für jede gültige Config MUSS gelten:

```text
materialize(decompose(normalize(config))) == normalize(config)
```

Die Gleichheit wird über kanonische Bytes und Fingerprint geprüft. Gruppenkopf-Felder von Sensoren/Aktoren müssen beim Zerlegen vollständig in der Zeilenrepräsentation erhalten und beim Materialisieren deterministisch rekonstruiert werden.

## 6. Master-Datenmodell und Heap

### 6.1 Datenbereiche

Config, Runtime-Registry, Rollouts, Proposals, Telemetrie und Audit bleiben getrennte fachliche Bereiche. Config-Shards sind abgeleitete Projektionen der globalen Entitäten, aber werden als versionierte Scope-Snapshots persistiert, damit Restart, Full-Rollout und Audit keine erneute Interpretation eines alten Zustands erfordern.

### 6.2 Verbindliche Tabellen

```sql
CREATE TABLE config_commits (
    commit_id text PRIMARY KEY,
    operation_id text NOT NULL UNIQUE,
    global_revision bigint NOT NULL UNIQUE,
    source_kind text NOT NULL,
    source_node_id text,
    base_global_revision bigint,
    status text NOT NULL,
    request_payload jsonb NOT NULL,
    result_payload jsonb NOT NULL,
    committed_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE config_entities (
    section text NOT NULL,
    entity_key text NOT NULL,
    entity_revision bigint NOT NULL,
    payload jsonb NOT NULL,
    deleted boolean NOT NULL DEFAULT false,
    last_commit_id text NOT NULL REFERENCES config_commits(commit_id),
    updated_by text NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (section, entity_key)
);

CREATE TABLE config_entity_versions (
    commit_id text NOT NULL REFERENCES config_commits(commit_id),
    op_index integer NOT NULL,
    section text NOT NULL,
    entity_key text NOT NULL,
    operation text NOT NULL,
    before_payload jsonb,
    after_payload jsonb,
    PRIMARY KEY (commit_id, op_index)
);

CREATE TABLE scope_heads (
    scope text PRIMARY KEY,
    scope_kind text NOT NULL,
    owner_node_id text,
    scope_revision bigint NOT NULL,
    global_revision bigint NOT NULL,
    fingerprint char(64) NOT NULL,
    contracts_version text NOT NULL,
    execution_epoch bigint NOT NULL,
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE scope_snapshots (
    scope text NOT NULL,
    scope_revision bigint NOT NULL,
    global_revision bigint NOT NULL,
    fingerprint char(64) NOT NULL,
    canonical_config jsonb NOT NULL,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    PRIMARY KEY (scope, scope_revision)
);

CREATE TABLE scope_ops (
    scope text NOT NULL,
    target_scope_revision bigint NOT NULL,
    op_index integer NOT NULL,
    operation text NOT NULL,
    section text NOT NULL,
    entity_key text NOT NULL,
    json_pointer text,
    value jsonb,
    value_hash char(64),
    commit_id text NOT NULL REFERENCES config_commits(commit_id),
    PRIMARY KEY (scope, target_scope_revision, op_index)
);

CREATE TABLE shard_rollouts (
    rollout_id text PRIMARY KEY,
    node_id text NOT NULL,
    scope text NOT NULL,
    target_scope_revision bigint NOT NULL,
    target_fingerprint char(64) NOT NULL,
    coordinator_term bigint NOT NULL,
    transfer_id text,
    state text NOT NULL,
    attempts integer NOT NULL DEFAULT 0,
    next_attempt_at timestamptz,
    detail jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    UNIQUE (node_id, scope, target_scope_revision)
);

CREATE TABLE worker_proposals (
    proposal_id text PRIMARY KEY,
    node_id text NOT NULL,
    scope text NOT NULL,
    base_scope_revision bigint NOT NULL,
    base_scope_fingerprint char(64) NOT NULL,
    operation_id text NOT NULL UNIQUE,
    operations jsonb NOT NULL,
    state text NOT NULL,
    result jsonb NOT NULL DEFAULT '{}'::jsonb,
    created_at timestamptz NOT NULL DEFAULT clock_timestamp(),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);

CREATE TABLE scope_activations (
    entity_key text PRIMARY KEY,
    active_scope text NOT NULL,
    execution_epoch bigint NOT NULL,
    state text NOT NULL,
    last_commit_id text NOT NULL REFERENCES config_commits(commit_id),
    updated_at timestamptz NOT NULL DEFAULT clock_timestamp()
);
```

Die bestehenden Registry-, Admission-, Authority-Term-, Audit- und Outbox-Tabellen werden wiederverwendet und nur um Scope-Anker, `contracts_version`, `protocol_version`, Capabilities und `runtime_nonce` ergänzt.

### 6.3 Warum `config_commits` und `scope_ops` getrennt sind

`config_commits` und `config_entity_versions` beschreiben die kanonische globale Änderung. `scope_ops` beschreiben die daraus deterministisch entstandenen Shard-Änderungen. Dadurch können Shared-Entitäten mehrere Scopes betreffen und Scope-Migrationen als Delete im alten sowie Upsert im neuen Scope dargestellt werden, ohne eine fehlerhafte Eindeutigkeit über `(operation_id, section, entity_key)`.

Eine `operation_id` identifiziert genau einen Commit. Einzelne Operationen werden über `commit_id + op_index` identifiziert.

### 6.4 Heap-Modell

Der Master hält atomar austauschbare Snapshots:

```text
global_entities
config_graph + reverse_dependencies
controller_uuid -> hardware owner route
scope -> head + immutable shard snapshot
node -> registration + capabilities + reported replica anchors
node/scope -> rollout state
cross domain -> required fact subscriptions
```

Alle Mutationen laufen durch einen Master-Commit-Thread. Leser erhalten Snapshot-Referenzen und verändern sie nie. Lock-Zeiten beschränken sich auf den Pointer-Swap. Netzwerk-, DB- oder Hashoperationen dürfen nicht unter einem globalen Lock stattfinden.

### 6.5 Crash-Konsistenz

Ein Commit schreibt in einer Postgres-Transaktion:

1. Commitzeile,
2. Entitäten und Versionen,
3. Graph-/Ownership-Ableitungen,
4. Scope-Heads, Snapshots und Scope-Ops,
5. Rollout-/Outbox-Arbeit.

Erst nach DB-COMMIT wird der vorbereitete Heap-Snapshot atomar aktiviert. Ein Crash davor ändert nichts; ein Crash danach wird aus Postgres rekonstruiert. Rollout-Nachrichten stammen ausschließlich aus der transaktionalen Outbox.

## 7. Atomare Commit-Pipeline und dynamische CRUD-Operationen

### 7.1 Eingangsformat

Alle Quellen – Operator, Import, Worker-Proposal, Migration – werden in dasselbe Commitformat normalisiert:

```json
{
  "operation_id": "operator:7df4...",
  "source": {"kind": "operator", "node_id": null},
  "base": {"global_revision": 41},
  "operations": [
    {
      "op": "replace_field",
      "section": "rules",
      "entity_key": "rule:1",
      "json_pointer": "/when/1/value",
      "expected_value_hash": "...",
      "value": 42.5
    }
  ]
}
```

Zulässige Operationen:

- `upsert_entity`,
- `delete_entity`,
- `replace_field`,
- `remove_field`,
- `replace_snapshot` nur für initialen Import/Migration.

JSON Pointer folgt RFC-6901-Escaping. Listen mit eigener fachlicher ID werden als eigenständige Entities normalisiert. Für sonstige Listen wird die vollständige Liste ersetzt; indexbasierte konkurrierende Inserts sind nicht zulässig. `expected_value_hash` verhindert, dass ein Feldpatch unbemerkt auf eine verschobene Struktur angewendet wird.

### 7.2 Commitablauf

Der Master-Single-Writer führt exakt aus:

1. authentifizierte Quelle außerhalb des Payloads aus Transport-/Sessionkontext bestimmen,
2. `operation_id` prüfen; Duplikat liefert dasselbe gespeicherte Ergebnis,
3. Base-Revision/-Fingerprint prüfen,
4. Operationen auf eine Kopie der globalen Entities anwenden,
5. GenericType-Normalisierung und vollständige Schema-/Referenzprüfung,
6. Graph, Ownership, Writer und Safety-Klassen neu berechnen,
7. betroffene Scopes über Reverse Dependencies bestimmen,
8. alte und neue betroffene Shards materialisieren,
9. Gates aus Abschnitt 7.3 prüfen,
10. Scope-Ops und Fingerprints deterministisch erzeugen,
11. unveränderte Fingerprints aus der betroffenen Menge entfernen,
12. globale und betroffene Scope-Revisionen vergeben,
13. alles einschließlich Outbox in einer DB-Transaktion schreiben,
14. Heap-Snapshot atomar aktivieren,
15. betroffene Rollouts signalisieren,
16. Commitresultat an die Quelle zurückgeben.

Kein Teilschritt wird extern sichtbar, bevor die DB-Transaktion committed ist.

### 7.3 Verpflichtende fachliche Gates

Diese Gates dürfen auch im Lab nicht deaktiviert werden:

- Schema und Entity-Key vollständig gültig,
- alle Referenzen auflösbar,
- genau ein Hardware-Owner pro physischem Controller,
- genau ein Execution-Owner pro State/Prozess,
- Rule-/Prozess-Owner existiert und ist aktiviert,
- Cross-Shard hardwarefrei,
- Cross-Owner besitzt `cross_runtime`-Capability,
- Safety-/zeitkritische Rule nicht Cross,
- kein Aktorintent ohne auflösbaren Hardware-Owner,
- keine Scope-Migration ohne neue `execution_epoch`,
- keine unbekannte Section oder Operation,
- alle Zielshards bestehen `verify_config_data` und Scope-Validator.

### 7.4 Authority-Matrix für Änderungen

| Bereich | Lokale Änderung | Masterverhalten |
| --- | --- | --- |
| Sensorwerte/Aktorfeedback | Runtime-Event, kein Config-Commit | validieren/routen, nicht in Config speichern |
| Runtime-State/zulässige Parameter | Local-first + Proposal | rebasen, validieren, committen oder sichtbar ablehnen |
| Cross-Runtime-State | nur Cross-Owner Local-first | Owner prüfen, dann wie oben |
| Rules/Prozesse/Timer | Proposal möglich, nicht zwingend lokal freigegeben | vollständige Graph-/Safety-Prüfung |
| Topologie/Ownership/Controller UUID | nicht Local-first | Master-/Engineering-only |
| Safety-Limits/Interlocks | nicht Local-first | Master-/Engineering-only, lokale Schutzprüfung zusätzlich |
| physischer Aktor-Write | kein Config-Commit | ausschließlich Hardware-Owner |

Die globale Konfliktpolitik bleibt eventual `master_wins`, wie in `CODE_AND_ARCHITECTURE_SPEZIFICATION.md` festgelegt. „Master wins“ bedeutet jedoch nie stilles Verwerfen: stale Proposals werden rebased, semantische Rejects werden durable beantwortet, und ein notwendiger lokaler Rollback erfolgt atomar über SEM.

## 8. Verbindliche Migration der sieben ausgeschlossenen Rules

### 8.1 Grundsatz

Der Master-v2-Code darf die Rules `5,7,8,9,13,15,17` nicht automatisch aktivieren oder ihre Semantik still verändern. Ein technischer Sharding-Algorithmus darf keine fachliche Safety-Entscheidung erfinden. Der Release MUSS jedoch ihren Status vollständig und maschinenprüfbar erfassen; keine Rule darf während der Migration unbemerkt verschwinden.

Jede globale Rule erhält ein `deployment_status` aus:

- `active_node`,
- `active_cross`,
- `replaced_local`,
- `monitor_only`,
- `disabled_with_reason`.

Für `replaced_local` muss eine Liste der Ersatz-Rule-IDs und deren Hardware-Owner vorliegen. `disabled_with_reason` erfordert einen nichtleeren Auditgrund und ist im Produktionsprofil für `group=safety` nicht freigabefähig.

### 8.2 Migrationsmatrix

| Rule | Diagnose | Verbindliche Zielbehandlung |
| ---: | --- | --- |
| 5 | Controllerübergreifende Bedingung, schreibt State 10 | in Cross-Entscheidung plus genau eine lokale Zielaktion zerlegen oder State 10 vollständig dem Cross-Scope zuweisen; kein Mixed Writer |
| 7 | Prozessweite Mutation mehrerer lokaler States | in owner-spezifische lokale Regeln zerlegen; gemeinsame Bedingung darf als nichtkritisches Cross-Faktum dienen |
| 8 | schreibt Prozesse 1 und 3 und konkurriert mit lokalen Writern | pro Prozess/State genau einen Writer festlegen; keine parallele Originalrule |
| 9 | Timer startet Prozesse 1 und 3 | Timerentscheidung deterministisch einem Scope zuweisen und owner-spezifische lokale Startaktionen erzeugen |
| 13 | kritischer Füllstand, Safety-Cross-Abhängigkeit | lokale Interlocks an jedem betroffenen Hardware-Owner oder Safety-PLC; Cross höchstens Alarm/Monitoring |
| 15 | Überdruck-Safety mit fremdem Signal | lokaler Druck-Interlock am Sensor-/Hardwarepfad beziehungsweise hart verdrahtetes Signal; Cross höchstens Alarm |
| 17 | manuell gelatchte Notabschaltung mehrerer States | in lokale gelatchte Emergency-Interlocks je Hardware-Owner zerlegen; Reset lokal autorisieren und auditieren |

### 8.3 Release-Gate für die Lab-Config

Das selbststartende Lab-Profil darf die bestehende, nachweislich writer-sichere Teilmenge `1,2,3,4,6,10,11` verwenden. Die sieben übrigen Rules müssen im erzeugten Migrationsreport sichtbar als `disabled_with_reason: migration_required` erscheinen. Das ist kein Produktions-Safety-Nachweis.

Für ein Produktionsprofil gilt:

- Rules `13,15,17` müssen durch getestete lokale/PLC-Interlocks ersetzt sein,
- Rules `5,7,8,9` müssen writer-eindeutig migriert oder fachlich ausdrücklich entfernt sein,
- die alte Originalrule und ihre Ersatzrules dürfen nie gleichzeitig aktiv sein,
- ein Test muss jede ursprüngliche Rule-ID auf aktive Zielrules oder einen genehmigten Entfernungsentscheid abbilden.

## 9. Scope-Migration und Execution-Epoch-Fencing

### 9.1 Problem

Eine atomare Master-DB-Transaktion macht einen `node -> cross`, `cross -> node` oder Owner-Wechsel nicht automatisch atomar auf mehreren Prozessen. Ohne zusätzliches Fencing könnten alter und neuer Scope gleichzeitig handeln oder es entstünde eine unkontrollierte Lücke.

### 9.2 Normatives Verfahren

Jede ausführbare Rule-/Prozess-/State-Gruppe besitzt:

- `execution_owner`,
- `execution_scope`,
- monotone `execution_epoch`,
- `activation_state = staged|active|retiring|inactive`.

Eine Migration läuft innerhalb eines Master-Commits fachlich festgelegt, aber verteilt als gefencete Aktivierung:

1. neuen Scope mit höherer `execution_epoch` als `staged` materialisieren,
2. neuen Scope an Zielworker übertragen und `READY` abwarten,
3. neue Epoch in der autoritativen Activation-Map committen,
4. Activation-Update an alle betroffenen Hardware-Owner und SEMs übertragen,
5. nach deren ACK den neuen Scope `active` schalten,
6. alten Scope `retiring` schalten; alte Intents werden wegen Epoch abgewiesen,
7. alten Scope entfernen und `inactive` bestätigen.

Remote-Aktorintents enthalten `execution_epoch` und `execution_scope`. Der Hardware-Owner akzeptiert nur die aktuell autorisierte Epoch. Lokale Cross-zu-Node-Intents durchlaufen dieselbe SEM-/Routerprüfung.

### 9.3 Verfügbarkeitsregel

- Safety- und zeitkritische Steuerung darf online niemals in einen Cross-Scope migriert werden.
- Reine lokale Scope-Änderungen auf demselben Worker werden durch einen SEM-Snapshot-Swap atomar ausgeführt.
- Cross-Migrationen liegen im Sekundenhorizont. Zwischen Epoch-Aktivierung und neuem ersten Intent ist eine kurze koordinative Lücke zulässig; Doppelwirkung ist nicht zulässig.
- Schlägt `READY`, Activation-ACK oder Validierung fehl, bleibt der alte Scope aktiv und die Migration wird sichtbar als `failed` markiert.

## 10. Distributed-Protokoll v2

### 10.1 Version und Capabilities

Neue Nachrichten verwenden:

```json
{
  "protocol": "automation.distributed.v2",
  "contracts_version": "1.1"
}
```

Registration und Heartbeat melden mindestens:

```json
{
  "node_id": "fn-01",
  "runtime_nonce": "...",
  "protocol_versions": ["automation.distributed.v1", "automation.distributed.v2"],
  "contracts_version": "1.1",
  "capabilities": {
    "node_full": true,
    "node_patch": true,
    "cross_runtime": true,
    "cross_full": true,
    "cross_patch": true,
    "chunk_reassembly": true,
    "config_proposals": true,
    "cross_fact_consumer": true,
    "remote_actuator": true,
    "master_automation_execution": false
  },
  "observed_global_revision": 42,
  "node_replica": {
    "scope": "node:fn-01",
    "revision": 7,
    "fingerprint": "..."
  },
  "cross_replica": {
    "scope": "cross:fn-01.default",
    "revision": 3,
    "fingerprint": "..."
  },
  "live_config_revision": 19,
  "pending_proposal_count": 0,
  "max_packet_size": 1048576
}
```

Ein Worker ohne Cross-Capability erhält keinen Cross-Shard. Ist er als Cross-Owner konfiguriert, wird der Commit abgelehnt beziehungsweise der Rollout sichtbar blockiert; die lokale Node-Regelung bleibt aktiv.

### 10.2 Gemeinsame Envelope-Regeln

Jede Command-Hülle enthält:

- stabile `message_id`,
- `command`,
- authentifizierten Source-/Target-Node außerhalb manipulierbarer Nutzdaten,
- `coordinator_node_id`,
- `coordinator_term`,
- `created_at_s` und `expires_at_s`,
- `global_revision`,
- `contracts_version`,
- Scope und Zielrevision, falls Config-bezogen.

Retries verwenden dieselbe `message_id` und denselben fachlichen Payload. Ein neuer Payload erhält eine neue ID. Der Empfänger journalisiert Inbox-Identität und Ergebnis; ein Duplikat liefert dasselbe Ergebnis.

Die Source-Identität wird aus dem mTLS-/ACL-gebundenen Topic-/Sessionkontext bestimmt. Ein frei gesetztes `source_node_id` im Payload ist niemals alleinige Autorisierungsgrundlage.

### 10.3 Full Node Apply

Command: `distributed.config.apply`

```json
{
  "shard_scope": "node_local",
  "scope_id": "node:fn-01",
  "shard_node_id": "fn-01",
  "authority_revision": 42,
  "authority_fingerprint": "...",
  "shard_fingerprint": "...",
  "target_scope_revision": 7,
  "target_scope_fingerprint": "...",
  "global_revision": 42,
  "execution_epoch": 5,
  "config": {}
}
```

Der Worker prüft Zielnode, Term, TTL, Verträge, Revision, Fingerprint, Schema, Scope, Ownership und Safety. Anschließend wird die Baseline-/Overlay-Prozedur aus Abschnitt 3.4 ausgeführt. Bei Fehler bleibt die bisherige Liveconfig unverändert.

### 10.4 Full Cross Apply

Command: `distributed.cross.apply`

```json
{
  "shard_scope": "cross",
  "scope_id": "cross:fn-01.default",
  "owner_node_id": "fn-01",
  "authority_revision": 42,
  "authority_fingerprint": "...",
  "cross_revision": 3,
  "cross_fingerprint": "...",
  "target_scope_revision": 3,
  "target_scope_fingerprint": "...",
  "global_revision": 42,
  "execution_epoch": 8,
  "config": {}
}
```

Zusätzlich werden `validate_cross_execution_shard`, Hardwarefreiheit und Owner-Capability geprüft. Der Cross-Snapshot wird atomar gespeichert und ausschließlich im isolierten Cross-Thread aktiviert.

### 10.5 Patch Node/Cross

Commands:

- `distributed.config.patch`,
- `distributed.cross.patch`.

```json
{
  "shard_scope": "node_local",
  "scope_id": "node:fn-01",
  "shard_node_id": "fn-01",
  "base_scope_revision": 7,
  "base_scope_fingerprint": "...",
  "target_scope_revision": 8,
  "target_scope_fingerprint": "...",
  "global_revision": 43,
  "execution_epoch": 5,
  "operations": [
    {
      "op": "replace_field",
      "section": "rules",
      "entity_key": "rule:1",
      "json_pointer": "/when/1/value",
      "expected_value_hash": "...",
      "value": 43.0
    }
  ]
}
```

Der Worker:

1. vergleicht Base-Revision und Baseline-Fingerprint,
2. wendet Ops auf eine Kopie der Master-Baseline an,
3. materialisiert/normalisiert den vollständigen Shard,
4. prüft Target-Fingerprint und vollständige Scope-Validierung,
5. rebased Pending-Proposals,
6. tauscht Baseline und Livezustand atomar aus.

Bei `patch_base_mismatch`, unbekannter Operation, Value-Hash-Mismatch, Journal-Lücke oder Validierungsfehler antwortet der Worker NACK. Der Master sendet automatisch Full. Ein Patch darf höchstens 500 Ops enthalten und muss inklusive Envelope in die ausgehandelten Paketgrenzen passen; sonst wird Full oder Chunk gewählt.

Die Felder `authority_revision`, `authority_fingerprint`, `shard_fingerprint`, `cross_revision` und `cross_fingerprint` bleiben in den Full-Hüllen als kompatible v0082-Anker enthalten. Die neuen `target_scope_*`-Felder sind die autoritativen Diff-Cursor. Beide Feldgruppen müssen konsistent sein; abweichende Aliaswerte werden abgelehnt. `shard_scope` behält die bisherigen Werte `node_local` beziehungsweise `cross`; `scope_id` trägt die eindeutige interne Scope-ID.

### 10.6 Proposal Worker → Master

Command: `distributed.config.propose`

```json
{
  "proposal_id": "proposal:fn-01:...",
  "operation_id": "local:fn-01:...",
  "scope": "node:fn-01",
  "base_scope_revision": 7,
  "base_scope_fingerprint": "...",
  "local_config_revision": 20,
  "operations": [],
  "reason": "rule_engine_action"
}
```

Der Master akzeptiert Node-Proposals nur vom Scope-Owner und Cross-Proposals nur vom Cross-Owner. Er mappt die Ops nicht über heuristische Action-Texte, sondern über dieselbe Entity-Registry. Ergebniszustände:

- `accepted` mit `commit_id`, `global_revision`, neuem Scope-Head,
- `already_committed`,
- `rebase_required` mit aktueller Scope-Revision/Fingerprint,
- `rejected` mit stabilem Fehlercode und Felddetails,
- `not_authorized`.

Nach `accepted` bleibt das Proposal pending, bis der entsprechende Master-Shard beim Worker als Baseline angekommen ist. Erst dann ist es `converged`.

### 10.7 Unabhängiger Global-Stand

Ein Commit an fn-01 erhöht `global_revision`, aber nicht fn-02s Scope-Revision. fn-02 erhält keinen Config-Rollout. Der Master kann im Heartbeat-ACK lediglich `observed_global_revision` aktualisieren. Proposal-Basen verwenden Scope-Revision/Fingerprint und werden deshalb nicht durch fremde Commits künstlich stale.

## 11. Begrenzter und restart-sicherer Chunk-Transport

### 11.1 Einsatz

Passt eine Full- oder Patch-Hülle nach realer Serialisierung nicht in `max_packet_size`, wird sie als `distributed.config.chunk` übertragen. `target_command` ist einer von:

- `distributed.config.apply`,
- `distributed.cross.apply`,
- `distributed.config.patch`,
- `distributed.cross.patch`.

### 11.2 Kodierung

1. vollständigen Zielpayload kanonisch als UTF-8-Bytes serialisieren,
2. optional deterministisch gzip-komprimieren,
3. in Byte-Segmente teilen,
4. jedes Segment Base64-kodieren,
5. erst danach in JSON einbetten.

Ein direktes Umwandeln beliebiger Byte-Slices in UTF-8-Teilstrings ist verboten, weil ein Slice ein Mehrbytezeichen trennen kann.

```json
{
  "transfer_id": "rollout:fn-01:node:8",
  "target_command": "distributed.config.apply",
  "scope": "node:fn-01",
  "target_scope_revision": 8,
  "target_scope_fingerprint": "...",
  "encoding": "gzip-base64",
  "payload_sha256": "sha256 der unkomprimierten kanonischen Bytes",
  "encoded_sha256": "sha256 des zusammengesetzten Transportbytestrings",
  "sequence": 2,
  "total_chunks": 7,
  "chunk_sha256": "sha256 der rohen Segmentbytes",
  "chunk_base64": "..."
}
```

### 11.3 Harte Ressourcenlimits

Default-Limits, über Config nur innerhalb harter Obergrenzen reduzierbar oder kontrolliert erhöhbar:

- maximales MQTT-Paket: vom Broker/Worker ausgehandelt, Lab default 1 MiB,
- rohe Chunkgröße: maximal 192 KiB,
- unkomprimierter Payload: maximal 32 MiB,
- komprimierter Payload: maximal 16 MiB,
- Chunks pro Transfer: maximal 512,
- parallele Transfers pro Node: maximal 2,
- Transfer-TTL: 3600 s,
- Missing-Chunk-NACK: nach 30 s,
- maximale Dekompressionsratio: 20:1.

Die tatsächliche serialisierte Envelope-Größe wird vor Publish geprüft. Ein fixer pauschaler Overhead ist nicht ausreichend.

### 11.4 Worker-Reassembly

Der Worker speichert Transferkopf und Chunks in SQLite mit Primary Key `(transfer_id, sequence)`. Duplikate mit gleichem Hash sind idempotent; gleiche Sequenz mit anderem Hash bricht den Transfer ab. Reassembly erfolgt in einer temporären Datei beziehungsweise begrenztem Buffer, niemals durch unbeschränktes String-Anhängen.

Erst wenn alle Chunks, Segmenthashes, Gesamthash, Größenlimits und Zielidentität stimmen, wird der rekonstruierte Payload an den unveränderten Full-/Patch-Handler übergeben. Dieser führt die fachliche Validierung und den atomaren Commit aus.

ACK/NACK:

- final `applied`/`already_current`,
- `chunks_missing` mit begrenzter Sequenzliste/Bitmap,
- `chunk_conflict`,
- `payload_hash_mismatch`,
- `transfer_limit`,
- `transfer_expired`,
- fachlicher Fehler des Zielhandlers.

Retries fehlender Chunks behalten dieselbe Transfer-ID. Eine höhere Zielrevision superseded den alten Transfer. Alte Transferdaten werden nach Terminalzustand oder TTL entfernt.

## 12. Rollout-Orchestrator und Konvergenz

### 12.1 Entscheidung pro Node und Scope

```text
Worker fingerprint == Scope head fingerprint
    -> already_current, kein Config-Traffic

bekannte Replica-Revision + lückenlose scope_ops + Patch innerhalb Limits
    -> PATCH

sonst
    -> FULL

serialisierte Nachricht > ausgehandeltes Paketlimit
    -> CHUNKED PATCH oder CHUNKED FULL
```

Ein Cross-Scope wird nur an seinen Owner mit passender Capability verteilt. Unveränderte Scopes bleiben still.

### 12.2 Zustandsmaschine

```text
unknown -> staged -> sending -> received -> applied -> converged
                 \-> retry_wait -> sending
                 \-> failed
                 \-> superseded
```

`received` bestätigt nur Transport/Reassembly. `applied` bestätigt Worker-Commit. `converged` wird durch den folgenden Heartbeat mit exakt passender Replica-Revision und Fingerprint bestätigt.

Pro `(node_id, scope)` existiert höchstens ein aktiver Rollout. Eine neuere Revision markiert eine ältere als `superseded`; sie überschreibt nie dieselbe Rollout-ID mit anderem Payload.

### 12.3 Retry

- exponentiell 1, 2, 5, 10, 30, 60 s, dann 60 s,
- jitter deterministisch aus `rollout_id`,
- keine enge Pollschleife,
- Terminalfehler mit sichtbarem Status,
- Heartbeat kann einen fehlenden Rollout erneut anstoßen,
- Full-Fallback bei jedem Patch-Basisproblem.

## 13. Cross-Fact-Router

### 13.1 Aufgabe

Nach Entfernung von `global_automation` bleibt im Master ein reiner Cross-Fact-Router. Er evaluiert keine Rule. Er kennt aus den materialisierten Cross-Shards lediglich:

```text
(controller_uuid, sensor_id) -> benötigte cross domains / owner nodes
```

### 13.2 Datenfluss

```text
Hardware-Owner SensorPoll
  -> lokaler EventRouter und lokale Automation
  -> MQTT distributed.sensor.fact
  -> Master Cross-Fact-Router
  -> nur betroffene Cross-Owner
  -> Cross-I/O-Adapter / Fakten-Cache
```

Lokale Fakten eines Cross-Owners dürfen direkt über dessen EventRouter in den Cross-Thread gelangen. Der Master-Rückweg ist dafür nicht erforderlich.

### 13.3 Fact-Envelope

```json
{
  "event_id": "sensor:uuid:3:4711",
  "source_node_id": "fn-02",
  "controller_uuid": "...",
  "sensor_id": 3,
  "value": 123.4,
  "quality": "good",
  "sample_sequence": 4711,
  "sampled_at_s": 1784150000.25,
  "source_config_revision": 12
}
```

Der Router prüft:

- cert-/topic-gebundene Source,
- Source ist Hardware-Owner der UUID,
- Sensor existiert im aktiven Node-Shard,
- Sequenz/Timestamp plausibel,
- Ziel ist aus aktiver Cross-Dependency-Map abgeleitet,
- Payloadgröße und Werttyp gültig.

### 13.4 Delivery-Semantik

Sensorfakten sind Latest-Value-Beobachtungen, keine Config-Commits:

- QoS 1 und Event-ID-Deduplizierung,
- pro Sensor coalescing auf den neuesten Wert,
- keine unbeschränkte durable Historienqueue,
- bei Reconnect aktueller Snapshot statt Replay aller Zwischenwerte,
- ältere Sequenzen werden verworfen,
- `max_signal_age_s` im Cross-Shard entscheidet über Gültigkeit,
- stale/missing Facts verhindern Cross-Aktionen oder aktivieren nur den konfigurierten nichtkritischen Cross-Default.

Master-/Broker-Ausfall lässt Remote-Fakten altern und Cross kontrolliert degradieren. Lokale Node-Regelung erhält ihre Sensorfakten weiterhin direkt und bleibt unverändert aktiv.

## 14. Cross-Aktor-Routing

Der bestehende sichere Pfad wird beibehalten und um Scope-/Epoch-Felder ergänzt:

```text
Cross Rule -> Cross Automation -> Cross-I/O-Adapter
 -> lokale UUID? lokaler EventRouter/SEM/Controller-Thread
 -> remote UUID? distributed.actuator.execute
 -> Master Registry Route
 -> Hardware-Owner Worker
 -> UUID + actuator_id lokal kanonisieren
 -> Safety/Ownership/Term/Epoch/TTL prüfen
 -> lokaler Controller-Thread
 -> Feldbus Write + Readback
 -> distributed.actuator.result
```

Ein Intent enthält mindestens:

- stabile `operation_id`,
- Source-Cross-Scope und `execution_epoch`,
- Ziel-`controller_uuid + actuator_id`,
- gewünschten logischen Wert,
- Config-/Scope-Revision,
- Coordinator-Term,
- Erzeugungszeit und kurze TTL.

Host, Port, Modbus-Adresse, Funktion oder Unit aus einer fremden Nachricht werden ignoriert beziehungsweise als Protokollverletzung abgelehnt. Der Zielworker löst sie ausschließlich aus seinem aktiven Node-Shard auf.

Durability darf keine gefährlichen verspäteten Writes erzeugen: Abgelaufene Intents werden terminal als `EXPIRED` zurückgemeldet und niemals nach langer Brokerunterbrechung ausgeführt. Deduplizierung und Readback bleiben zwingend.

## 15. Master-Module und zu entfernender Code

### 15.1 Gemeinsame Module

Siehe Abschnitt 4. Beide Codebasen importieren dieselbe versionierte Distribution des Contracts-Pakets. Der Release-Build prüft Hash und Version.

### 15.2 Neue oder wesentlich überarbeitete Master-Module

```text
master_node/src/core/config_authority.py
  Master-Single-Writer, Commitnormalisierung, Authority-Matrix

master_node/src/core/shard_materializer.py
  Contracts-Aufrufe, betroffene Scopes, Scope-Heads/Snapshots/Ops

master_node/src/core/rollout_orchestrator.py
  Resync, Full/Patch/Chunk-Wahl, Rolloutzustandsmaschine

master_node/src/core/proposal_ingest.py
  Worker-Proposals, Scope-Base, Rebase, Result/Echo

master_node/src/core/cross_fact_router.py
  Dependency-basierte Sensorfaktweiterleitung ohne Automation

master_node/src/core/scope_activation.py
  Execution-Epoch und Scope-Migration

master_node/src/core/coordinator_service.py
  Registration/Heartbeat/ACK, Capabilities, Routingintegration

master_node/src/library/master_entity_store_postgresql.py
  Transaktionen für Commits, Entities, Versionen, Scopes, Proposals

master_node/src/library/master_heap_store.py
  immutable Snapshots und atomarer Pointer-Swap

master_node/src/library/chunk_transport.py
  begrenzte Segmentierung, Hashes, Retries, Statistiken

master_node/src/interfaces/legacy_worker_mqtt_interface.py
  Protocol-v2-Kommandos, cert-/topic-gebundene Identität

master_node/src/library/migrations.py
  idempotente v0011 -> v2 Schema-/Datenmigration
```

### 15.3 Worker-Erweiterungen

Die lokale Automation wird nicht refactored. Minimal-invasive Ergänzungen:

```text
src/core/distributed_coordinator.py
  v2 Registration, Full/Patch/Chunk-Handler, getrennte Scope-Anker

src/library/_distributed_operation_store.py
  Schema für Replica-Baselines, Scope-Revisionsanker und Chunk-Metadaten

src/library/_scope_replica_store.py
  Baseline + Pending-Overlay + atomare Persistenz

src/library/_chunk_transfer_store.py
  begrenzte restart-sichere Reassembly

src/library/_cross_scope_library.py
  Logik in automation_contracts verschieben; nur Import-/Kompatibilitätsschicht

src/interfaces/mqtt_interface.py
  neue erlaubte Distributed-Kommandos und Größenprüfung
```

Bestehende `_worker_thread`, `_controller_thread`, `_cross_worker_thread`, RuleEngine, FSM, Automation und Feldbusadapter bleiben fachlich unverändert. Änderungen dort sind nur zulässig, wenn ein Test einen zwingenden Integrationsfehler nachweist.

### 15.4 Aus Master entfernen

- `core/global_automation.py`,
- `core/automation_authority.py`,
- `core/_automation_thread.py`,
- Imports, Start/Stop/Snapshot-Status und Tests dieser Ausführung,
- Sensor-Ingest-Aufrufe an globale Automation,
- Master-seitige Rule-/FSM-/Aktorentscheidung.

Ein Architekturtest durchsucht den Master nach verbotenen Imports, Factory-Aufrufen und gestarteten Automation-Threads. Gemeinsame reine Schema-/Shard-Bibliotheken dürfen weiterhin ähnlich benannte Datenstrukturen verwenden, solange nichts ausgeführt wird.

## 16. Idempotente Bestandsmigration v0011 → Master v2

### 16.1 Vorbedingungen

- vollständiges Postgres-Backup,
- Hash der importierten globalen Config,
- Export der Node-Registry, Admission, Terms, Outbox und aktuellen Worker-Anker,
- keine unbekannten offenen Config-Commits,
- Contracts-Version des Migrationswerkzeugs festgeschrieben.

### 16.2 Migrationsablauf

Das Migrationsprogramm läuft in einer DB-Transaktion beziehungsweise in wiederaufnehmbaren, idempotenten Phasen mit Migrationsmarker:

1. neue Tabellen/Spalten anlegen,
2. letzten gültigen globalen v0011-Snapshot laden,
3. site-frei normalisieren, ohne Legacy-Inputkompatibilität zu verlieren,
4. über Entity-Registry zerlegen,
5. Graph und Ownership validieren,
6. Rule-Migrationsreport erzeugen,
7. Node- und Cross-Shards materialisieren,
8. Scope-Heads und Revision `1` erzeugen,
9. Snapshots, Entity-Versionen und initialen Importcommit persistieren,
10. Registry-/Capability-Felder ergänzen,
11. alte Config-Tabellen read-only als Auditquelle behalten,
12. Roundtrip und Fingerprints verifizieren,
13. Master-v2-Activation-Marker atomar setzen.

Ein zweiter Lauf mit identischem Input ergibt denselben fachlichen Zustand und erzeugt keinen zweiten Importcommit. Eine andere Inputdatei bei bereits abgeschlossener Migration wird abgelehnt und erfordert einen normalen Config-Commit.

### 16.3 Migrationsergebnis

Das Werkzeug schreibt einen maschinenlesbaren und menschlich verständlichen Report mit:

- Quell-/Zielfingerprint,
- Entitätszahl je Sektion,
- Scope und Owner jeder Rule/jedes States,
- Node-/Cross-Fingerprints,
- aktiven und ausgeschlossenen Rules,
- ungelösten Referenzen,
- Safety-Migrationsstatus,
- unveränderten und migrierten Legacy-Feldern.

Ohne fehlerfreien Report wird Master v2 nicht aktiviert.

## 17. Fresh-Deploy und Upgrade ohne unnötige Testblocker

### 17.1 Profile

Es gibt mindestens zwei explizite Profile:

| Profil | Zweck | Verhalten |
| --- | --- | --- |
| `lab_v2` | reproduzierbarer Test-Rollout | Test-PKI automatisch erzeugen/verwenden, cert-gebundene Worker automatisch zulassen, deterministische Fixtures, Cross aktiviert |
| `production_v2` | produktiver Betrieb | vorhandene PKI, explizite Node-Allowlist/Admission, keine Testfixtures |

Im Lab-Profil entfallen manuelle Freigabeklicks, separate Tokenanlage und händisches Verschieben von Zertifikaten. Der Rollout erzeugt nur bei fehlender Test-PKI eine lokale Testidentität und bindet `node_id` an das Zertifikat. Es gibt keinen Klartext-MQTT-Fallback und kein globales „disable security“-Flag.

Schema-, Ownership-, Writer-, Safety-, Hash-, TTL-, Readback- und Fencing-Prüfungen sind Integritätsbedingungen und bleiben in beiden Profilen aktiv.

### 17.2 Fresh-Deploy

Ein frischer Lab-Rollout MUSS mit einem dokumentierten Befehl startbar sein:

```bash
./rollout.sh test --fresh --profile lab_v2
```

Der Befehl:

1. prüft Docker/Runtime, Ports und Schreibrechte,
2. erzeugt bei Bedarf Test-PKI und cert-gebundene Node-Identitäten,
3. startet Postgres und Mosquitto,
4. migriert Master v2,
5. startet Master und Proxy,
6. startet zwei Worker mit getrennten Node-Shards,
7. startet zwei getrennte Modbus-Simulatorinstanzen oder äquivalente reale Testendpunkte,
8. registriert Worker automatisch über den cert-gebundenen Lab-Admissionpfad,
9. wartet zustandsbasiert auf Node- und Cross-Konvergenz,
10. führt den vollständigen End-to-End-Test aus,
11. schreibt `TEST_RESULTS_MASTER_V2.txt` und gibt nonzero bei jedem Fehler zurück.

Es werden keine festen Sleeps als Erfolgskriterium verwendet. Readiness wird über Health, Registration, Scope-Anker und konkrete Readbacks festgestellt.

### 17.3 Upgrade-Reihenfolge

Für bestehende Installationen wird derselbe gemeinsame Release kompatibel gerollt:

1. Backup und Preflight,
2. Worker-Erweiterung deployen; sie versteht v1 und v2, lokale Automation läuft weiter,
3. Worker melden v2-Capabilities, alter Master kann weiter v1-Full senden,
4. Master v2 und DB-Migration deployen,
5. Master nutzt v2 nur bei passender Capability,
6. alle Scopes konvergieren lassen,
7. v1-Kompatibilität nach dokumentierter Beobachtungsperiode deaktivieren, aber nicht im selben laufenden Rollout entfernen.

Dies ist eine Rolling-Deployment-Reihenfolge eines einzigen vollständigen Releases, keine funktional unvollständige Produktstufung.

### 17.4 Rollback

- Vor Master-Aktivierung kann auf v0011 zurückgerollt werden; neue Tabellen bleiben ungenutzt.
- Nach v2-Config-Commits wird kein automatischer DB-Downgrade durchgeführt. Rollback bedeutet Master v2 stoppen, DB-Backup wiederherstellen und kompatiblen Worker weiter auf letzter lokaler Config betreiben.
- Worker-Rollback ist nur zulässig, wenn keine v2-only Replica-Baseline benötigt wird; Preflight prüft dies.
- Lokale Feldsteuerung läuft während Master-Rollback weiter.

## 18. Fehler- und Recovery-Matrix

| Fehler | Erwartetes Verhalten |
| --- | --- |
| Master-Prozess stirbt vor DB-COMMIT | keine Änderung sichtbar; Request kann mit gleicher `operation_id` wiederholt werden |
| Master stirbt nach DB-COMMIT vor Publish | transaktionale Outbox wird beim Neustart fortgesetzt |
| Heap-Swap fehlt nach Commit | Neustart rekonstruiert den committed Stand; kein DB-Rollback notwendig |
| Broker offline | Config-/Proposal-Outbox retry; lokale Node-Regelung unverändert |
| Worker offline | Rollout bleibt durable; neuere Revision superseded ältere; bei Rückkehr Full/Patch-Entscheidung |
| Worker startet ohne Replica | Full Node; optional Full Cross |
| Patch-Basis falsch | NACK `patch_base_mismatch`, anschließend Full |
| Chunk fehlt/dupliziert/ungeordnet | hashgeprüfte idempotente Reassembly, Missing-Chunk-Retry |
| Chunk zu groß/Zip-Bomb | frühzeitiger Reject `transfer_limit`, alte Config bleibt aktiv |
| Contracts inkompatibel | sichtbarer `contracts_incompatible`; Node lokal weiter aktiv; kein falscher Rollout |
| Proposal während Master-Ausfall | lokal committed, durable Outbox, späterer Rebase |
| Proposal stale durch Änderung desselben Scopes | `rebase_required`; Overlay bleibt nachvollziehbar |
| Änderung eines fremden Scopes | kein künstlicher Proposal-Konflikt |
| Cross-Fakt fehlt | Fakt wird stale/missing; Cross erzeugt keine unzulässige Aktion |
| Cross-Thread blockiert/stirbt | Cross degradiert; Node-Threads bleiben aktiv |
| Remote-Aktorintent läuft ab | `EXPIRED`, kein Write |
| Master-Failover | neuer Coordinator-Term; alte Commands/Intents werden abgelehnt |
| Twin-Worker gleiche node_id | Master vergibt eine aktive Session-Epoch an cert-gebundene jüngste gültige Registration; alte Session wird gefenced |
| Scope-Migration scheitert vor Activation | alter Scope bleibt aktiv |
| Scope-Migration scheitert nach Epoch-Wechsel | alte Intents bleiben gefenced; Orchestrator aktiviert vorbereiteten neuen Scope oder meldet kontrolliert degraded |
| Postgres nicht verfügbar | keine neuen Master-Commits; Heap darf nur read-only für Status/Routing weiterlaufen; keine nichtpersistente Authority |

## 19. Observability und Betriebsdiagnose

### 19.1 Status pro Node/Scope

`runtime.status` beziehungsweise Master-Status liefert mindestens:

- Node-Session, Zertifikatsidentität und Runtime-Nonce,
- Coordinator-Term,
- Protocol-/Contracts-Version,
- gemeldete Replica-Revision/Fingerprint,
- Master-Scope-Head,
- Rolloutzustand, Versuchszahl und letzter Fehler,
- Pending-/Rejected-Proposals,
- Cross-Fact-Alter und Drop-/Dedupe-Zähler,
- Remote-Aktorzustände und Readback,
- Node-/Cross-Threadzustand laut Worker-Heartbeat.

### 19.2 Pflichtmetriken

- `config_commits_total{result,source}`,
- `scope_materializations_total{scope,result}`,
- `rollouts_total{kind,result}`,
- `rollout_latency_seconds`,
- `chunk_transfers_active`, `chunk_retries_total`,
- `worker_proposals_total{state}`,
- `cross_facts_routed_total`, `cross_facts_dropped_total{reason}`,
- `remote_actuator_operations_total{state}`,
- `scope_migrations_total{state}`,
- `config_convergence_seconds`.

Logs enthalten IDs und Scopes, aber keine Private Keys, Secrets oder vollständigen vertraulichen Configs. Jede Reject-Entscheidung besitzt einen stabilen Fehlercode und eine verständliche Ursache.

## 20. Vollständiger Implementierungsplan innerhalb eines Releases

Die folgende Reihenfolge verhindert Doppelarbeit. Kein Arbeitspaket ist ein separater Produktrelease.

### WP1 – Baseline und Contracts

- v0011/v0082 unverändert archivieren und Hashes prüfen,
- `automation_contracts` aus v0082 extrahieren,
- Entity Registry vervollständigen,
- Master-/Worker-Bytegleichheit für Node und Cross testen.

Fertig: Contracts-Roundtrip, Graphpartition und Fingerprints sind byte-identisch.

### WP2 – Datenmodell und Migration

- neue Tabellen/Migrationen,
- Entity-Zerlegung und Roundtrip,
- Scope-Heads/Snapshots/Ops,
- Regelmigrationsreport,
- Heap-Rebuild.

Fertig: zweimalige Migration ist idempotent und reproduziert exakt dieselben Scope-Fingerprints.

### WP3 – Master Authority und Materializer

- Single-Writer-Committhread,
- dynamische CRUD-Ops,
- Graph-/Gate-Pipeline,
- transaktionale Outbox,
- atomarer Heap-Swap.

Fertig: Commit-/Crash-/Concurrent-Proposal-Tests grün.

### WP4 – Worker Replica-Erweiterung

- getrennte Scope-Anker,
- Baseline/Overlay,
- Full/Patch-Handler,
- Chunk-Store/Reassembly,
- Registration/Heartbeat v2.

Fertig: lokale Automation bleibt in allen Failure-Tests aktiv; Full/Patch/Chunk konvergieren restart-sicher.

### WP5 – Rollout-Orchestrator

- Resync-Matrix,
- Full/Patch/Chunk-Auswahl,
- Retry/Supersede,
- ACK/NACK/Heartbeat-Konvergenz.

Fertig: Änderungen erzeugen ausschließlich Traffic für betroffene Scopes.

### WP6 – Bidirektionale Proposals

- Proposal-Ingest,
- Authority-Matrix,
- Scope-Rebase,
- Konvergenz und Rollback semantischer Rejects.

Fertig: Local-first-Änderung wirkt sofort, überlebt Broker-/Master-Ausfall und konvergiert exakt einmal.

### WP7 – Cross Routing und Scope Activation

- Cross-Fact-Dependency-Map und Router,
- Remote-Aktorpfad um Epoch ergänzen,
- Scope-Migration/Activation,
- Cross-Failure-Containment.

Fertig: Remote-Fakt löst ausschließlich beim Cross-Owner aus; Zielhardware schreibt genau einmal mit Readback.

### WP8 – Master-Automation entfernen

- Ausführungsmodule, Imports und Lifecycle entfernen,
- Sensor-Ingest nur noch an Fact-Router,
- Architektur-Hardscan und Ersatztests.

Fertig: kein Master-Automationthread, keine Master-Ruleevaluation und kein Master-Aktorwrite im Quellcode oder Laufzeitstatus.

### WP9 – Lab-Deploy, Realtests und Release

- `lab_v2`-Bootstrap,
- zwei Worker/Modbus-Ziele,
- reale Mosquitto-mTLS-Kommunikation,
- Python 3.14t/no-GIL-Lauf,
- vollständige Testmatrix,
- SBOM, Hashes, Doku und Releasepaket.

Fertig: Abschnitt 21 vollständig grün; kein offener Blocker.

## 21. Verbindlicher Test- und Abnahmeplan

### 21.1 Contracts und Determinismus

1. alle Config-Sektionen besitzen konkrete Entity Keys,
2. Decompose/Materialize-Roundtrip bytegleich,
3. Master und Worker erzeugen identische Node-/Cross-Shards,
4. Reihenfolge unabhängiger Inputobjekte ändert Fingerprint nicht,
5. unbekannte/missing Keys fail-closed,
6. alle graphtransitiven Rule-/State-/Filter-/Prozessreferenzen werden erkannt.

### 21.2 Partition und Safety

1. Single-Controller-Rule wird Node,
2. Cross-Controller auf gleichem Node wird Cross,
3. Cross-Worker wird Cross,
4. Hardwarefelder fehlen im Cross-Shard,
5. unbekannte/duplizierte Owner werden abgelehnt,
6. Mixed Writer wird abgelehnt,
7. Safety-Cross wird abgelehnt,
8. Rule-Migrationsreport bildet alle 14 Originalrules ab.

### 21.3 Commit/CRUD

1. Feld ersetzen/entfernen,
2. Entity hinzufügen/ersetzen/löschen,
3. vollständiger Import,
4. stale Base und Value-Hash-Mismatch,
5. atomarer Multi-Entity-Batch,
6. gleiche Operation-ID liefert identisches Ergebnis,
7. Crash vor/nach DB-COMMIT,
8. Shared-Änderung aktualisiert exakt betroffene Scope-Projektionen.

### 21.4 Revision und Proposals

1. fn-01-Commit ändert fn-02-Scope-Revision nicht,
2. fremder Global-Commit macht fn-02-Proposal nicht stale,
3. Local-first Proposal bei Master-Ausfall,
4. Rebase bei gleichem Scope,
5. semantischer Reject und atomarer Overlay-Rollback,
6. Cross-Proposal nur vom Owner,
7. Proposal wird erst nach Baseline-Rollout `converged`.

### 21.5 Full/Patch/Chunk

1. Node/Cross Full,
2. Node/Cross Patch,
3. falsche Base -> Full-Fallback,
4. 500-Ops-/Größengrenze,
5. UTF-8-Mehrbytezeichen über Chunkgrenze,
6. Verlust, Duplikat, Umordnung und Hashkonflikt,
7. Worker-/Master-Neustart mitten im Transfer,
8. Dekompressionslimit/Zip-Bomb,
9. Supersede durch neuere Revision,
10. finales Fingerprint- und SEM-Commit.

### 21.6 Cross E2E

1. lokales Fakt -> Cross-Thread,
2. Remote-Fakt -> Master-Router -> Cross-Owner,
3. falscher Sensor-Owner wird abgelehnt,
4. stale Fakt verhindert Aktion,
5. lokales Cross-Ziel -> lokaler Controller-Thread,
6. Remote-Ziel -> Master -> Hardware-Owner,
7. Adresse/Funktion nur am Ziel aufgelöst,
8. Write genau einmal und Readback,
9. Intent TTL/EXPIRED,
10. Cross-Crash beeinflusst Node-Regelung nicht.

### 21.7 Scope-Migration

1. Node->Cross und Cross->Node,
2. Cross-Owner-Wechsel,
3. neuer Scope nicht ready -> alter bleibt aktiv,
4. alte Epoch nach Activation abgelehnt,
5. keine Doppelwrites unter Message-Reordering,
6. Crash in jeder Aktivierungsphase.

### 21.8 Master-Grenze

1. verbotene Module/Imports entfernt,
2. keine RuleEngine/FSM/Automation gestartet,
3. Sensorfakt erzeugt im Master keine Ruleevaluation,
4. Master kann keinen Feldbusadapter öffnen,
5. Master-Kill: beide Node-Regelungen laufen weiter,
6. Broker-Kill: beide Node-Regelungen laufen weiter.

### 21.9 no-GIL und Last

1. gesamte Master-/Worker-Suite unter Python 3.14t,
2. Thread-/Commit-/Heap-/Outbox-Stress ohne Race/Deadlock,
3. 100 Master-Restarts mit offenen Rollouts,
4. 100 Worker-Restarts mit Pending-Proposals/Chunks,
5. Revision-Churn 1/s auf fn-01; fn-02 erzeugt null Config-Rollouts,
6. 5-MiB-Shard über 1-MiB-Paketlimit unter 30 s im Lab,
7. Fact-Burst mit Coalescing und begrenztem Speicher,
8. Remote-Aktor-Dedupe unter QoS-1-Duplikaten.

### 21.10 Reale Abnahme

Verpflichtend, nicht durch Doubles ersetzbar:

- echter Mosquitto mit mTLS und ACL,
- zwei getrennte Worker-Identitäten,
- zwei getrennte Modbus-Simulator-/Zielendpunkte,
- Sensor -> lokale Rule -> Aktor -> Readback,
- Remote-Sensor -> Cross-Rule -> Remote-Aktor -> Readback,
- Initial-, Patch- und Chunk-Rollout,
- bidirektionales Proposal,
- Master-/Broker-Kill,
- Python 3.14t/no-GIL mindestens auf den Workern und Master, sofern Zielruntime.

## 22. Release-Artefakte

Der Release ist unvollständig, wenn eines fehlt:

- getrenntes Master-Control-Plane-ZIP,
- getrenntes Worker-ZIP mit v2-Replikationserweiterung,
- versioniertes `automation_contracts` in beiden Builds oder als identisch gehashtes Paket,
- idempotente DB-Migration,
- Fresh-/Upgrade-/Rollback-Runbook,
- `lab_v2`-Testprofil,
- `TEST_RESULTS_MASTER_V2.txt`,
- Testliste mit jeder ausgeführten Testdatei,
- SHA256SUMS und Secret-Scan,
- SBOM,
- Rule-Migrationsreport,
- aktualisierte `CODE_AND_ARCHITECTURE_SPEZIFICATION.md` nur dann, wenn eine gemeinsam genehmigte Spezifikationspräzisierung nötig wurde.

## 23. Definition of Done

Master v2 ist erst freigabefähig, wenn alle Aussagen wahr sind:

- [ ] Master führt keine Automation aus.
- [ ] Lokale Workerautomation läuft standalone und distributed identisch weiter.
- [ ] Node- und Cross-Shards stammen aus derselben graphbasierten Contracts-Logik.
- [ ] Jeder physische Controller hat exakt einen Hardware-Owner.
- [ ] Jeder State/Prozess hat exakt einen logischen Writer.
- [ ] Node-/Cross-Scope-Revisionen sind von lokaler SEM- und globaler Revision getrennt.
- [ ] Full, Patch und Chunk funktionieren für Node und Cross.
- [ ] Worker-Baseline und Pending-Proposal-Overlay sind restart-sicher.
- [ ] Proposals sind local-first, durable, rebase-fähig und sichtbar konvergent.
- [ ] Cross-Fact-Routing funktioniert ohne Master-Ruleevaluation.
- [ ] Remote-Aktorwrites sind UUID-basiert, gefenced, dedupliziert und readback-bestätigt.
- [ ] Scope-Wechsel sind über Execution-Epoch gefenced.
- [ ] alle 14 Ausgangsrules besitzen einen dokumentierten Migrationsstatus.
- [ ] Safety-Rules `13,15,17` sind produktiv lokal/PLC-seitig ersetzt oder das Produktionsprofil ist gesperrt.
- [ ] Lab-Fresh-Deploy startet ohne unnötige manuelle Security-Hürden.
- [ ] fachliche Integritäts- und Safety-Gates bleiben aktiv.
- [ ] sämtliche automatisierten und realen Tests aus Abschnitt 21 sind grün.
- [ ] Python-3.14t/no-GIL-Test ist tatsächlich ausgeführt und dokumentiert.
- [ ] Master- und Worker-ZIPs bleiben getrennt deploybar.

## 24. Dokumentverifikation und ehrlicher Geltungsstand

Diese Version 1.1 wurde gegen folgende bekannte Fakten konsistent formuliert:

- v0082 besitzt Full Node/Cross Apply und Proposals, aber noch kein Distributed Patch/Chunk.
- `local_config_revision` ist eine lokale SEM-Revision und kein Master-Scope-Cursor.
- v0011 startet aktuell noch Master-Automation; deren Entfernung ist Implementierungsumfang.
- v0082 partitioniert graphbasiert und writer-sicher; eine einfache UUID-Zählung reicht nicht.
- die sieben ausgeschlossenen Rules sind keine Mode-Gate-Lücke, sondern erfordern explizite Writer-/Safety-Migration.
- Cross ist logischer Execution-Scope, niemals Hardware-Writer.
- bisherige 36 Worker-Tests sind kein realer Mosquitto-/Modbus-/no-GIL-Nachweis.

Das Dokument ist damit ein vollständiger, testbarer Implementierungsvertrag. Es ist kein Nachweis, dass Master v2 bereits implementiert wurde. Dieser Nachweis entsteht ausschließlich durch die Definition of Done und die realen Testergebnisse des späteren Release-Artefakts.

## 25. Kurzfassung für die Umsetzung

1. Master v2 speichert eine globale Config und materialisiert daraus deterministische Node-/Cross-Shards.
2. Der Master orchestriert und routet; er führt niemals Automation oder Hardware aus.
3. Worker behalten ihre komplette lokale Pipeline und arbeiten bei Master-/Broker-Ausfall weiter.
4. Cross bleibt optional, isoliert, hardwarefrei und nur im Sekundenhorizont.
5. Revisionen werden pro Scope geführt; lokale SEM-Revisionen sind davon getrennt.
6. Config synchronisiert per Full, Patch oder begrenztem Chunk-Transfer; alle Pfade sind atomar und restart-sicher.
7. Worker-Änderungen wirken local-first, werden proposed, rebased und über den Master-Shard konvergent.
8. Remote-Fakten werden nur geroutet; Remote-Aktoren schreibt ausschließlich der Hardware-Owner mit Fencing und Readback.
9. Scope-Wechsel verwenden Execution-Epochen, damit nie zwei Ausführungsowner gleichzeitig wirken.
10. Der Release wird in einem vollständigen Paket umgesetzt und erst nach realem Zwei-Worker-/Mosquitto-/Modbus-/Python-3.14t-Test freigegeben.


# -*- coding: utf-8 -*-

# Automation System - Code Specifications 

> **Historische Referenz:** Diese eingebettete Altversion ist für v0025-r1
> nicht normativ. Maßgeblich ist die Workspace-Spezifikation
> `CODE_AND_ARCHITECTURE_SPEZIFICATION_V005.md` mit Authority V5, Contract
> `2.0`, Type Schema `2.0.0` und der einzigen Library-Wurzel `src/library/`.

Version: 1.1 - aligned with `MASTER_V2_PLANUNGSDOKUMENTATION_DE_v1.1.md`

Normative relation: This file defines the mandatory code and system
architecture rules. `MASTER_V2_PLANUNGSDOKUMENTATION_DE_v1.1.md` is the
binding implementation contract for Master v2 data models, protocols,
migration, deployment and acceptance. If both documents appear to conflict,
implementation MUST stop until the wording is made consistent. Shard,
fingerprint, entity-key and schema logic must never be duplicated locally.


# Module Headers & Module Layout :

1. -*- coding: utf-8 -*-
2. Directory Path to 'this' current File
3. Dokumentation Comment : What it is; Why is needed; How it works;
4. Module Imports
5. Cetral module "CONTEXT MANAGEMENT" definition & initialisation
6. Help-Functions
7. Codebase - Functions - 
8. Interface - External Callable (optional, this will depending)

# Example Header :

```text
# -*- coding: utf-8 -*-

# src/library/_event_interface.py


"""
 =============================================================================
 _EVENT_INTERFACE
 =============================================================================

 What it is:
     A lightweight library that provides a **robust event‑communication** layer
     built on top of plain Python data structures.  Events are represented as
     dictionaries and are transported between producers and consumers using a
     consistent JSON‑based serialisation format.

 Why it is needed:
     • Guarantees that every event can be safely persisted, logged or sent
       across process boundaries (JSON is universally understood).
     • Decouples event producers from consumers – no hard‑coded references,
       only loosely‑coupled message passing.
     • Allows functional‑programming style handlers via ``functools.partial``,
       making it easy to pre‑bind context, configuration or state.

 How it works:
     1.  An **event** is created as a plain ``dict`` that follows a tiny schema
         (``type``, ``payload`` and optional ``metadata``).
     2.  The event is handed to the **Context Manager** which attaches a unique
         identifier, timestamps and any global context information.
     3.  The enriched event is serialised to a JSON string via ``json.dumps``.
     4.  Consumers receive the JSON, deserialize it and invoke the appropriate
         handler that has been registered with the **Interface** layer.
 =============================================================================
"""


# =============================================================================
# MODULE IMPORTS
# =============================================================================

import json
import uuid
"""
```


# Code Codequalität & Spezifikations :

- Python v3.14+ - NO-GIL, ``free-threaded compatibility``
- Free-threaded correctness MUST use explicit Single-Writer queues, locks or
  immutable snapshot swaps for every shared mutable state. The implementation
  MUST NOT rely on the GIL and MUST NOT attempt to re-enable it.
- Comments only in English

- Code conversion compatiblity with C/C11/Cython [Codebase pure Python v3.14+]

- Deterministic, dynamic consistent data flow, data mutation

- Event Driven - Asynchron Processing via Threads/ThreadPool


- Only std types library !
- In principle, only standard libraries; third-party libraries may also be used upon consultation and as needed!

- In business/logic Code Layers only typed per "_generic_type_library.py" library


- Dependency Injection - via Context Management, ctx = {}
- Single Responsibility Principle - module/context dependent: how well the relation to a domain/owner fits and how strong or weak the coupling is
- Separation of Concerns - mostly 


- Synchronous functional Codebase

- Only usage of generic type libraries - Only functional code

- NoPycache - Or pycache should dump in /temp

- If more than 3 arguments/params are explicitly passed to a function or thread, “args” and “kwargs” should be used

 
- The highest possible code quality
- The highest possible code quality
- The highest possible code quality

- Clean, simple, readable, maintainable & understandable Code
- Clean, simple, readable, maintainable & understandable Code
- Clean, simple, readable, maintainable & understandable Code


- The Code Layout&Syntax must be highly normed and standardized
- Try & Exceptions Blocks to handle errors, at the most used functions
- Usefull, minimalistic Logging, when ist makes sense

- Importent is the usage of generic Libraries
- Deterministic consistent functionality
- Simple Clean Code (code that is easier to understand)

- No typehints, No "from __future__ import annotations"
- No typing

- No decorators
- No '@' Operators in Code!
- No dataclasses
- No OOP, No classes

- No SimpleNamespace !!!
- No Lambda functions - only functool.partial



# Worker Master Spezifikations :

## Operating Modes

The worker runs in exactly one of two operating modes, selected via profile:

1. **Single Worker (standalone)** - no MQTT, no master. The worker's own
   node-local config is the ONLY authority. Full automation pipeline runs
   locally and autonomously.
2. **Distributed Workers (master/worker)** - MQTT-connected. The master's
   config is the GLOBAL authority, eventually consistent. Each worker remains
   a SUBORDINATE authority for its own node shard.

## Invariants (must hold in EVERY mode, at ALL times)

- The complete automation pipeline (RuleEngine, FSM, Automation, Controller,
  Fieldbus-I/O) ALWAYS runs locally on the worker - in standalone AND in
  distributed mode. The master never replaces local processing!
- Control must NEVER fail because the master or the broker fails. Master and
  broker outage degrades ONLY synchronization and coordination - never local
  control. Local automation continues on the last consistent config.
- Disabling the local pipeline is never a silent default. If a deployment
  intentionally runs a worker as forward-only, this must be an explicit,
  loudly logged configuration decision - never an implicit fallback.
- The worker has access to MQTT ONLY. Never to PostgreSQL, never to any
  master-internal storage. All synchronization flows through MQTT.

## Config Authority

- Standalone mode : the worker's own node config is authoritative.
- Distributed mode: the master's config is authoritative but EVENTUALLY
  consistent. The worker applies master commits asynchronously; between
  commits the worker's local view is valid and actively controlling.
- Conflict policy is `master_wins`: on divergence the master's authoritative
  revision replaces the worker's local state; local proposals that became
  stale are rebased or rejected - deterministically, never silently dropped.
- Every worker holds a durable local journal (SQLite outbox/inbox) so that
  proposals, acknowledgements and replications survive restarts and broker
  outages without loss (store-and-forward, at-least-once).

## Bidirectional Synchronization (asynchronous, event-driven, MQTT)

- **Worker → Master (local-first):** internal changes (rule engine actions,
  FSM transitions, parameter updates) commit FIRST locally through the
  central Single-Writer (StateEventManagement), are journaled durably, and
  are then proposed asynchronously to the master
  (`distributed.config.propose`). The
  local commit never waits for the master.
- **Master → Worker (replication):** external changes (operator commits,
  imports, new devices/parameters/rules) commit at the master and replicate
  asynchronously to the affected workers (`distributed.config.apply`) - as
  the worker's NODE SHARD, not as the global config. A worker only ever
  receives its own Node shard and an optional separately owned Cross shard;
  convergence is verified per shard via revision + fingerprint.
- Synchronization is event-driven and eventually consistent: registration,
  heartbeat and commit events trigger resync; a fingerprint mismatch triggers
  replication; matching fingerprints converge to `already_current`.
- Replication and remote commands are fenced (coordinator term, ownership
  check); stale or foreign-epoch commands are rejected fail-closed. Remote
  actuation additionally requires hardware readback confirmation.

## Config Revisions, Replica Baseline and Distributed Protocol

- `global_revision`, Node `scope_revision`, Cross `scope_revision` and the
  Worker's local `local_config_revision` are different values. A local SEM
  revision MUST NEVER be used as the cursor for a Master scope diff.
- Every Worker persists the last applied Master Node replica revision plus
  fingerprint and, when Cross is enabled, one separate Cross replica revision
  plus fingerprint. A Worker has zero or one Cross replica/thread in this
  architecture.
- Local-first mutations form a durable pending-operation overlay on top of the
  last Master replica baseline. A Master rollout is staged on a copy, pending
  operations are deterministically rebased, the complete candidate is
  validated, and only then swapped atomically through StateEventManagement.
- Changes to one scope MUST NOT cause Config traffic or artificial proposal
  conflicts in an unchanged foreign scope. Worker proposals use the affected
  scope revision and fingerprint as their base.
- Initial or unknown state converges through full Node/Cross snapshots
  (`distributed.config.apply`, `distributed.cross.apply`). Later changes may
  use validated scope patches (`distributed.config.patch`,
  `distributed.cross.patch`). A patch base mismatch MUST fall back to FULL.
- Payloads larger than the negotiated MQTT packet limit use bounded,
  restart-safe `distributed.config.chunk` transfer. Binary slices MUST be
  Base64 encoded; arbitrary byte slices MUST NOT be decoded independently as
  UTF-8. Total size, chunk count, decompression ratio, concurrency and TTL
  require hard limits.
- Full, patch and chunk paths MUST validate target node/scope, contracts
  version, coordinator term, execution epoch, revision, canonical fingerprint,
  GenericType schema, ownership and Safety before an atomic commit. The last
  valid Worker Config remains active on every failure.
- Config messages, proposals and Actuator operations use stable message or
  operation IDs and durable Inbox/Outbox deduplication. Retries reuse the same
  identity and payload. New content receives a new identity.

## Responsibility Split

- Worker : owns and controls its node shard - sensors, actuators, rules,
  processes, states of its own controllers. Local, autonomous, real-time-near.
- Master : owns the global authority config, node registry, admission,
  audit, graph classification, Node/Cross materialization, distribution and
  routing. The Master NEVER runs RuleEngine, FSM, Automation, Controller or
  Fieldbus-I/O. Execution of cross logic happens on the owning worker node
  (see Cross-Scope Spezifikation).
  Coordination happens in the seconds horizon - time-critical and
  safety-critical control loops are ALWAYS worker-local.


Der Master ist die globale, eventual-konsistente Config-Autoritaet - der
Worker bleibt fuer seinen eigenen Shard eine eigenstaendige, untergeordnete
Autoritaet mit vollstaendig lokaler Pipeline. Synchronisation laeuft
bidirektional, asynchron und ereignisgesteuert ausschliesslich ueber MQTT:
lokale Aenderungen committen local-first und werden dem Master vorgeschlagen,
Master-Commits replizieren als Node-Shard zurueck (master_wins). Faellt der
Master oder der Broker aus, degradiert nur die Synchronisation - die Kontrolle
am Feld laeuft unveraendert weiter. Genau deshalb gilt: Der Master koordiniert
und autorisiert, aber er regelt niemals selbst die Hardware eines Workers.


# Cross-Scope Spezifikation (Cross-Controller / Cross-Node) :

## Placement & Ownership

- Cross logic (rules/processes whose transitive references span MORE THAN ONE
  controller) forms its own CROSS SHARD - whether the referenced controllers
  belong to the SAME node (Cross-Controller) or to DIFFERENT nodes
  (Cross-Worker). It is a config shard like any other,
  with exactly ONE `owner_node_id` (a designated worker node). Single-Writer
  is preserved: the cross shard has one owner - always.
- The cross shard is distributed by the master SEPARATELY from the node-local
  shards (own rollout, own revision + fingerprint, own convergence check).
- The master remains config authority and message relay ONLY. The master
  NEVER executes cross logic and NEVER actuates hardware itself.

## Isolated Execution: the Cross-Controller-Thread

- On the owning worker node, the cross shard is processed 100% ISOLATED in a
  dedicated **cross-controller-thread** - structurally identical to a normal
  controller thread, but WITHOUT own fieldbus hardware.
- Full isolation means: own thread, own event queues, own RuleEngine / FSM /
  Automation instances, own shard snapshot, own journal scope. The cross
  thread NEVER shares mutable state with local controller threads; exchange
  happens exclusively message-based via the EventRouter and the central
  Single-Writer (StateEventManagement).
- Failure containment: a crashing, blocking or overloaded cross thread must
  NEVER impair the local controller threads - and vice versa. Local control
  loops always take priority over cross coordination.
- Cross entities are NEVER loaded into the local pipeline and local entities
  are NEVER loaded into the cross thread (fail-closed via `owner_node_id`
  plus scope separation). Execution-owned entities are disjoint by
  construction; immutable shared type/template catalogs may be projected into
  both snapshots.

## Data Flow

- **Facts IN:** the cross thread consumes sensor facts by `controller_uuid` -
  local facts from the own node directly (event-based), remote facts as
  mirrored sensor values via MQTT (`distributed.sensor.fact`). The
  Master/Cross-Fact-Router validates and routes facts but NEVER evaluates a
  Rule. Remote facts carry source identity, sequence and timestamps; stale
  facts beyond `max_signal_age_s` are treated as missing. Fact delivery uses
  bounded latest-value/coalescing semantics, never an unbounded durable
  telemetry backlog.
- **Actions OUT (local targets):** actuator tasks targeting controllers of
  the OWN node are dispatched message-based to the owning local controller
  thread - the normal dispatch path, never direct hardware access.
- **Actions OUT (remote targets):** actuator tasks targeting controllers of
  FOREIGN nodes flow exclusively through the coordinator path
  (`distributed.actuator.execute` with fencing term, ownership check and
  hardware readback). The receiving node's controller thread executes the
  write - a cross thread never touches foreign fieldbus connections.
- Cross is a LOGICAL execution owner, never a hardware writer. The Hardware
  Owner resolves UUID plus local Actuator ID to address/function, validates
  ownership, Safety, TTL and execution epoch, writes exactly once and performs
  readback. Foreign bus coordinates are ignored or rejected.

## Constraints

- Cross coordination operates in the SECONDS horizon. Time-critical and
  safety-critical control loops are NEVER cross - they stay node-local or
  hard-wired. On master/broker outage the cross scope degrades gracefully
  (facts age out; expired actions are never replayed); local control is never
  affected. Safety-Cross Rules fail closed and require a tested local or PLC
  replacement before production release.


Cross-Logik ist ein eigener Shard mit genau einem Owner-Worker und laeuft dort
vollstaendig isoliert in einem dedizierten Cross-Controller-Thread - gleiche
Struktur wie ein Controller-Thread, nur ohne eigene Feldbus-Hardware. Fakten
kommen per controller_uuid (lokal direkt, remote gespiegelt via MQTT), lokale
Aktionen gehen nachrichtenbasiert an die lokalen Controller-Threads, remote
Aktionen ausschliesslich ueber den abgesicherten Koordinator-Pfad. Der Master
verteilt den Cross-Shard separat und bleibt reine Autoritaet - er fuehrt nie
selbst aus. Faellt Master oder Broker aus, degradiert nur die Cross-Ebene;
die lokale Kontrolle bleibt davon vollstaendig unberuehrt.


# Cross-Controller-Thread Architektur (Owner-Worker Node, z.B. fn-01) :

+-------------------+       +-------------------+       +---------------------+
|   _thread_pool    |<----->| thread_management |<----->|   _worker_thread    |
+-------------------+       +-------------------+       |   (legacy, 1..N)    |
        ^                            |                  +---------------------+
        |                            |                             |
        |                            v                             v
        |                 +---------------------+   +-----------+   +-----------+
        |                 | _cross_worker_thread|   | Controller|<->| Modbus TCP|
        |                 |   (0..1 pro Node)   |   +-----------+   +-----------+
        |                 +---------------------+         ^
        |                            |                    |  ACTUATOR_TASK (lokales Ziel,
        |                            v                    |  nachrichtenbasiert via Router)
+-----------+   +-----------+   +-----------+   +------------------+   +------------------+
|   FSM     |<->| RuleEngine|<->| Automation|<->| Cross-Controller |<->| _cross_io_adapter|
+-----------+   +-----------+   +-----------+   |  (virtuell, ohne |   |  (virtueller Bus)|
                                                |  eigene Hardware)|   +------------------+
                                                +------------------+      |            |
                                                                     FACTS IN      ACTIONS OUT
                                                                          |            |
                                              +---------------------------+            +---------------------------+
                                              |                                                                    |
                                    +-------------------+                                              +----------------------+
                                    |  Fakten-Cache     |                                              |  Ziel-UUID lokal?    |
                                    |  uuid+sensor_id   |                                              +----------------------+
                                    |  -> wert+ts       |                                              |  JA: Dispatch an     |
                                    +-------------------+                                              |  lokalen Controller- |
                                       ^            ^                                                  |  Thread (Standard)   |
                                       |            |                                                  |  NEIN: MQTT          |
                             lokal: EventRouter   remote: MQTT Mirror                                  |  distributed.        |
                             (SENSOR_VALUE_       (SENSOR_VALUE_UPDATE                                 |  actuator.execute    |
                              UPDATE direkt)       via Broker, ts-Alter                                |  -> Owner-Node fuehrt|
                                                   > max_signal_age_s                                  |  aus (Fencing +      |
                                                   = Fakt "fehlt")                                     |  Readback)           |
                                                                                                       +----------------------+

Legend:
  <->  Bidirektionale Verbindung (nachrichtenbasiert, via EventRouter/Queues)
  _cross_worker_thread ist strukturell identisch zum _worker_thread:
  gleiche FSM/RuleEngine/Automation-Instanzen (eigene Kopien!), gleicher
  Task-Pfad zum _thread_pool, gleiches Shard/Snapshot/Rollout-Modell.
  Einziger Unterschied: Der Cross-Controller ist ein VIRTUELLER Controller -
  statt _modbus_tcp_adapter haengt der _cross_io_adapter am Ende der Kette:
    FACTS IN   : liest nie Hardware - konsumiert nur Sensor-Fakten per
                 controller_uuid (lokal direkt, remote gespiegelt via MQTT)
    ACTIONS OUT: schreibt nie Hardware - lokale Ziele gehen als ACTUATOR_TASK
                 an den besitzenden lokalen Controller-Thread, remote Ziele
                 ausschliesslich ueber distributed.actuator.execute
  Isolation: eigener Thread, eigene Queues, eigene Engine-Instanzen, eigener
  Cross-Shard - kein geteilter mutabler State mit legacy Threads. Ausfall
  des Cross-Threads beruehrt die lokale Regelung NIE (und umgekehrt).

  Shard-Zuordnung (thread_management):
    node-shard  (owner_node_id = fn-01)    -> _worker_thread[1..N]  (legacy)
    cross-shard (owner_node_id = fn-01+X)  -> _cross_worker_thread  (virtuell)



# Identity & UUID-Mapping Spezifikation (Config-Norm V7) :

- Site-free identity model: the site tier is removed from the data model
- Hard I/O identity  : `controller_uuid` + local `sensor_id` / `actuator_id`
- Routing identity   : `node_id` + `local_controller_id`  (route_key: "<node_id>.ctrl_<n>")
- `controller_uuid` is globally unique across all nodes and is the ONLY
  resolution key for any reference (rules, io_refs, filters, virtual endpoints)
- Local IDs (`sensor_id`, `actuator_id`, `local_controller_id`) are unique
  ONLY within their controller/node scope - never globally

- One Node = One Node-Local Config = One Node Shard:
  Every worker node loads exactly ONE node-local config file
  (`device_state_data_v7_node_<node_id>.json`). It contains ONLY the own
  node view (controllers, virtual_controllers, I/O, routing, filters,
  rules, processes, process_states) plus the shared, node-free
  type/template library (sensor_types, actuator_types, scaling_profiles,
  schema_mappings, control_methods, rule_engine, timers, ...).
  NO cross-node information is ever present in the NODE-LOCAL config.
  An optional Cross-Owner receives a SECOND, strictly separate and
  hardware-free Cross snapshot for its isolated Cross thread. Node and Cross
  entities are never merged into one execution pipeline.

- Ownership (Single-Writer on Hardware):
  Exactly ONE node owns the write access to a controller. Ownership is
  declared in `site_controller_map` via `routing_role: "owner"`. Logic
  ownership is declared per rule/process via `owner_node_id` (fail-closed:
  entities without matching owner are never loaded). A controller, rule or
  process has exactly one owner - always.
  Hardware ownership and logical execution ownership are distinct: only the
  Controller's Node Owner writes hardware; a Cross Owner only creates intents.

- Cross-Controller & Cross-Node references (lightweight UUID mapping):
  Any rule condition, io_ref or filter may reference a foreign controller
  simply by its `controller_uuid` (`remote_signal_refs:
  allowed_by_controller_uuid`). No site tier, no lookup table, no extra
  mechanism is required - the UUID resolves the reference by itself,
  uniformly for cross-controller (same node) and cross-node (foreign node).
  READ references are lightweight and always allowed (facts arrive as
  mirrored sensor values via MQTT). WRITE targets always stay with the
  owning node - a foreign node never actuates foreign hardware directly;
  remote actuation flows exclusively through the coordinator path
  (`distributed.actuator.execute` with fencing term, ownership check and
  hardware readback).

- Master keeps the global view:
  Only the master holds the global authority config (all nodes, all shards,
  all cross logic). Worker node shards are deterministic projections of the
  global authority. Cross-node logic forms its own cross shard with exactly
  one owning worker node and never reaches the node-local shards by
  construction (see Cross-Scope Spezifikation).

- Master scope changes:
  A node-to-cross, cross-to-node or Cross-Owner migration requires a monotonic
  `execution_epoch`. The new scope is staged and validated before activation;
  old-epoch events and Actuator intents are rejected. A failed activation
  leaves the old valid scope active. Safety control MUST NOT be migrated to
  Cross online.

- Schema behaviour:
  The site tier is OPTIONAL, not forbidden (backwards compatible): configs
  with `sites`/`site_id` (V5 norm) stay valid; V7 node shards omit them
  entirely. Validation, referential checks and uniqueness scopes operate on
  `controller_uuid` + local IDs and `node_id` + `local_controller_id`.


Die Aufloesung jeder Referenz laeuft in V7 ausschliesslich ueber die global
eindeutige controller_uuid plus lokale ID - einheitlich fuer Cross-Controller
und Cross-Node. Ein Worker kennt nur seinen eigenen Shard; Lese-Referenzen
auf fremde Controller sind per UUID leichtgewichtig moeglich, waehrend die
Schreib-Ownership jeder Hardware immer bei genau einem Node liegt. Der Master
haelt als einzige Instanz die globale Gesamtsicht und rollt Node-Shards sowie
den Cross-Shard deterministisch, konsistent und separat aus - exakt wie im
Sharding-Konzept dieser Spezifikation gefordert. Die Cross-Ausfuehrung selbst
liegt dabei isoliert beim Owner-Worker, niemals beim Master.


# Graph-Based Config Sharding Specification :

- Cross classification MUST NOT be inferred from duplicated UUIDs or from an
  entity-local controller count alone. The complete transitive graph is
  resolved: Rule -> conditions/filters/processes/states -> io_refs -> Sensors
  and Actuators -> Controllers, including every mutated State and Process.
- Single-Controller logic with one unambiguous writer belongs to the owning
  Node shard. Multi-Controller logic belongs to exactly one Cross shard.
- Every runtime-mutable State and Process has exactly one execution scope.
  Mixed writers, unknown references, duplicate owners and ambiguous scope are
  rejected fail-closed.
- Shared type/template catalogs are projected into every shard that needs
  them. Physical Controller and I/O coordinates exist only in the Hardware
  Owner's Node shard. Cross materialization retains only UUID plus local IDs
  and the shared definitions needed by the Cross engines.
- Master and Worker MUST import the exact same versioned Contracts code for
  normalization, GenericType validation, entity keys, graph traversal, shard
  building, Cross stripping and canonical fingerprints. Byte-identical input
  MUST produce byte-identical Node/Cross shards and fingerprints.
- Dynamic add, update, field replacement, field removal and row deletion are
  atomic operations against the canonical Master Config. Derived shards are
  never edited as independent authorities.


# Master v2 Config Authority and Runtime Specification :

- PostgreSQL is the durable Master truth for global entities, commits, scope
  heads/snapshots, scope operations, proposals, rollouts and audit. The Master
  Heap is an immutable runtime mirror, not a competing authority.
- Exactly one Master Commit writer serializes Operator changes, imports and
  Worker proposals. A DB transaction persists the global change, affected
  scope projections and transactional Outbox before the prepared Heap snapshot
  is atomically activated.
- The Master materializes and versions only affected Node/Cross scopes. Full,
  patch or chunk rollout selection is made independently per `(node, scope)`.
- The Master is a pure Config/Registry/Routing authority. Removal of all Master
  Automation lifecycle, imports and Sensor-to-Rule paths is mandatory.
- Remote Sensor facts are routed only to Cross Owners whose active dependency
  graph requires them. Remote Actuator intents are routed only to the current
  Hardware Owner and retain fencing, TTL, deduplication and readback.
- Config/Proposal delivery is durable at-least-once. Sensor facts use bounded
  latest-value semantics. Expired Actuator intents terminate without a write.
- The detailed binding data model, protocol fields, migration algorithm,
  module plan and acceptance suite are defined in
  `MASTER_V2_PLANUNGSDOKUMENTATION_DE_v1.1.md` and are mandatory for the
  Master-v2 release.


# Test, Security and Release Specification :

- Professional integrity gates (schema, ownership, writer safety, fingerprints,
  fencing, TTL, Safety and readback) are active in every profile.
- A `lab_v2` profile MAY automatically create a test PKI and cert-bound Worker
  identities and MAY auto-admit those identities so a fresh test rollout needs
  no manual Security ceremony. It MUST NOT introduce plaintext MQTT, anonymous
  Nodes or a global bypass for integrity/Safety checks.
- Master and Worker are released as separate deployable ZIP codebases but as
  one compatible feature release. The release is incomplete until full,
  patch, chunk, proposals, Cross routing, scope fencing and Master Automation
  removal work together.
- Acceptance requires the complete automated suites plus a real two-Worker
  test with Mosquitto mTLS, separate Modbus targets, local and Cross Actuator
  readback, Master/Broker kill tests and Python 3.14t free-threaded execution.
- In-process MQTT/Fieldbus doubles and a GIL-enabled Python build are useful
  unit tests but are not a production release proof.



# Architecture Designs :

```text
+-------------------+       +-------------------+       +---------------------+
|   _thread_pool    |<----->| thread_management|<----->|   _worker_thread    |
+-------------------+       +-------------------+       +---------------------+
                                                               |
                                                               v
+-----------+   +-----------+   +-----------+   +-----------+   +-----------+
|   FSM     |<->| RuleEngine|<->| Automation|<->| Controller|<->| Modbus TCP|
+-----------+   +-----------+   +-----------+   +-----------+   +-----------+

Legend:
  <->  Bidirektionale Verbindung (nachrichtenbasiert)
  FSM / RuleEngine / Automation können schwere Berechnungen
  als *Task* an den _thread_pool senden (Pfeile von FSM/RE/AUT zum Pool).
```

_thread_pool  <‑‑>  thread_management  <‑‑>  _worker_thread
worker_thread  <‑‑>  fsm  <‑‑>  ruleengine  <‑‑>  automation  <‑‑>  controller  <‑‑>  modbus_tcp
[_thread_pool_worker] : [fsm  <‑‑>  ruleengine  <‑‑>  automation]


# Components :

- Multi-Threaded
- Event Driven - Event-Queues & Priority-Event-Queues & central Event-Broker

- At Runtime all Data in Heap (perfomance&latency)
- At Runtime all Data persistent syncing to FILE/DB
- Async and consistent data/config synchronisation

- Single-Writer(global config-data)
- Single-Writer(local controller thread config-data)

- Hierarchical Architecture&Datamodel
- Config-Master ist die einzige globale Config-Authority; der Worker bleibt
  die lokale untergeordnete Runtime-/Hardware-Authority.


# Components (pre-template) :

StateEventManagement (CentralConfigSingleWriter)

GenericThreadPool
GenericRuleEngine
GenericFiniteStateMachine

WorkerThread[1..N]
[ControllerThread]
[AutomationThread]

GenericEventRouter
DataManagement (JSON|SQLITE|HEAP)

MQTT Bridge
Device-IO



# Directory-Structure-Template :

```text

src/
│
├── adapter/
│   ├── __init__.py
│   ├── _adapter_interface.py
│   ├── _bus_timing.py
│   ├── _connection_registry.py
│   ├── _connection_thread.py
│   ├── fieldbus/
│   │   ├── __init__.py
│   │   ├── _ethernet_ip_adapter.py
│   │   ├── _knx_ip_adapter.py
│   │   ├── _modbus_tcp_adapter.py
│   │   ├── _profibus_gw_adapter.py
│   │   └── _profinet_rt_adapter.py
│   └── plc/
│       ├── __init__.py
│       ├── _plc_modbus_adapter.py
│       └── _plc_opcua_adapter.py
│
├── core/
│   ├── __init__.py
│   ├── _automation_thread.py
│   ├── _controller_thread.py
│   ├── _event_router.py
│   ├── _worker_thread.py
│   ├── state_event_management.py
│   └── thread_management.py
│
├── library/
│   ├── __init__.py
│   ├── _automation_engine.py
│   ├── _event_interface.py
│   ├── _generate_ca_signed_certs.py
│   ├── _generic_data_center.py
│   ├── _generic_finite_state_machine.py
│   ├── _generic_rule_engine.py
│   ├── _generic_thread_pool.py
│   ├── _generic_type_library.py
│   ├── _json_formator.py
│   ├── _queue_factory.py
│   └── _state_shard_manager.py
│
├── system/
│   ├── __init__.py
│   ├── _ascii_plotter.py
│   ├── _metrics_system.py
│   ├── _system_init.py
│   ├── bootstrap_runtime_interface.py
│   ├── bootstrap_system_interface.py
│   └── cpu_mem_probe.py
│
├── __init__.py
└── sync_xserver_main.py

```

```text

├── _config/
│   ├── application/
│   │   └── app_config.yaml
│   └── config_data/
│       ├── device_state_data_v5_multi_worker_norm.json
│       └── device_state_data_v5_validation_report.txt
│
├── _documentation/
│   ├── README.md
│   ├── directory_structure.md
│   └── github/
│       ├── CODE_OF_CONDUCT.md
│       ├── COMMERCIAL_LICENSE.md
│       ├── CONTRIBUTING.md
│       ├── DESCRIPTION.md
│       ├── LICENSE.md
│       ├── README.md
│       └── SECURITY.md
│
├── _logs/
│
├── _tools/
│   ├── README.md
│   ├── __find_py_imports_v2.py
│   ├── __tree_finder.ps1
│   ├── __tree_finder.py
│   ├── __tree_finder.sh
│   ├── _check_code_specific_standards.py
│   ├── gil-free_benchmark.py
│   ├── list_files.py
│   └── purge_pycache.sh
│
├── src/
│   ├── ...
│
└── test/
    └── test_generic_data_center.py

```



# Components Discription :

```text

StateEventManagement EventBased - MemCached - System SingleWriter, 

WorkerThread - 1..N Per Controller Thread - Orchestration of Controller-Tasks to Fieldbus-Adapter-Interface - Retrieves SensorValues
WorkerThread - 1..N Per Controller Thread - Orchestration of SensorValues to Automation-Tasks GenericThreadPool - Retrives Controller-Tasks

GenericThreadPool - ThreadPool - Automation-Tasks-Calculation
GenericRuleEngine - Automation RuleEngine - WorkerLocal - Rule-Management
GenericFSM		  - StateMachine ProcessStates - WorkerLocal - State-Management

GenericEventRouter - Routing Event between Threads (Priority-Event-Queues)
DataManagement - Read/Write JSON, SQLITE, Central MEMCACHE

```

The configuration data should first be dynamically and generically sharded correctly and loaded into the MEM cache. The way the data is stored is extremely important to ensure that it is distributed correctly and can later be synchronized correctly between threads. Event-state management synchronizes events based on specific occurrences. The distribution of the data should proceed as smoothly as possible. Correct sharding makes it possible to synchronize each worker thread individually. The synchronization itself should be performed via copy/snapshot.
 The primary dataset must be capable of being synchronized externally and internally at any time. The data for the worker threads must be sharded in such a way that it can be updated and rolled out separately, consistently, and deterministically. Synchronization should be efficient, as it is time-critical, though it does not have to be precise to the millisecond. Since the data must remain consistent, a lock or mutex without deadlocks should be used.
 Gradual synchronization could also be an option here, splitting horizontally across two or three levels, though this is not a requirement if the system works fine as is!!! Synchronization should also be event-driven.
 Synchronization can also occur internally via the worker threads themselves; if a state changes because a rule alters a process state, a write-commit or a write request must be sent to the central event-state manager to centrally propagate the state change!
 Only once the central cache memory is up to date again is the sync chain continued downstream. The thread that triggers the state change is not synchronized itself until the central write and downstream sync are complete.


Die Config Daten sollen zunächst dynamisch generisch korrekt gesharded und in den MEM cache geladen werden.
Die Art des speicherns der Daten ist extrem wichtig, damit diese korrekt verteilt und später korrekt zwischen
den threads synchronisiert werden können.
Event-State-Management synchronisiert event basiert bei bestimmten ereignissen.
Die verteilung der daten sollte dabei möglichst reibungsfrei verlaufen.
Durch das korrekte sharding ist es möglich, jeden WorkerThread einzeln zu synchronisieren.
Das syncing selber sollte per copy/snapshot verlaufen.
Der primär Datensatz soll jederzeit extern/intern synchronisiert werden können.
Die daten für die worker threads müssen so geshareded werden, dass diese separat aktualisiert
und ausgerollt werden können, konsistent und deterministisch.
Das synching sollte effizient verlaufen, da zeitkritisch, allerdings kommt es nicht auf eine millisekunde an.
Da die daten konsistent bleiben müssen, sollte eine lock/mutex ohne deadlock verwendet werden.
Auch stufenweise synchronisation könnte hier interessant sein, die horizontal auf zwei/drei ebenen
spliten, was aber kein muss ist, wenn es nicht aus so funktioniert !!!



###

###

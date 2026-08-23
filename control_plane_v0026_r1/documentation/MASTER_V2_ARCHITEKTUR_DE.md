# Master-/Control-Plane-Architektur v0026-r1

## 1. Systemgrenze

Im Standardmodus `node_local` ist jeder Worker die finale Config-Authority
seines Node-Shards. Die Control Plane erweitert die autonome Worker-Laufzeit um
Registry, Relay, Mirror, Audit, Authentifizierung und Flottenkoordination.

```text
Operator / Client
  -> Proxy (Auth, CSRF, RBAC, Transportvertrag)
  -> Master (Registry, Relay, Mirror, Audit)
  -> owning Worker (SEM, Safety, Authority-Commit)
  -> lokaler Hardware-Owner
  -> Feldbus + Readback

MCP Client
  -> MCP Controller (Bearer, Site-RBAC, Prepare/Approve/Commit)
  -> MQTTS mit source.instance_id=mcp-controller-01
  -> Master (derselbe Control-Vertrag)
  -> owning Worker (unveränderte finale Authority)
```

Der Master enthält keine RuleEngine, FSM, Automation, Controller-Threads oder
Feldbusadapter.

## 2. Ownership

| Bereich | finale Authority / Writer |
|---|---|
| Node-Config und Scope-Revision | owning Worker |
| SEM- und Safety-Entscheidung | owning Worker |
| physischer Controller | dessen Node-Hardware-Owner |
| Cross-Owner und nächste Execution-Epoch | Master Registry |
| Cross-Config-Akzeptanz | aktivierter Cross-Owner-Worker |
| Relay, Mirror und Audit | Master |
| Clientidentität und RBAC | Proxy |
| MCP-Protokoll, MCP-Bearer/RBAC und Approval-State | MCP Controller Server |
| Control-Plane-Persistenz | PostgreSQL |

`master_global` bleibt ein expliziter Legacy-Migrationsmodus. Ein Flagwechsel
allein darf Authority nicht übertragen. Nach einem node-autoritativen Commit
ist das kontrollierte A5-Transferprotokoll zwingend.

## 3. Proposal-Grenze

Das einzige verteilte Format ist `config_scope_operations_v1` mit den
entity-keyed Operationen `replace_field`, `upsert_entity` und
`delete_entity`.

- Der Proxy validiert Form, Scope und RBAC und mappt nicht.
- Der MCP Controller validiert MCP-Principal, Site-Scope, Tool-Katalog und
  Approval-Digest und erzeugt ausschließlich kanonische Master-Kommandos. Er
  mappt keine Worker-Operations und spricht keinen Worker direkt an.
- Der Master validiert Envelope und Registry-Target, journalisiert und relayed
  dieselbe Operationsliste. Er vergibt keine Node-Revision.
- Der Worker validiert Principal, Basis-Authority-Epoch, Scope-Revision,
  Fingerprint, SEM, Safety und Ownership und entscheidet final.
- Legacy Path-/Selector-Eingaben dürfen nur an einem Worker-Ingress einmalig
  gegen einen unveränderlichen Snapshot normalisiert werden.

## 4. Contract- und Type-Authority

Es gibt genau eine physische Authority-Wurzel je Projekt:

```text
src/library/_generic_type_library.py
src/library/_generic_system_contract_library/
```

Worker, Master und Proxy enthalten byte-identische Dateien. Der MCP Controller
importiert den bestehenden MQTT-/Envelope-Vertrag über eine isolierte
Komponentengrenze und erhält keine zweite Contract-Authority. Versionen:

- Contract `2.0`,
- Type Schema `2.0.0`,
- Node Storage V9,
- Cross Storage V8,
- expliziter V5-Import.

Das alte Paket `automation_contracts`, `contracts_src` und ein paralleles
`src/libraries/` sind im Release verboten.

## 5. Persistenz und Fencing

PostgreSQL ist die durable Wahrheit der Control Plane; Worker halten ihre
Authority zusätzlich crash-sicher in SQLite. Mirror und Registry vergleichen
immer:

`(authority_epoch, scope_revision, execution_epoch, fingerprint)`

Inbox, Outbox, Relay-Journal, Mirror, Registry-Übergänge, Restore und ACKs sind
idempotent und term-/epoch-gefenced. Ein Mirror ist Beobachter der Node-
Wahrheit und darf sie nicht als konkurrierende Authority überschreiben.

## 6. Cross Owner-Accept

Ein Ownerwechsel verläuft ausschließlich als:

1. alten Owner persistent einfrieren,
2. Freeze-ACK prüfen,
3. Registry-Entscheidung persistieren,
4. `execution_epoch` monoton erhöhen,
5. neuen Owner mit vollständigem Activation-Proof aktivieren,
6. Hardware-Schreibrecht erst nach erfolgreichem Owner-Accept freigeben.

Fehlende oder alte Proofs ergeben `execution_epoch_stale`.

## 7. Readiness und Recovery

- Node-Readiness bleibt von optionaler Cross-Readiness unabhängig.
- Worker- und Master-Neustarts rekonstruieren Scope-Heads und Mirror-Zustände
  aus der jeweiligen durablen Persistenz.
- Disaster Restore bleibt quarantänisiert, bis Snapshot, Proofs und Preflight
  vollständig verifiziert sind.
- Ein älterer Mirror benötigt einen expliziten, auditierten Override.

## 8. Releasegrenze

Worker und Control Plane werden niemals als gemeinsamer Quellbaum oder
gemeinsames ZIP ausgeliefert. `automation_control_plane_release_v0026_r1`
enthält MQTT, PostgreSQL, Master, Proxy und den eigenständigen MCP Controller,
pinnt aber weiterhin ausschließlich die Hashes des separat gebauten
`automation_system_v0101.zip` und seines internen Manifests.

Die Administration arbeitet in der festen Reihenfolge:

```text
database -> mqtt -> master -> proxy -> mcp_controller_server -> worker
```

Der MCP Controller verwendet die dedizierte Broker-Identität
`urn:automation:site-main:proxy:mcp-controller-01`, besitzt keinen
PostgreSQL-Zugang und keine Worker-Topics.

Siehe außerdem:

- `DIAGNOSE_AUTHORITY_V5_V0025_R1_DE.md` als Authority-Baseline
- `MCP_CONTROLLER_INTEGRATION_V0026_R1_DE.md`
- `DIAGNOSE_UND_INTEGRATION_V0026_R1_DE.md`
- `RELEASE_VALIDATION_V0026_R1_DE.md`
- übergeordnete `CODE_AND_ARCHITECTURE_SPEZIFICATION_V005.md`


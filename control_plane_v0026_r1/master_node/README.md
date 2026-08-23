# Master Node 0.26.0 / v0026-r1

Der Master ist im Authority-V5-Standardmodus **keine** globale Node-Config-
Authority. Er implementiert Registry, Relay, Mirror und Audit (RRMA) und führt
keine Rule, FSM, Automation oder Feldbusoperation aus.

## Aufgaben

- authentifizierte Proxy-Proposals unverändert an den owning Worker relayn,
- authentifizierte MCP-Proposals der dedizierten Instanz
  `mcp-controller-01` über denselben Control-Vertrag relayn,
- Registry- und Cross-Owner-Übergänge monoton sequenzieren,
- Worker-Scope-Heads und Snapshots als lazy Mirror persistieren,
- Relay-, Transfer-, Restore- und Campaign-Ereignisse auditieren,
- Outbox, Inbox, ACKs, Leases und Recovery crash-sicher verwalten,
- immutable Heap-Snapshots ausschließlich als abgeleitete Leseansicht halten.

Der Worker bleibt im Modus `node_local` die Wert-Authority. Der Master darf
weder eine Node-Revision vergeben noch eine Worker-Ablehnung in Annahme
umwandeln. `master_global` existiert nur für kontrollierte Migrationen.

## Persistente Invarianten

- PostgreSQL ist die durable Control-Plane-Wahrheit.
- Registry und Mirror vergleichen das vollständige Tupel
  `(authority_epoch, scope_revision, execution_epoch, fingerprint)`.
- Relay- und Operation-Identitäten werden aus der authentifizierten Envelope
  abgeleitet.
- Cross-Owner-Aktivierung verlangt Freeze-ACK, persistierte Registry-
  Entscheidung und passende nächste `execution_epoch`.
- Node-Readiness bleibt unabhängig von optionaler Cross-Readiness.
- Kein Master-Modul importiert Worker-Ausführungsengines oder Feldbuscode.

## Contract-Authority

Master, Proxy und Worker verwenden byte-identisch:

- `src/library/_generic_type_library.py` – Type Schema `2.0.0`,
- `src/library/_generic_system_contract_library/` – Contract `2.0`.

Das einzige verteilte Config-Operationsformat ist
`config_scope_operations_v1`.

## Laufzeit und Tests

```sh
./test.sh
./test_runtime_state.sh
./test_container.sh
./deploy.sh
./status.sh
```

Die lokale Python-Suite umfasst 40 Testdateien. Der Release-Rollout ergänzt
reale PostgreSQL-, Mosquitto-mTLS-, Worker-, Modbus- und Recovery-Probes.

Der Master akzeptiert in v0026 die Instanzen `proxy-01` und
`mcp-controller-01`. Beide bleiben auf `source.service=proxy` begrenzt; die
eigene Instance-ID und Broker-ACL trennen Zertifikat, ACK-Pfad und Audit.


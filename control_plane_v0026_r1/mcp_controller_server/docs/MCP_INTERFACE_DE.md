# MCP-Interface

## Transportvarianten

### Streamable HTTP

- Endpoint: `POST /mcp`
- Content-Type: `application/json`
- Authentifizierung: `Authorization: Bearer <token>`
- keine Server-Sessions und kein SSE-Stream in Version 0.1.0
- maximale Body-Größe standardmäßig 1 MiB
- Batch-Requests werden abgewiesen
- `Origin` wird, wenn vorhanden, exakt gegen `allowed_origins` geprüft

Der Server implementiert das stateless MCP-Protokoll `2026-07-28`. Für aktuelle
Clients müssen Body-Metadaten und HTTP-Header zusammenpassen:

```json
{
  "jsonrpc": "2.0",
  "id": 1,
  "method": "server/discover",
  "params": {
    "_meta": {
      "io.modelcontextprotocol/protocolVersion": "2026-07-28",
      "io.modelcontextprotocol/clientCapabilities": {},
      "io.modelcontextprotocol/clientInfo": {
        "name": "example-agent",
        "version": "1.0"
      }
    }
  }
}
```

Erforderliche Header sind `MCP-Protocol-Version` und `Mcp-Method`; für
`tools/call`, `prompts/get` und `resources/read` zusätzlich `Mcp-Name`. Ein
Header-/Body-Mismatch wird vor der Ausführung abgewiesen.

Legacy-Clients können `initialize` für `2025-11-25`, `2025-06-18` oder
`2025-03-26` verwenden. Der Server vergibt keine Session-ID; Legacy-HTTP bleibt
damit sessionlos. `ping` und `notifications/initialized` werden unterstützt.

### stdio

stdio verwendet eine UTF-8-JSON-RPC-Nachricht pro Zeile. Es gibt keine
Authentifizierung auf dem Pipe-Transport; der feste Principal kommt aus
`stdio.principal`. Der Default-Principal besitzt ausschließlich Leserechte.
MQTT-mTLS bleibt auch bei stdio verpflichtend.

## Tools

| Tool | Wirkung |
|---|---|
| `automation_connection_status` | lokaler, geheimnisfreier Bridge-/Queue-/Cache-Status |
| `automation_runtime_status` | Master-Runtime und Readiness über MQTT |
| `automation_nodes_list` | Registry-Nodes, Liveness, Admission und Ownership |
| `automation_config_get` | autoritativer Mirror, optional für einen Scope |
| `automation_config_history` | begrenzte Mirror-Historie |
| `automation_operation_get` | dauerhafter Operationszustand nach UUID |
| `automation_events_recent` | begrenzte lokale Eventliste |
| `automation_sensors_latest` | neueste lokale `sensor.value`-Sicht |
| `automation_change_prepare` | validiert und speichert einen exakten Plan, publiziert nichts |
| `automation_change_commit` | publiziert ausschließlich einen exakt freigegebenen Plan |
| `automation_approval_status` | liest Plan-, Freigabe- und Commitzustand ohne Payload |

Alle Schemas sind geschlossen (`additionalProperties=false`). Die Tool-Liste
kann in `policy.allowed_tools` weiter reduziert werden.

## Ressourcen

Feste Ressourcen:

- `automation://bridge/status`
- `automation://events/recent`
- `automation://sensors/latest`
- `automation://server/capabilities`

Templates:

- `automation://config/{URL-kodierter node:<id> oder cross:<id> Scope}`
- `automation://operations/{operation_id}`

Ressourcen führen dieselben Berechtigungsprüfungen wie die zugehörigen Tools aus
und werden auditiert.

## Prompts

- `assess_automation_health`: evidence-first Diagnose ohne Schreiboperation
- `plan_safe_automation_change`: liest Ist-Zustand und endet höchstens bei
  `automation_change_prepare`

Prompts erteilen keine zusätzlichen Rechte. Der Server entscheidet weiterhin
auf Basis des authentifizierten Principals und der festen Tool-Policy.

## Strukturierte Ergebnisse

Jeder Tool-Aufruf enthält Text für einfache Clients und identische strukturierte
Daten:

```json
{
  "content": [{"type": "text", "text": "{ ... }"}],
  "structuredContent": {"ok": true},
  "isError": false
}
```

Fachfehler bleiben erfolgreiche JSON-RPC-Antworten mit `isError=true`, einem
stabilen `code`, einer neutralen `message` und gegebenenfalls `retryable=true`.
Protokoll-, Methoden- und Parameterfehler werden als JSON-RPC-Fehler geliefert.
Interne Exceptions, Secrets und rohe Tracebacks werden nie an Modelle gegeben.

## Schreibworkflow

1. Modell liest Runtime, Nodes und den relevanten Konfigurations-/Operationsstand.
2. `automation_change_prepare` validiert Aktion und Payload, bindet sie an
   Subject und SHA-256 und speichert Zustand `PREPARED`.
3. Ein Operator führt außerhalb des Modellkanals aus:

   ```bash
   ./scripts/approve.sh <UUID> <64-hex> operator:<name>
   ```

4. `automation_change_commit` muss dieselbe UUID und denselben Digest liefern.
5. Plan-ID wird zugleich MQTT-`message_id`; Retry und Client-Replay sind dadurch
   idempotent. Ein terminaler Plan publiziert nicht erneut.

Der Default `approval_mode=external` trennt LLM-Ausgabe und operative Freigabe.
`automatic` ist implementiert, aber für Produktion nicht empfohlen.

## Client-Konfiguration

Generische HTTP-Clients benötigen:

```json
{
  "url": "https://mcp.example/mcp",
  "headers": {
    "Authorization": "Bearer ${MCP_CONTROLLER_TOKEN}"
  }
}
```

Generische lokale Clients können den Prozess starten:

```json
{
  "command": "python3",
  "args": ["-m", "mcp_controller_server.main", "stdio"],
  "env": {
    "PYTHONPATH": "/opt/mcp_controller_server/src",
    "MCP_CONTROLLER_CONFIG": "/etc/mcp_controller/controller_config.json"
  }
}
```

Konkrete Feldnamen unterscheiden sich je nach LLM-/Agent-Host; der MCP-Vertrag
bleibt identisch.

## Normative Referenzen

- [MCP 2026-07-28](https://modelcontextprotocol.io/specification/2026-07-28)
- [Streamable HTTP](https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http)
- [Versionierung](https://modelcontextprotocol.io/specification/2026-07-28/basic/versioning)
- [Tools](https://modelcontextprotocol.io/specification/2026-07-28/server/tools)
- [Caching](https://modelcontextprotocol.io/specification/2026-07-28/server/utilities/caching)

# Externe Authority-V5-Ende-zu-Ende-Abnahme für v0026-r1

Der aktive E2E wird über `integration_test/CURRENT_E2E` ausgewählt und liegt
unter `e2e_v0025/`. Das Control-Plane-Release enthält keinen Worker-Quellbaum;
Fresh und Upgrade installieren das separat verankerte
`automation_system_v0101.zip`.

## Geprüfte Hauptkette

```text
Client/WSS -> Proxy -> Master Relay -> Worker SEM/Authority -> Hardware
                                   <- Result/Mirror/Audit <-

MCP Client -> MCP Controller -> Master Relay -> Worker SEM/Authority -> Hardware
```

Der Core-E2E prüft Auth/RBAC, kanonische Proposals, Config-Commit und Restore,
Events, Actuator-Request/Readback, Security, Recovery und parallele WSS-
Clients. Der File-E2E ändert die aktive Worker-JSON-Datei reversibel und
verlangt denselben kanonischen Kandidaten und Fingerprint wie der native
Clientpfad.

Der MCP Controller besitzt eine eigene Readiness-/MQTT-Grenze, verändert aber
den qualifizierten Worker-/Authority-E2E-Vertrag nicht. Vor dem Core-E2E muss
`mcp_controller_server/ready.sh` grün sein; MCP-Kommandos verwenden denselben
Master-/Worker-Pfad und dürfen keinen direkten Worker- oder DB-Zugriff besitzen.

Node-Ränder verwenden V9-flat; Cross bleibt ein separater hardwarefreier
V8-Vertrag. Legacy Path-/Selector-Eingaben müssen am öffentlichen
`node_local`-Boundary als `unsupported_operation_format` scheitern.

Referenz-Lab:

- Control Plane: `192.168.0.82`
- Worker `fn-01`: `192.168.0.80`
- Modbus-Simulator: `192.168.0.3:5020`
- Topic-/PKI-Site: `site-main`

```sh
./rollout_automation.sh test
```

Die detaillierte Bedien- und Recovery-Dokumentation steht unter
`e2e_v0025/README_E2E_V0025_DE.md`; die manuelle Abnahmematrix unter
`scenarios/CONTROL_PLANE_ACCEPTANCE_DE.md`.


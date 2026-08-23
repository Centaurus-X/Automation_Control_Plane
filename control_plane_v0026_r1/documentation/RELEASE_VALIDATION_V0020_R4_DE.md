# Release-Validierung v0020-r4

## Pflichtprüfungen

- Statische Release-, Shell-, Python- und Architektur-Gates.
- Byteidentische Contract-Kopien in Master, Proxy und `contracts_src`.
- V9-Validierung für bestehende Modbus- sowie neue `plc_s7`-Controller.
- Administration-, Lifecycle-, Recovery- und Upgrade-Regressionen.
- Exakter Worker-v0096-ZIP- und Manifest-Anker.
- Client -> Proxy -> MQTT -> Master -> Worker Differential-Patch.
- Lokale V9-Datei -> File-WAL -> Master-first und Local-first.
- Master-Priorität bei konkurrierendem Local-Patch.
- WSS/TLS/RBAC, PostgreSQL Primary/Standby und MQTT.
- Reales Modbus Read/Write/Readback mit anschließendem Restore.
- Worker-Neustart, File-E2E- und Cross-Recovery.
- Worker-TIA/S7-Emulatortest mit verpflichtendem echten TCP-Backend.
- Reproduzierbarer, byteidentischer Release-Build.

## Versionsvertrag

| Komponente | Erwartung |
|---|---|
| Control Plane | `0.20.0 / v0020-r4` |
| E2E | `0.20.0-e2e.4` |
| Worker | `automation_system_v0096` |
| Worker-Artefakt | `automation-system-v0096` |

## Sicherheitsgrenzen

Ein Authority-Fallback ist nur für `required_mode`, `writable_fields` und
`engine_missing` zulässig. `parameter_limit`, `interlock`, ungültige
Zielreferenzen und technische Fehler bleiben fail-closed.

Der S7-Adapter akzeptiert keine Inline-Secrets. Optimierter DB-Zugriff und ein
fehlender Secure-Backend-Provider bleiben fail-closed. Control Plane und E2E
übertragen keine PLC-Zugangsdaten.

Die finalen Live-Ergebnisse, Paket-Hashes und Manifest-Hashes werden nach dem
Rollout in `TEST_RESULTS.txt` festgehalten.

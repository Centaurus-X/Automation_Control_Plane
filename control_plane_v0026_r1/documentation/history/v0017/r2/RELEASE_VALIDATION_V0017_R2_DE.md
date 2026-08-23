# Release-Validierung v0017-r2

## Validierte Ebenen

- Bash-Syntax sämtlicher Shell-Skripte;
- Python-AST/Bytecode-Kompilierung;
- JSON-Parsing und Konfigurations-Allowlist;
- MQTT-, PostgreSQL-, Master-, Proxy- und E2E-Static/Unit-Matrix;
- Master-Rollout-Recovery und ACK-Generation-Fencing;
- Worker-Lifecycle-r2-Regression;
- SSH-Reconnect-r2-Regression;
- Release-Struktur, Secret-Hygiene und Archiv-Ausschlüsse;
- Root-, Komponenten- und E2E-SHA256SUMS;
- deterministischer ZIP-Build und sichere Extraktion;
- vollständige Matrix aus dem frisch extrahierten Archiv.

## Externe Betriebsgrenze

Der reale Zwei-VM-, MQTT-, PostgreSQL-, WSS- und Modbus-Feldlauf kann nur auf
der Referenztopologie ausgeführt werden. Das Paket beansprucht daher keinen
lokalen Nachweis für Netzwerk- und Hardwarezugriffe. Diese Gates werden durch
`rollout_automation.sh` fail-closed erzwungen; Exit-Code 0 entsteht erst nach
Konvergenz, Telemetrie, vollständigem E2E und beiden Restores.

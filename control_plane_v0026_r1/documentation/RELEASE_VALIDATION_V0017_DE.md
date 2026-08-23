# Release-Validierung v0017-r13

## Verbindliche Prüfebenen

- Bash-Syntax sämtlicher Shell-Skripte;
- Python-AST-/Bytecode-Kompilierung;
- JSON-Parsing und Konfigurations-Allowlist;
- MQTT-, PostgreSQL-, Master-, Proxy- und E2E-Static-/Unit-Matrix;
- Master-Rollout-Recovery und ACK-Generation-Fencing;
- Worker-Lifecycle-Regression mit kanonischem Manifestanker;
- SSH-Reconnect-Regression;
- autonomer Release-Discovery-/Installations-/Delegations-Test;
- Auswahl `v0018-r3 > v0017-r13 > v0017-r12 > v0017-r11 > v0017-r10 > v0017-r9 > v0017-r8 > v0017-r7 > v0017-r6 > v0017-r5 > v0017-r4 > v0017-r3 > v0017-r2 > v0017-r1`;
- explizites `--release`-/`--archive`-Pinning;
- idempotente Wiederverwendung und atomare Selbstheilung;
- ZIP-Sidecar-, CRC-, Pfad-, Typ- und Symlink-Grenzen;
- Release-Struktur, Secret-Hygiene und Archiv-Ausschlüsse;
- Root-, Komponenten- und aktives-E2E-SHA256SUMS;
- deterministischer ZIP-Build und sichere Extraktion;
- vollständige Matrix aus dem frisch extrahierten Archiv.

## Externe Betriebsgrenze

Der reale Zwei-VM-, Docker-, MQTT-, PostgreSQL-, WSS- und Modbus-Feldlauf kann
nur auf der Referenztopologie ausgeführt werden. Das Paket beansprucht deshalb
keinen lokalen Nachweis für nicht erreichbare Netzwerk- oder Hardwarezugriffe.
`rollout_automation.sh` erzwingt diese Gates auf dem Zielsystem fail-closed:
Exit-Code 0 entsteht erst nach Worker-Konvergenz, fortschreitender Telemetrie,
vollständigem Core-E2E sowie bestätigtem Config- und Aktor-Restore.

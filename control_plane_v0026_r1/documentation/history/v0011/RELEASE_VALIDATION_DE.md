# Release-Validierung v0011

Validierungsdatum: 2026-07-15

## Im Erstellungsumfeld bestanden

- `test_static_all.sh` einschließlich MQTT-/PostgreSQL-Static-Gates;
- Master-Standardsprüfung: 56 Python-Dateien;
- Master-Tests: Authority, Config-Authority, globale Automation, Contracts,
  Migrationen, MQTT, Operations, Transaktionen und Auto-Admission;
- Proxy-Standardsprüfung: 46 Python-Dateien;
- sämtliche Proxy-, Security-, Scope-, ASGI- und Transaktionstests;
- Lifecycle-, Fresh-/Upgrade-, Recovery- und Rollout-Simulationen;
- JSON-Parsing, Python-Kompilierung und Shell-Syntax;
- identische Contract-Pakete in Root, Master und Proxy;
- konsistente Release-/Komponenten-Version `0.11.0`;
- keine Worker-Codebase im Control-Plane-Archiv;
- keine privaten Schlüssel, PEM/PYC, Runtime-Secrets oder Caches im Archiv.

## v0011-Aktor-/Authority-Regressionen

- ein Worker kann nur für ausdrücklich beworbene Controller
  `observe_only`-Authority erhalten;
- eine statische Route erweitert den aktiven Worker-Scope nicht;
- Regeln mit fehlenden Sensor- oder Timer-Facts werden global deferred;
- Safety- und `else_do`-Aktionen eines Partial-Snapshots werden nicht
  committed;
- RuleEngine-Aktionen verändern ausschließlich States des
  `required_mode_id`;
- manuelle States bleiben bei automatischen globalen Aktionen unverändert;
- bewahrte explizite Benutzer-/Proxy-Aktorroute bleibt aktiv;
- bewahrte automatische Master-Doppelsteuerung lokaler Controller bleibt
  fail-closed.

## Release-Identität

- MQTT: `eclipse-mosquitto:2.1.2-alpine`;
- PostgreSQL: `automation-postgres:18.4-v11`;
- Master: `automation-master-node:0.11.0`;
- Proxy: `automation-proxy-gateway:0.11.0`;
- OCI-Release-Label eigener Images: `0.11.0`.

## Hier nicht ausgeführt

Das Erstellungsumfeld besitzt keinen Docker-Daemon und keinen Zugriff auf das
private Zielnetz. Deshalb werden folgende Punkte nicht als bestanden
behauptet:

- Docker-Build und Live-Containerstart;
- reale PostgreSQL-Replikation, Backup und Restore;
- reale Mosquitto-mTLS-/ACL-Kommunikation;
- Worker-Registrierung und Config-Konvergenz im Ziel-LAN;
- Modbus-Ende-zu-Ende-Test gegen `192.168.0.3:5020`;
- physischer Failover.

## Verbindliche Zielhost-Gates

```bash
./rollout.sh test --fresh
./rollout.sh test-running
./test_extended_all.sh
./test_worker_auto_admission.sh fn-01 90
```

Danach sind mit Worker v0079 ein autonomer
Sensor→RuleEngine/FSM→Aktor-Test bei getrenntem Master sowie ein separater
Proxy→Master→Worker→Aktor-Test auszuführen.

v0011 bleibt ein vollständiger Lab-Release-Candidate. Eine
Produktionsfreigabe setzt die Zielhost-Gates und die dokumentierten
Multi-Worker-/HA-Grenzen voraus.

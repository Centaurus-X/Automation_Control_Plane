# Release-Validierung v0018-r3

## Verbindliche Prüfebenen

- Bash-Syntax sämtlicher Shell-Skripte;
- Python-AST-/Unit- und JSON-Konfigurationsprüfung;
- MQTT-, PostgreSQL-, Master-, Proxy- und erweiterte Core-E2E-Matrix;
- Master-Rollout-, ACK-/NACK-, Fencing-, Idempotenz- und Restore-Gates;
- sicherer, recoverabler Installer des verankerten Worker-v0083-Archivs;
- Worker-Lifecycle- und SSH-Reconnect-Regression;
- Sensorsequenz-Persistenz und Master-Floor-Synchronisation;
- Runtime-Kennung plus tatsächlicher Live-Konfigurationsfingerprint;
- atomare Paarung von globaler Revision und Fingerprint aus demselben
  persistierten Commit-Snapshot, einschließlich Scheduler-Race-Regression;
- idempotente Scope-Revisionssynchronisation bei bereits identischem Inhalt;
- atomare SEM-Aktivierung beziehungsweise kompensierende Revision;
- lokaler PID-/Modbus-Pfad für Aktor 524;
- echter Worker-Neustart mit unmittelbar fortschreitender Proxy-Telemetrie;
- YAML-gesteuerte Python-Cache-Policy sowie Projekt-`__pycache__`-Gates vor und
  nach echten Workerstarts;
- expliziter bytecodefreier Python-Aufruf für gestreamten Worker-File-E2E und
  dessen lokalen Ergebnis-Verifier, unabhängig vom Archiv-Execute-Bit;
- offizieller Client/WSS→Proxy→Master→MQTT→Worker-Konfigurationscommit mit
  dynamischem Sensor/Aktor, Regel-Threshold, PID-Sollwert, `mode_id` und
  `value_id`;
- reversibler JSON-File-Diff derselben Feldklassen: Struktur und Mode über
  `master_first`, PID-Parameter und State-Value über `local_first`, jeweils
  über Proposal-WAL→Master-Commit→Worker-Hot-Apply;
- CRC-, Pfad-, Typ-, Symlink-, Secret- und Archivhygiene-Gates;
- deterministischer Release-Build und Wiederholung der vollständigen statischen
  Matrix aus dem frisch extrahierten Archiv.
- Regression gegen unterschiedliche Paketinhalte unter derselben immutable
  Release-Revision.

## Abnahmegrenze

Der Release-Build beweist die statischen und isolierten Verträge. Der autonome
Rollout auf der Referenztopologie beweist zusätzlich Docker, MQTT/mTLS,
PostgreSQL, WSS, echten Worker, Modbus und Restart-Recovery. Exit-Code 0 darf
erst nach Restore beider Konfigurationspfade und finaler Live-Konvergenz
entstehen. Maschinenlesbare Nachweise liegen im jeweiligen Rollout-
Ergebnisverzeichnis.

Die Live-Abnahme toleriert außerdem Optimistic Concurrency zwischen
Engineering-Commit und Worker-RuleEngine-Proposal. Sie verifiziert begrenzten
CAS-Rebase, unveränderliche historische Snapshots, feldgenauen Restore und die
Regel, dass fremde Zwischencommits nicht durch einen pauschalen Rollback
verloren gehen dürfen.

# Release Notes – Automation Control Plane v0019-r31

`v0019-r31` ist der additive, unveränderliche Nachfolger von `v0019-r30`.
R30 und alle älteren Releases bleiben unverändert.

## Eng begrenztes Delta

- R31 verankert das getrennte Worker-Paket `automation_system_v0092`.
- Release-Manifest, Installer, Administration-Orchestrator, E2E-Verifier und
  Rollout-Dokumentation prüfen die exakten v0092-Archiv- und Manifest-Hashes.
- Der Worker-v0092-Fix stellt die kanonische, fingerprint-sichere
  V9-Dateidarstellung wieder her und verhindert, dass ein veralteter lokaler
  Rule-Action-Plan einen nachfolgenden JSON/File-WAL-Commit dauerhaft blockiert.

## Unverändert

Master, Proxy, MQTT, PostgreSQL, PKI, Topics, ACLs, Datenbankmigrationen,
Client-Protokoll und Administration-Architektur bleiben gegenüber R30
unverändert. Legacy-Rollout und zentraler Administration-Orchestrator bleiben
getrennte, weiterhin unterstützte Rolloutpfade.

## Freigabekriterien

- vollständige Worker-Suite und R31-Release-Gates;
- reproduzierbarer Zweit-Build;
- Administration-Rollout;
- Upgrade- und Fresh-Rollout;
- Client ↔ Proxy ↔ Master/MQTT ↔ Worker ↔ Modbus;
- nativer MQTT/Master-Diff und lokaler JSON/File-WAL-Diff einschließlich
  vollständigem Config-/Aktor-Restore.

# Einordnung der v0015-Planungsreferenz

Die Dateien in diesem Verzeichnis sind die unveränderte normative
Entwicklungsgrundlage für v0015. Aussagen wie „noch zu implementieren“
beschreiben den damaligen Planstatus und sind kein aktuelles Testergebnis.

Für den tatsächlich integrierten Release gelten vorrangig:

- `../../../ROLLOUT_TEST_ANLEITUNG_AUTOMATION_CONTROL_PLANE_V0015_DE.md`,
- `../../DIAGNOSE_UND_RELEASE_V0015_DE.md`,
- `../../RELEASE_VALIDATION_V0015_DE.md`,
- `../../../integration_test/e2e_v0015/README_E2E_V0015_DE.md`.

Ein lokaler statischer PASS ersetzt weder Docker-/Python-3.14t-Abnahme noch
den realen Worker-/Modbus- oder optionalen Full-Lab-Lauf. Fehlende externe
Vorbedingungen müssen als `NOT_RUN` beziehungsweise `BLOCKED` ausgewiesen
werden.

# Release Notes – Automation Control Plane v0018-r3

## Schwerpunkt

v0018-r3 beseitigt die nach Worker-Neustart gefundenen Inkonsistenzen, ohne
den bestehenden Master-Authority-Pfad zu ersetzen. Der zugehörige Worker ist
`automation_system_v0083`.

## Änderungen

- Der Worker reserviert Sensorsequenzen crashsicher in Blöcken und übernimmt
  beim Registrierungs-/Heartbeat-ACK zusätzlich den vom Master bestätigten
  Sequenz-Floor.
- Jede Worker-Laufzeit besitzt eine neue `runtime_nonce`; der Master akzeptiert
  Konvergenz nur für den gemeldeten Live-Zustand.
- Heartbeats verwenden den tatsächlichen Live-Konfigurationsfingerprint.
- Ein inhaltlich bereits identischer Worker-Shard wird erst dann als
  konvergent gewertet, wenn auch sein persistenter Scope-Revisionscursor dem
  Master entspricht; andernfalls wird ein idempotenter Full-Sync ausgelöst.
- Regel 1 und Regel 2 enthalten keine selbstverriegelnde Abhängigkeit vom
  eigenen Aktivzustand mehr. State 1 bleibt ein automatischer PID-State und
  steuert Aktor 3 / Register 524.
- Der bestehende SEM-Single-Writer kompensiert einen fehlgeschlagenen
  Runtime-Rollout atomar mit der vorherigen validen Konfiguration.
- Optionales, im Worker-Profil standardmäßig aktives JSON-File-Sync erkennt
  stabile semantische Änderungen, persistiert sie im WAL und leitet sie als
  Proposal an den Master weiter.
- `value_id`- und Parameteränderungen verwenden `local_first`; strukturelle
  Änderungen verwenden `master_first`. In beiden Fällen validiert und
  versioniert der Master die globale Wahrheit und repliziert sie zurück.
- Wiederholte identische A→B-Änderungen erhalten pro Edit eine neue durable
  Operation-ID; Crash-Retries derselben Änderung behalten ihre Identität.
- Rollout-Nachrichten binden globale Revision und globalen Fingerprint an
  denselben unveränderlichen Commit-Snapshot. Damit kann ein paralleler
  Outbox-Scheduler keine neue Revision mehr mit einem vor dem Commit gelesenen
  alten Fingerprint kombinieren; fehlende Snapshots führen fail-closed zu
  `retry_wait`.
- Worker-Start und -Tests erzwingen die YAML-Policy
  `runtime.python_cache`: Bytecode-Schreiben ist deaktiviert, ein optionaler
  Cache-Prefix liegt ausschließlich unter `/tmp`, und Projekt-`__pycache__`
  blockiert Installation, Reuse oder Start.
- Der Config-File-E2E und sein Ergebnis-Verifier werden explizit mit
  bytecodefreiem Python gestartet; die Abnahme hängt damit weder von einem
  Archiv-Execute-Bit ab noch kann sie Projekt-`__pycache__` erzeugen.
- Der Release-Dispatcher vergleicht bei einer bereits installierten Revision
  zusätzlich das kanonische `SHA256SUMS` mit der ausgewählten Quelle. Zwei
  unterschiedliche Inhalte unter derselben immutable Revision werden
  fail-closed abgelehnt und müssen als nächste `rN`-Revision veröffentlicht
  werden.
- Fresh/Upgrade installieren das exakt verankerte Worker-v0083-ZIP sicher und
  verschieben alte Worker-Verzeichnisse recoverabel nach `__OLD__`.

## Neue verbindliche E2E-Gates

- Worker-Neustart, neue Runtime-Kennung und sofort monotone Sensorsequenz;
- realer erfolgreicher Modbus-Write auf Register 524;
- offizieller Client/WSS→Proxy→Master→MQTT→Worker-Konfigurationscommit mit
  atomarem Hot-Add eines isolierten Sensors und Aktors sowie Variation von
  Regel-Threshold, PID-Sollwert, `mode_id` und `value_id`;
- Worker-Dateiedit mit denselben Feldklassen: Struktur und `mode_id` über
  `master_first`, PID-Parameter und `value_id` über `local_first`, jeweils mit
  Master-Commit, Rückreplikation, `CONVERGED`-Outbox und vollständigem Restore;
- der File-E2E verwendet deterministisch den von State 1/Aktor 524 referenzierten
  und dem Worker zugewiesenen Controller. Leere, ebenfalls zugewiesene I/O-Gruppen
  anderer Controller werden nicht irrtümlich als Matrixziel gewählt;
- Race-Regression für revisionsgebundene Authority-Metadaten;
- Cache-Prüfung vor und nach jedem echten Worker-Start;
- weiterhin vollständige WSS-Events/Commands, Aktoroperation/Readback,
  Idempotenz, Security-Negativtests und 128 parallele Clients.

## Kompatibilität und Sicherheitsgrenze

Der Master bleibt alleinige globale Autorität. Eine Dateiänderung wird niemals
als ungeprüfte zweite Wahrheit direkt global aktiviert. Master-Ablehnung,
Konflikt/Rebase und kompensierender Restore bleiben fail-closed. Reuse führt
keine Worker-Neuinstallation und keinen Neustart durch.

Der Core-E2E behandelt legitime parallele Worker-RuleEngine-Commits adaptiv:
Ein explizites `stale_revision` wird begrenzt gegen einen neu gelesenen
Snapshot wiederholt. Der E2E-Restore besitzt ausschließlich seine protokollierte
Menge eigener Felder. Ohne Zwischencommit wird weiterhin der historische
`config.rollback` geprüft; bei einem unabhängigen Zwischencommit werden nur
diese Felder in einem kompensierenden Proposal zurückgesetzt. Eine
konkurrierende Änderung eines eigenen Feldes bleibt fail-closed und wird niemals
überschrieben.

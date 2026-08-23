# Automation Control Plane v0019-r31 – autonomer Rollout und Test

Stand: 2026-07-23

Primärer Konfigurationsvertrag: V9 (flache Sensor-/Aktor-Entities). Der
hardwarefreie Cross-Shard bleibt separat V8. Ein V5-Dokument darf nur über den
expliziten Legacy-Importpfad eingelesen werden und wird danach als V9
persistiert.

Für reproduzierbare Pläne kann vor dem Legacy-Rollout der additive
Administration-Einstieg verwendet werden:

```bash
./administration/orchestrate.sh plan --inventory administration/inventory.example.json
./administration/orchestrate.sh apply --plan administration/runtime/plan.json
```

Der Administration-Layer ersetzt keine bestehende Deploy-Mechanik; der
vollständige Test-Rollout ruft weiterhin genau einmal
`rollout_automation.sh` als Backend auf.

## Benötigte Dateien unter `$HOME`

```text
automation_control_plane_release_v0019_r29.zip
automation_control_plane_release_v0019_r29.zip.sha256
automation_system_v0092.zip
automation_system_v0092.zip.sha256
rollout_automation.sh
credentials.json
```

## Fresh-Rollout

```bash
cd "$HOME"
chmod 700 rollout_automation.sh
chmod 600 credentials.json
sh ./rollout_automation.sh \
  --credentials "$HOME/credentials.json" \
  --worker-archive "$HOME/automation_system_v0092.zip" \
  --fresh --purge-host
```

Auf einem geteilten Docker-Host wird `--purge-host` weggelassen. Im
Fresh-/Upgrade-Pfad wird `automation_system_v0092.zip` übertragen. Das Archiv
wird vor und nach dem Transfer gegen ZIP- und Manifestanker geprüft; alte
Worker-Versionen werden recoverabel unter `$HOME/__OLD__` abgelegt.

Vor jeder mutierenden Rollout-Aktion prüft der E2E-Preflight außerdem den im
Worker-Profil konfigurierten Feldbus-Endpunkt. Ist dieser nicht erreichbar,
bricht der Lauf fail-closed ab, bevor Worker oder Control Plane ersetzt werden.

Der Ablauf umfasst:

1. automatische Release-Auswahl und interne Manifestprüfung;
2. sichere Worker-ZIP-Übertragung und recoverable Installation;
3. Control-Plane-Fresh-Rollout und Admin-Bootstrap aus `credentials.json`;
4. Worker-MQTT-PKI, Admission und Node-Shard-Konvergenz;
5. Live-Telemetrie Worker→Master→Proxy;
6. kontrollierten Worker-Neustart mit neuer Runtime-Kennung und sofort
   monotoner Sensorsequenz oberhalb des bisherigen Master-Floors;
7. Core-E2E über WSS mit offiziellem Master-Commit, Events, Commands,
   Aktor-Readback, Restore und 128 parallelen Clients;
8. Worker-JSON-File-E2E für `local_first` und `master_first`, jeweils mit
   Master-Commit, Rückreplikation, durablem `CONVERGED`, einem erst nach der
   Hot-Aktivierung erzeugten und bestätigten Modbus-Write auf Aktor 3 /
   Register 524 sowie vollständigem Restore;
9. abschließende Sync-Diagnose und `summary.json`.

Der aktive E2E-Stand wird aus `integration_test/CURRENT_E2E` gelesen.

## Release-Auswahl

```bash
sh ./rollout_automation.sh --list-releases
sh ./rollout_automation.sh --release v0019-r31 --fresh
sh ./rollout_automation.sh \
  --archive "$HOME/automation_control_plane_release_v0019_r29.zip" \
  --worker-archive "$HOME/automation_system_v0092.zip" \
  --fresh
```

`v0019-r31` wird numerisch vor `v0019-r7`, `v0019-r6` und `v0017-r13` gewählt.

## Reuse, Test, Status und Diagnose

```bash
sh ./rollout_automation.sh --reuse
sh ./rollout_automation.sh test
sh ./rollout_automation.sh status
sh ./rollout_automation.sh diagnose
```

Reuse erhält genau einen aktiven Worker-Prozess, seine installierte Identität,
die Runtime-Daten und die Sensorsequenz. Der `test`-Aufruf führt keine
Worker-Neuinstallation durch, testet aber beide reversiblen Config-Pfade.

## Erfolgskriterien

```text
Rollout automation result: PASSED (exit=0)
PASS Worker restart recovery: runtime_nonce changed ...
PASS Core-E2E: status=PASSED ...
PASS Worker config-file E2E: local_first + master_first + Master commit + actuator 524 + Worker convergence + restore ...
```

```bash
RUN_DIR="$(readlink -f \
  "$HOME/.local/state/automation-control-plane/rollouts/latest")"
python3 -m json.tool "$RUN_DIR/summary.json"
tail -n 250 "$RUN_DIR/rollout.log"
```

`summary.json` muss `status=PASSED`, `exit_code=0` und `stage=complete`
enthalten. Bei einem Fehler bleiben Logs, Remote-Diagnose und Restore-Nachweis
erhalten; beschädigte Pakete, unsichere Credentials, Config-Konflikte und
unvollständige Restores werden fail-closed abgewiesen.

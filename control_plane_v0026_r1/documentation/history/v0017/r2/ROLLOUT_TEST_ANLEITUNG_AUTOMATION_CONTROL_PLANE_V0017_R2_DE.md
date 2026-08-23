# Automation Control Plane v0017-r2 – standardisierter Rollout und Test

Stand: 2026-07-19

## 1. Archiv und Release prüfen

Neuer Releasebaum:

```bash
cd "$HOME"
sha256sum -c automation_control_plane_release_v0017_r2.zip.sha256
unzip -q automation_control_plane_release_v0017_r2.zip
cd "$HOME/automation_control_plane_release_v0017"
./tools/verify_checksums.sh
```

Bereits installierten r1-Feldstand unter Erhalt der aktiven PKI auf r2
aktualisieren:

```bash
(
  set -Eeuo pipefail
  umask 077

  ARCHIVE="$HOME/automation_control_plane_release_v0017_r2.zip"
  HASH_FILE="$ARCHIVE.sha256"
  RELEASE_DIR="$HOME/automation_control_plane_release_v0017"
  BACKUP_ROOT="$HOME/.local/state/automation-control-plane/release-backups"
  BACKUP_DIR="$BACKUP_ROOT/automation_control_plane_release_v0017.$(date -u +%Y%m%dT%H%M%SZ)"

  cd "$HOME"
  sha256sum -c "$(basename "$HASH_FILE")"
  test -d "$RELEASE_DIR"

  install -d -m 700 "$BACKUP_ROOT"
  cp -a -- "$RELEASE_DIR" "$BACKUP_DIR"
  chmod 700 "$BACKUP_DIR"

  rm -f --     "$RELEASE_DIR/ROLLOUT_TEST_ANLEITUNG_AUTOMATION_CONTROL_PLANE_V0017_R1_DE.md"     "$RELEASE_DIR/documentation/CODE_AUDIT_V0017_R1_DE.md"

  unzip -q -o "$ARCHIVE" -d "$HOME"
  cd "$RELEASE_DIR"
  test "$(cat RELEASE_REVISION)" = "v0017-r2"
  ./tools/verify_checksums.sh
)
```

Der Overlay-Pfad erhält generierte PKI und Runtime-Zustände. Den bestehenden
Releasebaum für diesen Reuse-Wiederanlauf nicht vollständig löschen.

## 2. Private Dateien installieren

```bash
install -d -m 700 "$HOME/.config/automation-control-plane"
install -m 600 rollout_automation.json.example \
  "$HOME/.config/automation-control-plane/rollout_automation.json"
install -m 600 "$HOME/credentials.json" \
  "$HOME/.config/automation-control-plane/credentials.json"
```

## 3. Aktuellen r1-Feldlauf ohne Neuaufbau fortsetzen

```bash
./rollout_automation.sh run \
  --reuse \
  --reset-worker-replica \
  --config "$HOME/.config/automation-control-plane/rollout_automation.json" \
  --credentials "$HOME/.config/automation-control-plane/credentials.json"
```

Dieser Befehl verwendet die laufende Control Plane weiter, erneuert die PKI,
prüft den vorhandenen Worker, archiviert nur sein Replica-Journal, startet ihn
neu und führt alle Tests aus. Es wird kein `automation_system_v0082.zip` übertragen.

## 4. Vollständiger Fresh-Rollout

Nur auf einem dedizierten Docker-Lab-Host:

```bash
./rollout_automation.sh run \
  --fresh \
  --purge-host \
  --config "$HOME/.config/automation-control-plane/rollout_automation.json" \
  --credentials "$HOME/.config/automation-control-plane/credentials.json"
```

## 5. Nur Status, Diagnose oder Tests

```bash
./rollout_automation.sh status --config "$HOME/.config/automation-control-plane/rollout_automation.json"
./rollout_automation.sh diagnose --config "$HOME/.config/automation-control-plane/rollout_automation.json"
./rollout_automation.sh test --config "$HOME/.config/automation-control-plane/rollout_automation.json"
```

Der aktive E2E wird über `integration_test/CURRENT_E2E` ausgewählt.

## 6. Verbindlicher Erfolg

```text
Rollout automation result: PASSED (exit=0)
summary.json: status=PASSED
summary.json: stage=complete
Worker site_main/fn-01 was admitted and its Node shard converged.
PASS Worker telemetry: source_sequence ...
PASS Core-E2E: status=PASSED ...
```

Bei einem Fehler nicht erneut blind `--fresh --purge-host` starten. Zuerst
folgende Dateien auswerten:

```bash
cd "$HOME/.local/state/automation-control-plane/rollouts/latest"
python3 -m json.tool summary.json
less rollout.log
less worker-diagnosis.log
less control-plane-diagnosis.log
```

## 7. Neue r2-Diagnoseausgaben

Zulässige Konfigurationsdrift:

```text
NOTICE: preserved Worker deployment configuration drift: ./_config/...
Worker deployment configuration: PRESERVED (... files differ)
```

Echte Paketabweichung:

```text
BLOCKED: installed Worker immutable package integrity failed
./src/...: FAILED
```

Manifestmanipulation:

```text
BLOCKED: installed Worker manifest is not the canonical v0082 manifest
```

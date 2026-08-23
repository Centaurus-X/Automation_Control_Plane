# Mini-Anleitung - autonomer Fresh-Rollout v0026-r1

## Dateien unter `$HOME`

```text
automation_control_plane_release_v0026_r1.zip
automation_control_plane_release_v0026_r1.zip.sha256
automation_system_v0101.zip
automation_system_v0101.zip.sha256
```

Konfiguration und Credentials werden unter
`$HOME/.config/automation-control-plane/` angelegt. In
`credentials.json` müssen Worker-SSH, Control-Plane-Administrator und das
mindestens 32 Zeichen lange MCP-Bearer-Token gesetzt sein; der ausgelieferte `CHANGE_ME_*`-Platzhalter wird abgewiesen.

## Prüfen und starten

```bash
cd "$HOME"
sha256sum -c automation_control_plane_release_v0026_r1.zip.sha256
sha256sum -c automation_system_v0101.zip.sha256
unzip -q automation_control_plane_release_v0026_r1.zip
cd automation_control_plane_release_v0026_r1
./tools/verify_checksums.sh

sh ./rollout_automation.sh \
  --archive "$HOME/automation_control_plane_release_v0026_r1.zip" \
  --worker-archive "$HOME/automation_system_v0101.zip" \
  --fresh
```

Nur auf einem dedizierten Docker-Laborhost darf `--purge-host` ergänzt werden.
Der Rollout installiert die fünf Control-Plane-Komponenten einschließlich MCP
sowie den separaten Worker v0101 und prüft Readiness, Worker-Authority,
Telemetrie, Restart-Recovery und Core-/File-E2E.

Erfolg liegt ausschließlich vor bei:

```text
Rollout automation result: PASSED (exit=0)
```

```bash
python3 -m json.tool \
  "$HOME/.local/state/automation-control-plane/rollouts/latest/summary.json"
```

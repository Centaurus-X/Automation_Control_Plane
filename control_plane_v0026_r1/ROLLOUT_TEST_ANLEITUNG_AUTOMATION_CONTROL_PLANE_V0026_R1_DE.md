# Rollout- und Testanleitung - Automation Control Plane v0026-r1

## 1. Benötigte Artefakte

Unter `$HOME` müssen liegen:

```text
automation_control_plane_release_v0026_r1.zip
automation_control_plane_release_v0026_r1.zip.sha256
automation_system_v0101.zip
automation_system_v0101.zip.sha256
```

Der Control-Plane-Installer muss das separate `automation_system_v0101.zip` übertragen.
Es wird nicht in das v0026-Archiv eingebettet.

## 2. Sidecars und ZIP prüfen

```bash
cd "$HOME"
sha256sum -c automation_control_plane_release_v0026_r1.zip.sha256
sha256sum -c automation_system_v0101.zip.sha256
unzip -q automation_control_plane_release_v0026_r1.zip
cd automation_control_plane_release_v0026_r1
./tools/verify_checksums.sh
```

Bei einer Hashabweichung sofort stoppen. Ein selbst erzeugter Sidecar ist kein
unabhängiger Herkunftsnachweis.

## 3. Konfiguration vorbereiten

```bash
install -d -m 700 "$HOME/.config/automation-control-plane"
install -m 600 rollout_automation.json.example \
  "$HOME/.config/automation-control-plane/rollout_automation.json"
install -m 600 credentials.json.example \
  "$HOME/.config/automation-control-plane/credentials.json"
```

Mindestens anpassen:

```text
CONTROL_PLANE_BIND_ADDRESS
MQTT_BIND_ADDRESS
PROXY_BIND_ADDRESS
PROXY_ORIGIN_ALLOWLIST
MCP_BIND_ADDRESS
MCP_HTTP_PORT
MCP_MQTT_SOURCE_INSTANCE_ID
MCP_AUTH_FILE
MCP_AUTH_CREDENTIAL_KEY
WORKER_SSH
WORKER_PATH
SITE_ID
NODE_ID
ACTUATOR_HOST
ACTUATOR_PORT
```

In `credentials.json` müssen gültige Werte für Worker-SSH, Control-Plane-Admin
und `mcp_controller.local_agent.token` stehen. Das MCP-Token muss mindestens
32 Zeichen lang sein. `MCP_AUTH_FILE` muss außerhalb des Releasebaums liegen.

## 4. Releaseauswahl prüfen

```bash
sh ./rollout_automation.sh --list-releases
```

Erwartet wird `0.26.0 / v0026-r1`. Der aktive E2E-Vertrag wird über
`integration_test/CURRENT_E2E` ausgewählt und bleibt `e2e_v0025`.

## 5. Fresh-Rollout

Auf einem normalen oder gemeinsam genutzten Docker-Host:

```bash
sh ./rollout_automation.sh \
  --release v0026-r1 \
  --archive "$HOME/automation_control_plane_release_v0026_r1.zip" \
  --worker-archive "$HOME/automation_system_v0101.zip" \
  --fresh
```

Nur auf einem ausschließlich vorgesehenen Laborhost:

```bash
sh ./rollout_automation.sh \
  --release v0026-r1 \
  --archive "$HOME/automation_control_plane_release_v0026_r1.zip" \
  --worker-archive "$HOME/automation_system_v0101.zip" \
  --fresh \
  --purge-host
```

`--purge-host` entfernt alle Docker-Projekte, Volumes, Images und ungenutzten
Netze des Hosts.

## 6. Reuse und Upgrade

Verifizierten laufenden Stand ohne Worker-Neustart prüfen:

```bash
sh ./rollout_automation.sh \
  --archive "$HOME/automation_control_plane_release_v0026_r1.zip" \
  --worker-archive "$HOME/automation_system_v0101.zip" \
  --reuse
```

Persistenten Zustand kontrolliert übernehmen:

```bash
sh ./rollout_automation.sh \
  --archive "$HOME/automation_control_plane_release_v0026_r1.zip" \
  --worker-archive "$HOME/automation_system_v0101.zip" \
  --upgrade
```

## 7. Erwartete Deployment-Reihenfolge

```text
PostgreSQL -> MQTT -> Master -> Proxy -> MCP Controller Server -> Worker
```

Der MCP Controller muss mit eigener mTLS-Identität laufen und darf nur
Master-Control-Kommandos, seinen ACK-Pfad und Master-Events verwenden.

## 8. Abschlussstatus

Erfolg liegt ausschließlich vor bei:

```text
Rollout automation result: PASSED (exit=0)
```

```bash
python3 -m json.tool \
  "$HOME/.local/state/automation-control-plane/rollouts/latest/summary.json"
```

Erwartet:

```json
{
  "status": "PASSED",
  "stage": "complete"
}
```

## 9. Betriebs- und MCP-Abnahme

```bash
./status_all.sh
./logs_all.sh
./test_running_all.sh
./mcp_controller_server/ready.sh
./mcp_controller_server/test.sh
```

Zu prüfen sind:

- PostgreSQL Primary/Standby, TLS, Rollen und Replikation;
- MQTT mTLS, ACL, Delivery und Persistenz;
- Master/Proxy `0.26.0 / v0026-r1`;
- MCP `0.1.0 / v0001-r1`, HTTP-Readiness und MQTT-Verbindung;
- eigener Zertifikats-CN
  `urn:automation:site-main:proxy:mcp-controller-01`;
- keine Worker- oder Broadcast-Schreibrechte für MCP.

## 10. Worker- und E2E-Abnahme

```bash
./test_worker_auto_admission.sh fn-01 180
./test_worker_sensor_telemetry.sh fn-01 60
./test_e2e.sh
./test_extended_all.sh
```

Erwartet werden `APPROVED`, `ACTIVE`, frischer Heartbeat, `node_local`,
gleiche Authority-/Revision-/Execution-/Fingerprint-Tupel, fortschreitende
Sequenzen sowie PASS für Core- und File-E2E. Teständerungen müssen vollständig
zurückgesetzt sein.

## 11. Administration-Orchestrator

```bash
STATE_ROOT="$HOME/.local/state/automation-control-plane/administration/test-lab"
mkdir -p "$STATE_ROOT/plans"

./administration/orchestrate.sh plan \
  --inventory administration/inventory.test-lab.json \
  --registry administration/component_registry.test-lab.json \
  --manifest administration/release_manifest.json \
  --output "$STATE_ROOT/plans/full.json"

./administration/orchestrate.sh apply \
  --plan "$STATE_ROOT/plans/full.json"
```

Nur MCP selektiv verwalten:

```bash
./administration/orchestrate.sh plan \
  --inventory administration/inventory.test-lab.json \
  --registry administration/component_registry.test-lab.json \
  --manifest administration/release_manifest.json \
  --component mcp_controller_server \
  --target control-plane-01 \
  --output "$STATE_ROOT/plans/mcp.json"

./administration/orchestrate.sh status \
  --plan "$STATE_ROOT/plans/mcp.json" \
  --live
./administration/orchestrate.sh test \
  --plan "$STATE_ROOT/plans/mcp.json"
```

Ein unterbrochenes Apply wird mit `--resume` fortgesetzt. Rollback erfolgt in
umgekehrter Komponentenreihenfolge; Datenbanken werden weiterhin nicht blind
automatisch restored.

## 12. Diagnose

```bash
sh ./rollout_automation.sh diagnose
./diagnose_worker_sync.sh
./mcp_controller_server/logs.sh
```

MCP-Auth-Token und private Keys dürfen in keiner Diagnoseausgabe erscheinen.

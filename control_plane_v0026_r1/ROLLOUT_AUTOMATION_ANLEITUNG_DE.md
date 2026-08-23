# Versionsunabhängige Rollout-Automatisierung v0026-r1

## Ziel

`rollout_automation.sh` ist der autonome, versionsunabhängige Vollinstaller für
Control Plane, MCP Controller und den separat gelieferten Worker. Er übernimmt:

1. Release-Erkennung und sichere Installation;
2. Fresh-, Reuse- und Upgrade-Modus;
3. Docker-/Port-/Netzwerk-Preflight;
4. MQTT-PKI, MCP-Auth und Worker-PKI;
5. Control-Plane- und MCP-Lifecycle;
6. Worker-SSH, Installation und Recovery;
7. Administration-Bootstrap, Admission, Telemetrie sowie Core-/File-E2E;
8. credential-freie Evidence und `summary.json`.

## Auswahlalgorithmus

Untersucht werden Skriptverzeichnis, Elternverzeichnis, aktuelles Verzeichnis
und `$HOME`. Gültig sind ZIP-Archive oder extrahierte Verzeichnisse mit Präfix
`automation_control_plane_release_v`. Die Identität stammt ausschließlich aus
`VERSION` und `RELEASE_REVISION`; die Revision muss `vNNNN-rR` entsprechen.

```text
v0026-r1 > v0025-r1 > v0019-r30 > ... > v0017-r13
```

```bash
sh ./rollout_automation.sh --list-releases
sh ./rollout_automation.sh --release v0026-r1 --fresh
sh ./rollout_automation.sh --release 0.26.0 --fresh
sh ./rollout_automation.sh \
  --archive "$HOME/automation_control_plane_release_v0026_r1.zip" \
  --fresh
```

## Integrität und Installation

Jedes ZIP benötigt seinen gleichnamigen `.sha256`-Sidecar. Vor dem Entpacken
werden absolute Pfade, Traversal, Backslashes, Duplikate, Symlinks und fremde
Dateitypen abgewiesen. Danach werden Root-, Komponenten- und alle versionierten
E2E-Manifeste geprüft.

Kanonisches Installationsziel:

```text
$HOME/automation_control_plane_release_v0026_r1
```

Ein byte-identischer Baum wird wiederverwendet. Ein beschädigter Baum wird über
einen atomaren Swap ersetzt. Frühere Releases bleiben separat erhalten.

## Betriebsmodi

| Modus | Wirkung |
|---|---|
| `--fresh` | neue Control-Plane-/MCP-/Worker-Baseline |
| `--reuse` | verifizierten laufenden Stand nutzen; Worker nicht neu starten |
| `--upgrade` | persistenten Zustand kontrolliert übernehmen |
| `--purge-host` | gesamten Docker-Host löschen; nur dedizierter Laborhost |

Ohne expliziten Modus wird ein neu gewähltes Release als Fresh installiert und
ein unveränderter aktueller Stand als Reuse geprüft.

## Externe Konfiguration und Credentials

```text
$HOME/.config/automation-control-plane/rollout_automation.json
$HOME/.config/automation-control-plane/credentials.json
$HOME/.config/automation-control-plane/mcp_auth.json
```

Die Dateien müssen regulär, nicht verlinkt und Eigentum des aufrufenden
Benutzers sein. Konfiguration und Credentials erhalten Modus `0600`.

Credential-Lookups:

```text
SSH:       philo oder ssh.philo
Admin:     admin oder control_plane.admin
MCP Token: mcp_controller.local_agent.token
```

Der Installer liest das MCP-Token ausschließlich über
`tools/credential_reader.py` und erzeugt mit `tools/prepare_mcp_auth.py` atomar
das externe Auth-Dokument. Das Token wird weder ausgegeben noch in Logs,
Evidence oder Release-Dateien geschrieben. Ein alternativer Pfad kann mit
`--mcp-auth-file` gesetzt werden, darf aber nicht innerhalb des Releasebaums
liegen.

Wichtige MCP-Werte:

```text
MCP_BIND_ADDRESS=127.0.0.1
MCP_HTTP_PORT=8080
MCP_MQTT_SOURCE_INSTANCE_ID=mcp-controller-01
MCP_AUTH_CREDENTIAL_KEY=mcp_controller.local_agent.token
```

Externer MCP-HTTP-Zugriff erfordert einen authentifizierten TLS-Reverse-Proxy.

## Referenzlabor

```text
Control Plane: 192.168.0.82
Worker:        philo@192.168.0.80
Worker-Pfad:   python-skripte_22/__NEW_FILES__/automation_system_v0101
Site/Node:     site-main / fn-01
Modbus-Lab:    192.168.0.3:5020
MCP HTTP:      127.0.0.1:8080
```

## MQTT-PKI und MCP-Identität

Fresh erzeugt die Labor-PKI und prüft Kette, Laufzeit, SAN-URI und
Zertifikat/Key-Paar. Zusätzlich zu Master, Proxy, Worker und Health-Probe wird
die dedizierte Identität erzeugt:

```text
urn:automation:site-main:proxy:mcp-controller-01
```

Die ACL erlaubt nur Master-Control-Kommandos, den eigenen ACK-Pfad und
Master-Events. Worker- und Broadcast-Kommandotopics bleiben verboten.

## Worker-Integrität

Fresh und Upgrade übertragen das separat gepinnte Worker-v0101-ZIP. Reuse
überträgt nichts und prüft Version, internes Manifest, installierte
MQTT-Identität und genau einen laufenden Prozess. Worker-Code bleibt außerhalb
des Control-Plane-Archivs.

## SSH

```text
ControlPersist=yes
ServerAliveInterval=15
ServerAliveCountMax=3
```

Vor jeder Remote-Operation wird der ControlMaster geprüft und bei Verlust
nicht-interaktiv aus Key/Agent oder `credentials.json` neu aufgebaut.

## Vollständiger Ablauf

1. Release entdecken, Sidecar und ZIP prüfen, atomar installieren;
2. Konfiguration/Credentials und externes MCP-Auth-Dokument vorbereiten;
3. Release-, Docker-, Port-, Netzwerk- und Feldbus-Preflight;
4. Control-Plane-Baseline: PostgreSQL, MQTT, Master, Proxy;
5. MCP Controller als eigene Komponente deployen und Readiness prüfen;
6. Worker-ZIP, PKI, SSH und Paketbaseline prüfen;
7. Worker installieren oder Reuse-Kontinuität verifizieren;
8. Administrator idempotent anlegen;
9. Worker Admission, Authority, Mirror und Telemetrie validieren;
10. Restart-/Sequenz-Recovery;
11. Core-E2E und JSON-File-E2E mit Restore;
12. Gesamtdiagnose und `summary.json` schreiben.

Die operative Orchestrierungsreihenfolge lautet:

```text
database -> mqtt -> master -> proxy -> mcp_controller_server -> worker
```

## Befehle

```bash
sh ./rollout_automation.sh --fresh
sh ./rollout_automation.sh --fresh --purge-host
sh ./rollout_automation.sh --reuse
sh ./rollout_automation.sh --upgrade
sh ./rollout_automation.sh test
sh ./rollout_automation.sh status
sh ./rollout_automation.sh diagnose
```

Beispiel mit expliziten Archiven:

```bash
sh ./rollout_automation.sh \
  --release v0026-r1 \
  --archive "$HOME/automation_control_plane_release_v0026_r1.zip" \
  --worker-archive "$HOME/automation_system_v0101.zip" \
  --fresh
```

## Logs und Erfolgskriterium

```text
$HOME/.local/state/automation-control-plane/rollouts/<Run-ID>/
$HOME/.local/state/automation-control-plane/rollouts/latest
```

Erfolg liegt nur bei Exit-Code `0`, `status=PASSED` und `stage=complete` vor.
Secrets werden aus Logs, Audit, Checkpoints und Evidence redigiert.

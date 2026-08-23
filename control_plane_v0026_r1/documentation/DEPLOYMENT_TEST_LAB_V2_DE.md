# Deployment- und Testkonzept `lab_v2` - v0026-r1

## Systemgrenze

Der Rollout verwaltet auf dem Control-Plane-Host:

```text
PostgreSQL Primary/Standby
Mosquitto mit mTLS
Master 0.26.0/v0026-r1
Proxy 0.26.0/v0026-r1
MCP Controller Server 0.1.0/v0001-r1
```

Fresh und Upgrade installieren zusätzlich das separat gepinnte Worker-Paket
v0101 auf dem Worker-Host. Der Worker bleibt außerhalb von Docker Compose und
finale Node-Authority im Modus `node_local`.

Der MCP Controller ist ein eigener Compose-Service. Er besitzt keinen
PostgreSQL-Zugang und keinen direkten Worker-Pfad; seine einzige Plattformkante
ist MQTTS zum Master.

## Voraussetzungen

- Linux und Docker Engine mit Compose v2;
- Docker-Berechtigung;
- OpenSSL, Python 3, `unzip`, `sha256sum`, `ssh`, `tar` und `install`;
- ausreichend CPU, RAM, Storage und Build-Zeit;
- ausgehender Zugriff oder interne Mirrors für Container-, Debian- und
  Python-Artefakte;
- Worker per SSH erreichbar;
- externe Konfiguration und Credentials mit Modus `0600`.

Benötigte Ports:

| Verbindung | Port |
|---|---:|
| MQTT Testlistener | 1883 |
| MQTT mTLS | 8883 |
| Proxy WSS | 8443 |
| Proxy HTTPS | 8444 |
| Master Metrics/Health | 9090 |
| MCP HTTP, standardmäßig nur Loopback | 8080 |
| PostgreSQL Primary/Standby Labor | 55432 / 55433 |
| Worker SSH | 22 |
| Referenz-Modbus | 5020 |

Eine Host-venv oder ein globales `pip install` ist nicht erforderlich. Die
qualifizierten Python-Gates laufen im Validation-Container.

## Fresh-Varianten

Scoped Fresh auf normalem oder gemeinsam genutztem Host:

```bash
./rollout.sh --fresh --profile lab_v2
```

Vollständiger Purge nur auf einem exklusiven Laborhost:

```bash
export CONTROL_PLANE_CONFIRM_HOST_PURGE=PURGE
./rollout.sh --fresh --purge-host --profile lab_v2
```

`--purge-host` entfernt alle Docker-Container, Volumes, Images, ungenutzten
Netze und Builder-Caches. Es wird ohne exakte Bestätigung abgewiesen.

Der scoped Fresh-Pfad entfernt nur bekannte Control-Plane-Ressourcen:

- Compose-Projekte `automation-mqtt`, `automation-postgres`,
  `automation-master`, `automation-proxy`, `automation-mcp-controller`;
- bekannte Control-Plane-Volumes einschließlich
  `automation-mcp-controller_mcp_controller_state`;
- Netze `automation_control` und `automation_database`;
- bekannte Images der Control Plane und Validation-Runtime;
- generierte PKI, Runtime-Secrets, TLS-, E2E- und Python-Cachezustände.

Ein Residual-Gate prüft, dass kein alter Control-Plane-Zustand übrig bleibt.

## Konfiguration

```bash
cd "$HOME/automation_control_plane_release_v0026_r1"

export CONTROL_PLANE_BIND_ADDRESS=192.168.0.82
export MQTT_BIND_ADDRESS=192.168.0.82
export MQTT_TEST_HOST=192.168.0.82
export MQTT_TEST_BROKER_IP=192.168.0.82
export PROXY_BIND_ADDRESS=192.168.0.82
export PROXY_WSS_PORT=8443
export PROXY_HTTPS_PORT=8444
export PROXY_ORIGIN_ALLOWLIST=https://192.168.0.82:8444

export MCP_BIND_ADDRESS=127.0.0.1
export MCP_HTTP_PORT=8080
export MCP_MQTT_SOURCE_INSTANCE_ID=mcp-controller-01
export MCP_AUTH_SOURCE="$HOME/.config/automation-control-plane/mcp_auth.json"
```

Der autonome Installer erzeugt `MCP_AUTH_SOURCE` normalerweise selbst aus
`credentials.json`. Ein direktes Lab-Deployment muss die Datei vorher mit
`tools/prepare_mcp_auth.py` erzeugen.

## Rollout-Reihenfolge

| Stufe | Gate |
|---:|---|
| 1 | Fresh-Purge oder Upgrade-State-Preflight |
| 2 | Bind-/Port-/Docker-/Netzwerk-Preflight |
| 3 | gepinnte Static-/Unit-/Contract-Gates |
| 4 | Clean-Room-Build aller Release-Images |
| 5 | Mosquitto-mTLS-Deployment |
| 6 | Broker-ACL-, Identity-, Delivery- und Persistence-Tests |
| 7 | PostgreSQL Primary/Standby/TLS |
| 8 | Master-Migration, Bootstrap und Start |
| 9 | Proxy-Migration, Direct-WSS und HTTPS-Start |
| 10 | MCP-Auth/PKI-Staging, Deployment und Readiness |
| 11 | laufende Health-, Image-, Replikations-, Rollen- und Transaktionsprobes |

Der erste Build kompiliert die gepinnte CPython-3.14.6t-Runtime und kann
mehrere Minuten dauern. Das MCP-Image wird im gemeinsamen Image-Staging gebaut
und beim Deployment nicht erneut gebaut.

## MCP-Test-PKI

Das Lab-PKI-Gate erzeugt und prüft:

```text
urn:automation:site-main:monitor:mqtt-health
urn:automation:site-main:master:master-01
urn:automation:site-main:proxy:proxy-01
urn:automation:site-main:proxy:mcp-controller-01
urn:automation:site-main:worker:fn-01
urn:automation:site-main:worker:fn-02
```

Die MCP-ACL erlaubt nur Master-Control-Kommandos, den eigenen ACK-Pfad und
Master-Events. Ein Worker-Command-Publish mit MCP-Zertifikat muss scheitern.

## Referenzaufruf

Bevorzugt über den autonomen Installer:

```bash
cd "$HOME/automation_control_plane_release_v0026_r1"

sh ./rollout_automation.sh \
  --release v0026-r1 \
  --archive "$HOME/automation_control_plane_release_v0026_r1.zip" \
  --worker-archive "$HOME/automation_system_v0101.zip" \
  --fresh
```

Direktes Control-Plane-Lab-Deployment ohne Worker-Installation:

```bash
./rollout.sh --fresh --profile lab_v2
```

Erwarteter Abschluss des direkten Lab-Rollouts:

```text
Control Plane 0.26.0/v0026-r1 with MCP 0.1.0/v0001-r1 is deployed and tested in lab_v2.
```

## Laufende Abnahme

```bash
./rollout.sh status
./rollout.sh test-running
./master_node/test_runtime_state.sh
./mcp_controller_server/ready.sh
./mcp_controller_server/test.sh

docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'
docker image inspect automation-proxy-gateway:0.26.0 \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
docker image inspect automation-mcp-controller-server:0.1.0 \
  --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
```

Erwartet:

- Master/Proxy `0.26.0 / v0026-r1`;
- MCP `0.1.0 / v0001-r1`;
- PostgreSQL/MQTT `0.14.0`;
- alle Container `healthy`;
- MCP-Readiness mit aktiver MQTT-Verbindung;
- keine stale Images oder falschen OCI-Revisionen.

## Externer Worker

Nach Fresh wird die neue Labor-PKI geschützt auf Worker v0101 installiert.
Der Installer prüft Paketbaseline, Version, Zertifikatsidentität, genau einen
laufenden Prozess und entfernt das temporäre PKI-Staging.

```bash
./test_worker_auto_admission.sh fn-01 180
./test_worker_sensor_telemetry.sh fn-01 60
./diagnose_worker_sync.sh fn-01
```

Erwartet werden `APPROVED`, `ACTIVE`, frischer Heartbeat, `node_local`,
identische Authority-/Revision-/Execution-/Fingerprint-Tupel und
fortschreitende Sequenzen.

## E2E

Der aktive Vertrag wird durch `integration_test/CURRENT_E2E` ausgewählt und
bleibt `e2e_v0025 / 0.25.0-e2e.3`. Der Runner prüft HTTPS/WSS, TLS, Origin,
Auth, RBAC, Audit, Replay, Backpressure, Worker-Konvergenz, Modbus-Aktor,
Restart-Recovery und beide Config-Commit-Pfade einschließlich Restore.

```bash
./run_core_e2e_lab.sh
./test_e2e.sh
./test_extended_all.sh
```

Die Befehle und Abnahmekriterien stehen in
`ROLLOUT_TEST_ANLEITUNG_AUTOMATION_CONTROL_PLANE_V0026_R1_DE.md`.

## Upgrade

```bash
export CONTROL_PLANE_STATE_SOURCE=/absoluter/pfad/zum/vorgaenger
./rollout.sh --upgrade --profile lab_v2
```

Upgrade importiert persistenten Zustand, purgt keine Volumes und ist mit
`--purge-host` unvereinbar. Vor dem MCP-Apply wird sein lokaler Approval-/Audit-
Zustand gesichert.

## Fehlerdiagnose

```bash
./rollout.sh status
LOG_TAIL=500 ./logs_all.sh
./master_node/test_runtime_state.sh
./mcp_controller_server/logs.sh
./diagnose_worker_sync.sh fn-01
```

Typische Einordnung:

- Port-Preflight: fremder Prozess oder Container belegt Zielport;
- MCP Preflight: Auth-/PKI-Datei fehlt, ist verlinkt, zu offen oder besitzt
  eine falsche Zertifikatsidentität;
- MCP Readiness 503: MQTT-Verbindung fehlt oder Auth-/Config-Laden schlug fehl;
- Worker `PENDING`/offline: Fresh-PKI nicht installiert oder Worker nicht neu
  gestartet;
- E2E `BLOCKED`: Mutationsbestätigung, Aktor, Logs, Worker-Konvergenz oder
  externe Full-Lab-Voraussetzung fehlt;
- alter OCI-Tag: Image-Staging oder entsprechende Deployment-Stufe ist
  fehlgeschlagen.

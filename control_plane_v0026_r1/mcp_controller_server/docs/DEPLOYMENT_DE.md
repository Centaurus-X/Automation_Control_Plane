# Deployment und Control-Plane-Integration v0026-r1

## Voraussetzungen

- Linux-Host mit Docker und Compose v2;
- externes Docker-Netz `automation_control`;
- erreichbarer MQTT-Broker `mqtt-broker:8883`;
- laufender Master `master-01`;
- MQTT-CA, dediziertes MCP-Clientzertifikat und privater Schlüssel;
- externes Bearer-Auth-Dokument;
- Loopback-Port `8080` oder explizit geplanter TLS-Reverse-Proxy.

Das Image basiert auf `python:3.14.6-slim-bookworm`; Runtime-Abhängigkeiten sind
exakt gepinnt.

## Bevorzugter Gesamtrollout

Im Bundle wird die Komponente durch `rollout_automation.sh` verwaltet. Der
Installer:

1. liest `mcp_controller.local_agent.token` aus geschützten Credentials;
2. erzeugt atomar ein externes `0600`-Auth-Dokument;
3. stellt Test-PKI oder vollständige Produktions-PKI bereit;
4. prüft Zertifikatskette, SAN/CN, Laufzeit und Key-Paar;
5. baut das MCP-Image im gemeinsamen Image-Staging;
6. deployt nach Proxy und vor Worker;
7. verlangt Container-Health und Anwendung-Readiness.

```bash
sh ./rollout_automation.sh \
  --release v0026-r1 \
  --archive "$HOME/automation_control_plane_release_v0026_r1.zip" \
  --worker-archive "$HOME/automation_system_v0101.zip" \
  --fresh
```

## Secret-Bereitstellung

Kein Secret befindet sich im ZIP. Für direkten Komponentenbetrieb müssen
vollständig vorhanden sein:

```text
MCP_MQTT_CA_SOURCE=/secure/mcp-controller/mqtt_ca.crt
MCP_MQTT_CERT_SOURCE=/secure/mcp-controller/mcp_mqtt.crt
MCP_MQTT_KEY_SOURCE=/secure/mcp-controller/mcp_mqtt.key
MCP_AUTH_SOURCE=/secure/mcp-controller/mcp_auth.json
```

Dateien müssen regulär, nicht verlinkt, nicht leer und restriktiv geschützt
sein. Der private Schlüssel und das Bearer-Token dürfen niemals in Source
Control, Image, Log, Evidence oder Release-ZIP gelangen.

Der Preflight kopiert die Quellen mit einem netzlosen read-only Docker-Helfer
nach `runtime-secrets/`, setzt UID/GID `10001` und Modus `0400` und prüft das
Ergebnis. Das Verzeichnis wird vom Root- und Komponenten-Builder ausgeschlossen.

## Dedizierter MQTT-Modus

Das v0026-Bundle verwendet ausschließlich:

```text
MCP_MQTT_SOURCE_INSTANCE_ID=mcp-controller-01
urn:automation:site-main:proxy:mcp-controller-01
```

Die zugehörige ACL und Master-Allowlist sind bereits integriert. Der alte
Standalone-Kompatibilitätsweg über `proxy-01` ist keine Bundle-Konfiguration
und darf in Produktion nicht verwendet werden.

## Compose-Hardening

- UID/GID 10001 ohne Login-Shell;
- read-only Root-Dateisystem;
- beschreibbares State-Volume und begrenztes `noexec`-tmpfs;
- keine Linux-Capabilities;
- `no-new-privileges`;
- Loopback-Portbindung als Default;
- Readiness nur bei laufender MQTT-Verbindung;
- Secrets ausschließlich über `/run/secrets`.

## Direkter Komponentenstart

```bash
export MCP_MQTT_CA_SOURCE=/secure/mcp-controller/mqtt_ca.crt
export MCP_MQTT_CERT_SOURCE=/secure/mcp-controller/mcp_mqtt.crt
export MCP_MQTT_KEY_SOURCE=/secure/mcp-controller/mcp_mqtt.key
export MCP_AUTH_SOURCE=/secure/mcp-controller/mcp_auth.json
export MCP_MQTT_SOURCE_INSTANCE_ID=mcp-controller-01

./preflight.sh
./deploy.sh
./ready.sh
./test.sh
```

## Administration-Orchestrator

Die Komponente ist vollständig im zentralen Orchestrator registriert:

```text
database -> mqtt -> master -> proxy -> mcp_controller_server -> worker
```

Selektiver Plan:

```bash
STATE_ROOT="$HOME/.local/state/automation-control-plane/administration/test-lab"
mkdir -p "$STATE_ROOT/plans"

../administration/orchestrate.sh plan \
  --inventory ../administration/inventory.test-lab.json \
  --registry ../administration/component_registry.test-lab.json \
  --manifest ../administration/release_manifest.json \
  --component mcp_controller_server \
  --target control-plane-01 \
  --output "$STATE_ROOT/plans/mcp.json"
```

Der zentrale Adapter unterstützt `preflight`, `backup`, `apply`, `ready`,
`status`, `test`, `restart` und `rollback` und übernimmt Locks, Fencing,
Idempotenz, Checkpoints, Audit, Evidence und Redaction.

## Remote-Zugriff

HTTP im Container ist absichtlich unverschlüsselt und standardmäßig nur auf
Loopback veröffentlicht. Ein TLS-Reverse-Proxy muss:

- TLS 1.2+ mit vertrauenswürdigem Zertifikat erzwingen;
- Request-Body und MCP-Header unverändert weiterreichen;
- erlaubte Netze/Clients begrenzen;
- Body-, Request- und Idle-Timeouts setzen;
- Authorization-Header und Payloads nicht protokollieren.

`MCP_BIND_ADDRESS=0.0.0.0` ohne TLS-Termination ist kein unterstützter
Produktionsmodus.

## Upgrade und Rollback

Vor `apply` wird `backup.sh` ausgeführt. Upgrade konvergiert idempotent mit
Compose. Rollback benötigt explizit ein vorhandenes altes Image:

```bash
MCP_CONTROLLER_ROLLBACK_IMAGE=registry.example/mcp-controller:0.0.9 \
  ./rollback.sh
```

Rollback löscht das State-Volume nicht. State-Restore bleibt ein separat
freizugebender Offline-Recovery-Vorgang und wird nicht automatisch an ein
Image-Rollback gekoppelt.

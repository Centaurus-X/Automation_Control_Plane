# Rollout- und Testanleitung für Control Plane v0011

## Zielnetz

| Funktion | Adresse | Ports |
|---|---:|---:|
| Modbus-Testserver | `192.168.0.3` | `5020` laut Testprofil |
| Worker v0079 | `192.168.0.80` | ausgehend MQTTS |
| Control Plane | `192.168.0.82` | `8883`, `8443`, lokal `9090` |
| alte MQTT-Instanz | `192.168.0.31` | unbenutzt |

## Voraussetzungen

Erforderlich sind Docker Engine, Docker Compose v2, OpenSSL, Python 3, curl und
unzip. Der Benutzer muss Docker ohne sudo ausführen können.

```bash
sudo apt-get update
sudo apt-get install -y ca-certificates curl unzip openssl python3
docker version
docker compose version
docker info
openssl version
python3 --version
```

## Release entpacken und prüfen

```bash
cd "$HOME"
unzip automation_control_plane_release_v0011.zip
cd automation_control_plane_release_v0011
chmod +x ./*.sh */*.sh */scripts/*.sh tools/*.sh
./tools/verify_checksums.sh
./rollout.sh test-static
```

## Frischer Lab-Rollout – empfohlen in der Testphase

Warnung: Dieser Modus löscht die Daten der vier Control-Plane-Compose-Projekte.
Alle Images werden zuerst gebaut. Erst wenn das erfolgreich war, werden alte
Container, die über die Projektlabels `automation-mqtt`,
`automation-postgres`, `automation-master` und `automation-proxy` identifiziert
wurden, beendet und entfernt. Anschließend entfernt das Skript nur die
zugehörigen benannten Volumes, die beiden Control-Plane-Netze und den generierten
State im aktuellen Release-Verzeichnis.

```bash
cd "$HOME/automation_control_plane_release_v0011"

unset CONTROL_PLANE_STATE_SOURCE
export CONTROL_PLANE_BIND_ADDRESS=192.168.0.82
export MQTT_BIND_ADDRESS=192.168.0.82
export MQTT_TEST_HOST=192.168.0.82
export MQTT_TEST_BROKER_IP=192.168.0.82
export PROXY_BIND_ADDRESS=192.168.0.82
export PROXY_ORIGIN_ALLOWLIST=https://192.168.0.82:8443

./rollout.sh test --fresh
```

`./rollout.sh test` verwendet ebenfalls Fresh-Modus. Die acht Stufen sind:

1. alle Release-Images pullen/bauen;
2. alte Control Plane stoppen und Testzustand purgen;
3. Mosquitto deployen;
4. MQTT-mTLS-, ACL- und Persistenztests;
5. PostgreSQL Primary/Standby deployen;
6. Master migrieren und deployen;
7. Proxy migrieren und deployen;
8. Image-, Health-, Replikations-, Migrations- und DB-Abnahme.

## Upgrade-Modus – Zustand bewusst bewahren

Für einen späteren Upgrade-Test zeigt `CONTROL_PLANE_STATE_SOURCE` immer auf
das tatsächlich vorhandene Vorgänger-Release. Es gibt keine fest codierte
Versionsannahme.

```bash
cd "$HOME/automation_control_plane_release_v0011"

export CONTROL_PLANE_STATE_SOURCE="$HOME/automation_control_plane_release_v0010"
export CONTROL_PLANE_BIND_ADDRESS=192.168.0.82
export MQTT_BIND_ADDRESS=192.168.0.82
export MQTT_TEST_HOST=192.168.0.82
export MQTT_TEST_BROKER_IP=192.168.0.82
export PROXY_BIND_ADDRESS=192.168.0.82
export PROXY_ORIGIN_ALLOWLIST=https://192.168.0.82:8443

./rollout.sh test --upgrade
```

`--upgrade` kopiert ausschließlich allowlist-basierten Runtime-State, bewahrt
Volumes und lehnt fehlende oder widersprüchliche Credentials vor dem Stoppen
laufender Container ab.

## Erfolg verifizieren

Die folgenden Kommandos erst nach Exit-Status 0 des Rollouts ausführen:

```bash
./rollout.sh status
./rollout.sh test-running

docker ps --format \
  'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

curl --fail --silent --show-error \
  http://127.0.0.1:9090/health/ready

curl --fail --silent --show-error --insecure \
  https://192.168.0.82:8443/api/v1/health/ready
```

Erwartete Images:

```text
automation-proxy-gateway:0.11.0
automation-master-node:0.11.0
automation-postgres:18.4-v11
eclipse-mosquitto:2.1.2-alpine
```

`test-running` vergleicht diese Referenzen direkt mit `Config.Image` und prüft
bei den eigenen Images zusätzlich das OCI-Release-Label `0.11.0`. Alte
`0.5.0`-/`v5`-Container lösen Status 70 und eine konkrete Stale-Image-Meldung
aus.

## Logs und Fehlerdiagnose

```bash
LOG_TAIL=300 ./logs_all.sh

docker ps -a --format \
  'table {{.Names}}\t{{.Image}}\t{{.Status}}\t{{.Ports}}'

docker inspect automation-proxy-proxy-gateway-1 \
  --format '{{.Config.Image}} {{.State.Health.Status}}'
```

Beim vorherigen Fehler war nicht Uvicorns ASGI3-Ausführung defekt: Der laufende
Container verwendete nach einem Preflight-Abbruch weiterhin
`automation-proxy-gateway:0.5.0`. Deshalb konnte die in v0006 vorhandene
`interface="asgi3"`-Korrektur nicht aktiv sein. v0007 machte Preflight-Szenarien
sichtbar und prüft die laufende Version verbindlich. v0008 setzt zusätzlich
den nicht veralteten Uvicorn-WebSocket-Adapter `websockets-sansio`.

Keine strikten Shelloptionen wie `set -e` in die interaktive SSH-Shell kopieren.
Ein erwarteter Testfehler könnte sonst die Sitzung beenden. Die Release-Skripte
setzen ihre Optionen intern. Für lange Builds kann `tmux` genutzt werden:

```bash
sudo apt-get install -y tmux
tmux new -s control-plane
```

## Erweiterte Persistenz- und Recovery-Tests

Erst nach erfolgreicher Basisabnahme:

```bash
./test_extended_all.sh
```

Dieser Test erzeugt Backups, recreatet PostgreSQL, validiert Replikation und
führt einen isolierten Restore aus. v0008 übergibt dabei die Host-UID an die
PostgreSQL-Container. Der Host kann deshalb `postgres_system/backup` nach dem
Container-Recreate lesen; der Restore sucht die Dump-Datei robust innerhalb
des Containers.

## Stoppen

```bash
./rollout.sh stop
```

`stop` entfernt Container, bewahrt aber Volumes und Runtime-State. Der nächste
`./rollout.sh test --fresh` purgt diese bewusst; `--upgrade` bewahrt sie.

## Produktionsmodus

Alle produktiven MQTT-, PostgreSQL- und HTTPS-Zertifikatsvariablen aus der
v0006-Anleitung bleiben erforderlich. Zusätzlich muss der Modus explizit sein.

Upgrade:

```bash
export CONTROL_PLANE_STATE_SOURCE=/opt/automation/releases/previous
./rollout.sh production --upgrade
```

Fresh – destruktiv und deshalb doppelt bestätigt:

```bash
export CONTROL_PLANE_CONFIRM_PURGE=YES
./rollout.sh production --fresh
```

Ohne `--fresh` oder `--upgrade` startet der Produktionsrollout nicht.

## Worker v0079, Authority und Modbus

Der Worker-Code ist nicht Bestandteil dieses Control-Plane-Pakets. Für die neue
Authority-Aufteilung wird das separat bereitgestellte Worker-Release v0079
verwendet. Nach frischem Rollout liegt die Lab-PKI
unter `mqtt_broker/test-pki`. Zertifikat und Schlüssel des Workers müssen
manuell sicher auf `192.168.0.80` übertragen werden. Der Worker verbindet sich
mit `192.168.0.82:8883`, TLS-Servername `mqtt-broker`, Site `site_main`, Node
`fn-01` und Master `master-01`. Der Modbus-Testpfad bleibt
`192.168.0.3:5020`.

Der Worker wird nach einem gültigen brokergebundenen Live-Heartbeat automatisch
genehmigt. Proxy-Login, Cookie und CSRF-Token sind für diesen normalen
Onboarding-Pfad nicht mehr erforderlich. Ohne laufenden Modbus-Server müssen
MQTT-Verbindung und Admission trotzdem funktionieren; nur Feldbusoperationen
können dann fehlschlagen. Die Control Plane prüft den kompletten Admission-
Pfad so:

```bash
cd "$HOME/automation_control_plane_release_v0011"
./test_worker_auto_admission.sh fn-01 90
```

Erfolg bedeutet: `APPROVED`, aktiver Registry-Zustand und mindestens ein in
PostgreSQL persistierter verteilter Worker-Heartbeat.

Die Master-Healthantwort zeigt nach dem ersten v0079-Heartbeat zusätzlich die
berechnete Automation-Authority:

```bash
curl --fail --silent --show-error \
  http://127.0.0.1:9090/health/ready |
python3 -c '
import json
import sys

automation = json.load(sys.stdin)["runtime"]["automation"]
print(json.dumps(automation["authority"], indent=2, sort_keys=True))
print(json.dumps(automation["statistics"], indent=2, sort_keys=True))
'
```

Für die Controller von `fn-01` wird `mode: observe_only` erwartet. Das bedeutet
nicht, dass Sensoren ignoriert werden: Der Master persistiert und streamt sie,
delegiert aber vollständig lokale Regeln an den Worker. Ein kurzer Offline-
Zeitraum darf diese Einträge nicht auf `master_execute` umschalten.

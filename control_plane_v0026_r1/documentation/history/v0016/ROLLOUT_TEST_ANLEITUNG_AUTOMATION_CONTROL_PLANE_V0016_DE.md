# Automation Control Plane v0016-r8 – vollständiger Fresh-Rollout und Core-E2E

Stand: 2026-07-19
Control Plane: `0.16.0 / v0016-r8`
Worker: separates Paket `automation_system_v0082.zip`

Diese Anleitung ist für den Referenz-Teststand ausgelegt:

- Control Plane: `192.168.0.82`
- separater Worker `fn-01`: `192.168.0.80`, SSH-Benutzer `philo`
- Modbus-Simulator: `192.168.0.3:5020`
- Gesamtpaket: `automation_control_plane_v0016_r8_gesamtpaket.zip`
- Control-Plane-Archiv: `automation_control_plane_release_v0016_r8.zip`
- korrigiertes Worker-Archiv: `automation_system_v0082.zip`
- verbindlicher Worker-ZIP-Hash:
  `f6bc082cc89deefd570df4718387c09ed38c5184968316dc6a56b94667e071a0`

Alle Blöcke sind für Bash formuliert. Die Control-Plane-Blöcke laufen auf
`192.168.0.82`. Nur ausdrücklich bezeichnete Blöcke laufen auf dem Worker.
Eine Python-venv und ein Host-`pip install` sind nicht erforderlich.

> **Destruktive Grenze:** `--purge-host` löscht ausnahmslos alle
> Docker-Container, Volumes und Images der Control Plane, zusätzlich ungenutzte
> Netzwerke sowie den gesamten Docker-Builder-Cache. Nur auf einem dedizierten
> Testhost verwenden. Auf einem Shared Host `--purge-host` und
> `CONTROL_PLANE_CONFIRM_HOST_PURGE` weglassen; der scoped Fresh-Lauf entfernt
> dann ausschließlich bekannte Control-Plane-Ressourcen und baut trotzdem
> ohne Cache.

## 1. Gesamtpaket auf der Control Plane prüfen und entpacken

Das Gesamtpaket und seine Begleitdatei müssen direkt unter `$HOME` liegen.
Der Block verweigert ein Überschreiben eines bereits vorhandenen
Gesamtpaket-Verzeichnisses.

```bash
(
  set -Eeuo pipefail
  cd "$HOME"

  BUNDLE=automation_control_plane_v0016_r8_gesamtpaket.zip
  BUNDLE_HASH=automation_control_plane_v0016_r8_gesamtpaket.zip.sha256
  BUNDLE_DIR="$HOME/automation_control_plane_v0016_r8_gesamtpaket"

  test -s "$BUNDLE"
  test -s "$BUNDLE_HASH"
  sha256sum -c "$BUNDLE_HASH"

  test ! -e "$BUNDLE_DIR" || {
    echo "BLOCKED: $BUNDLE_DIR existiert bereits; zuerst sichern oder entfernen." >&2
    exit 65
  }

  unzip -q "$BUNDLE"
  cd "$BUNDLE_DIR"
  sha256sum -c --strict SHA256SUMS

  test -s automation_control_plane_release_v0016_r8.zip
  test -s automation_control_plane_release_v0016_r8.zip.sha256
  test -s automation_system_v0082.zip
  test -s automation_system_v0082.zip.sha256
  test "$(sha256sum automation_system_v0082.zip | awk '{print $1}')" = \
    f6bc082cc89deefd570df4718387c09ed38c5184968316dc6a56b94667e071a0
)
```

Nur nach vollständigem `OK` aller Einträge fortfahren.

## 2. Voraussetzungen auf der Control Plane prüfen

```bash
(
  set -Eeuo pipefail
  docker --version
  docker compose version
  docker info >/dev/null
  openssl version
  python3 --version
  unzip -v | head -n 1
  sha256sum --version | head -n 1
  ssh -V
  scp -V 2>&1 | head -n 1 || true
)
```

Falls ausschließlich `docker info` an Rechten scheitert:

```bash
sudo usermod -aG docker "$USER"
```

Danach einmal vollständig ab- und neu anmelden und Schritt 2 wiederholen.

## 3. Laufenden Worker kontrolliert stoppen

Dieser Block beendet ausschließlich einen als
`python ... -m src.sync_xserver_main` laufenden Workerprozess. Er sendet
`SIGTERM`, wartet bis zu 30 Sekunden und führt absichtlich kein `SIGKILL` aus.
Wenn der Worker bereits im Vordergrund läuft, ist `Ctrl+C` in dessen Terminal
der bevorzugte Weg.

```bash
ssh philo@192.168.0.80 'bash -s' <<'WORKER_STOP'
set -Eeuo pipefail

for COMMAND in pgrep seq; do
  command -v "$COMMAND" >/dev/null
done

mapfile -t WORKER_PIDS < <(
  pgrep -f '[p]ython[^ ]* .*[-]m src\.sync_xserver_main' || true
)

if ((${#WORKER_PIDS[@]} == 0)); then
  echo 'Worker ist bereits gestoppt.'
  exit 0
fi

printf 'Stoppe Worker-PIDs: %s\n' "${WORKER_PIDS[*]}"
kill -TERM -- "${WORKER_PIDS[@]}"

for _ in $(seq 1 30); do
  LIVE=()
  for PID in "${WORKER_PIDS[@]}"; do
    kill -0 "$PID" 2>/dev/null && LIVE+=("$PID")
  done
  ((${#LIVE[@]} == 0)) && {
    echo 'Worker kontrolliert gestoppt.'
    exit 0
  }
  sleep 1
done

echo "BLOCKED: Worker reagierte nicht auf SIGTERM: ${LIVE[*]}" >&2
exit 70
WORKER_STOP
```

## 4. Alte Control-Plane-Dateisystemstände entfernen

Der Block löscht nur direkt unter `$HOME` liegende Verzeichnisse mit dem
Präfix `automation_control_plane_release_v0` und den historischen
Validation-Cache. Gesamtpaket und ZIP-Dateien bleiben erhalten. Die Liste
zuerst prüfen und dann exakt `DELETE` eingeben.

```bash
(
  set -Eeuo pipefail
  INSTALL_ROOT="$HOME"
  test -d "$INSTALL_ROOT"
  test "$INSTALL_ROOT" != /

  mapfile -t OLD_RELEASE_DIRS < <(
    find "$INSTALL_ROOT" -mindepth 1 -maxdepth 1 -type d \
      -name 'automation_control_plane_release_v0*' -print | sort
  )

  printf 'Zu löschende alte Release-Verzeichnisse:\n'
  if ((${#OLD_RELEASE_DIRS[@]})); then
    printf '  %s\n' "${OLD_RELEASE_DIRS[@]}"
  else
    printf '  <keine>\n'
  fi
  printf '  %s\n' "$HOME/.cache/automation-control-plane (falls vorhanden)"

  read -r -p 'Zum Löschen exakt DELETE eingeben: ' FILE_PURGE_CONFIRM
  test "$FILE_PURGE_CONFIRM" = DELETE

  for OLD_DIR in "${OLD_RELEASE_DIRS[@]}"; do
    case "$OLD_DIR" in
      "$INSTALL_ROOT"/automation_control_plane_release_v0*)
        rm -rf -- "$OLD_DIR"
        ;;
      *)
        echo "Unsicherer Pfad abgelehnt: $OLD_DIR" >&2
        exit 1
        ;;
    esac
  done

  rm -rf -- "$HOME/.cache/automation-control-plane"
)
```

## 5. Control-Plane-Release prüfen und sauber entpacken

```bash
(
  set -Eeuo pipefail
  BUNDLE_DIR="$HOME/automation_control_plane_v0016_r8_gesamtpaket"
  cd "$BUNDLE_DIR"

  sha256sum -c automation_control_plane_release_v0016_r8.zip.sha256
  unzip -q automation_control_plane_release_v0016_r8.zip -d "$HOME"

  RELEASE_DIR="$HOME/automation_control_plane_release_v0016"
  cd "$RELEASE_DIR"
  test "$(cat VERSION)" = 0.16.0
  test "$(cat RELEASE_REVISION)" = v0016-r8
  test "$(cat integration_test/e2e_v0016/VERSION)" = 0.16.0-e2e.5

  chmod 755 rollout.sh run_core_e2e_lab.sh \
    tools/verify_checksums.sh tools/run_in_validation_container.sh
  ./tools/verify_checksums.sh
)
```

Nur bei folgender Schlusszeile fortfahren:

```text
Root, component and integrated E2E SHA-256 manifests are valid and complete.
```

## 6. Vollständigen Fresh-Rollout auf dem dedizierten Docker-Host ausführen

```bash
cd "$HOME/automation_control_plane_release_v0016"

export CONTROL_PLANE_BIND_ADDRESS=192.168.0.82
export MQTT_BIND_ADDRESS=192.168.0.82
export MQTT_TEST_HOST=192.168.0.82
export MQTT_TEST_BROKER_IP=192.168.0.82
export PROXY_BIND_ADDRESS=192.168.0.82
export PROXY_WSS_PORT=8443
export PROXY_HTTPS_PORT=8444
export PROXY_ORIGIN_ALLOWLIST=https://192.168.0.82:8444
export CONTROL_PLANE_CONFIRM_HOST_PURGE=PURGE

(
  set -Eeuo pipefail
  ./rollout.sh --fresh --purge-host --profile lab_v2 2>&1 | \
    tee "$HOME/automation-control-plane-v0016-r8-rollout.log"
  grep -F \
    'Control Plane 0.16.0/v0016-r8 lab_v2 is deployed and tested on 192.168.0.82 (fresh mode).' \
    "$HOME/automation-control-plane-v0016-r8-rollout.log"
)
```

Der Lauf muss alle Stufen `1/10` bis `10/10` erreichen. Bei einem Fehler
keine Stufe manuell überspringen und keine alten Container selektiv
weiterverwenden.

## 7. Laufenden Control-Plane-Stand verifizieren

```bash
(
  set -Eeuo pipefail
  cd "$HOME/automation_control_plane_release_v0016"

  ./rollout.sh status
  ./rollout.sh test-running
  ./master_node/test_runtime_state.sh

  docker ps --format 'table {{.Names}}\t{{.Image}}\t{{.Status}}'

  test "$(docker image inspect automation-proxy-gateway:0.16.0 \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" = \
    v0016-r8
  test "$(docker image inspect automation-master-node:0.16.0 \
    --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}')" = \
    v0016-r8

  curl --fail --silent --show-error \
    http://127.0.0.1:9090/health/ready
  curl --fail --silent --show-error \
    --cacert proxy_gateway/test-pki/wss/ca.crt \
    https://192.168.0.82:8444/api/v1/health/ready
)
```

PostgreSQL und Broker behalten bewusst ihre geprüften v0014-Komponentenstände;
Master und Proxy müssen die OCI-Revision `v0016-r8` tragen.

## 8. Administrator anlegen

```bash
cd "$HOME/automation_control_plane_release_v0016"
./proxy_gateway/create_admin.sh admin
```

Das Passwort wird verdeckt eingegeben und muss mindestens 14 Zeichen lang
sein. `conflict` bedeutet, dass `admin` bereits vorhanden ist; dann das
bestehende Passwort verwenden.

## 9. Korrigiertes Worker-Paket und neue MQTT-Identität übertragen

Jeder Fresh-Lauf erzeugt eine neue Lab-CA. Der folgende Block prüft zunächst
Worker-ZIP und Clientzertifikat und überträgt dann Paket plus PKI in ein
separates Staging-Verzeichnis auf `192.168.0.80`.

```bash
(
  set -Eeuo pipefail
  RELEASE_DIR="$HOME/automation_control_plane_release_v0016"
  BUNDLE_DIR="$HOME/automation_control_plane_v0016_r8_gesamtpaket"
  WORKER_SSH=philo@192.168.0.80
  REMOTE_STAGE=automation-control-plane-v0016-r8-stage

  cd "$BUNDLE_DIR"
  sha256sum -c automation_system_v0082.zip.sha256
  test "$(sha256sum automation_system_v0082.zip | awk '{print $1}')" = \
    f6bc082cc89deefd570df4718387c09ed38c5184968316dc6a56b94667e071a0

  MQTT_LAB_CA="$RELEASE_DIR/mqtt_broker/test-pki/ca/ca.crt"
  FN01_CERT="$RELEASE_DIR/mqtt_broker/test-pki/clients/worker-fn-01/mqtt.crt"
  FN01_KEY="$RELEASE_DIR/mqtt_broker/test-pki/clients/worker-fn-01/mqtt.key"

  for FILE in "$MQTT_LAB_CA" "$FN01_CERT" "$FN01_KEY"; do
    test -s "$FILE"
  done

  openssl verify -purpose sslclient -CAfile "$MQTT_LAB_CA" "$FN01_CERT"
  openssl x509 -in "$FN01_CERT" \
    -noout -subject -issuer -dates -ext subjectAltName | \
    tee "$HOME/fn-01-mqtt-certificate.txt"
  grep -F 'URI:urn:automation:site_main:worker:fn-01' \
    "$HOME/fn-01-mqtt-certificate.txt"

  ssh "$WORKER_SSH" \
    "install -d -m 700 \"\$HOME/$REMOTE_STAGE\" \"\$HOME/$REMOTE_STAGE/pki\""

  scp \
    "$BUNDLE_DIR/automation_system_v0082.zip" \
    "$BUNDLE_DIR/automation_system_v0082.zip.sha256" \
    "${WORKER_SSH}:${REMOTE_STAGE}/"
  scp "$MQTT_LAB_CA" "$FN01_CERT" "$FN01_KEY" \
    "${WORKER_SSH}:${REMOTE_STAGE}/pki/"
)
```

## 10. Worker-Paket separat testen, alten Stand sichern und frisch installieren

Dieser Worker-Block testet das ZIP zuerst in einem Wegwerfverzeichnis. Erst
nach erfolgreichem `36/36`-Test wird ein vorhandener Workerstand atomar in ein
zeitgestempeltes Backup verschoben und das Archiv ein zweites Mal sauber
entpackt. Dadurch gelangen weder Testlogs noch eine alte Master-Replica in die
neue Baseline.

```bash
ssh philo@192.168.0.80 'bash -s' <<'WORKER_INSTALL'
set -Eeuo pipefail
umask 077

for COMMAND in install mktemp openssl python3 sha256sum unzip; do
  command -v "$COMMAND" >/dev/null
done

STAGE="$HOME/automation-control-plane-v0016-r8-stage"
WORKER_PARENT="$HOME/python-skripte_22/__NEW_FILES__"
WORKER_DIR="$WORKER_PARENT/automation_system_v0082"
BACKUP_ROOT="$HOME/automation-worker-backups"
ARCHIVE="$STAGE/automation_system_v0082.zip"
HASH_FILE="$STAGE/automation_system_v0082.zip.sha256"
EXPECTED_SHA=f6bc082cc89deefd570df4718387c09ed38c5184968316dc6a56b94667e071a0

install -d -m 755 "$WORKER_PARENT"
install -d -m 700 "$BACKUP_ROOT"
cd "$STAGE"
sha256sum -c "$HASH_FILE"
test "$(sha256sum "$ARCHIVE" | awk '{print $1}')" = "$EXPECTED_SHA"

VERIFY_ROOT=$(mktemp -d "$HOME/.worker-v0082-verify.XXXXXX")
INSTALL_ROOT=$(mktemp -d "$WORKER_PARENT/.worker-v0082-install.XXXXXX")
cleanup() {
  rm -rf -- "$VERIFY_ROOT" "$INSTALL_ROOT"
}
trap cleanup EXIT

unzip -q "$ARCHIVE" -d "$VERIFY_ROOT"
VERIFY_DIR="$VERIFY_ROOT/automation_system_v0082"
test -d "$VERIFY_DIR"
cd "$VERIFY_DIR"
sha256sum -c --strict SHA256SUMS
./test_all.sh | tee "$HOME/automation-system-v0082-validation.log"
grep -F 'PASS complete Worker validation: 36 executable tests' \
  "$HOME/automation-system-v0082-validation.log"

rm -rf -- "$VERIFY_ROOT"
VERIFY_ROOT=

unzip -q "$ARCHIVE" -d "$INSTALL_ROOT"
NEW_WORKER_DIR="$INSTALL_ROOT/automation_system_v0082"
test -d "$NEW_WORKER_DIR"
cd "$NEW_WORKER_DIR"
sha256sum -c --strict SHA256SUMS

test -x install_mqtt_identity.sh
test -x run_worker.sh
test -x test_all.sh

if [ -e "$WORKER_DIR" ]; then
  BACKUP_DIR="$BACKUP_ROOT/automation_system_v0082.$(date -u +%Y%m%dT%H%M%SZ).$$"
  mv -- "$WORKER_DIR" "$BACKUP_DIR"
  echo "Alter Worker gesichert: $BACKUP_DIR"
fi

mv -- "$NEW_WORKER_DIR" "$WORKER_DIR"
rmdir -- "$INSTALL_ROOT"
INSTALL_ROOT=

chmod 644 "$STAGE/pki/ca.crt" "$STAGE/pki/mqtt.crt"
chmod 600 "$STAGE/pki/mqtt.key"

cd "$WORKER_DIR"
sha256sum -c --strict SHA256SUMS
./install_mqtt_identity.sh \
  --source-dir "$STAGE/pki" \
  --site-id site_main \
  --node-id fn-01

openssl verify -purpose sslclient \
  -CAfile _config/certs/mqtt_ca.crt \
  _config/certs/worker_fn-01.crt
openssl x509 -in _config/certs/worker_fn-01.crt \
  -noout -subject -issuer -dates -ext subjectAltName | \
  grep -F 'URI:urn:automation:site_main:worker:fn-01'

AUTOMATION_PROFILE=worker_simulation ./run_worker.sh --preflight
trap - EXIT
cleanup

echo 'Worker-v0082-Baseline, Paketintegrität, PKI und Preflight: PASS'
WORKER_INSTALL
```

## 11. Worker in Terminal A im Vordergrund starten

Dieser Befehl bleibt absichtlich aktiv. Das Terminal offen lassen.

```bash
ssh -tt philo@192.168.0.80 \
  'cd "$HOME/python-skripte_22/__NEW_FILES__/automation_system_v0082" && exec env AUTOMATION_PROFILE=worker_simulation ./run_worker.sh'
```

## 12. Worker-Konvergenz in Terminal B auf der Control Plane prüfen

```bash
(
  set -Eeuo pipefail
  cd "$HOME/automation_control_plane_release_v0016"
  ./test_worker_auto_admission.sh fn-01 120
  ./diagnose_worker_sync.sh fn-01
)
```

Verbindliche Erfolgszeile:

```text
Worker site_main/fn-01 was admitted and its Node shard converged.
```

## 13. Live-Telemetrie bis in den Proxy-Replay-Cache prüfen

Dieses Telemetrie-Gate prüft genau die im r6-Feldlauf ausgefallene Kette:
`Worker SENSOR_VALUE_UPDATE → Master sensor_latest → Master sensor.value →
Proxy event_replay_cache`. Die Sequenz muss innerhalb des Zeitfensters sowohl
im Master als auch im Proxy strikt voranschreiten. Damit wird ein stillstehender
Eventpfad erkannt, bevor der WSS-E2E gestartet wird.

```bash
(
  set -Eeuo pipefail
  cd "$HOME/automation_control_plane_release_v0016"
  ./test_worker_sensor_telemetry.sh fn-01 45
)
```

Verbindliche Erfolgszeile beginnt mit:

```text
PASS Worker telemetry: source_sequence
```

Ein Timeout an dieser Stelle ist **kein Zertifikats- oder WSS-Handshakefehler**.
Dann zuerst Worker-`SENSOR_VALUE_UPDATE`, Master-Legacy-MQTT-Statistik und
Proxy-Event-Ingest prüfen; den E2E nicht überspringen.

## 14. Vollständigen Core-E2E ausführen

Der Test schreibt kurzzeitig ausschließlich auf den freigegebenen
Modbus-Lab-Endpunkt `192.168.0.3:5020` und stellt den zuvor gelesenen Wert im
Cleanup wieder her. Nicht gegen Produktionshardware ausführen.

Der r8-E2E mutiert für `fn-01` ausschließlich die Tags eines tatsächlich vom
Worker gemeldeten Owner-Controllers. Er verwendet die vom Master unterstützte
Operation `set`, verifiziert vor dem Commit, dass sich nur der Node-Shard und
nicht der Cross-Shard ändert, und bewertet Authority-, lokale Worker- und
Replica-Revision als getrennte monotone Domänen. Vor dem Pagination-Stresstest
wird der `command`-Filter des dauerhaft gehaltenen Event-Sockets atomar
entfernt; der separate Replay-Socket prüft die vollständige Pagination weiter.

```bash
bash -s <<'E2E_RUN'
set -Eeuo pipefail
cd "$HOME/automation_control_plane_release_v0016"

if [[ -z "${E2E_BEARER_TOKEN:-}" ]]; then
  read -r -s -p 'Admin-Passwort: ' AUTOMATION_ADMIN_PASSWORD </dev/tty
  printf '\n' >/dev/tty
  export AUTOMATION_ADMIN_PASSWORD
fi

./run_core_e2e_lab.sh

RESULT=integration_test/e2e_v0016/results/e2e-v0016-result.json
test -s "$RESULT"
python3 - "$RESULT" <<'PY'
import json
import sys

with open(sys.argv[1], "r", encoding="utf-8") as handle:
    result = json.load(handle)

assert result.get("version") == "0.16.0-e2e.5", result.get("version")
assert result.get("status") == "PASSED", result.get("error")
assert result.get("config_restore_confirmed") is True
assert result.get("actuator_restore_confirmed") is True

required_checks = {
    "HTTPS-READY", "TLS-001", "TLS-002", "TLS-003", "AUTH",
    "SEC-001", "SEC-002", "SEC-003", "SEC-006", "WSS-CONTROL",
    "PATH-ORIGIN", "PTH-001", "PTH-002", "PTH-003", "PTH-004",
    "WSS-EVENTS", "CMD-READS", "RD-001", "RD-002", "RD-003",
    "HTTP-001", "HTTP-002", "WORKER-FN01", "EVT-001", "EVT-002",
    "EVT-003", "EVT-001A", "EVT-004", "EVT-004A", "SEC-005",
    "SEC-007", "SEC-008", "CMD-NEGATIVE", "SEC-004", "CFG-001",
    "CFG-002", "CFG-003", "CFG-004", "CFG-005", "CFG-006",
    "CONFIG-DIFF", "ACT-001", "ACT-002", "ACT-003", "ACT-004",
    "PAR-001", "CROSS-WORKER", "ACT-RESTORE", "CFG-RESTORE",
    "SEC-LOG", "SEC-009",
}
checks = result.get("checks") or {}
missing = sorted(required_checks.difference(checks))
assert not missing, "fehlende Core-E2E-Checks: " + ", ".join(missing)

auth_detail = checks.get("AUTH", {}).get("detail") or {}
allowed_skips = {"CROSS-WORKER"}
if auth_detail.get("mode") == "bearer":
    allowed_skips.add("SEC-004")

failed = {
    name: checks[name].get("status")
    for name in sorted(required_checks)
    if checks[name].get("status") != "PASSED"
    and not (
        checks[name].get("status") == "SKIPPED"
        and name in allowed_skips
    )
}
assert not failed, "nicht bestandene Core-E2E-Checks: " + repr(failed)
pagination = checks["EVT-004A"].get("detail") or {}
assert pagination.get("retained_stream_command_filter_removed") is True, pagination
print(
    "PASS Core-E2E: status=PASSED, vollständige Core-Prüfmenge sowie "
    "Config- und Aktor-Restore bestätigt"
)
PY

unset AUTOMATION_ADMIN_PASSWORD
E2E_RUN
```

Verbindlich sind Exit-Code `0`, `status=PASSED` und beide bestätigten
Restores. Bei `FAILED` oder `BLOCKED` Recovery-Datei und Live-Logs unverändert
aufbewahren; keinen zweiten Lauf durch Löschen der Recovery-Datei erzwingen.

## 15. Abschlussdiagnose

```bash
(
  set -Eeuo pipefail
  cd "$HOME/automation_control_plane_release_v0016"
  ./rollout.sh status
  ./rollout.sh test-running
  ./master_node/test_runtime_state.sh
  ./diagnose_worker_sync.sh fn-01
  LOG_TAIL=500 ./logs_all.sh
)
```

Wichtige Zuordnung:

- Abbruch vor Stufe 1: ZIP, Manifest, Docker-Zugriff oder Modusangabe falsch;
- `--purge-host requires ...`: Bestätigung fehlt oder Host ist nicht als
  dediziert markiert;
- Abbruch in Stufe 2: nativer Dienst oder fremder Prozess belegt einen Zielport;
- Proxy-CA fehlt: Stufe 9 wurde nicht erfolgreich beendet;
- Worker offline: Fresh-PKI nicht installiert oder Worker nicht neu gestartet;
- `BLOCKED stale_replica`: kein sauber neu entpackter Worker-v0082-Stand;
- `Permission denied` bei Worker-Skripten: falsches/altes Worker-ZIP verwendet;
- Worker-`SHA256SUMS` schlägt fehl: Transfer oder Paket beschädigt; nicht starten;
- Telemetrie-Gate läuft in Timeout: Worker publiziert keine steigende
  `sensor_store_sequence`, der Master persistiert sie nicht oder der Proxy
  erhält kein neues `sensor.value`; nicht als WSS-Zertifikatsfehler behandeln;
- E2E meldet `matching WSS event timed out: sensor.value`: Telemetriepfad aus
  Schritt 13 erneut prüfen; TLS/WSS-Handshake wurde zu diesem Zeitpunkt bereits
  erfolgreich abgeschlossen;
- `primary E2E failure: ...; cleanup failure: ...` enthält absichtlich beide
  Fehler. Der erste Teil ist die ursprüngliche Testursache, der zweite Teil ein
  zusätzlicher Restorefehler; keinen der beiden Teile verwerfen;
- `WSS slow client send timed out` bedeutet einen kontrolliert mit Close-Code
  `1013` getrennten Client. Im r8-Core-E2E darf diese Warnung nicht durch den
  Paginationstest entstehen; bei erneutem Auftreten `proxy-live.log` sichern;
- `Unsupported upgrade request` am HTTPS-Port `8444` gehört zum absichtlichen
  Negativtest. Der produktive Direct-WSS-Listener läuft getrennt auf `8443`;
- E2E-Logpfad `BLOCKED`: Pfad liegt nicht im Release oder `E2E_RESULT_DIR`;
- Aktorziel abgelehnt: Host und Port müssen exakt `192.168.0.3:5020` sein.

## 16. Komplett neu beginnen

Zuerst den Worker mit Schritt 3 stoppen. Danach auf dem dedizierten
Control-Plane-Testhost:

```bash
cd "$HOME/automation_control_plane_release_v0016"
export CONTROL_PLANE_CONFIRM_HOST_PURGE=PURGE
./rollout.sh --fresh --purge-host --profile lab_v2
```

Nach jedem Fresh-Lauf Worker erneut aus der sauberen Paketbaseline installieren,
die neu erzeugte MQTT-PKI übertragen, den Worker starten, Konvergenz prüfen und
den Core-E2E wiederholen.

Das optionale Profil `full-lab` ist kein schneller Core-E2E. Es benötigt einen
extern kontrollierten SSH-/Worker-Launcher, signierte Broker-Reservation- und
Release-Proofs sowie zusätzliche Recovery-Voraussetzungen. Ohne diese externen
Mittel endet `full-lab` korrekt mit `BLOCKED`.

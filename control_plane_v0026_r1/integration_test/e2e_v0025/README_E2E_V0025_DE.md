# Qualifizierter e2e_v0025-Vertrag im Control-Plane-Bundle v0026-r1

Version: `0.25.0-e2e.3`

V9 ist in dieser Generation der verbindliche Node-Shard-Storage- und
Transportvertrag; Sensoren und Aktoren sind flache Entity-Tabellen. Im
Standardmodus `node_local` entscheidet ausschließlich der owning Worker über
seinen Node-Shard. Der Master hält Registry, Relay, Mirror und Audit, aber
vergibt keine Node-Revision. Der separate hardwarefreie Cross-Shard bleibt V8.
Die Abnahme unterscheidet zwei Operationsursprünge ausdrücklich:

1. lokaler V9-JSON-Edit → Worker File-WAL → kanonische entity-keyed Operations
   → SEM/Safety → lokaler Authority-Commit → Journal/Mirror-Outbox;
2. nativer Client-`config.propose` → WSS → Proxy → MQTT → Master-Relay →
   owning Worker → SEM/Safety → Authority-Commit → Result/Mirror, ohne
   `file:*`-Operation-ID.

Beide Matrizen ändern Controller, Sensor, Aktor, Rule-Threshold,
`process_states` und `processes.process_location`, prüfen Idempotenz und
stale-baseline-Verhalten und stellen danach ausschließlich die eigenen Felder
wieder her. `config_commit_path_equivalence_e2e.py` beweist zusätzlich die
identische V9-Materialisierung und den identischen Fingerprint beider
entity-keyed Diff-Semantiken.

## Authority-V5-Korrekturen des realen Systempfads

- Die Config-Probe verwendet `set` auf `controllers.N.tags`; die echte
  entity-keyed Änderungsengine wird im Selbsttest ausgeführt. Vor dem
  workerseitigen Authority-Commit darf sich ausschließlich der adressierte
  Node-Shard ändern.
- Authority-Epoch, Scope-Revision, Execution-Epoch und Fingerprint werden als
  ein vollständiges monotones Tupel geprüft. Master-Mirror und Registry sind
  Beobachter dieses Worker-Heads und keine konkurrierende Config-Authority.
- Vor dem mehrseitigen Command-Replay wird der Command-Filter vom dauerhaft
  gehaltenen Primär-Eventstream entfernt. Der dedizierte Replaystream bleibt
  unverändert auf Command-Events gefiltert.
- Ein Proxy-Send-Timeout beendet nur den langsamen Client kontrolliert mit
  Close-Code `1013`; er ist kein Zertifikats- oder Handshakefehler.
- Ein primärer E2E-Fehler und ein zusätzlicher Cleanup-Fehler werden gemeinsam
  ausgegeben, sodass die erste fachliche Ursache sichtbar bleibt.

## Zweck und ehrliche Systemgrenze

Dieser E2E verwendet das öffentliche Python-Modul `websockets` und prüft den
primären Clientpfad über den direkten TLS-WSS-Listener:

```text
E2E -> WSS :8443 -> Proxy -> MQTTS -> Master -> Worker fn-01 -> Modbus
```

HTTPS/REST bleibt ein sekundärer Paritätspfad auf `:8444`. Der E2E enthält
kein eigenes RFC-6455-Framing und verwendet Uvicorn nicht für WSS.

Event-Filter-Updates werden über denselben WSS-Kanal wie Live-Events
bestätigt. Bereits vor dem Update unterwegs befindliche Event-Envelopes dürfen
daher vor `subscription.accepted` eintreffen. Der Client validiert und puffert
diese Frames begrenzt, korreliert die Antwort exakt über `request_id` und führt
die Events anschließend verlustfrei der normalen Prüfung zu. Das Limit von 256
Frames gilt ausschließlich für das aktuelle Update-Fenster; ein schon vorher
vorhandener Replay-Backlog wird nicht erneut dagegen gerechnet.

Ein normales Live-Abonnement ohne explizites `last_event_id` übernimmt zuerst
den vom Server gemeldeten `replay_high_watermark` und verbindet genau einmal
mit diesem Cursor neu. Dadurch verarbeitet ein Wiederholungslauf nicht erneut
die gesamte dauerhafte Historie. Ereignisse zwischen beiden Verbindungen
werden auf der zweiten Verbindung regulär replayt. Alle Tests mit explizitem
Cursor behalten unverändert ihre Replay-Semantik.

`full-lab` ergänzt einen extern gestarteten Worker v0101 für
`fn-01` und den kurzlebigen MQTT-Protokollaktor `fn-02`. Beide gehören zu
`site-main`. Das ist ein Cross-Worker-Nachweis und ausdrücklich **kein**
positiver Cross-Site-Nachweis; jeder Report enthält `cross_site=false`.

## Installation

Der E2E-Einstiegspunkt baut und verwendet automatisch das gepinnte
Validation-Image `automation-control-plane-validation:0.26.0`. Auf dem Host
werden weder venv noch pip-Pakete angelegt. Voraussetzung ist ausschließlich
eine funktionierende Docker-Installation:

```bash
cd "$HOME/automation_control_plane_release_v0026_r1"
./test_static_all.sh
```

`requirements-e2e.txt` und die vollständige Validation-Abhängigkeitssicht sind
versionsgepinnt. Das Image wird mit `--pull --no-cache` neu gebaut, wenn sein
Fingerprint fehlt oder abweicht. Der Releasebaum wird ausschließlich lesbar
als `/release` eingebunden; nur `E2E_RESULT_DIR` wird schreibbar als `/results`
gemountet. Eine vorherige Shell-Aktivierung ist nicht nötig.

Vor einem Live-Lauf:

```bash
cd "$HOME/automation_control_plane_release_v0026_r1"
./integration_test/e2e_v0025/test_static.sh
```

## TLS, URLs und Authentisierung

TLS-Verifikation ist immer aktiv. Es gibt keinen `--insecure`-Schalter und
kein implizites `CERT_NONE`. Die CA muss beide direkt angesprochenen
Serverzertifikate verifizieren; SAN/Hostname beziehungsweise SAN/IP werden
von Python geprüft.

| Variable | Standard / Bedeutung |
|---|---|
| `E2E_HTTPS_URL` | `https://192.168.0.82:8444` |
| `E2E_WSS_URL` | `wss://192.168.0.82:8443` |
| `E2E_ORIGIN` | `https://192.168.0.82:8444`, muss exakt erlaubt sein |
| `E2E_CA_FILE` | Standard: `proxy_gateway/test-pki/wss/ca.crt` im Release; ohne lesbare Datei `BLOCKED` |
| `E2E_USERNAME` | `admin` |
| `E2E_PASSWORD_ENV` | Name der Passwortvariable, Standard `AUTOMATION_ADMIN_PASSWORD` |
| `E2E_BEARER_TOKEN_ENV` | Name der optionalen Bearer-Variable, Standard `E2E_BEARER_TOKEN` |
| `E2E_ACTUATOR_ID` / `E2E_SENSOR_ID` | explizite Core-Lab-Fixture, jeweils Standard `1` |
| `E2E_APPROVED_ACTUATOR_HOST` | einziger für Core-Aktorwrites erlaubter Host, Standard `192.168.0.3` |
| `E2E_APPROVED_ACTUATOR_PORT` | einziger erlaubter Port, Standard `5020` |
| `E2E_PROXY_LOG_SOURCE` | zwingende, nicht leere und frische reguläre Live-Logdatei des Proxy |
| `E2E_PROTOCOL_LOG_SOURCE` | zwingende, nicht leere und frische reguläre Protocol-/Master-Logdatei |
| `E2E_SECRET_LOG_SOURCE` | optionale weitere, von allen anderen Pfaden verschiedene reguläre Logquelle für die Canary-Suche |

Vor dem Containerstart kanonisiert der Wrapper alle konfigurierten Dateipfade.
Eingaben müssen innerhalb des Releasebaums oder innerhalb von
`E2E_RESULT_DIR` liegen. Sie werden auf `/release/...` beziehungsweise
`/results/...` abgebildet. Ergebnis-, Ablauf- und Recovery-Datei müssen
innerhalb von `E2E_RESULT_DIR` liegen. Absolute Fremdpfade und Symlink-Escapes
werden mit Exitcode `64` vor jedem Docker-Start fail-closed abgewiesen; der
Wrapper bindet niemals beliebige Hostverzeichnisse ein. Die Standard-CA
benötigt deshalb keine gesetzte Variable. Eigene CA-, Proof- oder Logdateien
müssen zuerst in einen der beiden freigegebenen Bäume gelegt werden.

Passwort, Bearer, Session, CSRF und ausgegebene Token werden niemals als
CLI-Werte übergeben. Der Logger redigiert sensible Felder rekursiv und prüft
am Ende die bekannten Authentisierungs-Canaries. Diese Prüfung läuft auch nach
einem Primär- oder Cleanup-Fehler. Verpflichtend gescannt werden das E2E-, das
Proxy- und das Protocol-Log. `E2E_SECRET_LOG_SOURCE` fügt optional genau eine
weitere reguläre Logquelle hinzu; sie muss von allen übrigen Ein-/Ausgabepfaden
verschieden sein und darf selbst keine neuen Secrets enthalten.

## Profil `core`

Der vollständige reversible Core-Lauf benötigt eine ausdrückliche
Mutationsfreigabe:

```bash
cd "$HOME/automation_control_plane_release_v0026_r1"
result_dir="$PWD/integration_test/e2e_v0025/results"
install -d -m 0700 "$result_dir"
: >"$result_dir/proxy-v0025.log"
: >"$result_dir/protocol-v0025.log"
chmod 0600 "$result_dir/proxy-v0025.log" "$result_dir/protocol-v0025.log"
(cd proxy_gateway && exec docker compose logs --no-color --tail 50 --follow proxy-gateway) \
  >>"$result_dir/proxy-v0025.log" 2>&1 &
proxy_log_pid=$!
(cd master_node && exec docker compose logs --no-color --tail 50 --follow master-node) \
  >>"$result_dir/protocol-v0025.log" 2>&1 &
protocol_log_pid=$!
trap 'kill "$proxy_log_pid" "$protocol_log_pid" 2>/dev/null || true' EXIT
for log in "$result_dir/proxy-v0025.log" "$result_dir/protocol-v0025.log"; do
  for _attempt in $(seq 1 20); do
    test -s "$log" && break
    sleep 1
  done
  test -s "$log"
done
unset E2E_CA_FILE
export E2E_PROXY_LOG_SOURCE="$result_dir/proxy-v0025.log"
export E2E_PROTOCOL_LOG_SOURCE="$result_dir/protocol-v0025.log"
export AUTOMATION_ADMIN_PASSWORD='nur-in-der-geschützten-Umgebung-setzen'
E2E_CONFIRM_MUTATIONS=1 E2E_PROFILE=core \
  ./integration_test/e2e_v0025/run_e2e.sh
```

Ohne `E2E_CONFIRM_MUTATIONS=1` lautet das Ergebnis absichtlich `BLOCKED`.
Der Core-Lauf prüft unter anderem:

- separates HTTPS-Ready auf `:8444` und direktes WSS auf `:8443`;
- `connection.ready`, `subscription.accepted`, Korrelation über
  `request_id`, Live-Event und Replay über dauerhafte Sequence;
- alle 23 Registry-Actions: sichere Reads, reversible Config-/Token-/Aktor-
  Pfade sowie fail-closed Negativproben für nicht reversible Actions;
- REST/WSS-Leseparität sowie eine fail-closed REST-Mutationsprobe mit
  vertraglich verpflichtendem, run-spezifischem `Idempotency-Key`;
- API-Token mit explizit leeren Rechten/Sites und anschließenden Revoke;
- atomare Config-Matrix über WSS/Proxy/Master/MQTT: temporärer Sensor und
  Aktor, numerischer Regel-Threshold, PID-Sollwert, `mode_id` und `value_id`;
- begrenzter CAS-Rebase sowie feldgenauer Restore, der fremde parallele
  Authority-Commits nicht überschreibt;
- Lesen des realen Modbus-Ausgangswerts, Schalten auf einen anderen Wert,
  `CONFIRMED` plus WSS-Operationsevent und Restore auf exakt den Ausgangswert;
- fail-closed Owner-Mapping sowie exakte Bindung des Schreibtests an den
  freigegebenen Modbus-Lab-Endpunkt; ein abweichender Host/Port ist `BLOCKED`;
- fremde Origin, unbekannten WSS-Pfad und abgelehnten Upgrade auf dem
  HTTPS-only-Uvicorn-Port.

Im Core-Report sind `worker_fn02.enabled=false`,
`cross_worker.executed=false`, `cross_worker.skipped=true` und
`cross_worker.tested=false` gesetzt. Das ist kein Cross-Worker-PASS.

## Profil `full-lab`

Zusätzlich sind sechs operatorseitig validierte Werte/Dateien zwingend:

| Variable | Inhalt |
|---|---|
| `E2E_BROKER_INTROSPECTION_PROOF` | frischer, autoritativer, Ed25519-signierter und gegen den gepinnten Authority-Key geprüfter exklusiver Session-Reservation-Proof für Client-ID **und** Zertifikatsidentität; aktive Session false, Run-Bindung, Ablauf und ACL-DENY-Proben zwingend |
| `E2E_BROKER_RELEASE_PROOF` | nach Stop und Heartbeat-Staleness neu erzeugter autoritativer Freigabe-Proof derselben Reservation |
| `E2E_BROKER_PROOF_PUBLIC_KEY` | Ed25519-Public-Key der Broker-Management-/Proof-Authority |
| `E2E_BROKER_PROOF_KEY_SHA256` | 64 lowercase Hexzeichen; vorab über einen vom Proof getrennten vertrauenswürdigen Kanal gepinnter SHA-256 der Public-Key-Dateibytes |
| `E2E_FN02_RUNTIME_CONFIG` | MQTT-Host/-Port und Pfade zur vorhandenen fn-02-Lab-CA/-Cert/-Key-Fixture |
| `E2E_WORKER_LAUNCHER_COMMAND_FILE` | JSON-Array eines argv-Prefixes für den Launcher auf dem Workerhost |

Beispiele liegen unter `fixtures/`. Die Broker-Datei ist nur ein
Formatbeispiel: Timestamp und Managementquelle müssen unmittelbar vor dem
Lauf aus einer echten Broker-Managementquelle stammen. Mosquitto-`$SYS`-
Aggregatzähler oder eine unvollständige/rotierte Logdatei sind **nicht**
autoritative Session-Introspektion. Fehlt diese Fähigkeit, bleibt `full-lab`
korrekt `BLOCKED`; der Simulator verdrängt niemals einen Client mit gleicher
ID.

Beispiel:

> Das Standard-Validation-Image enthält bewusst keinen SSH-Client und bindet
> weder Host-SSH-Agent noch Host-SSH-Konfiguration ein. Das mitgelieferte
> `ssh -T`-Launcherbeispiel ist daher nur ein Formatbeispiel. Ein Full-Lab-Lauf
> über SSH bleibt im Standardcontainer korrekt **BLOCKED**, bis der Betreiber
> einen separat kontrollierten externen Launchertransport bereitstellt. Für
> den schnellen, containerisierten Release-Test ist ausschließlich `core`
> vorgesehen; das Validation-Image wird dafür nicht um SSH erweitert.

```bash
cd "$HOME/automation_control_plane_release_v0026_r1"
result_dir="$PWD/integration_test/e2e_v0025/results"
install -d -m 0700 "$result_dir"
unset E2E_CA_FILE
export E2E_RUN_ID="e2e-v0025-$(date -u +%Y%m%dT%H%M%SZ)"
export E2E_BROKER_INTROSPECTION_PROOF="$result_dir/broker-proof.json"
export E2E_BROKER_RELEASE_PROOF="$result_dir/broker-release-proof.json"
export E2E_BROKER_PROOF_PUBLIC_KEY="$result_dir/broker-proof-authority.pub.pem"
export E2E_BROKER_PROOF_KEY_SHA256="$(tr -d '\n' </run/operator-secrets/broker-proof-key.sha256)"
export E2E_FN02_RUNTIME_CONFIG="$PWD/integration_test/e2e_v0025/fixtures/fn02_runtime_config.example.json"
export E2E_WORKER_LAUNCHER_COMMAND_FILE="$PWD/integration_test/e2e_v0025/fixtures/worker_launcher_command.example.json"
export E2E_PROXY_LOG_SOURCE="$result_dir/proxy-v0025.log"
export E2E_PROTOCOL_LOG_SOURCE="$result_dir/protocol-v0025.log"
E2E_CONFIRM_MUTATIONS=1 E2E_PROFILE=full-lab \
  ./integration_test/e2e_v0025/run_e2e.sh
```

Die Run-ID muss vor Erzeugung des Reservation-Proofs feststehen. Beide Proofs
sind Ed25519-signiert. Signiert werden die UTF-8-Bytes des Proof-JSON ohne das
Feld `signature`, mit `ensure_ascii=false`, verbotenen NaN-Werten,
lexikografisch sortierten Schlüsseln und den JSON-Separatoren `,` und `:` ohne
Whitespace. Die Signatur ist Base64. Reservation-ID, Run-ID, Quelle,
Ablaufzeit und gepinnter Signer müssen im Release-Proof exakt wiederkehren.
`E2E_FN02_HEARTBEAT_STALE_S` ist optional (Standard `45`, zulässig `30..300`);
bei vorhandener Registryzeile wird die Freigabe erst nach `online=false`,
`MAINTENANCE` und nachgewiesener Heartbeat-Staleness akzeptiert.

Die beiden Pflichtlogs müssen von vor dem E2E gestarteten Live-Followern
stammen. Leere Dateien, `/dev/null`, Aliase/Hardlinks sowie Ergebnis-, Log- und
Recovery-Dateien mit identischem Ziel werden fail-closed abgewiesen. SEC-009
scannt die beim Start frischen, nicht leeren Captures; die korrekte
Follower-Provenienz bleibt zusätzlich ein operatives Rollout-Gate. Weil die
Produktionslogger absichtlich still sein können, ist Dateiwachstum keine
fachliche PASS-Bedingung.

Ein extern bereitgestelltes argv-Prefix kann lokal oder über ein dort
vorhandenes `ssh -T` aufgerufen werden; der Standard-Validationcontainer stellt
SSH selbst nicht bereit. Der E2E hängt nur
`--action preflight|start|restore` und die Run-ID an. Der Launcherplan auf dem
Workerhost:

- hasht das separat bereitgestellte, vollständig manifestierte
  Worker-v0101-Archiv gegen
  `c1e62ead807817930cd8323852e0ba421d6d70baa336b37f89d86692f1939467`;
- hasht die originale `app_config.yaml` vor und nach dem Lauf;
- stoppt den normalen Prozess über ein argv-Array ohne Shellauswertung;
- erzeugt außerhalb des Worker-Roots eine private Overlaykopie mit nur
  `profiles.worker_simulation.cross_runtime.enabled=true`;
- setzt vor dem Runtimeimport den vorhandenen `default_config_path` auf das
  Overlay, validiert die Umgebung und startet den unveränderten Worker;
- stoppt den Testprozess, entfernt Overlay/Wrapper, startet den normalen
  Worker und bestätigt beide Originalhashes.

Der fn-02-Simulator verwendet ausschließlich die vorhandene Identität
`urn:automation:site-main:worker:fn-02`, publiziert ein live/nicht-retained
Heartbeat als Broker-Identitätsnachweis und verwendet außen
`automation.mqtt.v1`. Full-/Patch-/Actuator-Payloads verwenden innen
`automation.distributed.v2` mit `contracts_version=2.0`. Bei einem
Actuator-Execute sendet er sowohl den korrelierten ACK als auch ein separates
`distributed.actuator.result` mit `simulation=true`.

Die Cross-Fixture setzt Sensor 3 über den freigegebenen Modbus-Simulatorpfad
kontrolliert auf `74`, anschließend auf `76` und stellt danach den zuvor
gelesenen Originalwert wieder her. Der erwartete erste Fresh-Lab-Residual-
zustand ist eine `APPROVED`/`MAINTENANCE`, heartbeat-stale/offline fn-02-
Registryzeile. Sie wird nicht per direktem SQL gelöscht.

## Ergebnis, Exitcodes und Recovery

Standardpfade unter `integration_test/e2e_v0025/results/`:

- `e2e-v0025-result.json` – maschinenlesbares Urteil;
- `e2e-v0025.jsonl` – redigiertes Ablaufprotokoll;
- `e2e-v0025-recovery.json` – privates Recovery-Journal, Modus `0600`.

Sie lassen sich mit `E2E_RESULT_DIR`, `E2E_RESULT_FILE`, `E2E_LOG_FILE` und
`E2E_RECOVERY_FILE` verlegen. Die drei Einzeldateien müssen dabei innerhalb
des kanonischen `E2E_RESULT_DIR` bleiben. Ergebnis, Ablaufprotokoll und
Recovery-Journal müssen drei getrennte reguläre Dateien bleiben und dürfen
auch keine Pflicht- oder Zusatzlogquelle aliasieren. Status und Exitcode:

| Status | Exitcode | Bedeutung |
|---|---:|---|
| `PASSED` | 0 | alle aktivierten Gates und Restores bestätigt |
| `FAILED` | 1 | fachlicher Test oder Restore fehlgeschlagen |
| `BLOCKED` | 3 | sichere Vorbedingung fehlt; keine falsche Fachbehauptung |
| `SKIPPED` | 4 | eigenständig aufgerufenes optionales Teilmodul wurde nicht ausgeführt |

Vor der ersten Mutation wird das Journal atomar mit `O_EXCL` und Modus `0600`
angelegt. Es enthält Revisionen, IDs, Originalwerte und Hashes, aber keine
Passwörter, Token, Cookies, CSRF-Werte oder Private Keys. Eine offene oder
fehlgeschlagene Recovery-Datei wird beim nächsten Lauf mit identischem Profil,
Site-, Node- und Fixture-Binding vor jeder neuen Mutation automatisch
aufgenommen. Der Runner rekonstruiert Config-/Node-Replica-, Aktor-, Token-,
Sensor-, Simulator- und Launcher-Verpflichtungen und startet erst nach
bestätigtem Cleanup einen neuen Run. Weicht ein Binding ab oder lässt sich ein
Endzustand nicht beweisen, endet die Recovery fail-closed. Die Datei nie blind
löschen oder manuell als `COMPLETED` umetikettieren.

Ein Gesamtstatus `PASSED` wird erst nach einem internen Vollständigkeitsgate
gesetzt: alle profilabhängigen Pflicht-Check-IDs müssen vorhanden und grün
sein, nur die dokumentierten profil-/authabhängigen `SKIPPED`-Fälle sind
zulässig und Config-/Aktor-Restore sowie die Cross-/Lock-Invarianten müssen
explizit bestätigt sein.

## Verpflichtende isolierte E2E-Komponentengates

`./test_static.sh` führt zusätzlich `isolated_component_gates.py` aus. Ein
Fehler in einem dieser Gates beendet das Static-Gate ungleich null; die vier
IDs werden deshalb nicht durch einen bloßen Reporteintrag oder eine
Quelltextsuche als bestanden gewertet.

| Gate | Reale funktionale Prüfung | Bewusste Systemgrenze |
|---|---|---|
| `XWK-004` | Der produktive Master-Operation-Core erzeugt den gültigen v2-Aktorcommand; der fn-02-Protokollaktor akzeptiert den Kontrollfall, lehnt danach einen alten Authority-Term und eine alte Node-Execution-Epoch ohne Aktor-Trace oder Zustandsänderung ab. Eine alte Cross-Epoch wird bereits vor dem Master-Store fail-closed abgelehnt. | In-Process, ohne MQTT-Verbindung und ohne gefälschte fn-01-Brokeridentität; kein Live-Cluster-Gate. |
| `XWK-005` | Derselbe vom Master-Core erzeugte Command wird zweimal mit identischer Message-ID verarbeitet; Resultat und Dedupe-Eintrag bleiben identisch, während genau ein Aktor-Trace und genau eine Ausführung entstehen. | In-Process-Protokolltest; kein Live-Broker-Dedupe-Nachweis. |
| `XWK-006` | Eine abgelaufene Control-Envelope erreicht den Master-Store nicht. Ein abgelaufener MQTT-Aktorcommand erreicht weder Dedupe noch Execute/Trace; ein frischer Kontrollcommand derselben Form wird zuvor erfolgreich ausgeführt. | In-Process mit realem Master-Core und Protokollaktor; keine Manipulation der Lab-Uhr. |
| `PAR-002` | Eine temporäre Instanz des produktiven direkten `websockets`-Proxy-Listeners verwendet eine temporäre CA-signierte TLS-Identität und `max_connections=2`. Zwei echte WSS-Clients bleiben verbunden, der dritte TLS-WSS-Upgrade erhält `503`; Client-, Reservation- und Task-Zähler wachsen nicht und ein frei gewordener Slot ist erneut nutzbar. | Loopback und temporäre Test-PKI; getrennt vom laufenden Proxy und daher kein Live-Cluster-Lasttest. |

Diese isolierten Gates sind notwendiger Bestandteil des Release-Static-Gates,
aber keine Check-IDs des `core`-/`full-lab`-JSON-Reports. Der Live-Lauf
behauptet sie nicht erneut; umgekehrt ersetzen sie weder `PAR-001` noch
`XWK-001` bis `XWK-003` oder den Cleanup-Nachweis `XWK-007`.

## Einzelne Offline-Prüfungen

```bash
cd "$HOME/automation_control_plane_release_v0026_r1/integration_test/e2e_v0025"
python3 control_plane_wss_e2e_test.py --self-test
python3 fn02_protocol_simulator.py --self-test
python3 isolated_component_gates.py
python3 worker_cross_test_launcher.py --self-test
../../tools/test_worker_auto_admission_static.py
```

Diese Prüfungen mutieren keine laufende Control Plane und behaupten keinen
Running-E2E-PASS.

## Expliziter File-Routenvertrag

Seit `0.25.0-e2e.3` enthält jedes der sechs File-E2E-Phasentupel neben Phase
und `worker_node_local`-Scope auch die erwartete Route. Runtime-Änderungen
verwenden `action_plan`; strukturelle Änderungen und der Policy-Fallback
verwenden `scope_operations`. `_transition` akzeptiert keinen impliziten
Default: Eine fehlende, leere oder unbekannte Route beendet den Test
fail-closed, bevor ein Beleg falsch interpretiert werden kann.

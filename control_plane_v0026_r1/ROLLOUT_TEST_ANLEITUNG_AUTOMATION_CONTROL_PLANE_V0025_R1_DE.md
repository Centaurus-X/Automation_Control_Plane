# Fresh-Rollout und Abnahme – Control Plane v0025-r1

## Getrennte Artefakte

Der Rollout verwendet zwei voneinander unabhängige Archive:

- `automation_system_v0101.zip` plus `.sha256`,
- `automation_control_plane_release_v0025_r1.zip` plus `.sha256`.

Der Worker-Quellbaum darf nicht in das Control-Plane-Archiv eingebettet sein.
Das Control-Plane-Manifest pinnt stattdessen den exakten Worker-ZIP- und
Worker-Manifest-Hash.

## Lokale Vorprüfung

```sh
cd automation_system_v0101
python3 _tools/build_release_archive.py

cd ../automation_control_plane_release_v0025_r1
sh tools/validate_release.sh
python3 tools/build_release_archive.py
```

## Fresh-Rollout

Zugangsdaten werden nur aus der externen, nicht paketierten Testsystemdatei
geladen. Sie dürfen weder in Logs noch in Archive oder Manifeste gelangen.

```sh
cd automation_control_plane_release_v0025_r1
sh ./rollout_automation.sh \
  --release v0025-r1 \
  --archive ../automation_control_plane_release_v0025_r1.zip \
  --worker-archive ../automation_system_v0101.zip \
  --fresh
```

Vor dem Start lässt sich die deterministische Releaseauswahl prüfen:

```sh
sh ./rollout_automation.sh --list-releases
```

Für einen bereits validierten, unveränderten Laufzeitstand steht der explizite
Modus `--reuse` zur Verfügung. Beim frischen Rollout wird das separate
`automation_system_v0101.zip` übertragen. Der aktive E2E-Satz wird
ausschließlich über `integration_test/CURRENT_E2E` ausgewählt; das maschinelle
Endergebnis wird credential-frei in `summary.json` protokolliert.

## Abnahmekriterien

1. Master, Proxy, Broker und Datenbank melden Readiness.
2. Worker meldet exakt `automation_system_v0101` und `node_local`.
3. Topics und Zertifikatsidentitäten verwenden den Deployment-Default
   `site-main`.
4. Registry- und Mirror-Tupel konvergieren exakt mit dem Worker-Head.
5. Ein kanonisches Proposal wird akzeptiert; Legacy-Form wird als
   `unsupported_operation_format` abgewiesen.
6. Neustart von Worker und Master erhält Revision, Epoch, Journal und Mirror.
7. Cross-Owner-Schreibzugriff ohne gültigen Activation-Proof bleibt gesperrt.
8. Installationsverzeichnisse und Archive von Worker und Control Plane bleiben
   getrennt.

## Rückfall

Ein Rollback vor dem ersten node-autoritativen Commit darf den Featurepfad
deaktivieren. Danach ist ein Rückwechsel zu `master_global` nur über das
kontrollierte A5-Transferprotokoll zulässig; ein Dateiaustausch allein ist kein
gültiger Authority-Transfer.

## Verbindliche Journal-Regel für Fresh und Reuse

- `--fresh`: Control-Plane-Registry und Worker-Replica-Baseline werden als
  gemeinsamer Release-Satz neu aufgebaut; `RESET=yes` ist hier zulässig.
- `--reuse` oder reiner Worker-Restart: Das Node-local-Replica-Journal muss
  erhalten bleiben; der Lifecycle wird mit `RESET=no` ausgeführt.
- Ein verlorenes Journal bei bereits vorhandener Registry wird nicht durch
  direkte SQLite-Änderungen oder eine implizite Revisionserhöhung repariert.
  Erforderlich ist der explizite Restore-/Authority-Transition-Pfad oder ein
  vollständig neuer Fresh-Rollout.

Das Auto-Admission-Gate muss neben `APPROVED/ACTIVE` auch Revision,
Fingerprint, Owner/Quelle, Authority-/Execution-Epoch und einen persistierten
exakten Mirror-Snapshot bestätigen. `DEGRADED` oder `stale_revision` beendet
die Abnahme fail-closed.

## Zusätzlicher Authority-V5-Dateibeweis

Der Lab-File-E2E führt sechs lückenlos aufeinanderfolgende lokale
Scope-Revisionen aus: Policy-Fallback hin/zurück, strukturelle Änderung
hin/zurück und Runtime-Overlay hin/zurück. Für jede Revision müssen
File-WAL-`last_operation`, SEM-Ergebnis, Authority-Journal, Scope-Head,
Mirror-Outbox und exaktes Mirror-ACK dieselbe Operation binden. Der Master darf
weder eine Node-Revision vergeben noch einen Config-Commit oder eine
authoritative Outbox für diese Übergänge erzeugen.

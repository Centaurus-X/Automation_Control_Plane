# Release-Validierung v0027-r2

Die tatsächlich ausgeführten Prüfungen, Ergebnisse, optionalen Skips und noch
ausstehenden Zielumgebungs-Gates stehen maschinenlesbar in
`VALIDATION_RESULTS_V0027_R2.json`.

## Automatischer Einstieg

```sh
./test_release.sh
```

Optional werden zusätzliche externe Gates aktiviert:

```sh
AUTOMATION_RUNTIME_ARCHIVE=/path/automation_system_v0101.zip \
AUTOMATION_RUNTIME_TREE=/path/automation_system_v0101 \
RUN_PRESERVED_CONTROL_TESTS=1 \
./test_release.sh
```

## Enthaltene Gates

- Releaseidentität und Zwei-Archiv-Grenze;
- byteidentische v0026-r1-Control-Plane;
- vollständige interne und äußere SHA-256-Manifeste;
- funktionaler Python-Codestandard;
- JSON-/Shell-/JavaScript-Syntax;
- Web-CSP-/DOM-Sicherheitsprüfung;
- Helm-Security- und RBAC-Policy;
- vendored Umbrella-Dependency-Gleichheit;
- Planner-/DAG-/Policy-/Secret-/Digest-Regressionssuite;
- Journal-/Fencing-/Audit-Regressionssuite;
- Providervertrag und asynchroner API-Rollout;
- eigenständiger Dienst-Lifecycle;
- optional Helm Lint/Template, Runtime- und Baseline-Tests.

## Build des finalen Archivs

```sh
python3 kubernetes_orchestration/tools/build_release.py \
  --root . \
  --output ../automation_control_plane_release_v0027_r2.zip
```

Der Build vendorisiert die Helm-Dependencies, erzeugt den vollständigen
File-Audit, schreibt `SHA256SUMS`, validiert den fertigen Baum erneut und baut
ein ZIP mit stabilen Zeitstempeln und Dateimodi. Ein SHA-256-Sidecar wird neben
dem Archiv erzeugt.

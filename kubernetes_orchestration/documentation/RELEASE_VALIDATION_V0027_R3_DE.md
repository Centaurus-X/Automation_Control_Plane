# Release-Validierung v0027-r3

## Automatisierte Gates

`scripts/test.sh` führt fail-closed aus:

- Byteidentität und internen Manifestabgleich der 588 erhaltenen v0026-r1-Dateien;
- Release-, Runtime- und Architekturplan-Digestbindungen;
- Python-AST-Regeln (keine benutzerdefinierten Klassen, Decorator, Lambdas,
  `eval`, `exec` oder `shell=True`);
- JSON-, Shell-, Web-CSP-/DOM- und Helm-Sicherheitsprüfung;
- Chart-/Descriptor-/Catalog-/Runtime-Grenzen und vendorte Umbrella-Identität;
- Planner-, Placement-, Journal-, API-, CSRF-, Rollen-, Wave-, Pause-, Agent-,
  GitOps-, Provider-, Rollback-, Audit- und Lifecycle-Tests;
- JavaScript-Syntax;
- optional Helm lint/template, Runtime-Archivtest, alle Runtime-Tests und
  Docker-abhängige erhaltene Control-Plane-Tests, sofern die Tools/Gates
  explizit bereitstehen.

## Reproduzierbarkeit

`tools/build_release.py` normalisiert ZIP-Zeitstempel auf 1980-01-01,
sortiert alle Pfade, übernimmt nur Dateimodi und verwendet eine feste
Kompressionsstufe. Vor dem Archivieren werden Umbrella-Dependencies neu
vendort, der vollständige File Audit erzeugt, alle Dateien in `SHA256SUMS`
gebunden und der Verifier erneut ausgeführt. Zwei unabhängige Builds müssen
denselben ZIP-Digest ergeben.

## Ergebnisprotokoll

Die finalen Zählwerte, Eingangs-Digests und tatsächlich ausgeführten
beziehungsweise wegen fehlender externer Infrastruktur übersprungenen Gates
stehen in `VALIDATION_RESULTS_V0027_R3.json`. Der Digest des äußeren Archivs
liegt wegen der unvermeidlichen Selbstreferenz ausschließlich im gleichnamigen
`.sha256`-Sidecar. Ein SKIP ist kein PASS und bleibt auch im Übergabehinweis
sichtbar.

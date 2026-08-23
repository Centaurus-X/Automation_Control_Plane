# Release-Validierung v0028-r1

## Automatisierte Gates

`scripts/test.sh` prüft fail-closed:

- Root-Identität `automation_control_plane_release_v0028_r1`, Version `0.28.0`
  und Revision `v0028-r1`;
- Parent-, Control-Plane-, Runtime- und Architekturplan-Digestbindungen;
- Byteidentität aller 588 erhaltenen `v0026-r1`-Dateien;
- funktionalen Python-Code ohne benutzerdefinierte Klassen, Decorator,
  Lambdas, `eval`, `exec` oder `shell=True`;
- JSON-, statische YAML-, Shell-, JavaScript-, CSP-/DOM- und
  Helm-Sicherheitsverträge;
- Identität von Source-Charts und vendorten Umbrella-Dependencies;
- Planner, Placement, Plan v2, neun Saga-Schritte, Journal, Approval,
  Pause/Resume, Agent-Lease/Fencing, Helm/GitOps-Provider, API, Audit,
  Evidence und Lifecycle;
- `helm lint --strict` und `helm template` für neun Einzelcharts und das
  Umbrella-Chart, sofern Helm im Testsystem verfügbar ist.

## Reproduzierbarkeit

`tools/build_release.py` sortiert alle Pfade, setzt ZIP-Zeitstempel auf
`1980-01-01T00:00:00`, verwendet eine feste Deflate-Kompressionsstufe, bindet
alle Dateien in `SHA256SUMS` und erzeugt einen externen SHA-256-Sidecar. Zwei
unabhängige Builds aus demselben Release-Baum müssen denselben Archiv-Digest
ergeben.

## Produktionsgrenze

Ein erfolgreiches Quell- und Chart-Gate ersetzt keine reale Zielabnahme.
Registry, Kubernetes API, CNI, Storage, PKI, PostgreSQL, Argo CD/Flux,
Image-Signaturen, Backup/Restore sowie Failure- und Recovery-Tests bleiben in
der vorgesehenen Infrastruktur verpflichtend. Übersprungene Gates werden als
`SKIP`, niemals als `PASS`, protokolliert.

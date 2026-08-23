# Automation Control Plane 0.28.0 / v0028-r1

## Zweck des Releases

`v0028-r1` überführt den nach der Veröffentlichung von `v0027-r3`
diagnostizierten und unter Linux verifizierten Helm-Patch in ein vollständiges,
eigenständig prüfbares Release-Paket. Die fachliche Architektur, die
Komponentenlandschaft und die Authority-Grenzen bleiben erhalten.

## Technische Änderungen

- Die `required`-Ausdrücke der Image-Referenzen in den Master- und
  MQTT-Workloads verwenden Helm-kompatible Template-Quotierung.
- Alle neun strikten Subchart-Schemas akzeptieren Helms reservierten
  `global`-Wertebaum, ohne ihre übrige `additionalProperties: false`-Grenze
  aufzuweichen.
- Source-Charts und vendorte Kopien des Umbrella-Charts werden vor Prüfung und
  Archivierung deterministisch synchronisiert.
- Manifest, File Audit und Sidecar verwenden explizit LF-Zeilenenden und sind
  dadurch mit GNU `sha256sum -c` unter Linux direkt prüfbar.
- Release-, OCI-, Chart-, Fleet-, Katalog-, Beispiel-, SBOM-, Provenance- und
  Audit-Identitäten wurden konsistent auf `0.28.0 / v0028-r1` angehoben.
- Das Parent-Release ist an
  `automation_control_plane_release_v0027_r3.zip` mit SHA-256
  `eb7b3ed52273dc6cdd6dd22516bc50cb516ccee58de2d11697fb074d8196a971`
  gebunden.

## Beibehaltene Komponenten

- byteidentische eingebettete Control Plane `v0026-r1` mit Proxy, Master,
  MQTTS, PostgreSQL-Integration und MCP Controller;
- Automation Fleet mit Planner, Placement, Plan v2, Journal, neun
  Saga-Schritten, Approval, Evidence und Audit-Hashkette;
- direkte Helm-, Argo-CD- und Flux-Provider;
- outbound-only Cluster-Agent mit Kubernetes-Lease, zentraler Epoche, HMAC
  und Fencing;
- Soft-Runtime-/Simulationspfad und externe, digestgebundene
  `automation_system_v0101`-Runtime;
- Observability, Web-UI, CLI, Compose-, systemd-, Image- und
  Kubernetes-Betriebsartefakte;
- neun einzeln installierbare Helm-Charts und ein optionales Umbrella-Chart.

## Kompatibilität und Migration

Die Änderung modifiziert keine Control-Plane-Domainverträge und verschiebt
keine Hardware-, Safety- oder SEM-Authority. Bestehende `v0027-r3`-Inventare,
Policies und Desired-State-Dateien können übernommen werden; der
`release_ref` und optionale Field-Manager sollten auf `v0028-r1` aktualisiert
werden. Bereits erzeugte Pläne bleiben absichtlich an ihre alte Release-
Identität gebunden und müssen für `v0028-r1` neu erzeugt und freigegeben werden.

## Reifegrad

Der Paketstatus lautet `PASS_WITH_EXTERNAL_GATES_OPEN`. Integrität,
Byteidentität, funktionale Tests, statische Sicherheitsgrenzen, Helm
lint/template und deterministische Archivierung werden geprüft. Eine
Produktionsfreigabe benötigt weiterhin die reale Zielabnahme für Registry,
Kubernetes API, CNI, Storage, PKI, PostgreSQL, GitOps, Image Supply Chain sowie
Fehler- und Recovery-Szenarien.

## Integrität

`SHA256SUMS` bindet jede Datei innerhalb des Release-Verzeichnisses. Der
Digest des äußeren ZIP-Archivs steht ausschließlich im gleichnamigen
`.zip.sha256`-Sidecar, um Selbstreferenz zu vermeiden.

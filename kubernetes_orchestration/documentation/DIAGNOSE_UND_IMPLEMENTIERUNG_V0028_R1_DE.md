# Tiefe Diagnose und Implementierung v0028-r1

## Ausgangslage

Die vollständige Linux-Verifikation von `v0027-r3` bestätigte sämtliche
statischen, funktionalen und Integritätsprüfungen, deckte jedoch zwei erst im
realen Helm-Parser sichtbare Portabilitätsprobleme auf. Beide Fehler lagen in
der Deploymentdarstellung, nicht in Planner, Saga, Agentenprotokoll,
Control-Plane-Kommunikation oder Worker-Authority.

## Befund 1: Template-Quotierung

Master und MQTT verwendeten in doppelt gequoteten YAML-Scalars mit Backslash
maskierte Anführungszeichen innerhalb von Helm-Actions. Helm parst den
Template-Ausdruck vor YAML und interpretierte den Backslash als unerwarteten
Operator. Die korrigierte Form lässt die Stringliterale innerhalb der
`required`-Action unverändert und quotiert ausschließlich den gerenderten
YAML-Wert außen.

Betroffen sind:

- `automation-master/templates/migration-job.yaml`;
- `automation-mqtt/templates/statefulset.yaml`;
- die deterministisch vendorten Kopien beider Dateien im Umbrella-Chart.

## Befund 2: reservierter global-Wertebaum

Helm reicht den reservierten Wertebaum `global` an aktive Subcharts weiter.
Die neun Subcharts verwenden bewusst strikte JSON-Schemas mit
`additionalProperties: false`. Ohne explizite `global`-Eigenschaft wurde daher
bereits die standardmäßig aktive PostgreSQL-Integration beim Umbrella-Lint
abgewiesen; nach Aktivierung wären dieselben Fehler sukzessive bei den übrigen
Subcharts aufgetreten.

Die Lösung erlaubt ausschließlich für `global` ein freies Objekt. Alle
fachlichen Chart-Properties bleiben weiterhin strikt, typisiert und gegen
unbekannte Felder geschlossen. Die Änderung wurde in allen neun Source-
Schemas vorgenommen; der Release-Builder erzeugt daraus die vendorten Kopien.

## Architekturwirkung

Die Korrektur verändert keine Laufzeit- oder Authority-Grenze:

- Fleet bleibt Management- und Deployment-Plane außerhalb des Steuerpfades.
- Genau ein Write Owner bleibt je Ziel zulässig.
- Master, Proxy, MQTT, MCP und PostgreSQL behalten ihre bisherigen Rollen.
- Der owning Worker bleibt alleiniger Besitzer von SEM, Safety, Feldbus und
  finaler Aktuatorentscheidung.
- Kubernetes Hard-RT bleibt ohne gesonderte Qualifikation ausgeschlossen.

## Release-Härtung

`v0028-r1` hebt alle aktiven Identitäten auf `0.28.0`, bindet das unveränderte
Parent-Archiv kryptographisch, synchronisiert Source- und Umbrella-Charts,
erzeugt einen vollständigen File Audit und verifiziert das innere
`SHA256SUMS` vor der deterministischen ZIP-Ausgabe. Historische
`v0027-r3`-Diagnosen bleiben als Nachweis der Entwicklungslinie enthalten.

## Ergebnis

Alle zehn Charts – neun einzeln installierbare Komponenten und das
Umbrella-Chart – müssen `helm lint --strict` und `helm template` bestehen.
Zusätzlich bleiben Byteidentität, JSON/YAML/Shell/JavaScript-Prüfung,
funktionaler Python-Code-Standard, Planner-, Provider-, Agent-, API-,
Approval-, Journal-, Evidence- und Lifecycle-Tests fail-closed aktiv.

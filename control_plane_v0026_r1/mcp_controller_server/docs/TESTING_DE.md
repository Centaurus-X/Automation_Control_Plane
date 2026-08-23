# Tests und Verifikation

## Vollständiger Lauf

```bash
CONTROL_PLANE_REFERENCE_ROOT=/path/to/automation_control_plane_release_v0026_r1 \
  ./test_all.sh
```

Ohne `CONTROL_PLANE_REFERENCE_ROOT` läuft das Package eigenständig; nur der
zusätzliche Cross-Source-Vergleich wird mit einer sichtbaren SKIP-Meldung
übersprungen.

## Gates

| Gate | Inhalt |
|---|---|
| Functional Python | AST-Prüfung: keine Klassen, Lambdas, Decorators, Typannotationen oder verbotenen Imports |
| Struktur | Pflichtdateien, JSON, keine Symlinks/Secrets/Bytecode, exakt gepinnte Runtime-Abhängigkeiten |
| Plattformgrenze | kein PostgreSQL-/ORM-Import, Proxy-Control-Grenze, mTLS und externe Freigabe als Defaults |
| Konfiguration | unbekannte Felder, unsicheres MQTT und falsche Rollen fail-closed |
| Auth | Bearer-Prüfung, Principal und begrenztes Rate Limit |
| Automation-Vertrag | Topics, Envelope, TTL, Identität, Payload und Node-Local-Revisionen |
| Approval | Prepare, Digest-Mismatch, externe Freigabe, Commit und Replay ohne Doppel-Publish |
| MQTT | echte Bridge-Threads mit Fake-Transport und korreliertem Application-ACK |
| MCP | moderne Discovery/Metadaten und Legacy-Initialize/Notifications |
| HTTP | Bearer, Origin und Header-/Body-Bindung über echte ASGI-Events |
| Cache | Update-Reihenfolge und bounded latest-sensor semantics |
| Referenz | Topic-, Envelope-, Payload- und MQTT-Client-Vergleich mit dem gelieferten Master-Quellstand |
| Shell | `bash -n` für alle Lifecycle-Skripte |
| Packaging | Datei-Hashes, deterministischer ZIP-Build, CRC, sichere Pfade, keine Symlinks |


## Bundle-Integrationsgates

Im zusammengeführten Control-Plane-Release werden zusätzlich die Grenzen zwischen
MCP, MQTT, Master und Administration geprüft:

```bash
cd /path/to/automation_control_plane_release_v0026_r1
python3 tools/test_mcp_bundle_integration.py
python3 tools/test_administration_release_set.py
```

Das erste Gate validiert insbesondere die dedizierte MQTT-Identität, ACL-Grenzen,
Master-Allowlist, externen Auth-Pfad, Compose-Härtung und die feste Reihenfolge
`database -> mqtt -> master -> proxy -> mcp_controller_server -> worker`. Das
zweite Gate prüft Manifest, Registry, Inventory und Adapterbindung des gesamten
Release-Sets.

## Baseline der gelieferten Systeme

Die unveränderten Quellarchive werden separat geprüft, damit ein bereits
vorhandener Fehler nicht der neuen Komponente zugerechnet wird. Der Worker-Lauf
verwendet dessen eigenes `test_all.sh`; die Control Plane deren
`test_static_all.sh` mit einem expliziten beschreibbaren `TMPDIR`.

Netzwerk-, Docker- und Hardwaretests können in der Build-Umgebung bewusst nicht
ausgeführt werden. Das Package trennt daher dependency-freie Vertrags-/ASGI-Tests
von `scripts/preflight.sh` und dem Live-Readiness-Gate auf dem Zielhost.

## Release-Beweis

Der konkrete Lauf mit Datum und Ergebnissen steht in `TEST_RESULTS.md`.
`SHA256SUMS` schützt jede enthaltene Datei. Der Builder erzeugt das ZIP zweimal
und verlangt byteidentische Ergebnisse; dessen äußerer SHA-256 wird beim Build
ausgegeben und zusammen mit dem Download übergeben.

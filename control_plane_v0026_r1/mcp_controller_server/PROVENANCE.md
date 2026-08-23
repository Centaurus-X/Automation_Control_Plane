# Provenance

Die Komponente wurde am 2026-08-12 gegen die vom Benutzer bereitgestellten
Archive `automation_control_plane_release_v0025_r1(3).zip` und
`automation_system_v0101(3).zip` erstellt.

`src/mcp_controller_server/_lightweight_mqtt_client.py` wurde byteidentisch aus
dem gelieferten Master-Package übernommen, damit Transportverhalten und TLS-
Semantik nicht divergieren. SHA-256:

```text
8e8a5a8bb86b24dbff5de80667af9fcc054572901a6a31dfd6d46b9203379e33
```

Der Control-Envelope-/Topic-/Payload-Subset wurde unabhängig gekapselt und im
Release-Test gegen die ausführbaren Referenzfunktionen des gelieferten Masters
verglichen. Es wurden keine Dateien in den beiden Quellarchiven verändert.

## Integration in Automation Control Plane v0026-r1

Am 2026-08-15 wurden MCP-Fachkern, Protokollverträge und Tests aus dem
verifizierten Standalone-Release additiv mit Control Plane v0025-r1 zum
Bundle v0026-r1 zusammengeführt. Integrationsflächen für Deployment, PKI,
ACL, Lifecycle, Administration und Dokumentation wurden kontrolliert
erweitert; der MCP-Fachkern wurde nicht durch eine alternative
Implementierung ersetzt. Der
äußere SHA-256 des bereitgestellten MCP-Archivs war:

```text
16c42b77b9583ced7287a8c3138567f124e63864b9e9afdd962570fd911c202b
```

Die Integration ergänzt dedizierte PKI-/ACL-/Master-Identität,
Docker-Compose-Lifecycle, externes Auth-Secrets-Handling, zentrale
Administration/Orchestration sowie bundleweite statische und vertragliche Gates.
Die MCP-Komponentenversion bleibt `0.1.0 / v0001-r1`; die Integrationsversion des
Gesamtbundles ist `0.26.0 / v0026-r1`.

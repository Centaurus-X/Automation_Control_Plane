# Release Notes v0020-r4

`automation_control_plane_release_v0020_r4` verankert ausschließlich
`automation_system_v0096`.

## Delta zu v0020-r3

- Worker-Artefakt, Lifecycle, Inventar, Rollout und E2E sind vollständig auf
  `automation_system_v0096` gepinnt.
- Der aktive E2E-Vertrag ist `0.20.0-e2e.4`.
- Master, Proxy und `contracts_src` enthalten denselben erweiterten V9-Vertrag
  für `plc_s7`, Stringadressen sowie `rack`, `slot` und Batchparameter.
- Cross-Scope-I/O bleibt hardwarefrei; die Modbus-/S7-Union wird im
  Cross-Profil rekursiv auf die logischen Identitätsfelder reduziert.
- Der Release-Builder akzeptiert ausschließlich das exakte sibling-Artefakt
  `automation_system_v0096.zip` und dessen kanonisches internes Manifest.
- Fresh-, Upgrade-, Remote-Lifecycle- und Worker-Launcher-Pfade erwarten
  ausschließlich Version v0096.
- Die statischen, isolierten und vollständigen E2E-Gates prüfen den neuen
  Worker- und Contract-Anker.

## Übernommene Sicherheitsgrenzen

Der bestehende Config-File-Policy-Fallback und der Master-vs.-Local-
Präzedenzfall bleiben aktiv. Ein Authority-Fallback ist nur für die bereits
freigegebenen Policy-Codes zulässig. Safety-Verstöße, technische Fehler,
abweichende Hashes oder fremde Worker-Versionen bleiben fail-closed.

S7-Zugangsdaten sind kein Bestandteil von Fachkonfiguration, Control Plane oder
Release-Artefakten. Die konkrete PLC-Kommissionierung verbleibt beim Worker-
Hardware-Owner.

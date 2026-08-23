# E2E v0025

Die vollständige deutsche Bedien-, Sicherheits- und Recovery-Dokumentation
steht in [README_E2E_V0025_DE.md](README_E2E_V0025_DE.md).

`config_commit_path_equivalence_e2e.py` beweist zusätzlich, dass der lokale
File-WAL-Entity-Diff und der native Client/Proxy/MQTT/Master-Relay-Diff auf dem
owning Worker denselben V9-Kandidaten und Fingerprint materialisieren; der
Master beobachtet das Ergebnis als Mirror. Cross bleibt separat V8.

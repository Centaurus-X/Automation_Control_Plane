# Release Notes – Automation Control Plane v0017-r2

Stand: 2026-07-19

## Behobene Feldfehler

### SSH-Lifecycle

`v0017-r1` hielt den SSH-ControlMaster nur 180 Sekunden ohne aktive
Slave-Verbindung. Ein Fresh-Build konnte länger dauern, wodurch der spätere
PKI-Transfer mit SSH-Exit-Code 255 abbrach. r2 hält den Master bis zum
expliziten Abschluss, prüft ihn vor jeder Operation und baut ihn bei Verlust
nicht interaktiv über die geschützte Authentisierungsgrenze neu auf.

### Worker-Integrität

r1 prüfte das vollständige Worker-Manifest, verbarg aber den konkreten
Fehlpfad. Der Worker erlaubt reguläre Kommissionierung in
`_config/application` und `_config/config_data`; solche Änderungen dürfen
nicht mit Code-Manipulation gleichgesetzt werden.

r2 verankert die kanonische `SHA256SUMS`, prüft alle unveränderlichen Dateien
streng und behandelt ausschließlich die klar begrenzten Deployment-
Konfigurationsbäume als veränderlich. Abweichungen werden mit Soll- und
Ist-Hash protokolliert. Eine veränderte Python-, Shell- oder sonstige
Paketdatei blockiert den Rollout mit dem exakten Pfad.

## Neue Regressionstests

- `tools/test_rollout_ssh_reconnect.py`
- `tools/test_worker_remote_lifecycle.py`

Sie prüfen unter anderem den Verlust und Wiederaufbau des SSH-Masters, die
r1→r2-Reuse-Kompatibilität, den kanonischen Worker-Manifest-Anker,
Konfigurationsdrift, Code-Tamper, PKI-Installation, Journal-Archivierung und
kontrollierten Worker-Neustart.

## Installation über einen laufenden r1-Feldstand

Für den Reuse-Wiederanlauf wird r2 kontrolliert über den bestehenden
`automation_control_plane_release_v0017`-Baum installiert. Dadurch bleiben die
bereits erzeugte r1-Lab-PKI und zulässige Runtime-Zustände erhalten. Vor dem
Overlay werden genau die zwei r1-exklusiven Dokumente entfernt und der gesamte
Releasebaum zeitgestempelt gesichert. Der vollständige, fail-closed Block steht
in `README.md` und den Rollout-Anleitungen.

# Code-Audit v0017-r2

## Befund des Feldlaufs

Der r1-Reuse-Lauf erreichte die erfolgreiche PKI-Übertragung und brach erst in
`install identity and restart existing Worker` ab. Das r1-Lifecycle-Skript
leitete die Detailausgabe von `sha256sum` nach `/dev/null`; daher war aus dem
Log nicht feststellbar, welche Datei abwich. Gleichzeitig waren regulär
kommissionierbare Worker-Dateien Teil des Gesamtmanifests.

## Korrekturen

### SSH

- unbegrenztes `ControlPersist=yes` bis zum expliziten Exit;
- `ssh -O check` vor jeder Remote-Operation;
- automatischer Wiederaufbau bei Verbindungsverlust;
- Wiederaufbau ausschließlich nicht interaktiv;
- Remote-Slaves mit `BatchMode=yes` und null Passwort-Prompts;
- Diagnosepfad kann die Sitzung ebenfalls sicher rekonstruieren.

### Worker-Baseline

- erwartete Worker-Version wird geprüft;
- kanonischer SHA-256-Anker der Worker-`SHA256SUMS` wird geprüft;
- Manifestzeilen, relative Pfade und Duplikate werden validiert;
- unveränderliche Dateien werden vollständig und mit sichtbarem Fehlpfad
  geprüft;
- fehlende und unerwartete Code-Dateien werden aufgelistet;
- `_config/application/*` und `_config/config_data/*` sind explizit
  kommissionierbar;
- `preserve` protokolliert und erhält deren Drift;
- `strict` blockiert jede Drift;
- Runtime, Zertifikate, Logs, PID und Bytecode werden nicht fälschlich als
  Paketcode behandelt;
- alle Worker-Symlinks werden abgelehnt; sichere zusätzliche JSON/YAML/TXT-
  Konfiguration wird protokolliert, unbekannte Zusatzdateitypen blockieren;
- aktive Worker-Zertifikatsnamen werden generisch aus `node_id` gebildet;
- die Baseline wird vor einem Fresh-Purge geprüft.

### Release-/Runtime-Trennung

Ein r2-Tooling-Paket kann im `--reuse`-Modus konsistente r1-Images derselben
Version bedienen. `summary.json` protokolliert deshalb Paket- und
Runtime-Revision getrennt. Ein Fresh- oder Upgrade-Lauf erzeugt r2-OCI-Labels.

## Regressionstests

`tools/test_worker_remote_lifecycle.py` prüft:

- kanonischen Manifestanker;
- erlaubte Konfigurationsdrift;
- Strict-Block;
- exakten Code-Tamper-Pfad;
- falschen Manifestanker;
- PKI-Installation;
- Journalarchiv;
- Start, Status und Stop.

`tools/test_rollout_ssh_reconnect.py` prüft:

- privaten Unix-Control-Socket;
- absichtlichen Masterverlust;
- automatischen nicht interaktiven Wiederaufbau;
- r1-Runtime-Kompatibilität;
- getrennte Release-/Runtime-Revision im Ergebnis.

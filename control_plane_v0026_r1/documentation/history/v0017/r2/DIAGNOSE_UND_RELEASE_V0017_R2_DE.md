# Diagnose und Release v0017-r2

## Aktueller Fehler

```text
Worker MQTT identity transferred ... PASS
install identity and restart existing Worker
sha256sum: WARNING: 1 computed checksum did NOT match
```

Die PKI-Übertragung und der r2-SSH-Keepalive-Workaround funktionierten. Der
Abbruch entstand in der r1-Funktion `verify_worker_package`, die das komplette
Worker-Manifest prüfte und die konkrete `sha256sum`-Ausgabe unterdrückte.

Aus dem r1-Log allein lässt sich der abweichende Pfad deshalb nicht beweisen.
Da der Worker regulär kommissionierbare Konfiguration im Manifest führt, ist
Konfigurationsdrift eine plausible Ursache; echte Codeabweichung bleibt aber
ebenfalls möglich. r2 löst diese Unsicherheit durch eine exakte, kategorisierte
Diagnose.

## Sicherer r2-Wiederanlauf

```bash
./rollout_automation.sh run --reuse --reset-worker-replica \
  --config "$HOME/.config/automation-control-plane/rollout_automation.json" \
  --credentials "$HOME/.config/automation-control-plane/credentials.json"
```

Dieser Lauf baut die erfolgreiche r1-Control-Plane nicht neu. Erst nach einer
sichtbaren Worker-Baseline-PASS-Meldung werden Identität, Journal und Prozess
verändert.

## Fail-closed-Grenzen

- falsche Worker-Version;
- veränderte Worker-Manifestdatei;
- veränderte, fehlende oder zusätzliche Code-/Skriptdatei;
- unsicherer Pfad oder Symlink;
- verlorene SSH-Sitzung, die nicht nicht-interaktiv rekonstruiert werden kann;
- inkonsistente Master-/Proxy-Version oder Revision;
- fehlende PKI, falscher SAN oder Key-Pair-Mismatch;
- fehlende Konvergenz, Telemetrie oder E2E-Restores.

# Diagnose: Stage 4 MQTT-ACL-Test (Korrekturstand r6)

Stand: 16. Juli 2026

## Symptom

Der Fresh-Rollout erreichte den gesunden MQTTS-Broker, brach aber in
`4/8 MQTT security and persistence tests` mit folgender Meldung ab:

```text
Authorized MQTT subscriber did not receive its ACL readiness marker.
```

## Ursache

Korrekturstand r5 beschränkt den Proxy absichtlich auf Master-originierte
Control-Events:

```text
automation/v1/site_main/events/+/master-01
```

Der nachfolgende Live-Test verwendete dennoch `proxy` als angeblich
autorisierten Subscriber für dieses Worker-Eventtopic:

```text
automation/v1/site_main/events/security-<id>/fn-02
```

Der Broker setzte die ACL somit korrekt durch und lieferte auch den
Readiness-Marker nicht. Der Test interpretierte genau dieses sichere Verhalten
fälschlich als Fehler. mTLS, Zertifikatsprüfung, Broker-Health und der vorherige
Publish-Deny-Test waren im Zielhost-Log bereits erfolgreich.

## Korrektur

- Der Worker-Publish-Deny wird auf einem Proxy-lesbaren, Master-originierenden
  Topic `events/security-<id>/master-01` geprüft. Der Master veröffentlicht
  Readiness- und Kontrollmarker; der Worker darf dort nicht publizieren.
- Ein eigener Read-Deny-Oracle prüft danach, dass der Proxy einen retained
  Worker-Marker unter `events/sensor-value/fn-01` nicht erhält.
- Die Prüfung bewertet die tatsächliche Payload-Zustellung. Sie hängt nicht
  von einem bestimmten Exitcode für negative MQTT-v5-SUBACKs ab.
- Statische und gemockte Regressionstests lehnen das alte widersprüchliche
  `.../fn-02`-Probe-Topic ab und erkennen simulierte Publish- sowie Read-Leaks.

## Sicherheitswirkung

Die produktive ACL wird nicht aufgeweicht. Der Proxy bleibt auf
Master-originierte Control-Events beschränkt; Worker-Telemetrie bleibt vom
Proxy getrennt. Geändert wird nur der fehlerhafte Test-Oracle.

## Zielhost-Verifikation

```bash
./rollout.sh --fresh --profile lab_v2
```

Erwartete Stage-4-Meldungen enthalten:

```text
Verified broker-side MQTT publish denial for worker.
Verified MQTT subscription denial for proxy.
MQTTS, mTLS identity, worker-proof ACL, allow/deny and persistence tests passed.
```

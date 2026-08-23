# MQTT-Broker-Sicherheitsmodell v0011

## Trust Boundary

Mosquitto läuft als eigenes Compose-Projekt `automation-mqtt`. Der Broker ist
nur mit `automation_control` verbunden, besitzt keine PostgreSQL-Credentials und
keinen Zugang zum internen Netz `automation_database`. Extern wird genau Port
8883 an eine explizite Hostadresse gebunden; Port 1883 wird nicht geöffnet.

## Transport

- Eclipse Mosquitto `2.1.2-alpine` im Lab; Produktion verlangt einen geprüften
  Image-Digest.
- TLS mindestens 1.2; Servername der internen Clients ist `mqtt-broker`.
- `require_certificate true`: jeder Client benötigt ein gültiges Zertifikat.
- `allow_anonymous false`: kein anonymer Zugriff.
- `use_identity_as_username true`: der Zertifikat-CN ist der ACL-Benutzer.
- QoS maximal 1, Paketgröße maximal 4 MiB, begrenzte Inflight-/Offline-Queues.
- Brokerzustand und Retained Status liegen im benannten Volume.

## Technische Identitäten

```text
urn:automation:site_main:master:master-01
urn:automation:site_main:proxy:proxy-01
urn:automation:site_main:worker:fn-01
urn:automation:site_main:monitor:mqtt-health
```

Der Common Name muss exakt stimmen. Zusätzlich erzeugt die Test-PKI dieselbe
URN als URI-SAN. Die Produktions-CA-Privatdatei darf niemals auf Broker-,
Master-, Proxy- oder Worker-Hosts gespeichert werden.

Vor dem Start kopiert `prepare_runtime_config.sh` die geprüften Quelldateien in
ein generiertes Bind-Mount. Konfiguration und ACL gehören dort UID/GID 1883;
die ACL besitzt Modus 0700. Mosquitto 2.1 verwendet
`listener_allow_anonymous false` und `max_packet_size`; die veralteten
Direktiven `per_listener_settings` und `message_size_limit` sind nicht aktiv.

## Effektive ACL

| Identität | Erlaubtes Schreiben | Erlaubtes Lesen |
|---|---|---|
| Proxy | Commands an `master/master-01` | eigene ACKs, Site-Events |
| Master | Proxy-ACKs, Site-Events, Worker-Commands/-ACKs, eigener Status | eigene Control-Commands, Coordinator-Commands/-ACKs, Events, Worker-Heartbeats |
| Worker `fn-01` | eigener Status/Heartbeat/Registration/Config-State, eigene Events, Distributed-Commands/-ACKs an `master-01` | eigene Commands/ACKs, Broadcast-Commands |
| Health | nichts | nur `$SYS/broker/uptime` |

Die Datei `mqtt_broker/config/acl.conf` enthält diese Regeln konkret für
`site_main`, `master-01`, `proxy-01` und `fn-01`. Neue Instanzen oder Worker
erfordern ein eigenes Zertifikat und eine bewusst geprüfte ACL-Erweiterung.

## Brokergebundene automatische Worker-Aufnahme

Das Leserecht des Masters auf `nodes/+/heartbeat` ist kein allgemeines
Schreibrecht. Ein Worker darf laut ACL nur seinen exakt benannten Heartbeat-
Topic schreiben. Der Master nutzt einen frischen, nicht-retained Heartbeat auf
diesem Topic als kurzlebigen Nachweis, dass der registrierende Worker das
zugeordnete mTLS-Zertifikat besitzt. Erst zusammen mit der aktiven
Master-Konfiguration, der kanonischen Zertifikats-URN und der aktiven
DB-Authority darf `PENDING` automatisch zu `APPROVED` werden. Eine
selbstbehauptete `source_node`, ein retained Replay oder ein nicht
konfigurierter Worker genügt nicht. `DENIED` und `QUARANTINED` bleiben
administrativ gesperrt.

## Retained Messages

Status- und Config-Latest-State-Pfade dürfen retained sein. Commands und ACKs
werden von Master/Proxy nur mit `retain=false` versendet. Die Master-Control-
Schnittstelle weist eingehende retained Commands unabhängig von der Broker-ACL
zurück. Der Live-Test prüft die Retained-Persistenz ausschließlich auf dem
Master-Statuspfad und entfernt den Testwert danach.

## Test-PKI

```bash
cd mqtt_broker
./scripts/generate_test_pki.sh
./scripts/verify_pki.sh
```

Das Skript ist idempotent und wiederverwendet vollständiges markiertes Material.
Eine erzwungene Neuerzeugung ist nur explizit möglich:

```bash
FORCE_TEST_MQTT_PKI=1 ./scripts/generate_test_pki.sh
```

Unmarkierte bestehende PKI-/Secret-Verzeichnisse werden nicht überschrieben.
Broker-Secrets und CA-/Clientmaterial bleiben getrennt. `secrets/` und
`test-pki/` sind aus Docker-Build-Kontext, Git und Release-ZIP ausgeschlossen.

## Positive und negative Laufzeittests

`mqtt_broker/test.sh` prüft:

1. gesunden Container und gültige TLS-Hostnamekette,
2. `$SYS`-Zugriff mit der Health-Identität,
3. Ablehnung eines Clients ohne Zertifikat,
4. erlaubtes Proxy→Master-Control-Publish,
5. verbotenes Proxy→Worker-Publish,
6. erlaubten Worker-Status und verbotenes Schreiben in `fn-02`,
7. QoS-1-/Retained-Persistenz über Broker-Restart,
8. Nichterreichbarkeit eines Plain-Listeners auf 1883.

Die Python-Tests prüfen ergänzend die Anwendungsebene: geschlossene Topics,
Envelope-/Identity-/Expiry-Prüfung, retained Command Reject und ACK-Korrelation.

## Bekannte Betriebsgrenze

Dieses Paket stellt einen einzelnen persistenten Mosquitto-Broker bereit.
Persistenz und Docker-Restart sind keine Broker-HA. Für automatische Broker-HA
ist später ein separat qualifiziertes Clusterprodukt mit eindeutigem
Konsistenz-, Session- und Fencing-Modell erforderlich.

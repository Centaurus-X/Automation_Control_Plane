# Code-Audit und Patchbericht v0009

## Befund

Die v0008-Control-Plane lief auf `192.168.0.82` vollständig gesund. MQTT,
PostgreSQL Primary/Standby, Master, Proxy, Migrationen, Transaktionsprobes,
Backup, Restore und erweiterte Abnahme waren erfolgreich. Der Worker `fn-01`
erreichte den Master ebenfalls: `publish_received` und `last_seen_at` stiegen.
Trotzdem blieben `admission_state=PENDING`, `commands_ok=0`, `acks_received=0`
und `node_heartbeats=0`.

Das war kein Modbus-Fehler. Der Worker sendet während des Onboardings
regelmäßig `distributed.node.register`. Der Master persistierte die Node, wies
sie jedoch bis zur expliziten Admission erwartungsgemäß zurück. Parallel
sendet der unveränderte Worker bereits einen generischen Live-Heartbeat auf
`automation/v1/site_main/nodes/fn-01/heartbeat`; der Master hatte diesen Topic
bisher weder abonniert noch als sicheren Besitznachweis ausgewertet. Daher
konnte die vorhandene mTLS-/ACL-Identität nicht automatisch in eine Admission-
Entscheidung einfließen.

## Minimal-invasiver Patch

1. Mosquitto erlaubt dem Master das Lesen von Worker-Heartbeat-Topics. Das
   Worker-Schreibrecht bleibt exakt auf den eigenen Node-Topic begrenzt.
2. Das Master-MQTT-Interface klassifiziert diese Heartbeats vor dem Command-
   Dispatcher und validiert Protokoll, Site, Nachrichtentyp, Zeitfenster,
   Retain-Flag sowie Topic-/Payload-Identität.
   Der bisher für alle eingehenden MQTT-Nachrichten erhöhte Zähler
   `commands_queued` zählt jetzt tatsächlich nur Command-Topics;
   `messages_queued` bildet den gesamten Ingress ab.
3. Der Coordinator hält einen erfolgreichen Besitznachweis höchstens 45
   Sekunden im Speicher. Er genehmigt nur konfigurierte, aktivierte Worker auf
   der aktiven DB-Authority und nur bei der kanonischen Zertifikats-URN.
4. Existiert die Node noch nicht, wird der Proof kurz gehalten. Die nächste
   Registrierung erzeugt `PENDING`, genehmigt idempotent und wird unmittelbar
   erneut ausgewertet. Existiert `PENDING` bereits, genehmigt der Live-
   Heartbeat direkt; die nächste Worker-Registrierung erhält den erfolgreichen
   ACK.
5. `DENIED` und `QUARANTINED` werden nie überschrieben. Die bisherige manuelle
   Proxy-Admission bleibt verfügbar.

Worker-Code, Worker-Konfiguration, verteilte Payloads, PostgreSQL-Schema,
Proxy-Geschäftslogik und Feldbus-/Modbuspfade wurden nicht verändert.

## Sicherheitsmodell

Ein gültiges CA-Zertifikat allein genügt nicht. Automatische Aufnahme verlangt
zusätzlich eine konkrete Broker-ACL für die Zertifikats-CN, einen passenden
Node-Topic, einen frischen nicht-retained Heartbeat, einen aktivierten Worker
in der Authority-Konfiguration und den aktiven Master-Leader. Ein Worker kann
damit nicht durch eine bloß behauptete fremde `source_node` eine andere Node
genehmigen.

## Tests

Der neue Funktionstest deckt vier Szenarien ab:

- gültiger ACL-gebundener Heartbeat wird akzeptiert;
- retained, Topic-/Payload-Mismatch und Master-Eigenheartbeat werden
  abgelehnt beziehungsweise ignoriert;
- ein Proof vor der ersten Registrierung führt anschließend zu genau einer
  automatischen Approval-Entscheidung;
- unbekannte, quarantänisierte und nicht live nachgewiesene Worker bleiben
  ungeeignet beziehungsweise `PENDING`.

Zusätzlich bleiben sämtliche Master-, Proxy-, Broker-, PostgreSQL-,
Lifecycle-, Upgrade-, Recovery-, Secret-Hygiene- und Release-Gates aktiv. Der
Zielhosttest `test_worker_auto_admission.sh fn-01 90` validiert nach dem Rollout
die reale automatische Freigabe und mindestens einen persistenten verteilten
Heartbeat.

## Freigabegrenze

Docker/LAN und der reale Worker sind aus dem Erstellungscontainer nicht
erreichbar. Der Release kann hier vollständig statisch und funktional geprüft
werden; die reale mTLS-/Broker-/Worker-Kette muss nach dem Rollout auf
`192.168.0.82` mit dem mitgelieferten Zielhosttest bestätigt werden. Modbus ist
für diesen Admission-Test nicht erforderlich.

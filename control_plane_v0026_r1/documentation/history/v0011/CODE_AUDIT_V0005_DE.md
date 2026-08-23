# Code-Audit und minimal-invasiver MQTT-Testpatch v0005

**Release:** `automation_control_plane_release_v0005` / `0.5.0`  
**Zielhost:** `192.168.0.82`  
**Patchgrenze:** ausschließlich Control Plane; Worker unverändert

## Befund

Der Zielhost-Output belegt einen erfolgreichen Image-Build, geordnetes
Container-Replacement und einen gesunden Mosquitto-2.1.2-Broker. Der Rollout
brach reproduzierbar in Stufe `4/8 MQTT security and persistence tests` ab.
PostgreSQL, Master und Proxy wurden deshalb vom fail-closed Startgraph nicht
gestartet; dies ist ein kontrollierter Folgeeffekt und kein separater
Startfehler dieser drei Komponenten.

Der Brokerlog zeigt für den negativen Proxy-Test eine erfolgreiche mTLS-
Authentifizierung. Der bisherige Test erwartete danach einen Fehlerstatus von
`mosquitto_pub`. Dieser Status ist kein belastbarer ACL-Oracle: Ein Broker kann
einen MQTT-v5-QoS-1-Publish mit negativem PUBACK korrekt abweisen, ohne dass das
CLI dies als Prozessfehler zurückliefert.

## Korrektur

Die beiden negativen Publish-Probes beobachten nun die sicherheitsrelevante
Eigenschaft direkt:

1. Ein autorisierter Publisher setzt einen eindeutigen retained Marker.
2. Ein autorisierter Subscriber empfängt den Marker und beweist damit seine
   Bereitschaft.
3. Der nicht autorisierte Principal publiziert den verbotenen Payload.
4. Ein autorisierter Principal publiziert einen Kontroll-Payload.
5. Der Subscriber muss exakt Marker und Kontroll-Payload empfangen.

Wird der verbotene Payload zugestellt, schlägt der Test weiterhin hart fehl.
Der Marker wird am Ende entfernt, auch bei einem Fehlerpfad.

Geprüfte Deny-Kanten:

- Proxy darf nicht in den Command-Pfad von Worker `fn-01` schreiben.
- Worker `fn-01` darf nicht als `fn-02` in den Eventpfad schreiben.

## Unverändert

- Broker-ACL und Mosquitto-Konfiguration
- mTLS-CA und Zertifikatsidentitäten
- Topics, Payloads und Contract-Pakete
- PostgreSQL-Schema und Migrationen
- Master- und Proxy-Anwendungslogik
- Worker-Code und Worker-Deployment

## Freigabe

Shell-, JSON-, YAML-, Unit-, Architektur-, Secret-Hygiene-, Lifecycle- und
Release-Checks werden vor Archivierung erneut ausgeführt. Das Erstellungsumfeld
besitzt keinen Docker-Daemon und keinen Zugriff auf das Lab-Netz; der reale
Containerpfad bleibt deshalb über `./rollout.sh test` auf `192.168.0.82`
verbindlich abzunehmen.

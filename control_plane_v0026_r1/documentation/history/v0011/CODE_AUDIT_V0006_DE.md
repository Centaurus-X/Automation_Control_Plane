# Code-Audit und minimal-invasiver Proxy-Runtime-Patch v0006

**Release:** `automation_control_plane_release_v0006` / `0.6.0`  
**Zielhost:** `192.168.0.82`  
**Patchgrenze:** ausschließlich Control Plane; Worker unverändert

## Verifizierter v0005-Stand

Der vollständige Zielhost-Output bestätigt:

- MQTT-Broker gesund; mTLS-, ACL-Allow-/Deny-, QoS-1- und Persistenztest grün
- PostgreSQL Primary und Standby gesund
- Streaming-Replikation und physischer Slot aktiv
- Master- und Proxy-Migration 1 erfolgreich angewendet
- Master gesund, DB bereit, MQTT verbunden und DB-gefenceter Leader
- Proxy-Datenbankrolle per TLS/SCRAM autorisiert
- Proxy-MQTT-Client per TLS verbunden

Der Gesamtrollout war dennoch nicht erfolgreich, weil Stufe 8 beim
Proxy-Healthcheck endete. Die nachfolgenden Transaktions-, Security- und
erweiterten Backup-/Restore-Gates wurden dadurch noch nicht ausgeführt.

## Fehlerursache

`create_https_application` liefert ein mit `functools.partial` gebundenes
ASGI3-Callable. Seine effektive Signatur ist `(scope, receive, send)`. Uvicorns
automatische Interfaceerkennung behandelte dieses Objekt im gepinnten
Runtimepfad als ASGI2 und rief es nur mit `scope` auf. Daraus entstand bei jedem
Healthcheck:

```text
TypeError: gateway_asgi() missing 2 required positional arguments: 'receive' and 'send'
```

Der Prozess blieb aktiv, jede HTTPS-Anfrage antwortete aber mit HTTP 500.

## Korrektur

`gateway_main` übergibt `interface="asgi3"` explizit an `uvicorn.run`. Dies
ändert weder Routing noch Middleware, Authentisierung, RBAC, MQTT, Datenbank
oder WebSocketlogik. Ein neuer Test prüft sowohl die Uvicorn-Konfiguration als
auch den direkten ASGI3-Aufruf einschließlich Security-Headern.

## Upgrade-Kontinuität

Da v0005 erstmals PostgreSQL- und Proxy-Persistenz erzeugt hat, muss ein neues
Releaseverzeichnis dieselben DB-Passwörter und Proxy-Pepper weiterverwenden.
v0006 importiert diese Daten zusammen mit PKI/TLS vor dem Containerstopp aus
dem vorherigen Release. Der Import akzeptiert nur reguläre Dateien und
Verzeichnisse, überschreibt keine Konflikte und bricht bei vorhandenem
PostgreSQL-Volume ohne vollständige Credentials vor jeder Laufzeitänderung ab.

## Unverändert

- Worker-Code und Worker-Konfiguration
- SQL-Schemata und Migrationen
- MQTT-Topics, Payload-Verträge und ACL
- Master- und Proxy-Geschäftslogik
- benannte Docker-Datenvolumes

## Noch auf dem Zielhost auszuführen

Der korrigierte Proxy-Healthcheck, wiederholte Migrationen, reale DB-
Transaktions-/Security-Probes und `test_extended_all.sh` bleiben durch
`./rollout.sh test` und den erweiterten Test auf `192.168.0.82` verbindlich
abzunehmen.

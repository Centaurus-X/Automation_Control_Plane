# Diagnose: E2E-Aktor-Lifecycle v0013-r3

Stand: 17. Juli 2026  
Betroffener Lauf: `automation_control_plane_e2e_v0012_20260717-024524.log`  
Betroffene Operation: `e059232a-3ff0-44c5-b051-837a39a82318`

## 1. Beobachteter Fehler

Der E2E-Lauf erreichte die Control Plane, authentifizierte sich, bestätigte den
Worker, öffnete den WSS-Stream und empfing Sensordaten. Die Aktoroperation
wurde um `00:45:33` dauerhaft angelegt und wechselte von `RECEIVED` nach
`ROUTED`. Bis zum Ablauf des Testzeitfensters um `00:46:34` erschien kein
weiterer Zustand. Der Test endete deshalb korrekt mit:

```text
FAILED: operation e059232a-3ff0-44c5-b051-837a39a82318 did not reach a terminal state
```

Damit ist Modbus nicht als primäre Ursache belegt. Der Fehler lag davor in der
Master-seitigen Auslieferungs- und Terminalisierungskette.

Die bereitgestellten Logs enthalten keinen nachgelagerten PostgreSQL-
Outbox-Dump. Sie beweisen deshalb nicht, ob genau dieser Datensatz zuletzt
`PENDING`, `SENT` oder nach einem negativen ACK `FAILED` war. Belegt ist aber,
dass die Operation selbst dauerhaft in `ROUTED` verblieb. Die Codeanalyse hat
mehrere Pfade gefunden, die exakt diesen nichtterminalen Zustand erlaubten.

## 2. Lokalisierte Ursachen

### 2.1 Transport-Publish öffnete kein Dispatchfenster

Ein erfolgreich an den MQTT-Transport übergebener
`distributed.actuator.execute`-Befehl markierte nur die Outbox als `SENT`.
Die Operation blieb `ROUTED`. Dadurch war im Journal nicht erkennbar, dass eine
Hardwarewirkung bereits möglich war.

### 2.2 Negatives Worker-ACK terminalisierte nur die Outbox

Ein abgelehntes Execute-ACK setzte die Outbox auf `FAILED`, ließ die Operation
aber weiterhin `ROUTED`. Der E2E-Client konnte diesen Fehler daher nicht als
terminales `FAILED` beobachten.

### 2.3 Terminales Worker-Resultat schloss die Execute-Outbox nicht

Ein gültiges `distributed.actuator.result` terminalisierte zwar das
Operationsjournal, korrelierte aber die zugehörige Execute-Outbox nicht
abschließend. Dadurch waren unnötige Wiederholungen möglich.

### 2.4 Die ursprüngliche TTL ging beim Publish verloren

Die Operation speicherte die Client-TTL, der spätere Outbox-Publish erzeugte
jedoch eine neue TTL ab Publishzeit. Ein alter Auftrag konnte dadurch länger
gültig werden als vom Aufrufer erlaubt.

### 2.5 Abgelaufene Operationen blieben nichtterminal

Für abgelaufene Zustände `RECEIVED`, `ROUTED` und `DISPATCHED` gab es keinen
periodischen Abschluss. Der beobachtete Zustand konnte daher auch nach Ablauf
der Operations-TTL bestehen bleiben.

### 2.6 Worker-Aktivität war nicht an den Node-Shard gebunden

Nach Admission konnte ein Worker als `ACTIVE` gelten, bevor sein gemeldeter
Node-Fingerprint dem aktuellen `node:<node-id>`-Ziel entsprach. Dadurch konnte
der Master Hardwarearbeit zu früh routen. Ein optionaler Cross-Shard darf den
normalen Node-Sync dagegen ausdrücklich nicht blockieren.

## 3. Minimal-invasiver Patch

Korrekturstand `v0013-r3` ändert keine Verträge und keine Datenbankschemata.
Er schließt nur die vorhandenen Lifecycle-Lücken:

1. `ACTIVE` wird erst nach exakter Übereinstimmung des gemeldeten Node-
   Fingerprints mit dem aktuellen Node-Scope gespeichert. Cross-Konvergenz
   bleibt davon unabhängig.
2. Ownership und neue Aktoroperationen werden nur für einen zugelassenen,
   gewünschten und tatsächlich synchronisierten `ACTIVE`-Owner freigegeben.
3. Die ursprüngliche Operations-TTL wird in die Outbox übernommen und beim
   MQTT-Publish unverändert verwendet.
4. Execute-Datensätze werden in der fälligen Outbox vor Konfigurationspaketen
   abgearbeitet.
5. Ein erfolgreicher Transport-Publish setzt die Operation auf `DISPATCHED`.
6. Ein positives ACK bestätigt den Dispatch idempotent; ein negatives ACK
   setzt die Operation auf `FAILED` und publiziert ein `operation.result`.
7. Ein gültiges terminales Worker-Resultat schließt die korrelierte Execute-
   Outbox als `ACKED` und publiziert das Terminalereignis für Proxy/WSS.
8. Abgelaufene, noch nicht publizierte Operationen werden `FAILED`.
   Abgelaufene Operationen nach geöffnetem Dispatchfenster werden
   sicherheitskonservativ `UNCERTAIN`. Zugehörige Outbox-Arbeit wird
   abgebrochen.

## 4. Kompatibilität mit Worker v0082

Der Wire-Befehl bleibt `distributed.actuator.execute`; die bestehende Payload
mit `operation_id`, Authority-Fencing, Ziel-Node-Fingerprint, Task und
Readback-Pflicht bleibt erhalten. `expires_at_s` ist Master-interne
Outbox-Metadaten und wird vom bestehenden MQTT-Adapter in das bereits
unterstützte Legacy-Envelope umgesetzt. Der Worker ist weiterhin ein separates
Paket und wurde nicht in die Control Plane aufgenommen oder verändert.

Die v0082-Kompatibilitätsprobe akzeptiert den erzeugten Full-Config-Befehl und
den Aktor-Execute-Befehl. Node-Sync, Execute-ACK und terminales Resultat werden
zusätzlich durch isolierte Master-Regressionsprüfungen abgedeckt.

## 5. Verifikation

Neu hinzugefügte Regressionstests prüfen:

- exakte Node-Fingerprint-Konvergenz und Cross-Unabhängigkeit;
- fail-closed Routing bei nicht synchronisiertem Owner;
- Publishzustand und unveränderte TTL;
- positive und negative ACK-Korrelation;
- Abschluss der Outbox durch ein Worker-Resultat;
- `FAILED`/`UNCERTAIN` bei Ablauf und Abbruch der Outbox.

Die vollständige Master-Suite, die statischen Control-Plane-Gates, JSON-/Shell-
Syntax, Archivvollständigkeit und reproduzierbarer ZIP-Build werden vor dem
Release lokal ausgeführt.

## 6. Noch erforderliche reale Abnahme

Das Erstellungsumfeld besitzt keinen Docker-Daemon und keinen Zugriff auf die
drei Ziel-VMs. Der reale `--fresh`-Lauf, MQTTS, PostgreSQL, WSS, Worker v0082,
Modbus-Schreib-/Readback und der vollständige E2E-Lauf bleiben daher bis zur
Ausführung auf dem Teststand `NOT_RUN`. Ein lokaler Unit-Test ersetzt diese
Abnahme nicht.

Die normative siebenstufige Online-Scope-Migration bleibt unverändert
`BLOCKED`; unsichere Topologieänderungen werden weiterhin fail-closed
abgelehnt.

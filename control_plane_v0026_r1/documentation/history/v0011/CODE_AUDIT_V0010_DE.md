# Code-Audit und Patchbericht v0010

## Ergebnis

v0010 ergänzt v0009 ausschließlich in der Control Plane. Der Worker ist nicht
Bestandteil dieses Pakets. Die Master-Automation erkennt nun die von Worker
v0076 publizierte Capability `local_automation_authority=true` und verhindert
damit zwei automatische Schreiber für denselben Controller.

Die Konsistenz ist bewusst lokal-synchron und verteilt schließlich konsistent:
Der Worker bleibt Feldbus-Owner und führt lokale Regeln auch ohne erreichbaren
Master aus. Lokale Konfigurationsänderungen werden durable über MQTT vorgeschlagen;
der Master serialisiert sie als globale Revision und repliziert die autoritative
Momentaufnahme zurück. Zeitstempel-LWW wird nicht verwendet. Revision, Fencing,
Idempotency-ID und `master_wins` entscheiden Konflikte reproduzierbar.

## Diagnose des Ausgangscodes

v0009 besaß bereits:

- die PostgreSQL-basierte globale Config-Authority;
- Worker-Registrierung, mTLS-Auto-Admission und Controller-Leases;
- persistente MQTT-Inbox/Outbox und Worker-ACK-Korrelation;
- globale RuleEngine/FSM/Automation sowie den externen Aktor-Router.

Die Worker-Capabilities wurden bei der Registrierung gespeichert, aber von der
globalen Automation nicht ausgewertet. Jeder akzeptierte Sensorwert lief durch
die vollständige Master-RuleEngine und konnte einen automatischen
`distributed.actuator.execute`-Auftrag erzeugen. Ein Worker v0076 hätte parallel
dieselbe vollständig lokale Regel ausgeführt. Heartbeats aktualisierten die
gespeicherten Capabilities außerdem nicht.

## Authority-Aufteilung

Die neue reine Core-Komponente `core/automation_authority.py` erzeugt aus
globaler Konfiguration und persistiertem Node-Record eine gecachte Policy:

- Fehlt die Capability, bleibt das v0009-Verhalten `master_execute` erhalten.
- Ist ein zugelassener Worker lokal autoritativ, werden seine konfigurierten
  Controller `observe_only`.
- Online-Status und Lease-Ablauf ändern diese Authority nicht. Dadurch startet
  der Master bei einem kurzen Worker-/Netzausfall keinen zweiten Schreiber.
- Eine PENDING-/nicht zugelassene Capability kann die Master-Automation nicht
  deaktivieren.
- Vollständig einem Worker zuordenbare Regeln werden delegiert. Regeln mit
  Sensoren oder Aktorzielen über mehrere Worker bleiben global beim Master.
- Master-Aktoraufträge an lokal autoritative Controller werden zusätzlich im
  Router fail-closed unterdrückt. Proxy-/Benutzeraufträge verwenden einen
  separaten Pfad und bleiben erlaubt.

Die RuleEngine wertet delegierte Regeln am Master nur beobachtend aus. Ihre
Action-Plan-Einträge werden nicht committed. So bleiben Monitoring und ein
späterer expliziter Authority-Wechsel möglich, ohne parallele Zustandswrites.

## Synchronisation und Konflikte

1. Der Worker commitet einen lokalen Regel-/FSM-Zustand in seiner lokalen SEM.
2. Eine SQLite-Outbox hält den Proposal bis zur bestätigten Übertragung.
3. Der Master prüft Admission, Basisrevision und Schema.
4. Der Master erzeugt genau eine globale PostgreSQL-Revision oder bestätigt
   eine identische Änderung als No-op.
5. Die autoritative Config wird über die persistente Master-Outbox an Worker
   repliziert und per ACK abgeschlossen.
6. Ein veralteter Proposal wird nicht nach Uhrzeit überschrieben, sondern gegen
   die neue Master-Revision neu aufgebaut. Damit entsteht schließlich derselbe
   Zustand auf beiden Seiten.

Benutzeränderungen über Proxy und lokale RuleEngine-Änderungen dürfen deshalb
beide vorkommen. Während einer Trennung können die Ansichten abweichen. Der
Worker steuert lokal weiter; nach Wiederverbindung ordnet der Master die
Änderungen revisionsbasiert und beide Seiten konvergieren.

## Zusätzliche Robustheitskorrekturen

- Worker-Heartbeats aktualisieren Capabilities additiv. Legacy-Heartbeats ohne
  Capability-Feld löschen einen vorhandenen Authority-Nachweis nicht.
- Identische Benutzer- und Worker-Konfigurationsänderungen erzeugen keine neue
  Revision und keinen unnötigen Rollout.
- Nach einer echten Worker-Änderung werden Controller-Leases sofort gegen die
  neue globale Revision reconciled; der Sensor-/Aktorpfad wartet nicht auf das
  periodische Ownership-Intervall.
- Der Proxy-Logout typisiert UUID-/Text-Parameter explizit. Damit kann
  PostgreSQL auch bei NULL-Alternativen den Parametertyp deterministisch
  bestimmen.
- Weitere optionale PostgreSQL-Filter wurden ebenfalls explizit typisiert.

## Regressionstests

Die neue Testmatrix deckt ab:

- lokaler Worker bleibt auch mit `online=false` autoritativ;
- Legacy-Worker ohne Capability behalten Master-Automation;
- Cache-Invalidierung nach Capability-Änderung;
- PENDING Worker kann Master-Automation nicht abschalten;
- Worker-Proposals dürfen ausschließlich vom Master delegierte Regeln ändern;
- vollständig lokale versus übergreifende Regelzuordnung;
- beobachtende Sensorpipeline ohne Config- oder Aktorwrite;
- Defense-in-depth am internen Aktor-Router;
- manueller Proxy-Aktorpfad bleibt aktiv;
- Registration und Heartbeat transportieren dieselben Capabilities;
- identische Config-Änderung ist No-op;
- PostgreSQL-Logoutparameter besitzen explizite Typen.

## Verbleibende reale Abnahme

Ohne Zugriff auf den Docker-Daemon und die Lab-Hosts kann die Erstellung keine
reale MQTT-/Modbus-Schalthandlung bestätigen. Nach dem Rollout sind deshalb
`./rollout.sh test-running`, `./test_extended_all.sh` und der getrennte autonome
sowie manuelle End-to-End-Aktortest auf 192.168.0.82 verbindlich.

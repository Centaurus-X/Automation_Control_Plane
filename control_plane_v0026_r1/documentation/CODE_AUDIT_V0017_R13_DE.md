# Code-Audit v0017-r13

## Ergebnis r7

Der r6-Fresh-Lauf bestätigte den Idempotenzpatch vollständig: CFG-001 bis
CFG-005, Config-Diff, Aktor-Ausführung sowie Config-/Aktor-Restore bestanden.
Danach wurde erstmals PAR-001 erreicht. Dort scheiterte ein Teil der
parallelen WSS-Handshakes, obwohl alle Dienste gesund blieben und die
isolierte harte Limitprüfung PAR-002 bereits bestanden hatte.

## Root Cause

PAR-001 zielt auf 128 gleichzeitige Verbindungen. Das ist exakt das
konfigurierte Principal-Limit. Zwei dauerhafte E2E-Verbindungen werden
beibehalten und 126 weitere geöffnet. Direkt davor wurde ein temporärer
Operation-Event-Socket geschlossen, ohne serverseitigen Client- und
Reservierungsabbau über `runtime.status` abzuwarten.

Die Clientbibliothek kann ihren Close-Handshake bereits beendet haben, während
der Server die Handler-Finalisierung noch abschließt. Eine solche kurzlebige
Altverbindung oder fehlgeschlagene Upgrade-Reservierung reduziert am exakten
Grenzwert vorübergehend die freie Kapazität. Der bisherige Test sammelte nur
einen einmaligen Handshake-Satz und verbarg zudem die konkreten Fehlertypen.

## Fail-closed-Lösung

- ein aktiver `runtime.status`-Poll bestätigt vor dem Test exakt zwei Clients,
  null Reservierungen und null ausstehende Antworten;
- nach einem Teilsatz wird dessen serverseitiger Zustand vor einem einzigen
  Ersatzversuch erneut bestätigt;
- erfolgreiche Sockets bleiben offen; der Endzustand muss daher weiterhin
  echte 128 gleichzeitige Clients erreichen;
- der Ersatzversuch kann ein echtes niedrigeres Limit nicht umgehen;
- nach Schließen aller Test-Sockets werden Client-, Reservierungs-, Response-
  und Task-Leaks aktiv geprüft;
- Self-Test und statische Gates schützen die Zustandsprädikate und
  diagnostische Fehleraggregation.

Die Produktions-WSS-Implementierung und ihre Limits werden nicht verändert.

## Reuse-Diagnose und r8-Korrektur

Der reale r7-Fresh-Lauf bestand sämtliche 51 Core-E2E-Prüfungen. Der direkt
folgende Reuse-Lauf blieb erst beim Live-Telemetrie-Gate stehen: Master-Sequenz
168 und Proxy-Event-Sequenz 502 änderten sich 60 Sekunden lang nicht. Die
Worker-Diagnose zeigte gleichzeitig sechs neue Sensorereignisse je Zyklus,
aktive MQTT-, Persistence- und Coordinator-Threads sowie einen unmittelbar vor
dem Gate neu gestarteten Prozess.

Die Master-Persistenz erzwingt absichtlich monotone `source_sequence`-Werte.
Der Worker beginnt diese nach Prozessstart neu; ein Reuse-Neustart kann deshalb
gegen den persistenten Datenbank-High-Water-Mark zurückfallen. r8 beseitigt
diese Ursache an der Lifecycle-Grenze: Reuse erhält die aktive PID und prüft
Paket und Identität vollständig. Fresh/Upgrade bleiben unverändert restartend.

## r8-Fresh-Feldevidenz und r9

r8 bestätigte die neue Worker-Lifecycle-Logik, alle fünf gesunden Container,
Live-Telemetrie (`source_sequence 0→30`, Proxy `event_sequence 0→13`) sowie
sämtliche Config-/Aktor- und Restore-Gates. PAR-001 blieb bei 127 von 128
Clients stehen: zwei initiale Handshakes schlugen transient fehl, der
gleichzeitige Ersatzsatz ersetzte nur einen; der andere erhielt HTTP 429.

Die Zustandsdiagnose enthielt `clients` und `reserved_connections`, aber nicht
die Phase zwischen erfolgreicher Authentifizierung und Client-Registrierung.
r9 ergänzt genau diesen beobachtbaren Zustand, verlangt fünf ruhige Samples
und serialisiert ausschließlich die fehlenden Ersatzhandshakes. Der primäre
Lastsatz bleibt mit 126 gleichzeitig gestarteten zusätzlichen Sockets
unverändert; Produktionslimits werden nicht erhöht oder abgeschwächt.

## r9-Feldevidenz und r10

r9 bestand den echten Fresh-Lauf und den unmittelbar folgenden direkten
Reuse-Lauf vollständig. Die zusätzliche argumentlose Autonomieprobe traf einen
noch selteneren Teardown-Race: Von drei initial fehlgeschlagenen Pfaden konnten
zwei ersetzt werden; der letzte Ersatz erhielt HTTP 429, obwohl die fünf
Status-Samples zuvor den erwarteten exakten Stand meldeten. Damit war die
Einmal-Wiederholung zwar fail-closed, aber für einen länger lebenden
Alt-Handshake nicht ausreichend.

r10 behält sämtliche Produktionslimits und den unveränderten 126er-Lastsatz.
Nur transient klassifizierte Fehler werden sequenziell innerhalb eines
gemeinsamen 30-Sekunden-Fensters wieder eingereiht. Vor jedem Versuch gilt
erneut das vollständige Fünf-Sample-Prädikat. Das gemeinsame Deadline-Objekt
verhindert eine unbeschränkte Laufzeit; permanente Fehler werden nicht
wiederholt. Self-Test und statische Release-Gates sichern Klassifizierung,
Schleife, Zustandskopplung und Ergebnisdiagnostik ab.

## r10-Feldevidenz und r11

Der r10-Fresh-Lauf `20260720T200832Z-417515` bestand den vollständigen Rollout
und alle 51 Core-E2E-Checks. PAR-001 provozierte den Zielrace real: ein
initialer Fehler erforderte neun transiente Wiederholungen, danach waren 128
Clients gleichzeitig aktiv und der Endzustand leak-frei.

Im folgenden Reuse-Lauf wurde der letzte Ersatz kurz vor Ablauf der gemeinsamen
Deadline akzeptiert. Der Fehlerzustand zeigte bereits exakt 127 erwartete und
127 beobachtete Clients sowie null Reservierungen, Pending-Principals und
Responses. Es fehlte ausschließlich Zeit für die fünf Bestätigungssamples.
r11 entfernt deshalb die Bestätigung nach einem Erfolg aus der
Öffnungsdeadline. Vor einem weiteren Versuch erfolgt sie am Schleifenanfang;
nach dem letzten Erfolg erfolgt sie im separaten regulären Drain-Fenster.

Die PostgreSQL-Evidenz zeigte zusätzlich einen legitimen `worker_proposal` auf
Revision 5 nach dem E2E-Engineering-Commit 4. Der Cleanup verweigerte korrekt,
diesen konkurrierenden Commit durch einen pauschalen Rollback zu verlieren.
r11 minimiert das Konfliktfenster, indem die reversible E2E-Config vor PAR-001
restauriert wird. Der bestätigte Restore schließt anschließend nur die
E2E-eigene Verpflichtung; spätere autonome Commits bleiben erhalten.

## r11-Feldevidenz und r12

Der r11-Fresh-Lauf bestätigte die vorgezogene Config-Wiederherstellung: Sowohl
`CFG-RESTORE` als auch Aktor-Restore und Secret-Scan waren beim späteren
PAR-Abbruch PASS. Der Proxy meldete erneut exakt 127 erwartete und beobachtete
Clients mit null Reservierungen, Pending-Principals und Responses.

Die Quotenzähler des Proxy werden direkt aus registrierten Clients und
Pending-Principals berechnet; ein verborgener persistenter Zähler existiert
nicht. Die Ursache lag stattdessen im Client-Helfer: `_open_events` richtet
standardmäßig jeden neuen Stream durch einen ersten kurzlebigen Socket am
Replay-High-Watermark aus und öffnet danach rekursiv den beibehaltenen Socket.
Dieses korrekte Verhalten für einzelne Streams ist am exakten Parallel-Limit
ungeeignet, weil der Teardown des ersten Sockets den letzten Slot belegt.

r12 führt diese Ausrichtung genau einmal vor dem Lastsatz durch und wartet
anschließend auf den vollständig abgebauten Basiszustand. Alle parallelen
Event-Clients erhalten den gemeinsamen Cursor explizit und deaktivieren die
erneute Ausrichtung. Dadurch bleibt die Replay-Semantik definiert, während
jeder Lastpfad exakt einer Verbindung entspricht.

## r12-Feldevidenz und r13

Der echte r12-Fresh-Lauf `20260720T210630Z-557036` bestand alle Rollout-Gates,
51 Core-E2E-Prüfungen und PAR-001 mit 128 gleichzeitig gehaltenen Clients ohne
Handshakefehler. Im unmittelbar folgenden Reuse-Lauf konvergierte die
Config-Mutation von Revision 3 auf 4, der erste Aktorauftrag wurde danach aber
mit `not_found: controller has no active owner lease` abgewiesen.

Die Produktionsprüfung koppelt die Owner-Lease absichtlich an Authority-Term
und exakte Config-Revision. Die Node-Replikation kann die neue Revision bereits
bestätigt haben, bevor der periodische Controller-Ownership-Abgleich seine
Lease für dieselbe Revision veröffentlicht. Die Ablehnung verhindert in diesem
Fenster korrekt eine Ausführung ohne eindeutigen Besitzer.

r13 ändert deshalb keine Produktionssemantik. Nur der E2E-Client wartet in
einem gemeinsamen, höchstens 30 Sekunden langen Fenster auf exakt diesen
Lease-Zustand oder `worker_not_synchronised`. Alle Versuche behalten denselben
Idempotency-Key; andere `not_found`-Meldungen und alle sonstigen Ablehnungen
bleiben sofort fatal. `ACT-001` weist die Anzahl der Bereitschaftsversuche aus.

# Diagnose und Release v0017-r13

## Reale Fehlerkette

1. **r3:** benannte Ownership-Prüfung scheiterte ohne NSS-Eintrag für UID 1000.
2. **r4:** numerische UID-Prüfung beseitigte die falsche Namensabhängigkeit.
3. **r5:** `procps` fehlte im hermetischen Worker-Lifecycle-Preflight.
4. **r6:** synchrone Config-Idempotenz wurde fälschlich ausschließlich über
   eine `operation_id` beurteilt. r6 akzeptiert vertragskonform dieselbe
   terminale Commit-Identität und bestand im Feld alle CFG- und Restore-Gates.
5. **r7:** PAR-001 startete am exakten Principal-Limit, ohne vorher den
   serverseitigen Abbau eines kurz zuvor geschlossenen Event-Sockets zu
   bestätigen.
6. **r7-Reuse:** Der Rollout startete einen gesunden Worker unnötig neu. Dessen
   Prozess-lokale Sensorsequenz begann wieder bei 1, während PostgreSQL den
   Fresh-High-Water-Mark 168 korrekt beibehielt. Sensoren wurden weiter
   gelesen, ihre zunächst älteren MQTT-Ereignisse aber fail-closed verworfen.
7. **r8-Fresh:** Nach zwei transienten PAR-001-Fehlschlägen erreichte der
   parallele Ersatzsatz 127 statt 128 Clients. `runtime.status` zeigte zwar
   registrierte Clients und rohe Reservierungen, nicht aber bereits
   authentifizierte `pending_principals`; dadurch konnte ein kurzer
   Zwischenzustand fälschlich als stabil gelten.
8. **r9-Autonomieprobe:** Fresh und direkter Reuse bestanden vollständig. Ein
   nachgelagerter argumentloser Lauf öffnete in PAR-001 jedoch nur 125 von 126
   zusätzlichen Sockets; nach drei initialen transienten Fehlern blieb ein
   HTTP-429-Ersatzfehler übrig. Ein bereits registrierter Alt-Handshake konnte
   den exakten Clientstand während der ruhigen Samples kurz substituieren.
9. **r10-Reuse:** Der echte r10-Fresh-Lauf bestand vollständig und übte den
   neuen Pfad mit einem initialen Fehler und neun Wiederholungen erfolgreich
   aus. Im direkten Reuse wurde der letzte Ersatz-Handshake unmittelbar vor
   Ablauf der gemeinsamen Deadline angenommen. Der Proxy meldete bereits den
   exakten sauberen Stand 127/127, hatte aber nicht mehr genug Zeit für fünf
   Samples. Währenddessen wurde ein legitimes `worker_proposal` als Revision 5
   nach dem E2E-Engineering-Commit 4 angenommen; der verspätete Cleanup verweigerte
   dessen pauschalen Rollback korrekt.
10. **r11-Fresh:** Config-/Aktor-Restore und Secret-Scan bestanden nun auch
    beim PAR-Abbruch. PAR blieb aber mit 127 sauberen Clients stehen. Die
    Codeanalyse zeigte, dass jeder Event-Pfad in `_open_events` erst einen
    Replay-Ausrichtungs-Socket öffnete und schloss und danach den eigentlichen
    Socket öffnete. Am 128er-Principal-Limit konkurrierte der kurzlebige erste
    Socket deterministisch mit dem beibehaltenen zweiten Socket.
11. **r12-Reuse:** Der Fresh-Lauf bestand einschließlich 128er-PAR-Last. Im
    folgenden Reuse konvergierte die E2E-Config-Revision vor dem periodischen
    Ownership-Abgleich. Der Controller hatte für genau diese Revision kurz
    noch keine aktive Owner-Lease und wies den Aktorzugriff korrekt
    fail-closed ab; der E2E-Client behandelte die Bereitschaft als permanent.

## r7-Lösung

Der Live-Test synchronisiert sich jetzt über den vom Proxy veröffentlichten
WSS-Zustand. Er beginnt erst bei exakt zwei bewusst gehaltenen Verbindungen
und null Reservierungen. Nach einem transient unvollständigen Satz wird der
Abbau fehlgeschlagener Reservierungen bestätigt und nur die fehlende Anzahl
einmal nachgeholt. Alle erfolgreichen Sockets bleiben offen; 128 gleichzeitige
Clients, parallele Reads und ein leak-freier Endzustand bleiben unverändert
verpflichtend.

## r8-Lösung

Reuse erhält den aktiven Worker-Prozess. Vor den Live-Gates werden der
kanonische Paketanker, die installierte CA, Zertifikat, privater Schlüssel,
Key-Match, Gültigkeit, Client-Purpose, SAN und genau eine aktive Worker-PID
geprüft. Ein Reuse-Replica-Reset ist unzulässig. Fresh und Upgrade verwenden
weiterhin den kontrollierten Stop-, PKI-Installations- und Startpfad.

## r9-Lösung

Der Proxy veröffentlicht die Anzahl authentifizierter Pending-Principals. Das
E2E-Stabilitätsprädikat verlangt diesen Wert explizit bei null und bestätigt
den Gesamtzustand fünfmal nacheinander. Fehlgeschlagene Originalpfade werden
anschließend einzeln wiederholt; nach jedem Erfolg muss der Server den neuen
exakten Clientstand erneut ruhig melden. Ein dauerhaftes Limit unter 128 wird
dadurch weiterhin nicht umgangen.

## r10-Lösung

Die sequenzielle Wiederholung ist nun eine zustandssynchronisierte, begrenzte
Recovery-Schleife. Ausschließlich transiente HTTP-429/503-, Timeout- und
Betriebssystemfehler werden erneut versucht. Alle Versuche teilen ein einziges
Zeitbudget von höchstens 30 Sekunden; vor jedem Versuch muss der erwartete
Proxy-Zustand wieder fünfmal ruhig sein. Permanente Fehler oder ein echtes
Limit unter 128 bleiben fail-closed. Anzahl der initialen Fehler und der real
ausgeführten Wiederholungen werden getrennt protokolliert.

## r11-Lösung

Das gemeinsame Recovery-Budget gilt nur noch für weitere Öffnungsversuche.
Jeder weitere Versuch setzt weiterhin einen fünfmal ruhigen Serverzustand
voraus; nach dem letzten erfolgreichen Öffnen prüft das reguläre, eigenständig
begrenzte Drain-Fenster den vollständigen 128er-Zustand. Dadurch wird die
Deadline nicht verlängert, aber eine bereits angenommene Verbindung auch nicht
wegen fehlender Restzeit falsch verworfen.

Die E2E-Config-Mutation wird jetzt direkt nach Config-/Aktorprüfung und vor
PAR-001 zurückgesetzt. Nach semantisch und per Fingerprint bestätigtem Restore
wird die E2E-Mutationspflicht geschlossen. Ein späterer autonomer
Worker-Proposal-Commit bleibt dadurch unangetastet; erscheint ein konkurrierender
Commit schon vor dem Restore, bleibt der Rollback weiterhin fail-closed.

## r12-Lösung

PAR-001 ermittelt einmal einen gemeinsamen Replay-High-Watermark, schließt
diesen einzelnen Preflight-Socket und wartet aktiv auf den exakten
Zwei-Client-Basiszustand. Danach verwenden alle Event-Pfade denselben Cursor
mit deaktivierter per-Client-Ausrichtung. Jeder der 63 Event-Pfade besitzt
damit genau einen beibehaltenen Socket; zusammen mit 63 Control-Pfaden und den
zwei Basisverbindungen werden weiterhin real 128 Clients erreicht. Die
Produktionslimits werden weder erhöht noch umgangen.

## r13-Lösung

Der Aktor-E2E-Pfad wiederholt ausschließlich die exakt klassifizierte fehlende
Controller-Owner-Lease und `worker_not_synchronised` innerhalb eines
gemeinsamen Fensters von höchstens 30 Sekunden. Der Idempotency-Key bleibt über
alle Versuche identisch. Eine nicht passende `not_found`-Meldung, jede andere
Ablehnung oder ein abgelaufenes Fenster bleibt ein harter Fehler. Die
Produktions-Lease- und Fail-closed-Regeln werden nicht verändert.

## Verbindlicher Feldnachweis

Akzeptiert wird nur ein aus dem verifizierten ZIP gestarteter Fresh-Lauf mit
`stage=complete`, `status=PASSED` und `exit_code=0`, gefolgt von einem
erfolgreichen Reuse-Lauf. Worker-Konvergenz, fortschreitende Telemetrie,
vollständiger Core-E2E und bestätigter Config-/Aktor-Restore sind Pflicht.

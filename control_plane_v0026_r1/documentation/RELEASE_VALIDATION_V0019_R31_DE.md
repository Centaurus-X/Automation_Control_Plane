# Release-Validierung v0019-r31

Zusaetzliche R31-Grenzen:

- gepufferte `mosquitto_sub`-Diagnoseausgabe gilt nicht als beobachtbare
  Bereitschaftsgrenze; insbesondere darf der absichtlich gestoppte Worker den
  Nachweis weder ermoeglichen noch verhindern;
- maximal drei pro Versuch eindeutige Challenge-Payloads konvergieren jeweils
  innerhalb eines fest begrenzten Zwei-Sekunden-Beobachtungsfensters;
- nur die gemeinsam beobachtete SUBACK-Bestaetigung und der bytegenau passende
  Challenge-Payload gelten als Erfolg; SUBACK allein, Payload allein und
  paralleler Fremdverkehr bleiben fail-closed;
- die ausfuehrbare Regression belegt Erfolg beim ersten und zweiten Challenge,
  verzoegerten Publisher-Start sowie Ablehnung bei fehlender SUBACK oder
  ausschliesslichem Fremdverkehr;
- ein Live-Nachweis mit absichtlich gestopptem Worker bestand ohne
  Produktions-Sensorverkehr; Produktionslogik, PKI, V9 und beide
  Config-Commit-Pfade bleiben unveraendert.

Uebernommene R29-Grenzen:

- nach bestaetigter SUBACK-Bereitschaft muss der Subscriber maximal
  120 Sekunden aktiv bleiben und damit einen separat startenden
  Publisher-Tools-Container umfassen;
- der hostseitige exakte Payload-Nachweis bleibt separat auf 300 Versuche
  mit 0,1 Sekunden Abstand begrenzt;
- eine ausfuehrbare Regression verzoegert den Publisher-Start und lehnt
  Subscriber-Zeitrahmen unter 90 Sekunden ab;
- vorzeitig beendeter Subscriber, fehlender Publisher oder fehlender exakter
  Payload bleiben fail-closed.

Uebernommene R28-Grenzen:

- die hostseitige SUBACK-Pruefung umfasst fest begrenzt maximal 600 Versuche
  mit 0,1 Sekunden Abstand und damit auch langsamen Tools-Container-Start;
- eine ausfuehrbare Regression muss erst nach 11 Sekunden SUBACK melden und
  damit nachweislich die alte Zehn-Sekunden-Grenze ueberschreiten;
- Timeout, vorzeitig beendeter Subscriber und fehlender exakter Payload bleiben
  fail-closed; Produktionslogik, PKI, V9 und Commit-Pfade bleiben unveraendert.

Uebernommene R27-Grenzen:

- der Cross-Fact-Test darf erst nach bestaetigter MQTT-SUBACK-Bereitschaft
  publizieren;
- er muss den pro Lauf eindeutigen Probe-Payload exakt nachweisen und darf
  parallel eintreffende echte Master-Nachrichten auf demselben erlaubten Topic
  weder als Erfolg noch als vorzeitiges Ende werten;
- SUBACK- und Payload-Wartezeit bleiben fest begrenzt; fehlende Bereitschaft
  oder ein fehlender exakter Probe-Payload bleiben fail-closed;
- die ausfuehrbare Regression muss Erfolg trotz vorangestelltem Fremdverkehr,
  fehlende SUBACK-Bereitschaft und ausschliesslichen Fremdverkehr abdecken;
- Produktionslogik, Topic-/ACL-Vertrag, PKI, Worker v0092, V9 und beide
  Config-Commit-Pfade bleiben unveraendert.

Uebernommene R26-Grenzen:

- ein nach Legacy-Rollout bereits aktives MQTT-Secrets-Verzeichnis darf nur
  dann in-place konvergieren, wenn es exakt das verifizierte aktive Broker-Ziel
  und zugleich das erwartete Release-Ziel ist;
- die fuenf erforderlichen Dateien muessen regulaer, symlinkfrei und nicht leer
  sein; ausser den beiden bekannten Markern sind keine weiteren Eintraege
  zulaessig;
- Test-PKI muss im Testprofil markiert sein und bleibt im Produktionsprofil
  verboten;
- jedes andere, unvollstaendige, zusaetzliche oder verlinkte Ziel bleibt
  fail-closed; insbesondere gilt die Ausnahme nicht fuer
  `runtime-pki-handoff/`;
- ein ausfuehrbarer Guard-Test und die statische Release-Validierung muessen
  Positiv- und Negativfaelle reproduzierbar erzwingen.

Uebernommene R25-Grenzen:

- der Administration-PostgreSQL-Test darf keinen Deploy-, Recreate-, Restart-
  oder TLS-Handoff-Pfad aufrufen;
- er muss ausschliesslich die laufenden Primary-/Standby-Container, alle
  Runtime-Rollen und die Streaming-Replikation pruefen;
- ein nach einem Legacy-Fresh-Rollout veralteter Admin-TLS-Handoff darf die
  aktive PostgreSQL-CA weder lesen noch veraendern;
- `postgres_system/test.sh` bleibt der vollstaendige
  Entwickler-/Provisionierungstest, waehrend `test_running.sh` die getrennte
  nicht deployende Administration-Grenze bildet;
- Regression und statische Validierung muessen diese Trennung fail-closed
  erzwingen.

Uebernommene R24-Grenzen:

- jeder Administration-Worker-Apply verifiziert die geplante MQTT-Identitaet
  erneut, bevor der laufende Worker gestoppt wird;
- CA, Zertifikatskette, URI-Target, Restlaufzeit, Dateirechte und
  Zertifikat-/Private-Key-Paar muessen fail-closed passen;
- die Uebertragung verwendet ein eng begrenztes, atomar aktiviertes
  Einmal-Staging, das der Worker-Lifecycle nach Installation sicher entfernt;
- das Test-Lab akzeptiert nur markierte Test-PKI; Produktion verlangt einen
  absoluten externen `pki_source_path`;
- die Administration-Regression umfasst 26 deterministische Szenarien und
  belegt insbesondere `stage-pki` vor `stop-compatible`.

Uebernommene R23-Grenzen:

- die dynamischen Site-/Rollenwerte des Admin-Security-Events sind im
  PostgreSQL-Statement explizit mit `::text` typisiert;
- eine Regression fuehrt den echten Credential-Updatepfad gegen eine
  kontrollierte Connection aus und prueft Statement sowie Parameterbindung;
- der reale Psycopg/PostgreSQL-Pfad darf beim idempotenten Admin-Bootstrap
  keine polymorphe `IndeterminateDatatype`-Exception mehr erzeugen;
- alle R22-Sicherheits-, TLS-, V9- und Commit-Grenzen bleiben unveraendert.

Uebernommene R22-Grenzen:

- der Legacy-Upgradepfad bindet die fuenf PostgreSQL-TLS-Dateien aus dem
  expliziten Bestandsrelease vor dem Datenbank-Deploy;
- TLS-Quellen muessen regulaer, nicht leer und symlinkfrei sein; Kette,
  SAN-Identitaeten, Restlaufzeit und Zertifikat-/Private-Key-Paare werden
  fail-closed geprueft;
- ein Upgrade muss den SHA-256-Fingerprint der aktiven PostgreSQL-CA bytegenau
  beibehalten;
- ein bestehender Admin-Benutzer wird nur bei exakter aktivierter
  `platform_admin`-Rolle der konfigurierten Site idempotent auf das geschuetzt
  uebergebene Sollkennwort konvergiert;
- Kennwortrotation setzt Fehlversuche und Sperre zurueck, widerruft aktive
  Sessions und schreibt ein nicht geheimes Security-Event;
- ein bereits passendes Kennwort ist ein schreibfreier No-op; deaktivierte
  Benutzer sowie Rollen-/Site-Abweichungen bleiben fail-closed;
- isolierte Regressionen pruefen beide No-op-/Rotationspfade, Autorisierung,
  TLS-Symlink-Abweisung und geheimnisfreie Argument-/Ausgabebehandlung.

Uebernommene R21-Grenzen:

- die interne Quellbaumkopie der containerisierten Rollout-Validierung verwendet
  exakt die expliziten Runtime-Grenzen der Root-Manifestpruefung;
- geschuetzte MQTT-Laufzeitkonfiguration, PKI-/TLS-Handoffs, Backups, Secrets
  und aktive E2E-Ergebnisse werden nicht geoeffnet oder kopiert;
- eine Regression erzeugt eine nicht lesbare MQTT-Runtime-Konfiguration und
  beweist deren sichere Ausblendung;
- Dateien ausserhalb der freigegebenen Grenzen bleiben fail-closed sichtbar;
- Runtime-Berechtigungen und Produktionslogik bleiben unveraendert.

Uebernommene R20-Grenzen:


- vor einem Datenbank-Apply wird die tatsaechlich vom gesunden Primary
  gemountete PostgreSQL-CA zusammen mit Primary- und Standby-Zertifikaten sowie
  den zugehoerigen Private Keys fail-closed an den neuen Release uebergeben;
- gemeinsame CA, Zertifikatsketten, Restlaufzeit, SAN-Identitaeten,
  Zertifikat-/Key-Paare sowie regulaere, nicht leere und symlinkfreie Dateien
  werden vor der Uebergabe verifiziert;
- der Administration-Adapter setzt die verifizierten
  `POSTGRES_*_SOURCE`-Pfade vor dem unveraenderten Datenbank-Installer. Ein
  Upgrade erzeugt deshalb keine neue, fuer Master und Proxy unbekannte CA;
- der separate Administration-Datenbanktest bindet dieselben verifizierten
  `POSTGRES_*_SOURCE`-Pfade erneut, bevor `test.sh` das Test-Deployment
  ausfuehrt. Apply und Test muessen dadurch bytegenau dieselbe CA behalten;
- das Produktionsprofil lehnt markierte Test-CA fail-closed ab.
  Fresh-Provisioning ohne laufenden Primary bleibt unveraendert;
- `postgres_system/runtime-tls-handoff/` ist eine eng benannte
  Runtime-Grenze fuer Reuse und muss im gebauten Release-Archiv weiterhin
  vollstaendig fehlen;
- die uebernommene Datenbank-Regression beweist
  Handoff-Reihenfolge, exakte Environment-Bindung sowie deren erneute
  Verwendung im separaten Datenbank-Testprozess.

Uebernommene R18-/R17-/R16-Grenzen:

- das generierte `postgres_system/backup/` ist eine explizite Runtime-Grenze;
  logische Dumps, Base-Backups und Pruefsummen werden nicht als unverankerte
  Release-Dateien fehlklassifiziert;
- alle in `SHA256SUMS` verankerten Dateien bleiben bytegenau geprueft;
  zusaetzliche Dateien ausserhalb der eng benannten Runtime-Grenzen werden
  weiterhin fail-closed abgewiesen;
- eine ausfuehrbare Regression erzeugt MQTT-Handoff, PostgreSQL-TLS-Handoff
  und PostgreSQL-Backup in einer vollstaendigen Release-Kopie, beweist die
  idempotente Wiederverwendung und weist unerwartete Dateien weiterhin
  fail-closed ab;

- der markierte, generierte `mqtt_broker/runtime-pki-handoff/` ist eine
  explizite Runtime-Grenze und wird nicht als unverankerte Release-Datei
  fehlklassifiziert;

- vor einem MQTT-Apply werden Broker-, Health-, Master- und Proxy-Identitaet aus
  den tatsaechlich laufenden Containermounts uebernommen;
- alle MQTT-CAs muessen bytegleich sein; Chain, Restlaufzeit, SAN und
  Zertifikat-/Private-Key-Paar werden vor der Uebergabe geprueft;
- das Testprofil uebernimmt die vollstaendige markierte Lab-PKI einschliesslich
  fn-01 und fn-02, damit bereits provisionierte Worker vertrauenskompatibel
  bleiben; das Produktionsprofil lehnt markierte Test-PKI fail-closed ab;
- fehlende, leere, verlinkte, unmarkierte oder inkonsistente Runtime-Quellen
  werden nicht ersetzt oder ueberschrieben;
- MQTT-Bind-Adresse, Test-Host und Zertifikats-IP werden aus dem signierten
  Inventar an jede Adapteraktion weitergegeben;
- der Persistenztest verwendet pro Lauf ein eindeutiges Events-Probe-Topic und
  kann nicht durch den retained Master-Status ueberschrieben werden;
- Fresh-Installationen ohne aktive MQTT/Master/Proxy-Kette provisionieren ihre
  Vertrauenskette weiterhin neu.

- vor einem Datenbank-Apply wird das authentifizierte, vom laufenden Primary
  gemountete Runtime-Material atomar und symlink-sicher an den neuen
  Release-Ordner uebergeben; Fresh-Installationen ohne Primary bleiben
  unveraendert;
- nach Datenbank-Deploy und bei Status muessen Superuser-, Master-/Proxy-
  Migrator- und App-Logins via TLS zu den gemounteten File-Secrets passen;
- Streaming-Replikation wird vor und nach der Uebergabe weiterhin zwingend
  nachgewiesen;
- Rollback-Readiness verwendet die registrierte Adapteraktion `ready`; der
  interne Evidenzname wird nicht mehr als unbekannte Aktion uebergeben;
- isolierte Regressionen pruefen die Reihenfolge Handoff vor Apply und den
  gueltigen Readiness-Aktionsvertrag.

Uebernommene R15- und fruehere Grenzen:

- der Datenbank-Backup-Hook verifiziert das Artefakt am absoluten, vorhandenen
  und symlinkfreien /backup-Bind-Mount des tatsaechlich laufenden Primary-
  Containers statt am Pfad des neuen Zielreleases;
- --archive-terminal archiviert nur complete, rolled_back oder
  failed_rolled_back unter __OLD__; aktive Fehler und manual recovery bleiben
  gesperrt, waehrend Locks, Fencing und Adapter-Idempotenz erhalten bleiben;
- zwei Regressionen beweisen sicheren Plan-Handoff und die Recovery-Sperre;

- die Administration-Readiness wiederholt den unveränderten strikten Status-
  und Container-Health-Nachweis höchstens 120 Sekunden lang mit festem
  Ein-Sekunden-Intervall; Timeout und der reine Status bleiben fail-closed;
- eine isolierte Regression beweist starting nach healthy sowie das
  deterministische Ende am Readiness-Timeout;

- die Port-Prüfung nach dem Fresh-Purge toleriert ausschließlich eine
  kurzzeitige `EADDRINUSE`-Freigabeverzögerung mit einer begrenzten Anzahl
  deterministischer Versuche; dauerhafte Belegung und andere Bind-Fehler
  bleiben fail-closed;
- eine ausführbare Socket-Regression beweist sowohl den begrenzten Fehlerpfad
  als auch die spätere Freigabe desselben real belegten Ports;
- Worker v0092 verhindert fail-closed das Ueberschreiben eines parallel sichtbaren
  lokalen V9-Dateiedits durch interne State-Persistenz und beweist den Guard
  mit einem kanonischen V9-Regressionsfall;
- terminale `CFG-006`-/`CFG-RESTORE`-Evidenz bleibt nach erfolgreichem Restore
  unveränderlich; spätere Cleanup-Läufe schreiben nur
  `CFG-RESTORE-REVALIDATION` und eine verwaiste Revalidierung scheitert;
- vollständiges AST-Gate über alle aktiven Python-Dateien, eingebetteten
  Python-Programme und Rollout-Heredocs; keine Klassen, Lambdas,
  `SimpleNamespace`, Decorators, Typehints oder verbotenen Imports;
- R13 enthält ausschließlich den aktiven E2E-v0019-Code. Historische
  E2E-Pakete bleiben über die unveränderten Releases R9/R8 und das lokale `__OLD__`-Archiv
  nachvollziehbar, werden aber nicht mehr als aktiver R13-Code ausgeliefert.

Zusätzliche v0019-Abnahmegrenzen:
- Worker-v0092-Safety-Fallback nur im `worker_simulation`-Profil, nur für den
  freigegebenen fn-01-Simulator-Controller und nur im Registerbereich
  0..65535; stale Revisionen und fremde Controller bleiben gesperrt;
- Upgrade-Recovery aus einem expliziten `E2E_RESULT_DIR` mit Pre-Read,
  schreibfreiem `verified_noop` bei bereits erreichtem Originalwert und
  zwingendem zweiten, unabhängigen Readback vor Journalabschluss; Drift
  bewirkt keinen Write und hält Journal sowie Config-Restore fail-closed offen;
- exakte gemeinsame Replica-Konvergenz von persistierter Scope-Revision und
  zugehörigem persistiertem Scope-Fingerprint; der Live-Fingerprint bleibt
  separat und die globale Config-Revision darf den Scope-Cursor nicht ersetzen;
- V9-Global-/Node-Storage strikt flach; V5 nur expliziter Import; separater
  hardwarefreier V8-Cross-Shard;
- im aktiven Worker-Config-Verzeichnis ausschließlich V9 Node und V8 Cross;
  Configs mit tatsächlicher Version kleiner 8 ausschließlich unter `_legacy`
  und fail-closed ohne explizites `allow_legacy_import`;
- primäre, deterministisch reproduzierbare V9-Node-Dateien für `fn-01` und
  `fn-02`; V8 ausschließlich als separat aktivierter Cross-Vertrag;
- exakte fn-02-Bootstrap→Master-Shard-Rekonstruktion durch den gepinnten
  Worker sowie erster Authority-Abgleich mit `IDLE`-WAL und null File-Proposals;
- bytegleiche gemeinsame Contractmodule in Quelle, Master und Proxy;
- strenger Drei-Mirror-Vergleich im vollständigen Releasebaum sowie Prüfung
  des tatsächlich importierten Contract-Mirrors im Master-Komponentenimage;
- fail-fast Testschleifen in Master- und Proxy-Containerbuilds;
- Proxy-/Master-Policy-Parität für V9-`processes` und `timers` bei weiterhin
  abgewiesenen Secret-Pfaden;
- persistenter File-WAL-Diff getrennt vom nativen Client/Proxy/MQTT/Master-Diff;
- exakte File-WAL-Korrelation von berechnetem Diff/Action-Plan, Operation-ID,
  Outbox-Payload, Master-Post-Commit und Node-Rollout;
- native MQTT-Payload-Bindung an den persistenten Worker-Inbox-Hash sowie
  strikt nachfolgende Authority- und Scope-Revisionen des lokalen File-WAL;
- deterministische Admin-Pläne, Drift-Gates, Locks, Resume, Audit und
  reversibler Rollback mit Fake-Adapter-Regressionen;
- plan-gebundener Lock-Root sowie exakte Release-Root-/Worker-Archiv-Bindung
  des weiterhin verwendeten Legacy-Rollout-Backends;
- kanonischer flacher V9-Aktorresolver im Master mit gruppiertem
  Legacy-Kompatibilitätstest und zusammengesetzter Controller-/Aktoridentität;
- physische Verwendung und Wiederherstellung der dynamisch hinzugefügten
  Runtime-Matrix-Sensor-/Aktor-Entities vor dem differenziellen Config-Restore;
- physischer Modbus-Baseline-Restore auch für den lokalen File-WAL-E2E,
  einschließlich durablem Ziel-/Ausgangswertbeleg und direktem Readback;
- deterministische Regeneration und Einzelprüfung des SHA-256-Manifests des
  einzigen aktiven E2E-v0019-Pakets.

## Verbindliche Prüfebenen

- Bash-Syntax sämtlicher Shell-Skripte;
- Python-AST-/Unit- und JSON-Konfigurationsprüfung;
- MQTT-, PostgreSQL-, Master-, Proxy- und erweiterte Core-E2E-Matrix;
- Master-Rollout-, ACK-/NACK-, Fencing-, Idempotenz- und Restore-Gates;
- sicherer, recoverabler Installer des verankerten Worker-v0092-Archivs;
- verpflichtender Cross-Artefakt-Test des Paketbauers gegen das SHA-gepinnte,
  intern manifestgeprüfte Worker-v0092-ZIP statt gegen einen Quellmirror;
- Worker-Lifecycle- und SSH-Reconnect-Regression;
- Sensorsequenz-Persistenz und Master-Floor-Synchronisation;
- Runtime-Kennung plus tatsächlicher Live-Konfigurationsfingerprint;
- atomare Paarung von globaler Revision und Fingerprint aus demselben
  persistierten Commit-Snapshot, einschließlich Scheduler-Race-Regression;
- idempotente Scope-Revisionssynchronisation bei bereits identischem Inhalt;
- atomare SEM-Aktivierung beziehungsweise kompensierende Revision;
- lokaler PID-/Modbus-Pfad für Aktor 524;
- echter Worker-Neustart mit unmittelbar fortschreitender Proxy-Telemetrie;
- YAML-gesteuerte Python-Cache-Policy sowie Projekt-`__pycache__`-Gates vor und
  nach echten Workerstarts;
- expliziter bytecodefreier Python-Aufruf für gestreamten Worker-File-E2E und
  dessen lokalen Ergebnis-Verifier, unabhängig vom Archiv-Execute-Bit;
- offizieller Client/WSS→Proxy→Master→MQTT→Worker-Konfigurationscommit mit
  dynamischem Sensor/Aktor, Regel-Threshold, PID-Sollwert, `mode_id` und
  `value_id`;
- reversibler JSON-File-Diff derselben Feldklassen: Struktur und Mode über
  `master_first`, PID-Parameter und State-Value über `local_first`, jeweils
  über Proposal-WAL→Master-Commit→Worker-Hot-Apply;
- CRC-, Pfad-, Typ-, Symlink-, Secret- und Archivhygiene-Gates;
- deterministischer Release-Build und Wiederholung der vollständigen statischen
  Matrix aus dem frisch extrahierten Archiv;
- Regression gegen unterschiedliche Paketinhalte unter derselben immutable
  Release-Revision.
- bytecodefreie Ausführung des vollständigen Release-Validators mit isolierter
  Regression gegen selbst erzeugte `__pycache__`-/`.pyc`-Artefakte.

## Abnahmegrenze

Der Release-Build beweist die statischen und isolierten Verträge. Der autonome
Rollout auf der Referenztopologie beweist zusätzlich Docker, MQTT/mTLS,
PostgreSQL, WSS, echten Worker, Modbus und Restart-Recovery. Exit-Code 0 darf
erst nach Restore beider Konfigurationspfade und finaler Live-Konvergenz
entstehen. Maschinenlesbare Nachweise liegen im jeweiligen Rollout-
Ergebnisverzeichnis.

Die Live-Abnahme toleriert außerdem Optimistic Concurrency zwischen
Engineering-Commit und Worker-RuleEngine-Proposal. Sie verifiziert begrenzten
CAS-Rebase, unveränderliche historische Snapshots, feldgenauen Restore und die
Regel, dass fremde Zwischencommits nicht durch einen pauschalen Rollback
verloren gehen dürfen.

Die Referenztopologie enthält einen realen fn-01-Worker. Der fn-02-Vertrag ist
statisch und prozessual interoperabel geprüft; der Docker-Lab-Endpunkt
`modbus-fn02:5020` ist ohne gesonderte Hardware-Kommissionierung keine
Produktionsfreigabe für einen zweiten physischen Worker.

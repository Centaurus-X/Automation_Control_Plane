# Automation Control Plane v0016 – Tiefendiagnose und Release-Einordnung

Stand: 2026-07-19
Release: `0.16.0 / v0016-r8`
Ausgangsbasis dieser Revision: `automation_control_plane_release_v0016_r4.zip`,
`0.16.0 / v0016-r4`; die im Dokument erhaltenen RC-01 bis RC-17 stammen aus
der vollständigen v0015→v0016-Baselineanalyse.

## 1. Ergebnis in einem Satz

Der reale r7-Feldlauf beweist, dass die in r7 reparierte Telemetriekette nun
funktioniert: `EVT-001`, Filterwechsel, Replay und die mehrseitige Pagination
bestanden. Der Lauf scheiterte anschließend an drei E2E-/Proxy-Randfehlern:
Der Test verwendete für einen String die nur für Dictionaries gültige
Config-Operation `update`, der Pagination-Stresstest ließ denselben dauerhaft
gehaltenen Event-Client 217 zusätzliche Command-Events ungelesen puffern, und
die Restore-Prüfung setzte die Worker-lokale Revision fälschlich mit der
globalen Authority-Revision gleich. Der Cleanup verdeckte danach den ersten
Fehler. r8 korrigiert genau diese Grenzen minimal-invasiv, ohne CA, Zertifikate,
TLS-Kontexte, MQTT-Verträge, Datenbankschema oder Worker-Runtimecode zu ändern.

## 2. Analysierter Umfang

Die bereitgestellte r4-ZIP wurde vor der Analyse mit SHA-256
`d43398a62ea4ceadc71663aa64079c7103818e12cb86115da36cf07595fe3be8`
verifiziert. Das Archiv enthält 415 reguläre Dateien, davon 177 Python- und 94
Shell-Dateien; die Python-Sourcen umfassen 52.557 Zeilen. Zusätzlich wurden
`results.zip` mit 11,2 MiB E2E-JSONL, Ergebnis-/Recovery-Dateien und beiden
Live-Logs, die Rolloutanleitung sowie das separate Worker-v0082-Archiv
untersucht. Das Worker-Originalarchiv besitzt SHA-256
`ff25096b87fce3150c32896198a527761360fe1e7c5c2bf69aef61829a8c5b32`.

Die Prüfung umfasste:

- vollständige Archiv-/Manifest- und Dateibauminventur;
- Syntax/AST/JSON/YAML-/Compose- und Shell-Gates;
- alle vorhandenen Proxy-, Master-, Contract-, Lifecycle-, Packaging- und
  E2E-Selftests;
- manuellen Kontrollfluss- und Persistenzreview von Rollout, Leadership,
  Outbox, Scope-Rollout, Proposal-Ingest, WSS/HTTPS, Audit, MQTT-ACL und
  E2E-Recovery;
- Abgleich von Code, Versionen, OCI-Labels, SBOMs, Requirements und
  Dokumentation;
- reproduzierbare Mock-Docker-Lifecycle- und Fehlersimulationen.

Ein echter Docker-Daemon war in der Analyseumgebung nicht installiert. Daher
konnten Image-Build, realer Host-Purge, laufender Multi-Container-Rollout und
Live-Hardware-E2E hier nicht ausgeführt werden. Diese vier Punkte sind keine
behaupteten PASS-Ergebnisse; die Zielhost-Anleitung führt sie als verbindliche
Abnahme aus.

## 3. Einordnung des beigefügten r2-Berichts

Der Zusatzbericht analysiert `v0015-r2` mit einem anderen ZIP-Hash. Er beweist
korrekt den damaligen Direct-WSS-Abbruch durch PostgreSQL-`inet` als
`ipaddress.IPv4Address`. Die vom Benutzer gelieferte Haupt-ZIP ist jedoch
`v0015-r3`. In r3 waren folgende WSS-Korrekturen bereits vorhanden:

- `IPADDRESS_TYPES` im Direct-WSS-Serialisierer;
- Normalisierung vor dem Queueing;
- fail-closed Ablehnung eines einzelnen unbekannten Backendtyps ohne
  Socket-Abbruch 1011;
- passende WSS-Regressionstests.

Der r2-WSS-Crash war deshalb nicht mehr die Ursache des aktuellen
Feldprotokolls. Eine verwandte Lücke blieb allerdings im HTTPS-Auditpfad
aktuell und wurde in v0016 geschlossen.

## 4. Root Causes aus Baseline und aktuellem r4-Feldlauf

RC-01 bis RC-17 dokumentieren die bereits in der r4-Baseline enthaltenen
v0015→v0016-Korrekturen. RC-18 bis RC-20 sind die aus r4 abgeleiteten
r6-Änderungen. RC-21 stammt aus dem anschließend auf dem realen Referenzstand
wiederholten r6-Core-E2E und begründet r7.

### RC-01 – Fresh-Rollout wurde vor dem Purge durch eine Host-venv blockiert

Beweis im Terminalprotokoll:

```text
BLOCKED host validation environment: the selected directory is non-empty and not managed by this release
```

Die alte Reihenfolge war: Checksummen → Host-Validation/venv → erst später
Fresh-Purge. Dadurch erreichte `--fresh` die Bereinigung nie. Zurück blieben
alte Container im Zustand `Exited (127)`, alte Images und keine neu erzeugte
Proxy-PKI. Ein zweiter Versuch kollidierte mit demselben externen
Validation-Verzeichnis.

Korrektur:

- Host-venv-Bootstrap vollständig entfernt;
- gepinnter Validation-Container eingeführt;
- Fresh-Purge vor Static-Gate und vor Image-Staging verschoben;
- explizite Mode-Pflicht und gegenseitiger Ausschluss von `--fresh` und
  `--upgrade`;
- Restzustand wird nach dem Purge verifiziert.

### RC-02 – Der alte Fresh-Purge war nicht vollständig und nicht beweisend

v0015 entfernte primär Compose-gelabelte Ressourcen. Bekannte alte Volumes,
Legacy-Container, Proxy-Test-PKI, Validation-Images, Python-Caches und
Build-Cache-Semantik waren nicht vollständig erfasst; eine abschließende
Residualprüfung fehlte.

Korrektur:

- bekannte und gelabelte Container/Volumes/Netzwerke werden kombiniert
  entfernt;
- fünf release-eigene Image-Repositories werden purgt;
- generierte PKI/Secrets/TLS/E2E-/Python-Caches werden entfernt;
- alle Builds verwenden `--no-cache`;
- scoped Fresh greift nicht global in fremde Docker-Projekte ein;
- optionaler, doppelt bestätigter `--purge-host` führt auf einem dedizierten
  Host den ausdrücklich verlangten vollständigen Docker-/Cache-Wipe aus.

### RC-03 – HTTPS-Audit konnte PostgreSQL-`inet` nicht als JSON ausgeben

`audit_store.list_audit` reichte psycopg3-`inet`-Objekte durch.
`https_interface._json_safe` kannte die `ipaddress`-Familie nicht. Dadurch
konnte `GET /api/v1/audit` bei realen Auditzeilen in einen HTTP 500 laufen,
obwohl der r3-WSS-Pfad bereits korrigiert war.

Korrektur:

- `source_ip` wird am gemeinsamen Audit-Store-Rand zu kanonischem Text;
- der HTTPS-JSON-Rand akzeptiert zusätzlich Address, Interface und Network
  für IPv4 und IPv6;
- Unit-Test deckt die PostgreSQL-`inet`-Familie ab;
- der Live-E2E liest Audit über WSS und HTTPS und prüft den JSON-Typ.

### RC-04 – Falsche Taskzahl im Parallel-WSS-E2E

Der E2E erlaubte nach dem Paralleltest nur zwei Tasks pro verbliebenem
Client. Der Direct-WSS-Server besitzt konstruktiv vier langlebige Tasks:
Reader, Writer, Auth-Watchdog und Disconnect-Watcher. Bei zwei Retained-
Clients erwartete der Test höchstens fünf, der korrekte Zustand kann aber neun
enthalten. Das erzeugte ein deterministisches False Negative.

Korrektur: Grenzwert `retained_clients * 4 + 1`, wobei `+1` das gerade
laufende `runtime.status` berücksichtigt. Clients, Reservations und
Response-Queue müssen weiterhin vollständig leer beziehungsweise auf dem
Retained-Niveau sein.

### RC-05 – Recovery eines neu erworbenen Master-Terms wurde beim Start verpasst

`master_runtime._seed_if_leader` erwarb den Term über `renew_now`. Dieser Pfad
setzte `notify=False`; `_renew` koppelte `recover_uncertain` aber an eine
akzeptierte Role-Notification. Beim ersten normalen Thread-Renew war der Term
bereits gespeichert und nicht mehr „neu“. Alte PENDING/SENT-Outbox- und
Rolloutzustände konnten deshalb nach einem Failover ungefenced bleiben.

Korrektur:

- nur `renew_now` aktiviert den expliziten Startup-Recovery-Pfad;
- ein neuer Leader-Term löst Recovery genau einmal aus;
- Authority-Services bleiben bis zur akzeptierten Role-Notification nicht
  ready;
- Regression prüft Startup-Renew, nachfolgenden Thread-Renew und exakt einen
  Recovery-Aufruf.

### RC-06 – Validation-Buildkontext konnte generierte Secrets einschließen

Der neue Validation-Dockerfile verwendet den Release-Root als Buildkontext.
Ohne Root-`.dockerignore` könnten bei einem späteren Neubau generierte PKI,
Secrets, Backups und E2E-Logs an den Docker-Builder übertragen werden, obwohl
der Dockerfile sie nicht kopiert.

Korrektur: harte Root-Allowlist für Dockerfile, Requirements und
Dependency-Checker; die Allowlist ist Teil des Validation-Fingerprints und
der statischen Gates.

### RC-07 – E2E-Hostpfade und Containerpfade waren nicht kompatibel

Der Validation-Container mountet den Releasebaum unter `/release` und die
Resultate unter `/results`. Dokumentierte absolute Hostpfade unter `$HOME`
existieren dort nicht. CA- und Pflicht-Logpfade konnten deshalb vor der ersten
Mutation `BLOCKED` enden.

Korrektur: Release- und Resultpfade werden deterministisch in die beiden
Mountziele übersetzt. Nicht gemountete externe Absolutpfade werden fail-closed
abgelehnt. Die v0016-Anleitung schreibt die Live-Logs in das explizit
gemappte Results-Verzeichnis.

### RC-08 – Full-Fallback kollidierte mit Patch-Outbox und ACKs waren nicht monoton

Nach `patch_base_mismatch` plante der Master einen Full-Fallback, verwendete
aber dieselbe content-adressierte `message_id` wie für den vorherigen Patch.
Der strikte Outbox-Store lehnte das geänderte Envelope als Konflikt ab; der
Rollout blieb in `retry_wait`. Zusätzlich konnte ein verspäteter
`received`-/`failed`-ACK einen bereits `applied` oder `converged` markierten
Rollout zurückstufen.

Korrektur:

- der erste Delivery-Versuch behält seine kompatible `rollout_id`;
- nur der Full-Fallback verwendet stabil `rollout_id:fallback-full`, inklusive
  Transfer-/Chunk-Namespace;
- persistente Rolloutübergänge werden unter Row-Lock monoton geprüft;
- verspätete Rückstufungen liefern idempotent `ignored`, ohne den Zustand zu
  verändern;
- ein terminales `failed` wird durch verspätete `received`-/`applied`-ACKs
  nicht wiederbelebt;
- Regression simuliert einen strikten content-adressierten Outbox-Store und
  out-of-order ACKs.

### RC-09 – Actuator-Outbox war zwischen Routing und Enqueue nicht authority-fenced

Die Operation konnte bereits dauerhaft als geroutet existieren, während der
anschließende Outbox-Enqueue ohne aktuellen Leader-Lease-Fence erfolgte. Ein
Lease-Verlust in diesem Fenster ließ damit einen alten Leader Hardware-Intent
stagen. Zusätzlich wurde ein transient fehlgeschlagener Enqueue als
Inbox-Ergebnis terminal gespeichert; derselbe idempotente Retry rekonstruierte
den fehlenden Outbox-Command nicht zuverlässig. Der Legacy-ACK-Cache konnte
einen solchen retrybaren Fehler ebenfalls festhalten.

Korrektur:

- unmittelbar vor dem Enqueue wird die aktive Authority erneut gelesen;
- der Outbox-Write erhält Site, Instance, Lease-Token und Authority-Term als
  atomaren Store-Fence;
- retrybare Actuator-Enqueue-Fehler lassen den Inbox-Eintrag wiederholbar;
- Control- und Legacy-Retry rekonstruieren deterministisch denselben
  Outbox-Command und dieselbe Message-ID, ohne zweite Hardware-Operation;
- retrybare Legacy-Actuator-Fehler werden nicht im In-Memory-ACK-Cache
  terminalisiert;
- Fail-once-, identischer-Retry- und Lease-loss-race-Regressionen decken den
  vollständigen Fehlerpfad ab.

### RC-10 – Semantische No-op-Proposals und Engine-Identität waren nicht geschlossen

Ein leeres oder semantisch identisches vorbereitetes v2-Worker-Proposal konnte
trotz unverändertem Konfigurationsfingerprint einen neuen globalen Commit
erzeugen. Im Legacy-Action-Plan konnte außerdem eine unbekannte, deaktivierte
oder zur `rule_id` widersprüchliche `engine_id` den `required_mode`-Fence
umgehen.

Korrektur:

- Engineering-, Action-Plan- und vorbereitete v2-Proposals vergleichen den
  effektiven Inhalt beziehungsweise den kanonischen Fingerprint;
- ein No-op liefert `changed=false` mit aktueller Revision/Fingerprint und
  erzeugt weder Commit-Journal noch Rollout;
- `rule_id` wird vor jeder Mutation an seine konfigurierte Engine gebunden;
- unbekannte, deaktivierte und widersprüchliche Engines werden fail-closed
  abgewiesen;
- gültige Legacy-Actions ohne explizite Engine sowie explizit konsistente
  Engine-Payloads behalten ihre kompatible Semantik.

### RC-11 – Reordering konnte Sensor- und Heartbeat-Latest-State zurücksetzen

Der Sensor-Upsert akzeptierte `>=` und ließ auf beiden Seiten `NULL` als
Updategrund zu. Damit konnten gleiche, fehlende oder ältere Sequenzen einen
neueren Wert überschreiben. Die Heartbeat-Historie deduplizierte zwar die
Message-ID, das anschließende Update der aktuellen Node-Sicht schrieb Term,
Config- und Replica-Anker jedoch ohne monotone Ordnung zurück.

Korrektur:

- ein bestehender Sensor-Latest-Wert wird nur durch eine nichtleere, strikt
  größere `source_sequence` aktualisiert;
- gleiche, fehlende und ältere Sequenzen liefern `stale=true`, ohne Wert,
  Qualität oder Zeitstempel zu verändern;
- die unveränderliche Heartbeat-Historie wird weiterhin geschrieben, aber ein
  gegenüber der aktuellen Sicht kleinerer Authority-Term, Config- oder
  Node-/Cross-Replica-Stand aktualisiert die Node-Latest-Zeile gar nicht;
- akzeptierte Heartbeats führen Revision/Fingerprint-Paare und optionale
  Global-/Live-Revisionen monoton fort;
- Wire-Contract und Datenbankschema bleiben unverändert.

### RC-12 – Validation-Selbsttest konnte auf Docker-`tmpfs` nicht starten

Der erste reale v0016-r1-Zielhostlauf erreichte nach vollständig bestätigtem
Docker-Host-Purge und Port-Preflight die containerisierte Stufe 3. Das
Validation-Image auf `python:3.14.6-slim-bookworm` wurde korrekt gebaut;
Master- und Proxy-Suiten liefen vollständig grün. Erst
`test_validation_container.py` scheiterte beim direkten Start seines
absichtlich unter `/tmp` erzeugten Fake-`docker`:

```text
/tmp/automation-validation-paths-.../bin/docker: Permission denied
```

Ursache war nicht die Dateimode `0700`, sondern die fehlende explizite
`exec`-Option des Docker-`tmpfs`. Die nachfolgenden fehlenden Container,
Images, PKI-Dateien und Readiness-Verbindungen sind reine Folgefehler, weil
der Rollout korrekt nach Stufe 3 abbrach.

Korrektur:

- Validation-`/tmp` bleibt flüchtig, `nosuid`, `nodev`, größenbegrenzt und
  wird weiterhin nur im non-root, read-only Validation-Container verwendet;
- `exec` wird explizit gesetzt, weil die vertrauenswürdige Regression dort
  erzeugte Fake-CLI-Shims ausführen muss;
- der Test prüft die vollständige Mountoption und verhindert eine erneute
  stille `noexec`-Regression;
- die ZIP-Prüfanleitung beendet sich bei fehlender SHA-Begleitdatei jetzt
  fail-closed, statt nach einem fehlgeschlagenen `sha256sum` weiterzulaufen.

### RC-13 – HTTPS-Mutationsprobe verletzte den eigenen Idempotenzvertrag

Der r2-Zielhostlauf erreichte Direct-WSS, Auth, Event-Subscription, alle
Read-Commands und HTTP-Read-Parität. `HTTP-002` erwartete danach für einen
absichtlich unbekannten Node `404 not_found`, sendete beim mutierenden
`PATCH /api/v1/nodes/...` aber keinen `Idempotency-Key`. Der Proxy lehnte die
Mutation deshalb korrekt bereits mit HTTP 400 ab.

Korrektur: Der E2E-HTTP-Client validiert und überträgt den stabilen,
run-spezifischen Idempotency-Header. Die API-Anforderung bleibt unverändert
streng. Der Selftest prüft CSRF- und Idempotency-Header gemeinsam; unerwartete
HTTP-Statusmeldungen enthalten zusätzlich den nicht sensitiven Fehlercode.

### RC-14 – Terminale Worker-NACKs wurden als `sending` wieder geöffnet

Ein Worker mit persistierter Node-Replica Revision 2 lehnte die frische
Master-Revision 1 korrekt als `stale_replica` ab. Der Outbox-Eintrag wurde
`FAILED`. Jeder folgende Heartbeat setzte denselben Rollout jedoch erneut auf
`staged`; das idempotente Outbox-Enqueue akzeptierte den bereits terminalen
Datensatz als Duplicate und der Rollout wechselte wieder auf `sending`. So
entstand der widersprüchliche Befund `outbox=FAILED, rollout=sending` mit
dreistelligem Attempt-Zähler, ohne entsprechende echte Neuübertragungen.

Korrektur:

- `sending` ist nicht mehr erneut fällig; die persistente Outbox besitzt den
  Delivery-Retry;
- `failed` ist für denselben Rollout terminal und kann nicht durch einen
  späten Staging-/ACK-Pfad wiederbelebt werden;
- Heartbeats öffnen denselben Zustand nur bei einem neuen Authority-Term und
  ausschließlich für nichtterminale Phasen; ein historisches
  `target_missing` ohne Outbox darf nach erfolgter Registrierung rearmed werden;
- ein Duplicate mit Outboxzustand `ACKED` oder `FAILED` endet fail-closed als
  Zustandsinkonsistenz statt als neuer Versand;
- fehlender Node ist retrybar, ein fehlender immutable Scope-Snapshot dagegen
  terminal und separat diagnostiziert.

### RC-15 – Fresh-Control-Plane und persistierter Worker bildeten verschiedene Generationen

`--purge-host` bereinigt absichtlich nur den dedizierten Control-Plane-Host.
Die neue MQTT-PKI ersetzt nicht die persistierte Master-Replica-Baseline des
separaten Workers. Eine ältere Master-Revision darf diese nicht still
überschreiben.

Korrektur: Die Rollback-Sicherung bleibt unverändert. Admission bricht bei
frischem Heartbeat, nichtälterer Worker-Revision und abweichendem Fingerprint
sofort mit `BLOCKED stale_replica` ab und zeigt beide Revisionen. Der
Diagnosepfad klassifiziert `FAILED/stale_replica` explizit. Die Anleitung
verlangt vor einem echten Fresh-Lauf eine saubere, vom separaten Worker-
Release unterstützte Replica-Baseline und behauptet keinen erfundenen
Worker-internen Löschbefehl.

### RC-16 – Strikter E2E-Block konnte die interaktive Shell beenden

Die alte Anleitung setzte `set -Eeuo pipefail` direkt in der Login-Shell und
ließ danach `test "$E2E_RC" -eq 0` fehlschlagen. Je nach SSH-/Terminalstart
beendete das die ausführende Session. Der Admission-Poller blieb zudem bis
zum Timeout still.

Korrektur: `run_core_e2e_lab.sh` kapselt Strict-Mode, Live-Log-Follower,
Worker-Preflight, Passwortvariable, Cleanup und finale Reportprüfung in einem
eigenen Prozess. Der Admission-Poller meldet periodischen Fortschritt und
bricht eine divergente Replica ohne 120-Sekunden-Wartezeit ab.

### RC-17 – Live-Event konnte vor der Filter-Update-Bestätigung eintreffen

Der r3-Zielhostlauf bewies eine gesunde, exakt konvergierte `fn-01`-Replica
und bestand den realen WSS-Pfad bis `EVT-001`. Beim anschließenden atomaren
Filter-Update wartete der E2E-Client jedoch auf genau den nächsten Frame als
`subscription.accepted`. Auf demselben Event-Socket war bereits ein legitimes
`automation.control.v1`-Sensor-Event unterwegs. Dieses traf zuerst ein und
wurde fälschlich gegen das disjunkte `event_response`-Schema validiert. Das
erklärt exakt `event_response JSON contract failed at <root> (oneOf)`.

Korrektur: Der Client liest bis zur über `request_id` korrelierten
Control-Antwort, validiert zwischenzeitliche Event-Envelopes vollständig,
prüft ihre Site-Grenze und puffert höchstens 256 Frames. `_wait_event` und der
Drain-Pfad konsumieren den Puffer vor neuen Socket-Frames, sodass kein Event
verloren geht. Ein echter WebSocket-Selftest sendet absichtlich Event vor ACK
und beweist Korrelation, Erhalt und vollständiges Leeren des Puffers. Der
Server- und Drahtvertrag bleiben unverändert.


### RC-18 – Lokale Token-Idempotenz konnte native PostgreSQL-Werte nicht persistieren

Beweis im bereitgestellten Proxy-Live-Log:

```text
Local action result persistence failed: action=api_tokens.create error_type=TypeError
```

`auth_management.issue_api_token` liefert neben dem einmal sichtbaren Secret
die gespeicherte Token-Metadatenzeile zurück. Psycopg repräsentiert deren
`UUID` und timezone-aware Zeitstempel als native Python-Objekte. Der
Dispatcher redigierte zwar Secretfelder, übergab das Ergebnis danach aber
unverändert an den JSONB-basierten lokalen Idempotenz-Store. Die strikte
JSON-Serialisierung scheiterte deshalb mit `TypeError`; der fachlich bereits
erzeugte Token wurde als `internal_error` ausgegeben und der sichere
Wiederholungswert konnte nicht abgeschlossen werden.

Korrektur in r6:

- `_persistable_local_result` normalisiert den bereits redigierten Wert mit
  der bestehenden gemeinsamen `json_ready`-Grenze;
- erst danach werden bei `api_tokens.create` alle Token-/Secretfelder außer
  `token_id` entfernt und `secret_already_delivered=true` gespeichert;
- die Wire-Antwort des ersten erfolgreichen Aufrufs bleibt unverändert;
- der Regressionstest verwendet echte `uuid.UUID`- und UTC-`datetime`-Werte,
  erzwingt `json.dumps(..., allow_nan=False)` und beweist, dass das Secret
  weder als Schlüssel noch als Wert im persistierten Ergebnis vorkommt.

### RC-19 – Wiederholungslauf verwechselte Replay-Backlog mit Update-Interleaving

Der zweite bereitgestellte Core-E2E öffnete den Event-Socket ohne
`last_event_id`. Der Protokollvertrag interpretiert das als Replay ab Sequenz
0. Die Annahme meldete `replay_high_watermark=289`; im Log wurden unmittelbar
Frames bis mindestens `replay_sequence=264` zugestellt. Beim anschließenden
Filter-Update verwendete r4 die Gesamtlänge von `pending_events` als Grenze
für höchstens 256 **während dieses Updates** interleavte Frames. Ein bereits
vor dem Update vorhandener Replay-Backlog löste daher das falsche Ergebnis aus:

```text
FAILED: event filter update interleave buffer exceeded its bound
```

Der danach im Proxy sichtbare Event-Writer-`TimeoutError` ist ein Folgefehler:
der E2E schloss den Socket während der Proxy noch den großen Replaystrom
sendete. Die Uvicorn-Warnung am HTTPS-Port ist dagegen der erwartete negative
Test eines WebSocket-Upgrades auf dem absichtlich HTTPS-only Listener.

Korrektur in r6:

- normale Live-Abonnements ohne expliziten Cursor lesen zunächst den vom
  Server akzeptierten High-Water-Mark und verbinden genau einmal mit diesem
  Cursor neu;
- Ereignisse, die zwischen beiden Verbindungen entstehen, werden auf der
  zweiten Verbindung regulär replayt; es entsteht kein Verlustfenster;
- Aufrufe mit explizitem `last_event_id` behalten unverändert die vollständige
  Replay-Semantik;
- das Interleave-Limit zählt ausschließlich Frames, die zwischen dem aktuellen
  Update-Request und seiner korrelierten Antwort eintreffen;
- `subscription.accepted`/`subscription.rejected` werden exakt über
  `request_id` korreliert; fremde Control-Frames und `replay.resync_required`
  enden eindeutig fail-closed;
- reale lokale WebSocket-Selftests prüfen den einmaligen Live-Edge-Reconnect,
  einen bereits 256 Frames großen Backlog und ein zusätzlich interleavtes
  Event ohne Verlust.

### RC-20 – Das gelieferte Worker-v0082-ZIP war nicht installierbar verpackt

Das Worker-Originalarchiv war weiterhin ein separates Paket; seine Prüfung
zeigte jedoch drei voneinander unabhängige Verpackungsfehler:

1. `sha256sum -c --strict SHA256SUMS` meldete fünf falsche Einträge:
   zwei aktive Shards, den Standardschecker und zwei Runtime-Dateien;
2. fünf vorhandene Dateien waren nicht manifestiert, darunter drei
   unreferenzierte Alt-Kopien mit Suffix `_` und zwei Paket-`__init__.py`;
3. alle regulären Dateien waren mit Modus `0666`, alle Verzeichnisse mit
   `0777` archiviert. Dadurch scheiterte `test_worker_preflight.py`
   reproduzierbar beim direkten Start von `install_mqtt_identity.sh` mit
   `PermissionError`.

Bei den beiden Shards entsprachen die alten Manifesthashes exakt den
unreferenzierten Suffix-`_`-Kopien, nicht den tatsächlich von Runtime und Tests
verwendeten kanonischen Dateien. Die ausgelieferten aktiven Dateien bestanden
sämtliche fachlichen Tests; deshalb wurde kein Worker-Runtime-Code ausgetauscht.

Korrektur des separaten Gesamtpaket-Artefakts:

- exakt diese drei unreferenzierten Suffix-`_`-Altstände entfernt:
  `_config/config_data/device_state_data_v7_cross_fn-01_.json`,
  `_config/config_data/device_state_data_v7_node_fn-01_.json` und
  `_config/config_data/device_state_data_v7_node_fn-02_.json`;
- beide legitimen Testpaket-`__init__.py` in das Manifest aufgenommen;
- vollständiges, byteweise sortiertes Manifest mit 131 Payloaddateien neu
  erzeugt und strikt gegen den Dateibaum geprüft;
- Verzeichnisse und Shell-Entrypoints auf `0755`, alle übrigen Dateien auf
  `0644` normalisiert;
- deterministisches ZIP zweimal identisch gebaut, CRC, Top-Level-Pfad,
  Entpackmodi und verschachteltes Manifest geprüft;
- der vollständige originale Worker-Entrypoint `./test_all.sh` besteht danach
  `36/36` ausführbare Tests;
- neuer verbindlicher ZIP-Hash:
  `f6bc082cc89deefd570df4718387c09ed38c5184968316dc6a56b94667e071a0`.

Die Control Plane enthält weiterhin keinen Worker-Quellbaum. Das korrigierte
Worker-ZIP liegt nur als separates, eigenständig geprüftes Artefakt im
Gesamtpaket; der E2E-Launcher und seine Fixture pinnen genau dessen neuen Hash.

### RC-21 – v0082-Telemetrie wurde ohne ihre monotone Sequenz persistiert

Der reale r6-Core-E2E bestand vor dem Fehler unter anderem:

```text
TLS-001 PASSED
TLS-002 PASSED
TLS-003 PASSED
WSS-CONTROL PASSED
WSS-EVENTS PASSED
WORKER-FN01 PASSED
```

Danach verging das vollständige Event-Zeitfenster ohne `EVT-001`; der Report
endete mit `FAILED: WSS response timed out`. Damit sind Zertifikatskette,
Hostname, Origin, Authentisierung, beide WSS-Pfade und die Subscription selbst
als unmittelbare Ursache ausgeschlossen. Der Timeout bezeichnete nur den Ort,
an dem der Client das fehlende Upstream-Event bemerkte.

Der Worker v0082 erzeugt nach jedem erfolgreichen Sensor-Store-Commit ein
`SENSOR_VALUE_UPDATE` mit:

```text
sensor_store_sequence = store_entry.sequence
```

Der Master entpackte den verschachtelten Worker-Event korrekt, setzte in r6
aber ausschließlich `payload.get("sequence")` als `source_sequence`. Im
kanonischen v0082-Payload existiert dieses Feld nicht. Der erste Event konnte
noch eine neue `sensor_latest`-Zeile mit `NULL`-Sequenz anlegen; alle weiteren
Events blieben wegen der bewusst strikten SQL-Bedingung
`EXCLUDED.source_sequence IS NOT NULL` stale. Deshalb publizierte der Master
keine neuen `sensor.value`-Events an den Proxy. Der r6-E2E richtete sein
Live-Abonnement korrekt am aktuellen High-Water-Mark aus und konnte den alten
ersten Event daher nicht als neues Live-Ereignis missverstehen.

Minimal-invasive r7-Korrektur:

- am bestehenden Worker-Kompatibilitätsrand bleibt `sequence` als bisherige
  Primärquelle unverändert;
- fehlt dieses Feld, wird die kanonische v0082-`sensor_store_sequence` als
  kompatibler Fallback übernommen;
- boolesche, nicht-ganzzahlige, fehlende und nichtpositive Sequenzen werden
  vor Ownership-, DB- und Eventverarbeitung fail-closed abgelehnt;
- Persistenz, Cross-Routing und Proxy-Publikation bleiben ansonsten unverändert;
- ein funktionaler Test verwendet die reale doppelt verschachtelte
  v0082-Envelopeform, beweist zwei fortlaufende Updates und die daraus
  entstehenden `sensor.value`-Publikationen;
- der E2E benennt bei einem Event-Timeout nun den erwarteten Eventtyp;
- `test_worker_sensor_telemetry.sh` verlangt vor dem E2E einen Fortschritt der
  Master-`source_sequence` und des Proxy-`sensor.value`-Replay-Cursors.

Nicht geändert wurden CA, Zertifikate, TLS-Kontexte, `websockets`-Server,
WSS-Handshake, Origin-Policy, Eventfilter oder Proxy-Fanout. Für diese Ebenen
liegt aus dem r6-Feldlauf positive Evidenz vor.

### RC-22 – Der E2E verwendete `update` für einen Stringwert

Unmittelbar nach `SEC-004 PASSED` fehlen im r7-Report sämtliche erwarteten
Config-Prüfungen `CFG-001` bis `CFG-005`. Der Test sprang direkt in den
Cleanup. Der Quellcode erzeugte dafür:

```json
{"operation":"update","path":"nodes.0.description","value":"..."}
```

Die Master-Änderungsengine definiert `update` absichtlich nur als
Dictionary-Merge. Für einen String endet sie korrekt mit:

```text
invalid_payload: update requires dictionary values
```

Der ursprüngliche Fehler war im Ergebnis nicht sichtbar, weil der nachfolgende
Cleanup ebenfalls fehlschlug und den Primärfehler ersetzte.

Zusätzlich war `nodes.N.description` kein guter E2E-Probekandidat: Die
Beschreibung fließt sowohl in den Node- als auch in den Cross-Shard ein. Der
Legacy-Worker v0082 meldet für `fn-01` zwar Node-Full- und Remote-Actuator-
Fähigkeit, aber keine Cross-Runtime-Fähigkeit. Eine solche Testmutation erzeugt
also unnötig einen dauerhaft retryenden Cross-Rollout.

Minimal-invasive r8-Korrektur:

- der E2E wählt einen aktiv gemappten Owner-Controller des realen Workers;
- bevorzugt wird eine vom Worker selbst gemeldete `controller_uuid`;
- mutiert wird ausschließlich `controllers.N.tags`;
- verwendet wird die Master-konforme Operation `set`;
- vor dem Commit werden die realen Node-/Cross-Shards materialisiert;
- die Mutation wird nur zugelassen, wenn sich der Node-Fingerprint ändert und
  der Cross-Fingerprint bytegenau unverändert bleibt;
- der Selbsttest lädt `initial_config_v2.json` und führt die Änderung über die
  echte Funktion `config_authority.apply_changes()` aus.

### RC-23 – Der Paginationstest blockierte seinen eigenen Event-Client

Der r7-Lauf meldete für `EVT-004A`:

```text
page_size=200
records_produced=217
records_replayed=258
replay_high_watermark=339
live_marker_sequence=340
```

Während diese 217 Read-Commands produziert wurden, blieb der primäre
Event-Socket geöffnet und weiterhin auf `resource_type=command` abonniert. Der
Test las von diesem Socket in dieser Phase nicht. Damit füllte der Test selbst
Client- und TCP-Puffer. Eine Sekunde nach `EVT-004A PASSED` lief der
Proxy-`connection.send()` in den konfigurierten Send-Timeout.

Das ist weder ein Zertifikatsfehler noch ein Defekt des `websockets`-Handshakes.
Der TLS-WSS-Listener, die Subscription und mehrere Replay-/Filterprüfungen
waren zu diesem Zeitpunkt bereits erfolgreich. Die Uvicorn-Warnung zum
Upgrade auf Port `8444` stammt außerdem aus dem absichtlichen Negativtest gegen
den HTTPS-only Listener; der Direct-WSS-Listener läuft getrennt auf `8443`.

Minimal-invasive r8-Korrektur:

- vor der Massenproduktion entfernt der Test den `command`-Filter atomar vom
  dauerhaft gehaltenen Event-Socket;
- der separate Pagination-Socket bleibt auf Command-Events abonniert und prüft
  weiterhin alle Seiten, den High-Water-Mark und den Live-Handoff;
- der Report dokumentiert
  `retained_stream_command_filter_removed=true`;
- ein Send-Timeout eines tatsächlich langsamen Clients wird im Proxy nicht
  mehr als interner Handler-Crash mit Traceback behandelt;
- der Proxy schließt diesen Client kontrolliert mit RFC-6455-Code `1013` und
  dem Grund `send timeout`, räumt Reader/Writer/Watchdog-Tasks auf und lässt
  andere Clients unbeeinträchtigt;
- ein funktionaler Direct-TLS-WSS-Test reproduziert exakt diesen Timeoutpfad.

### RC-24 – Drei verschiedene Revisionen wurden fälschlich gleichgesetzt

Der fehlgeschlagene r7-Cleanup meldete einen Worker, der tatsächlich:

```text
admission_state=APPROVED
observed_state=ACTIVE
online=true
node_replica_revision=1
node_replica_fingerprint=<exakter Baseline-Fingerprint>
observed_global_revision=1
live_config_revision=2
```

war. Diese Werte sind semantisch konsistent. `observed_global_revision` ist die
vom Worker beobachtete globale Authority-Revision. `live_config_revision` ist
die lokale Worker-Revision und kann nach initialem Full-Apply bereits um eins
höher liegen. `node_replica_revision` gehört wiederum zum materialisierten
Node-Scope. r7 verlangte für alle drei Werte dieselbe globale Revision und
wartete deshalb 90 Sekunden auf einen unmöglichen Zustand.

Minimal-invasive r8-Korrektur:

- Readiness führt getrennte Mindestwerte für Authority-, lokale Worker- und
  Node-Replica-Revision;
- die Fingerprint-Bedingung bleibt exakt und fail-closed;
- bei Mutation und Rollback muss jede relevante Domäne monoton voranschreiten;
- ein bereits exakt wiederhergestellter Baseline-Fingerprint wird ohne
  künstlichen zusätzlichen Rollout akzeptiert;
- Timeoutdiagnosen enthalten jetzt pro Teilbedingung `true/false`, beobachtete
  Werte und Erwartungen statt eines unstrukturierten Gesamtdumps;
- der Regressionstest akzeptiert ausdrücklich den realen Legacy-Zustand
  `global=1`, `local=2`, `node_replica=1`.

### RC-25 – Cleanup-Fehler verdeckte die ursprüngliche Testursache

r7 speicherte zunächst die primäre Exception, führte dann korrekt den Cleanup
aus, warf danach aber ausschließlich den Cleanup-Fehler. Deshalb zeigte der
Report nur `config restore ... did not become ... converged`, obwohl die
Config-Mutation zuvor bereits vom Master abgewiesen worden war.

r8 behält die Wiederherstellungspflicht bei, kombiniert aber beide Ursachen in
stabiler Reihenfolge:

```text
FAILED: primary E2E failure: ...; cleanup failure: ...
```

Damit bleibt die erste fachliche Ursache sichtbar, ohne einen fehlgeschlagenen
Restore zu verschweigen. Das Recovery-File bleibt weiterhin verbindlich und
wird nicht automatisch gelöscht.

## 5. Weitere gehärtete Rollout-Invarianten

| Bereich | v0016-Invariante |
| --- | --- |
| Compose | `COMPOSE_FILE`, `COMPOSE_PROJECT_NAME`, `COMPOSE_PROFILES` und `COMPOSE_ENV_FILES` werden am Dispatcher entfernt |
| Modus | genau einer aus `--fresh`/`--upgrade`; `--purge-host` nur mit Fresh |
| Reihenfolge | Fresh-Purge → Ports → Static → Images → Services |
| Ports | 1883, 8883, 8443, 8444, 9090, 55432 und 55433 werden vorab gebunden/geprüft |
| Images | Runtime vor Master; Master ohne falschen Registry-Pull; alle Builds `--no-cache` |
| Integrität | alle ausgelieferten Dateien bleiben manifestgedeckt; nur bekannte Runtimepfade sind beim Wiederholungscheck ausgenommen |
| Prozessgrenze | kein Worker-Paket, -Image, -Container oder -Lifecycle im Control-Plane-Archiv; separates Worker-ZIP nur im Gesamtpaket |
| Validation | read-only Release-Mount, non-root Host-UID, gepinnte Dependency-Closure, kein Host-pip |

## 6. Minimal-invasive Änderungsmatrix

| Datei/Gruppe | Änderung |
| --- | --- |
| `deploy_test_lab.sh`, `rollout.sh` | eindeutiger Modus, neue Reihenfolge, Host-Purge, Port-Gate, Compose-Env-Härtung |
| `scripts/control_plane_lifecycle.sh` | vollständiger scoped Purge, dedizierter Host-Wipe, Residualprüfung, no-cache Builds |
| `runtime/validation/Dockerfile`, `.dockerignore` | gepinnte containerisierte Testumgebung und minimaler Buildkontext |
| `tools/run_in_validation_container.sh` | Fingerprint, read-only Mounts, sichere E2E-Pfadgrenze |
| `requirements-validation.txt` | eine gepinnte kombinierte Dependency-Closure |
| `proxy_gateway/.../audit_store.py` | kanonische `source_ip`-Normalisierung |
| `proxy_gateway/.../https_interface.py` | vollständige `ipaddress`-JSON-Grenze |
| `client_command_dispatcher.py` und Dispatcher-Test | gemeinsame JSON-Normalisierung für lokale Idempotenzresultate; Token-Secret bleibt einmalig |
| `async_websockets_server.py` und Direct-WSS-Test | langsamer Writer wird kontrolliert mit `1013` beendet; kein interner Handler-Traceback |
| `control_plane_wss_e2e_test.py` | scope-sichere `set`-Mutation, getrennte Revisionsdomänen, Pagination ohne Selbst-Backpressure, Primär-/Cleanup-Fehlererhalt sowie bestehende Live-Edge-/Interleave-Korrelation |
| `cluster_orchestrator.py` | einmalige Recovery bei Startup-Termgewinn |
| `rollout_orchestrator.py`, Authority-/Entity-Stores | stabile Delivery-IDs, terminale Outbox-Duplicate-Grenze, nicht wiedergeöffnete `failed`-/`sending`-States |
| `coordinator_service.py`, Legacy-MQTT-Interface | authority-fenced Actuator-Enqueue, rekonstruierbarer retrybarer Idempotenzpfad und v0082-Sensorsequenz-Kompatibilität |
| `config_authority.py`, Proposal-Ingest | echte No-op-Erkennung und fail-closed Rule-/Engine-Bindung |
| `node_store_postgresql.py` | strikt monotone Sensor-Sequenzen und Term-/Replica-Heartbeat-Sicht |
| Validation-Runner und -Regression | explizit ausführbarer, weiterhin flüchtiger und gehärteter Test-`tmpfs` |
| `integration_test/e2e_v0016/` | Versionierung, HTTP-Audit-Parität, HTTPS-Idempotency, korrekte WSS-Taskformel, verlustfreies Event/Update-ACK-Demultiplexing |
| Worker-/E2E-Rootskripte | frühes `stale_replica`, Fortschrittsausgabe, terminal-sicherer Core-E2E-Runner |
| Packaging/Validatoren/Tests | v0016-Pfade, Versionslabels, VCS-/Transient-Rejection, neue positive und negative Gates |
| separates Worker-v0082-Artefakt | vollständiges Manifest, sichere POSIX-Modi, unreferenzierte Alt-Kopien entfernt, deterministischer ZIP-Build |

Die normative WSS-/Action-Protokollspezifikation bleibt v0015. v0016 ändert
keine Drahtaktion, kein Topic und kein Payloadschema; es korrigiert Runtime-,
Rollout- und Testverhalten.

## 7. Bestätigte Restbefunde außerhalb des minimalen Lab-Hotfixes

Die folgenden Befunde sind real und werden nicht als „behoben“ ausgegeben.
Sie blockieren den frischen Single-Leader-Core-E2E nicht, müssen aber vor
einer Produktionsfreigabe architektonisch beziehungsweise mit eigenen
Migrationen geschlossen werden.

| Schwere | Befund | Fresh/Core-E2E | Produktionswirkung |
| --- | --- | --- | --- |
| kritisch | Worker-ACLs erlauben gemeinsame Node-Command/ACK-Namespaces; `require_hmac=False` bindet Payload-Identität nicht kryptografisch an den Broker-Principal | nicht ausgelöst | Worker-zu-Worker-Identitätsspoofing möglich; Protokoll/ACL/Worker gemeinsam ändern |
| mittel | zwischen Auswahl eines fälligen Rollouts und Outbox-Staging endet der Datenbank-Lock; ein inzwischen supersedeter Rollout kann noch einen obsoleten Command enqueuen, auch wenn sein persistenter Zustand nicht mehr wiederbelebt wird | Race nicht im Happy Path | unnötige/stale Übertragung; Worker-Revision/Fingerprint-Fences bleiben letzte Schutzlinie |
| mittel | termtragende Legacy-Commands akzeptieren ACK ohne Term | Kompatibilitätspfad | verspäteter ACK kann Fencing schwächen |
| mittel | Cross-Fact-Sequenz wird vor erfolgreichem Publish als gesehen markiert | Publish-Fehlerpfad | Retry desselben Facts kann verloren gehen |
| mittel | Proposal bleibt bei Crash zwischen Inbox-Dedup und Commit im Zustand `received` | Crashfenster | kein automatischer Fortschritt ohne Recovery-Job |
| mittel | Full-Lab-Worker-Launcher und fn-02-Retained-Publish besitzen zusätzliche externe Prozess-/QoS-Recovery-Risiken | Core unabhängig | Full-Lab nur mit kontrolliertem externem Launcher freigeben |

Diese Punkte absichtlich in einem „minimal-invasiven“ Hotfix halb zu ändern,
würde Broker-ACL, Worker-Kompatibilität, Datenmigration und Safety-Verträge
inkonsistent machen. v0016 ist deshalb als Fresh-/Lab-/Core-E2E-Release
einzuordnen, nicht als abschließend produktionsfreigegebene HA-/Multi-Worker-
Security-Version.

## 8. Verifikation und ehrliche Freigabegrenze

Die finalen lokalen Ergebnisse stehen in
`documentation/RELEASE_VALIDATION_V0016_DE.md` und `TEST_RESULTS.txt`.
Erwartete Zielhost-Gates sind:

1. Gesamtpaket-, Control-Plane- und Worker-ZIP-Hash sowie alle
   verschachtelten Manifestprüfungen;
2. kompletter containerisierter Static-/Unit-/Contract-Lauf;
3. bestätigter Fresh-/Host-Purge ohne Residuen;
4. zehn erfolgreiche Rolloutstufen;
5. laufende Readiness-/Replikations-/DB-/Image-Abnahme;
6. korrigiertes Worker-ZIP mit `36/36`, neuer MQTT-PKI und Preflight;
7. externer Worker admitted und Node-Shard konvergiert;
8. Live-Telemetrie mit steigender Worker-/Master-Sequenz und neuem
   Proxy-`sensor.value`-Replayeintrag;
9. Core-E2E mit Exit 0, `status=PASSED`, vollständiger Core-Prüfmenge und
   bestätigten Restores.

Ohne die Punkte 3 bis 9 auf dem tatsächlichen Docker-/Worker-/Modbus-Teststand
ist das r8-ZIP ein statisch und funktional geprüftes Release-Kandidatpaket, aber
kein behaupteter Live-E2E-PASS. Die schnelle Anleitung macht genau diese
Abnahme reproduzierbar.

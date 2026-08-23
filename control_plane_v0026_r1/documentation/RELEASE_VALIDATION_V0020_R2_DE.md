# Release-Validierung v0020-r2

## Gegenstand

Geprueft wird ausschliesslich der neue Releasebaum
`automation_control_plane_release_v0020_r2` zusammen mit dem getrennten,
finalen `automation_system_v0094.zip`. Die gelieferten Ausgangspakete
`automation_control_plane_release_v0019_r31` und `automation_system_v0092`
werden nicht veraendert.

## Verbindliche Identitaeten

| Artefakt | Identitaet |
|---|---|
| Control Plane | `0.20.0 / v0020-r2` |
| Master | `0.20.0 / v0020-r2` |
| Proxy | `0.20.0 / v0020-r2` |
| aktiver E2E | `e2e_v0020 / 0.20.0-e2e.2` |
| Worker | `automation_system_v0094 / 0.94.0` |
| Worker-ZIP SHA-256 | `788511d4dc65a54dca1e5280415e020b98d8bcf7b4592bb2060c908afb3e1371` |
| Worker-Manifest SHA-256 | `25076d274d2a300bc69cd4f76428ad9cb4c389d375532d59a173b776a1d6f95a` |

## Diagnostischer Nachweis

Die Problem-Logs enthalten 2033 Meldungen
`local-first startup proposal conflicts with live config`, 318 Meldungen
`live configuration materialization failed` und ein in Sammellogs dupliziertes
Ereignis `file proposal was not durably journaled`.

Die Korrelation mit den Codepfaden belegt:

- Der Whole-Config-Fingerprint klassifizierte unbeteiligten Runtime-Drift als
  Konflikt des lokalen Action-Plans.
- Der Lost-Update-Guard kannte nur den aktuellen Heap-Fingerprint, nicht den
  explizit vorbereiteten und bereits journalisierten Dateikandidaten.
- Eine message-id-lose `NO_CHANGE`-Antwort konnte zwischen Prepare und Commit
  entstehen und trotz bereits erreichter Konvergenz einen Rollback ausloesen.
- Der Controller-Cache wurde bei geaenderten PID-/Sensor-/Aktor-Definitionen
  nicht ueber eine vollstaendige Konfigurationsidentitaet invalidiert.

Die Korrektur bleibt auf Worker-Synchronisation, atomare Persistenz,
Controller-Cache, die fachliche V9-Pfadpolicy und die fehlende
Sensor-5-Profilreferenz begrenzt. Bei einer echten Local-vs.-Master-Kollision
gewinnt der Master ohne Rollback; der lokale Kandidat wird einmalig per
WARNING verworfen und danach nicht erneut verarbeitet.

## Worker-Abnahme

- Finales ZIP und Sidecar auf Testsystem .80: PASS.
- Internes `SHA256SUMS`: PASS.
- Ausfuehrbare Shell-Dateimodi aus dem ZIP: PASS.
- Python 3.14.5 no-GIL, exakter entpackter ZIP-Inhalt: 42/42 PASS.
- `_generic_data_center`: 41/41 interne Checks PASS.
- OpenSSL/mTLS, POSIX-Recovery, Preflight und no-GIL-Pfade: PASS.
- Deterministischer Cache-Rebuild bei Gain-/Profil-/Aktor-Aenderung: PASS.
- Identische PID-Konfiguration plus identische Input-/Zeitfolge erzeugt an
  Adresse 524 und 546 dieselbe Registerfolge: PASS.
- Echter Worker-Modbus-Adapter gegen isolierten Simulator: Read/Write/Readback
  fuer 0, 1.25, 2.5, 5 und 10 V PASS; beide Register abschliessend auf 0
  restauriert.

## Control-Plane-Gates

Folgende Gates bestanden im unveraenderten Releasebaum und erneut aus dem
entpackten ZIP:

1. `tools/check_release_code_standards.py`;
2. `tools/validate_release.sh` und `tools/verify_checksums.sh`;
3. Master-, Proxy-, MQTT-, PostgreSQL- und aktives E2E-Static-Gate;
4. Administration-Release-Set mit verpflichtendem Worker-Artefakt;
5. Worker-Lifecycle-, Rollout-, SSH-Reconnect- und Bootstrap-Regressionen;
6. bytegleicher Contract-Bundle-Vergleich zwischen Control Plane und Worker;
7. V9/File-WAL-Cross-Artefakt-Test gegen das angeheftete Worker-ZIP;
8. CRC-, Pfad-, Modus-, internes Manifest- und externes Sidecar-Gate;
9. zweiter Build mit identischem SHA-256.

Der Release-Builder schreibt alle verschachtelten und das Root-`SHA256SUMS`
neu, validiert zuerst den Quellbaum, dann das exakte Schwester-Worker-ZIP und
zuletzt den entpackten Control-Plane-Archivbaum.

## Sicherheits- und Drift-Grenzen

- Keine privaten Schluessel, Zertifikats-Secrets, Credentials, Runtime-Logs,
  Caches, Backups oder generierten E2E-Ergebnisse im Releasebaum.
- Keine symbolischen Links und keine nicht regulaeren ZIP-Mitglieder.
- Keine Klassen, Decorators, `lambda` oder dynamische Codeausfuehrung im
  aktiven Python-Releasecode; `functools.partial` bleibt der Callback-Mechanismus.
- V9 bleibt kanonisches Global-/Node-Format, V8 bleibt Cross-Vertrag und V5
  bleibt expliziter Legacy-Import.
- Master bleibt einzige globale Config-Authority; lokale Worker-Dateiaenderung
  bleibt ein durable Proposal in denselben Commit-/Replikationspfad.
- MQTT, PostgreSQL, PKI, Topics, ACLs, Datenbankmigrationen und
  Client-Protokoll bleiben unveraendert. Master- und Proxy-Aenderungen sind
  auf die gemeinsame fachliche V9-Pfadpolicy und deren generische
  Vertragspruefung begrenzt.

## Abschlussstatus

Worker und Live-Modbus-Nachweis: PASS.

Control-Plane-Abnahme auf Testsystem .82: PASS. Der Host lief mit Python 3.14.4;
die angeheftete Clean-Room-Umgebung mit Python 3.14.6. Nachgewiesen wurden:

- 210 aktive Python-Dateien und 7 eingebettete Python-Programme im harten
  releaseweiten Code-Standard-Gate;
- Master- und Proxy-Unit-/Contract-Suiten sowie MQTT/PostgreSQL-Static-Gates;
- 26/26 Administration-Orchestrator-Tests;
- Lifecycle, Upgrade, Recovery, Rollout, SSH-Reconnect und Image-Staging;
- WSS-E2E v0020, fn-02-Protokoll, XWK-004/005/006 und PAR-002;
- echter v0094-File-WAL-/V9-Cross-Artefaktpfad einschliesslich erstem
  Authority-Abgleich mit exakt null lokalen Proposals;
- Quellbaum- und entpackte Archivvalidierung: PASS;
- zwei unmittelbar aufeinanderfolgende Builds mit identischem SHA-256: PASS.

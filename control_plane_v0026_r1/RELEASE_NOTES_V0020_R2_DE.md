# Release Notes - Automation Control Plane v0020-r2

`v0020-r2` ist der additive, unveraenderliche Nachfolger von `v0020-r1`.
`v0020-r1`, `automation_system_v0093` und alle aelteren Pakete bleiben
unveraendert.

## v0020-r2-Delta

- Proxy, Master-Authority und Worker-Datei-Ingest erlauben denselben
  kanonischen Satz aller fachlichen V9-Top-Level-Abschnitte, einschliesslich
  `pid_configs` und aller gemeinsamen Typ-/Profil-/Methodenkataloge.
- Ein generisches Vertragsgate beweist, dass jede fachliche V9-Sektion durch
  Client -> Proxy -> Master veraenderbar ist und verhindert Policy-Drift.
- Der Commit-Pfad-E2E enthaelt einen echten PID-Gain-Patch und prueft die
  identische File-WAL-/native-Master-Materialisierung.
- Derselbe E2E startet den gepinnten Worker-Praezedenzfall und verlangt
  `master_wins=true`, genau ein Warning, null Rollbacks und Log-Coalescing.
- Das Release pinnt exklusiv `automation_system_v0094` / `0.94.0` und
  `0.20.0-e2e.2`.
- `meta`, Credentials, Zertifikate, Secrets und TLS bleiben unveraenderliche
  Vertrags-/Sicherheitsgrenzen.


## Geerbte v0020-r1-Basis

- Die Control Plane verankert das getrennte Worker-Paket
  `automation_system_v0094` mit dem exakten Archiv-Hash
  `788511d4dc65a54dca1e5280415e020b98d8bcf7b4592bb2060c908afb3e1371`
  und dem exakten internen Manifest-Hash
  `25076d274d2a300bc69cd4f76428ad9cb4c389d375532d59a173b776a1d6f95a`.
- Fresh-, Upgrade-, Administration-, Worker-Lifecycle- und E2E-Pfade pruefen
  dieselben beiden Anker. Ein Quellmirror oder ein aelteres Worker-ZIP ist kein
  zulaessiger Ersatz.
- Der aktive E2E-Vertrag ist als `e2e_v0020` mit Version
  `0.20.0-e2e.2` geschlossen versioniert. Ergebnis-, Recovery-, Marker- und
  Aktionsmatrix-Namen verwenden denselben v0020-Namensraum.
- Sensor 5 besitzt in den aktiven fn-01-Konfigurationen explizit
  `profile_id: 1`. Worker-Node-Datei und Control-Plane-Runtime-Mirror sind
  bytegleich; die globale Master-Initialkonfiguration enthaelt dieselbe
  Sensorprofilzuordnung in ihrem groesseren Site-Datensatz.

## Enthaltener Worker-Fix

Worker v0094 behebt minimal-invasiv sechs gekoppelte Laufzeitfehler:

1. Lokale Datei-Proposals werden gegen ihren betroffenen Runtime-Overlay statt
   gegen den Fingerprint der gesamten, parallel fortschreitenden Config
   verglichen.
2. Das Persistenz-Gate akzeptiert den exakt vorbereiteten und journalisierten
   Kandidaten auch dann, wenn der In-Memory-Heap bereits weitergelaufen ist;
   fremde spaetere Dateiaenderungen bleiben gesperrt.
3. Ein `NO_CHANGE`-Wettlauf gilt nur dann als Erfolg, wenn der vorbereitete
   Action-Plan inzwischen tatsaechlich konvergiert ist.
4. PID-, Sensor- und Aktor-Aenderungen invalidieren den Controller-Cache ueber
   eine deterministische Konfigurationssignatur. Reglergedaechtnis wird nur bei
   identischem Typ und identischer Signatur uebernommen.

5. Bei einem echten Local-vs.-Master-Rennen gewinnt der Master deterministisch;
   der lokale Kandidat wird ohne Rollback geschlossen und genau einmal als
   verworfenes WARNING protokolliert.
6. Wiederholte identische Verarbeitungsfehler werden zeitlich koalesziert und
   mit einem Suppression-Zaehler zusammengefasst, statt die CLI im
   Watcher-Intervall zu fluten.
Die tiefe Ursachenanalyse, Logkorrelation und Kurvenbewertung steht im Worker-
Dokument `DIAGNOSE_CONFIG_PRECEDENCE_V0094_DE.md`.

## Kurven 524 und 546

Die Registerpfade sind technisch identisch skaliert: Profil 2 bildet 0..10 V
auf 0..65535 Rohwert ab; die Anzeige teilt den Rohwert durch 1000. Identische
PID-Konfiguration, identischer Engineering-Input und identischer Zeitverlauf
erzeugen deshalb an 524 und 546 identische Registerfolgen.

Die gelieferten Screenshots zeigen jedoch keine identischen Reglerbedingungen:
524 nutzt State 1/PID 1 mit geglaettetem Sensorprofil und sehr kleinen Gains;
546 nutzte State 21/PID 2 mit deutlich staerkeren Gains und D-Anteil. Zusaetzlich
liefert der Simulator unabhaengige Zufallswerte je Sensor. Der Threshold waehlt
den State, formt aber nicht die PID-Kurve. Das erklaert die glatte 524- und die
sprunghafte bzw. saettigende 546-Kurve.

## Unveraendert

MQTT, PostgreSQL, PKI, Topics, ACLs, Datenbankmigrationen, Client-Protokoll und
Administration-DAG bleiben gegenueber v0020-r1 unveraendert. Master und Proxy
aendern ausschliesslich die gemeinsame fachliche V9-Pfadpolicy. Der Patch
erweitert keine Hardware-/Netzwerkberechtigung und keine zweite Config-Authority.

## Freigabekriterien

- Worker-ZIP extern und intern hashverifiziert;
- Worker 42/42 Tests auf Python 3.14.5 no-GIL aus dem finalen ZIP;
- Control-Plane-Code-, Static-, Komponenten-, Administration-, E2E- und
  Cross-Artefakt-Gates;
- reproduzierbarer Control-Plane-Zweitbuild;
- keine Klassen, Decorators oder Lambda-Ausdruecke im aktiven Releasecode;
- keine privaten Schluessel, Laufzeitlogs, Caches oder lokalen Secrets im ZIP.

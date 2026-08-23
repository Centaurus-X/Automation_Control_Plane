# Release Notes - Automation Control Plane v0020-r1

`v0020-r1` ist der additive, unveraenderliche Nachfolger von `v0019-r31`.
`v0019-r31`, `automation_system_v0092` und alle aelteren Pakete bleiben
unveraendert.

## Eng begrenztes Delta

- Die Control Plane verankert das getrennte Worker-Paket
  `automation_system_v0093` mit dem exakten Archiv-Hash
  `6d090e558e45e2cb42cd808d6128fea74e1a1dd7f954f1e751361d0484ff9668`
  und dem exakten internen Manifest-Hash
  `936fba07d824d477935c7812c769c4ee1e3b443b3117b2ce3870932bb66c257a`.
- Fresh-, Upgrade-, Administration-, Worker-Lifecycle- und E2E-Pfade pruefen
  dieselben beiden Anker. Ein Quellmirror oder ein aelteres Worker-ZIP ist kein
  zulaessiger Ersatz.
- Der aktive E2E-Vertrag ist als `e2e_v0020` mit Version
  `0.20.0-e2e.1` geschlossen versioniert. Ergebnis-, Recovery-, Marker- und
  Aktionsmatrix-Namen verwenden denselben v0020-Namensraum.
- Sensor 5 besitzt in den aktiven fn-01-Konfigurationen explizit
  `profile_id: 1`. Worker-Node-Datei und Control-Plane-Runtime-Mirror sind
  bytegleich; die globale Master-Initialkonfiguration enthaelt dieselbe
  Sensorprofilzuordnung in ihrem groesseren Site-Datensatz.

## Enthaltener Worker-Fix

Worker v0093 behebt minimal-invasiv vier gekoppelte Laufzeitfehler:

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

Die tiefe Ursachenanalyse, Logkorrelation und Kurvenbewertung steht im Worker-
Dokument `DIAGNOSE_HOT_RELOAD_PID_V0093_DE.md`.

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

Master- und Proxy-Produktionslogik, MQTT, PostgreSQL, PKI, Topics, ACLs,
Datenbankmigrationen, Client-Protokoll und Administration-DAG bleiben
gegenueber v0019-r31 unveraendert. Der Patch erweitert keine Hardware- oder
Netzwerkberechtigung und fuehrt keinen parallelen Config-Authority-Pfad ein.

## Freigabekriterien

- Worker-ZIP extern und intern hashverifiziert;
- Worker 42/42 Tests auf Python 3.14.5 no-GIL aus dem finalen ZIP;
- Control-Plane-Code-, Static-, Komponenten-, Administration-, E2E- und
  Cross-Artefakt-Gates;
- reproduzierbarer Control-Plane-Zweitbuild;
- keine Klassen, Decorators oder Lambda-Ausdruecke im aktiven Releasecode;
- keine privaten Schluessel, Laufzeitlogs, Caches oder lokalen Secrets im ZIP.

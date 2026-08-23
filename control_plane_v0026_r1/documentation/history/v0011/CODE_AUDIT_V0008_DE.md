# Code-Audit und Zielhost-Diagnose v0008

**Release:** `automation_control_plane_release_v0008` / `0.8.0`  
**Datum:** 2026-07-14  
**Änderungsbereich:** ausschließlich Control Plane; Worker unverändert

## Ergebnis des übermittelten Zielhostlaufs

Der normale Rollout ist korrekt und gesund. Mosquitto, beide PostgreSQL-
Instanzen, Master und Proxy liefen in den erwarteten v0007-Images. Das
verbindliche Laufzeitgate bestätigte Image-Identität, Health, Replikation,
Migration, Commit/Isolation/Rollback, Rollen und API-Token-Sicherheit.

`test_extended_all.sh` war dagegen **nicht vollständig erfolgreich**. Folgende
Teile liefen nachweislich grün:

1. erste vollständige Laufzeitabnahme;
2. Persistenz nach erzwungenem Primary-Recreate (`DELETE 1`);
3. Streaming-Replikation und physischer Slot;
4. `pg_dump`, `pg_basebackup` und `pg_verifybackup`.

Der nächste Schritt scheiterte mit `find: 'backup': Permission denied`.
Wegen `set -Eeuo pipefail` wurden isolierter Restore, MQTT-Snapshot und die
abschließende zweite Laufzeitabnahme nicht ausgeführt. Das separat danach
ausgeführte `rollout.sh test-running` war vollständig grün; es bestand keine
Beschädigung der laufenden Dienste.

## Technische Ursache

`container-entrypoint.sh` setzte `/backup` mit `install -d -m 0700 -o postgres`.
Da `/backup` ein Host-Bind-Mount ist, wurden Besitzer UID 999 und Modus 0700
direkt auf `postgres_system/backup` des Hosts übertragen. Containerinterne
Backup-Befehle konnten schreiben und verifizieren, der Linux-Benutzer des
Rollouts durfte das Verzeichnis anschließend jedoch nicht traversieren.

## Minimal-invasive Korrekturen

- `POSTGRES_BACKUP_HOST_UID` wird in `setup.sh` aus `id -u` ermittelt, numerisch
  geprüft und beiden PostgreSQL-Diensten übergeben.
- Das Bind-Mount-Verzeichnis erhält Host-UID, Gruppe `postgres`, Modus 2770.
  Fertige Artefakte werden nach `pg_verifybackup` auf 0750/0640 normalisiert.
- Ein aus v0007 geerbtes, für den Host zunächst unzugängliches Verzeichnis wird
  im Setup nicht vorzeitig per Host-`chmod` verändert; Container-root repariert
  es beim Primary-Start. Symlinks werden weiterhin fail-closed abgelehnt.
- `backup.sh` prüft die Host-Lesbarkeit explizit; `restore_test.sh` sucht den
  neuesten Dump containerseitig.
- Der im Zielhost bereits bestätigte Sentinel-Cleanup verwendet dauerhaft
  `WHERE value='$sentinel'`.
- Mosquitto mountet eine generierte Runtime-Konfiguration mit UID/GID 1883 und
  restriktiven Rechten. Veraltete 2.1-Direktiven sind entfernt.
- Der Proxy bleibt Uvicorn/ASGI3 und wählt `websockets-sansio`; ein risikoreicher
  Frameworkwechsel zu aiohttp ist nicht nötig.

## Architektur- und Scope-Prüfung

Die Paketgrenzen bleiben unverändert: Proxy ist externer HTTPS/WSS-Zugang,
Master ist Config-Authority, PostgreSQL hält getrennte Master-/Proxy-Schemata,
Mosquitto transportiert mTLS-geschützte Control-/Statusnachrichten. Weder
Worker-Code noch Contracts, Topics, Payloads, SQL-Schemata oder fachliche
Master-/Proxy-Logik wurden durch den v0008-Patch verändert.

Fresh bleibt der Standard für die Testphase und entfernt ausschließlich die
vier Compose-Projekte, deren benannte Volumes, zwei Control-Plane-Netze und
allowlist-basierten Runtime-State. Upgrade bleibt explizit und importiert
Credentials/PKI aus `CONTROL_PLANE_STATE_SOURCE`, während Volumes erhalten
bleiben.

## Freigabekriterium

Nach lokal bestandenen Release-Gates muss auf `192.168.0.82` genau diese
Version mit `./rollout.sh test --fresh` und anschließend
`./test_extended_all.sh` bis zur Meldung
`Extended persistence, backup and isolated restore tests passed.` laufen.
Erst dann ist die v0008-Recovery-Kette vollständig real bestätigt.

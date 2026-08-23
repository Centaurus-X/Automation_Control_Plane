# Code-Audit und minimal-invasiver Runtime-Patch v0004

**Release:** `automation_control_plane_release_v0004` / `0.4.0`  
**Scope:** ausschließlich Control Plane; Worker-Code unverändert und nicht enthalten

## Reproduzierte Runtime-Befunde

1. Mosquitto konnte Compose-File-Secrets mit Hostmodus `0600` nach seinem
   Privilege-Drop auf UID/GID `1883` nicht lesen. Derselbe Eigentümerkonflikt
   war für PostgreSQL (`999`) und die unprivilegierten Python-Container
   (`65532`) latent vorhanden.
2. Nach Behebung der Secret-Rechte lief Mosquitto stabil, wurde aber als
   `unhealthy` markiert. Der Healthclient sendete eine leere Client-ID, während
   `allow_zero_length_clientid false` korrekt aktiviert war.
3. Der autorisierte MQTT-Livetest konnte das Test-PKI nicht öffnen. Der
   Tools-Container verlor mit `cap_drop: ALL` den DAC-Override und lief nicht
   unter der UID des Eigentümers des `0700`-PKI-Verzeichnisses.
4. `test-running` wartete auch dann den kompletten Health-Zeitraum, wenn ein
   Container überhaupt nicht existierte. Dies wirkte wie ein Hänger.
5. Ein in der interaktiven Login-Shell gesetztes `set -e` kann die SSH-Shell
   nach dem erwarteten Rollout-Fehler beenden. Kein Release-Skript beendet oder
   signalisiert den SSH-Dienst oder seine Parent-Shell.

## Implementierte Korrekturen

- Jedes der vier eigenständigen Pakete enthält seinen eigenen
  `scripts/prepare_compose_secrets.sh`. Nach Secret-Erzeugung und Image-Build
  setzt ein kurzlebiger, netzloser Helper die Dateien auf Host-UID,
  Runtime-GID und Modus `0640`. Die Helper besitzen nur `CHOWN`, `FOWNER` und
  `DAC_OVERRIDE`; Secret-Inhalte werden nicht ausgegeben.
- Der MQTT-Healthcheck verwendet `automation-mqtt-health` als Client-ID.
- Alle autorisierten MQTT-Testclients erhalten eindeutige Client-IDs und laufen
  mit der Host-UID/GID. Der negative Test ohne Client-Zertifikat besitzt
  ebenfalls eine Client-ID, sodass er tatsächlich die mTLS-Grenze prüft.
- `test-running` meldet einen fehlenden Dienst sofort. Aggregierte Rollouts
  protokollieren acht Stufen und nennen bei Abbruch die aktuelle Stufe.
- Images, VERSION-Dateien, SBOM-Metadaten und Releasevalidator wurden konsistent
  auf `0.4.0` beziehungsweise `automation-postgres:18.4-v4` angehoben.

## Unveränderte Grenzen

- Keine Datenbankmigration und keine Änderung von Tabellen oder Rollen.
- Keine Änderung von Master-/Worker-Topics oder Payload-Verträgen.
- Keine Änderung am Worker und keine automatische Worker-Ausbringung.
- Benannte Volumes werden bei Rollout und Stop nicht gelöscht.
- Port `1883` und anonymer MQTT-Zugriff bleiben verboten.

## Freigabegates

Vor Archivierung müssen Shell-Syntax, JSON, Python-Kompilierung, Unit- und
Policytests, Release-Hygiene, Versionskonsistenz und SHA-256-Manifeste bestehen.
Da im Erstellungscontainer kein Docker-Daemon und kein Zugriff auf das private
Labornetz verfügbar sind, bleiben Image-Build und der achtstufige Runtime-Test
verpflichtende Zielhost-Gates auf `192.168.0.82`.

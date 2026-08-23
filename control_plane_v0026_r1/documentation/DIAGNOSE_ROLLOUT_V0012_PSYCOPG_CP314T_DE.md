# Diagnose: psycopg auf CPython 3.14t

Stand: 16. Juli 2026  
Korrekturstand: `r3`

## Fehlerbild

Der Fresh-Purge aus r2 lief korrekt und entfernte den alten v0011-Stand. Das
anschließende Image-Staging baute CPython 3.14.6t und PostgreSQL, brach jedoch
beim Master ab:

```text
No matching distribution found for psycopg-binary==3.3.4
Lab rollout failed in stage: 2/8 image staging.
```

Leere `rollout.sh status`-Tabellen und nicht erreichbare Ports 9090/8443 sind
in diesem Fall Folge der echten Fresh-Reihenfolge: Alte Container sind bereits
entfernt, neue wurden wegen des fehlgeschlagenen Builds noch nicht gestartet.

## Ursache

Der Master muss gemäß Spezifikation unter CPython 3.14t/free-threaded laufen.
Sein ABI ist `cp314t`. `psycopg-binary==3.3.4` stellt auf PyPI Wheels für
`cp310` bis `cp314`, aber kein `cp314t`-Wheel und keine Source-Distribution
bereit. Das Extra `psycopg[binary]` kann im Master daher nicht aufgelöst werden.

`psycopg==3.3.4` ohne Extra ist eine portable Pure-Python-Distribution. Diese
Implementierung benötigt zur Laufzeit das System-`libpq`. Auf Debian liefert
`libpq-dev` die Clientbibliothek `libpq5` als Abhängigkeit sowie die bereits
zum Runtime-Konzept passenden Header.

## Wichtige Master-/Proxy-Trennung

Der Proxy ist nicht vom gleichen ABI-Problem betroffen. Er basiert auf
`python:3.14.6-slim-bookworm`, also normalem `cp314` mit GIL. Für dieses ABI
existiert das gepinnte `psycopg-binary`-Wheel. Deshalb bleibt der Proxy
minimal-invasiv auf `psycopg[binary]==3.3.4` und benötigt kein separates
System-`libpq`.

Die beiden Grenzen werden explizit erzwungen:

| Dienst | Python | psycopg | erzwungene Implementierung |
| --- | --- | --- | --- |
| Master | CPython 3.14.6t | `psycopg==3.3.4` + System-libpq | `python` |
| Proxy | CPython 3.14.6 | `psycopg[binary]==3.3.4` | `binary` |

`check_runtime_dependencies.py` importiert alle Pakete im Image, prüft die
gewählte psycopg-Implementierung und verlangt eine positive libpq-Version. Ein
versehentlicher Rückwechsel oder eine fehlende Clientbibliothek bricht bereits
den Image-Build ab.

## Host-Python

`python3 --version` auf dem Zielhost meldete `3.14.4`. Das ist kein Fehler:
Host-Python führt nur portable Prüf- und Orchestrierungsskripte aus. Master und
Proxy verwenden ausschließlich ihre Docker-Runtimes 3.14.6t beziehungsweise
3.14.6. Ein Host-Upgrade oder Downgrade ist für diesen Fix nicht erforderlich.

## Patchumfang

- `master_node/requirements.txt`: Binary-Extra entfernt;
- `runtime/python314t/Dockerfile`: `libpq-dev` ergänzt;
- Master: `PSYCOPG_IMPL=python`;
- Proxy: `PSYCOPG_IMPL=binary`;
- Runtime-Dependency-Gate prüft Implementierung und libpq-Version;
- statischer Regressionstest prüft beide ABI-Grenzen;
- SBOM, Dokumentation und SHA-256-Manifeste aktualisiert.

Der Master-/Proxy-Anwendungscode, Datenbankzugriff, MQTT, Sharding und die
externe Worker-Grenze bleiben unverändert.

## Verifikation

Im Erstellungsumfeld ohne Docker wurden sämtliche statischen, funktionalen,
Manifest- und Archivtests ausgeführt. Der reale Zielhostnachweis bleibt:

```bash
./rollout.sh --fresh --profile lab_v2
./rollout.sh status
curl -f http://127.0.0.1:9090/health/ready
curl -fk https://192.168.0.82:8443/api/v1/health/ready
```

Erst ein vollständig erfolgreicher Acht-Stufen-Lauf macht die Docker-/no-GIL-/
mTLS-/PostgreSQL-Gates zu `PASS`.

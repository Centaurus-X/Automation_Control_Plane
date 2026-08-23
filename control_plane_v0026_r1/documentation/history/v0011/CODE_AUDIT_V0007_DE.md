# Code-Audit und Rollout-Diagnose v0007

**Release:** `automation_control_plane_release_v0007` / `0.7.0`  
**Datum:** 2026-07-14

## Verifizierte Hauptursache

Der Zielhost meldete nach dem vermeintlichen v0006-Rollout weiterhin:

```text
automation-proxy-gateway:0.5.0
automation-master-node:0.5.0
automation-postgres:18.4-v5
```

Der Trace aus Uvicorns `asgi2.py` stammt deshalb nachweislich aus dem alten
v0005-Proxy. Die bereits in v0006 vorhandene explizite
`interface="asgi3"`-Konfiguration konnte in diesem Container nicht enthalten
sein.

Der übermittelte Rollout-Output endete nach den ersten beiden statischen
Testgruppen, noch bevor eine nummerierte Deploy-Stufe ausgegeben wurde. Das
isoliert den Abbruch auf `tools/test_upgrade_state.sh`. Der Test übergab dem
Importer absichtlich einen leeren Quellpfad. Die Expansion
`${1:-${CONTROL_PLANE_STATE_SOURCE:-}}` unterschied jedoch nicht zwischen
„explizit leer“ und „nicht übergeben“. Eine in der SSH-Sitzung noch gesetzte
Vorgängervariable wurde dadurch ungewollt übernommen. Die folgende Fresh-
Assertion brach mit Status 1 und ohne eigene Diagnose ab. Das fail-closed
Verhalten war sicher, aber operativ zu still.

## Minimal-invasive Korrekturen

1. `scripts/import_runtime_state.sh` wertet einen vorhandenen ersten Parameter
   exakt aus. Nur wenn überhaupt kein Parameter übergeben wurde, wird die
   Umgebungsvariable verwendet.
2. Der Upgrade-State-Test benennt jedes Szenario und gibt erwarteten sowie
   tatsächlichen Status aus. Eine gesetzte Störvariable ist Teil der
   Regression.
3. Lab verwendet standardmäßig Fresh-Modus. Nach erfolgreichem Image-Staging
   werden nur die bekannten Compose-Projekte, deren gelabelte Volumes, die
   beiden exakten Netznamen und die allowlist-basierten Runtime-Verzeichnisse
   des aktuellen Releases bereinigt.
4. Upgrade ist nur über `--upgrade` aktiv und benötigt einen expliziten,
   vorhandenen Quellpfad. Ein Konflikt zwischen vollständigem Ziel-State und
   Quelle wird abgelehnt.
5. Produktion verlangt `--upgrade` oder `--fresh`; ein produktiver Purge
   benötigt zusätzlich `CONTROL_PLANE_CONFIRM_PURGE=YES`.
6. `test_running_all.sh` verifiziert die laufenden Image-Referenzen vor den
   Healthchecks. Zusätzlich tragen PostgreSQL, Master und Proxy ein festes
   OCI-Release-Label `0.7.0`. Ein gesunder alter Container oder ein falsch
   getaggtes altes Image ist kein bestandener Rollout.

## Entscheidung Uvicorn versus aiohttp

Der Proxy ist als Starlette-/ASGI-Anwendung mit einem ASGI3-Adapter aufgebaut.
Uvicorn ist der dazu passende Prozessserver und kann das Interface explizit
festlegen. aiohttp stellt ein eigenes HTTP-Anwendungs-, Routing-, Middleware-
und WebSocket-Modell bereit; es ist kein austauschbarer ASGI-Prozessserver.
Eine Migration würde zahlreiche sicherheitsrelevante Requestpfade neu schreiben
und widerspräche dem minimal-invasiven Auftrag. Deshalb bleibt Uvicorn erhalten,
mit explizitem ASGI3-Modus und direktem Adaptertest.

## Unveränderte Bereiche

- Worker-Code und Worker-Paket
- MQTT-Topics, Payloads, Zertifikatsidentitäten und ACL-Policy
- PostgreSQL-Schemata und Migrationen
- Master-/Proxy-Geschäftslogik
- Authentifizierung, RBAC, CSRF, Sessions und Token-Semantik

## Verifikationsgrenze

Shell-, Python-, JSON-, YAML-, Contract-, Security-, Lifecycle-, State- und
Archivgates werden im Erstellungsumfeld ausgeführt. Der reale Image-Build,
Containerstart, MQTTS-Netztest, PostgreSQL-Replikation und Worker/Modbus-
Ende-zu-Ende-Test benötigen den Docker-Host `192.168.0.82` und werden nicht
ohne Zielhostbeleg als bestanden behauptet.

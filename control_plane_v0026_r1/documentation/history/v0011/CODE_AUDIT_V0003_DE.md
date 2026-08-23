# Vollständiger Code-Audit und Patchbericht v0003

**Basis:** unveränderte, manifestgeprüfte v0002  
**Release:** automation_control_plane_release_v0003 / 0.3.0  
**Zielbetrieb:** ein Worker fn-01, ein aktiver Master und ein Proxy auf dem
Control-Plane-Host 192.168.0.82

## 1. Diagnoseergebnis

Der externe v0002-Prüfbericht wurde vollständig gegen die Implementierung
geprüft. Die beiden beschriebenen Proxy-P0-Lücken waren im Code reproduzierbar:

1. /api/v1/users verlangte zwar user.manage, übernahm role_name danach aber
   ohne Rollen- oder Permission-Grenze.
2. authenticate_api_token() setzte resource_scopes immer auf eine leere Liste;
   die Policy wertete das bei einem Resource-Typ ohne passende Scopes als
   unbeschränkt aus.

Auch der P1-Befund zur fest eingebauten Browser-Origin war korrekt. Die drei
Befunde sind in v0003 geschlossen. Dafür war keine Datenbankmigration und keine
Änderung an Worker, Master-Protokoll oder MQTT-ACL nötig.

## 2. Architektur und Datenfluss

    Browser/App -- HTTPS/WSS --> Proxy -- MQTTS/mTLS --> Mosquitto
                                      |                    |
                                      |                    +--> Master --> Worker
                                      |                           |
                                      +------ PostgreSQL <--------+

- Der Proxy ist die einzige Benutzer-Access-Plane. Authentifizierung, CSRF,
  Rate Limit, RBAC, Site-/Resource-Scope und Audit liegen vor jedem fachlichen
  Command.
- Der Master ist die DB-gefencete Config- und Operations-Authority. Nur der
  Leader startet Worker-Ingress und globale Automation.
- PostgreSQL ist die persistente Wahrheit für Authority, Config, Registry,
  Operations, Benutzer, Sessions, Tokens, Audit, Inbox und Outbox.
- Mosquitto ist ausschließlich Transportgrenze auf MQTTS/8883 mit mTLS und
  CN-basierten ACLs.
- Der Worker bleibt alleiniger Hardware-/Modbus-Owner und ist nicht Bestandteil
  dieses Releases.

Der neue Rollenpfad lautet:

    Session/API-Token
      -> user.manage + Site prüfen
      -> Zielsite muss exakt site_main sein
      -> Rolle aus PostgreSQL laden
      -> Rollen-Permissions müssen Teilmenge der Principal-Permissions sein
      -> erst danach Passwort hashen und Benutzer + Grant atomar speichern

Der neue API-Token-Pfad lautet:

    Tokenhash laden
      -> aktuellen Benutzerzugriff aus PostgreSQL laden
      -> Token-Permissions ∩ aktuelle Benutzer-Permissions
      -> Token-Sites ∩ aktuelle Benutzer-Sites (Wildcard-semantisch)
      -> aktuelle Benutzer-Resource-Scopes übernehmen
      -> Principal an dieselbe Access Policy wie Sessions übergeben

Damit kann ein Token nie mehr Rechte als sein gespeichertes Token-Ceiling oder
der Benutzer zum aktuellen Zeitpunkt besitzen. Rollen- und Site-Entzüge wirken
beim nächsten Request ohne Token-Neuausstellung.

## 3. Minimal-invasive Änderungen

Produktiv funktional geändert wurden ausschließlich:

- proxy_gateway/src/core/access_policy.py
- proxy_gateway/src/core/auth_management.py
- proxy_gateway/src/interfaces/https_interface.py
- proxy_gateway/src/system/bootstrap_system_interface.py
- proxy_gateway/compose.yaml
- Root-Lab-/Produktionsdeployment für die Origin-Variable

Additiv hinzugekommen sind drei isolierte Regressionstestdateien und ein realer
PostgreSQL-Security-Probe. Versions-, SBOM-, Validator- und Dokumentationsdaten
wurden auf 0.3.0 angehoben. Master-, Broker- und PostgreSQL-Anwendungslogik
sowie beide SQL-Migrationen sind gegenüber v0002 unverändert.

## 4. Geschlossene Berichtsbefunde

| Befund aus dem v0002-Bericht | v0003-Status | Nachweis |
|---|---|---|
| P0 Rollen-Eskalation | geschlossen | 6 Rollen-Grant-Checks + echter DB-Rollenkatalog |
| P0 API-Token Resource-Scope-Bypass | geschlossen | 5 Token-Checks + echter DB-Token-Probe |
| P1 Origin nicht deploybar | geschlossen | 4 Config-Checks + Produktionspreflight |
| Reale DB-Sicherheitsprobe fehlte | ergänzt | tatsächlicher Hash/Store/Scope/Revoke/Cleanup |

Die Origin-Liste wird aus PROXY_ORIGIN_ALLOWLIST als CSV geladen, getrimmt,
normalisiert und dedupliziert. Leere Einträge, Credentials, Pfade, Querys,
Fragmente und ungültige Ports werden verworfen. Produktion akzeptiert nur
https://; http:// ist ausschließlich mit explizitem Lab-Testmodus möglich.

## 5. Bewusst offene Grenzen

Diese Punkte wurden nicht in den Security-Hotfix hineingemischt:

- Die Legacy-Worker-Payload enthält noch keine vom Master beweisbare Bindung
  zwischen Zertifikat, source-gebundenem Topic und source_node. Deshalb keine
  Multi-Worker-Produktionsfreigabe.
- Automatisches logisches Multi-Master-Routing nach Lease-Wechsel fehlt.
- Offene Proxy-Commands besitzen noch keine instanzübergreifende Claim-Lease.
- Das 4-MiB-App-/MQTT-Packet-Overhead-Grenzverhalten bleibt als P2-Härtung offen.
- CVE-/SCA-Scan, Signaturprüfung und Digest-Pinning aller Basisimages sind noch
  kein automatisches Release-Gate.
- Docker-, LAN-, Worker- und Modbus-End-to-End-Gates müssen auf dem Zielhost
  ausgeführt werden.

Für den vorgesehenen isolierten Ein-Worker-Pilot sind die Proxy-P0-Blocker
geschlossen. Eine allgemeine Produktions- oder HA-Freigabe wird nicht
behauptet.

## 6. Release-Gates

Lokal/statisch:

    ./test_static_all.sh
    ./tools/verify_checksums.sh

Auf 192.168.0.82:

    CONTROL_PLANE_BIND_ADDRESS=192.168.0.82 \
    PROXY_ORIGIN_ALLOWLIST=https://192.168.0.82:8443 \
    ./rollout.sh test
    ./rollout.sh test-running
    ./test_extended_all.sh

Erst wenn alle drei Zielhost-Kommandos sowie Worker-/Modbus-Abnahme grün sind,
ist der Ein-Worker-Pilot freigabefähig.

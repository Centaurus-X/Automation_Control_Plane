# Verbindliche WSS-Action- und Payload-Verträge v0015

Version: `automation.proxy.websocket.v1`  
Status: normativer Entwicklungsvertrag für den gemeinsamen Proxy-Dispatcher

Dieses Dokument legt fest, wie post-authentisierte WSS-Anfragen auf die
bereits vorhandenen v0014-Funktionen abgebildet werden. Es erweitert den
Transport, nicht die fachliche Authority: Config-Commits, Node-Zustände,
Admission und Aktuatoroperationen bleiben Master-Entscheidungen.

## 1. Gemeinsamer Requestvertrag

Jede Nachricht auf `/ws/v1/control` besitzt genau diese Top-Level-Felder:

```json
{
  "protocol": "automation.proxy.websocket.v1",
  "type": "command.request",
  "request_id": "abc-123",
  "action": "config.propose",
  "idempotency_key": "site_main:change-4711",
  "ttl_s": 30,
  "csrf_token": "browser-session-csrf",
  "payload": {}
}
```

* `request_id`: 1–128 Zeichen, Pattern `[A-Za-z0-9._:-]+`; nur
  Transportkorrelation.
* `action`: ausschließlich ein Name aus der Registry in Abschnitt 3.
* `payload`: JSON-Objekt; unbekannte Felder werden abgelehnt.
* `idempotency_key`: 1–128 Zeichen; für jede Mutation verpflichtend und über
  Retries stabil. Die Eindeutigkeit gilt mindestens pro Site, Principal und
  Action.
* `ttl_s`: endliche Zahl von 1 bis 300 Sekunden. Ohne Angabe gilt 30. Für
  Mutationen muss der gemeinsame Dispatcher den Wert bis in den bestehenden
  Master-/MQTT-Aufruf weiterreichen.
* `csrf_token`: nur für cookie-authentisierte mutierende Requests
  verpflichtend. Bearer-API-Tokens benötigen kein CSRF, bleiben aber RBAC-,
  Scope-, Rate-Limit- und Audit-pflichtig.

Der Transport prüft nur Hüllenform, Größen- und Typgrenzen. Action-Registry,
CSRF, RBAC, Ressourcen-Scope, Rate-Limit, Idempotenz, Audit und fachliche
Validierung gehören in **einen** gemeinsamen REST/WSS-Dispatcher. REST und WSS
dürfen deshalb keine getrennten Mutationsimplementierungen besitzen.

## 2. Gemeinsames Ergebnis

Der Dispatcher liefert intern genau einen der folgenden Zustände:

* `{ok: true, state, operation_id?, reply?}`;
* `{ok: false, code, message?, details?}`.

Der gemeinsame Dispatcher normalisiert sichere Zusatzinformationen wie
`fields`, `reset_at`, Retry- und Uncertainty-Hinweise in `details`. Credential-,
Cookie-, CSRF-, Token- und Passwortwerte werden vorher rekursiv redigiert. Der
WSS-Transport reicht nur dieses normalisierte Objekt weiter; unbekannte
Top-Level-Fehlerfelder oder rohe Exceptiondaten werden nicht gespiegelt.

WSS bildet dies auf `command.accepted`, optional unmittelbar auf
`command.result`, oder auf `command.rejected` ab. Asynchrone fachliche
Ergebnisse erscheinen zusätzlich korreliert auf `/ws/v1/events`. Ein
`command.accepted` ist **keine** Bestätigung der Hardwarewirkung; dafür muss
die Operation `CONFIRMED` und – bei Aktuatoren – das Readback erfolgreich
sein.

Die bestehenden v0014-Fehlercodes bleiben maßgeblich, unter anderem
`authentication_required`, `authorization_denied`, `invalid_payload`,
`conflict`, `stale_revision`, `not_found`, `not_ready`, `expired`,
`not_config_authority` und `internal_error`.

## 3. Action-Registry

`Lokal` bedeutet Proxy-PostgreSQL beziehungsweise Proxy-Runtime. `Master`
bedeutet eine feste, allowlistete Action über den bestehenden Command-Gateway;
Actionnamen werden niemals zu frei zusammengesetzten MQTT-Topics.

| Action | Payload | Pflichtrecht | Ziel | Mutation |
|---|---|---|---|---|
| `users.list` | `{limit?}` | `user.manage` | Lokal | nein |
| `user.create` | siehe 3.1 | `user.manage` | Lokal | ja |
| `roles.list` | `{}` | `role.manage` | Lokal | nein |
| `permissions.list` | `{}` | `role.manage` | Lokal | nein |
| `api_tokens.list` | `{limit?}` | authentifizierter Principal | Lokal | nein |
| `api_tokens.create` | siehe 3.2 | authentifizierter Principal | Lokal | ja |
| `api_tokens.revoke` | `{token_id}` | eigener Token | Lokal | ja |
| `sites.list` | `{}` | Site-Scope | Lokal | nein |
| `nodes.list` | `{}` | `node.read` | Master `nodes.list` | nein |
| `node.get` | `{node_id}` | `node.read` + Resource-Scope | Master `nodes.list`, exakt filtern | nein |
| `runtime.status` | `{}` | `node.read` | Master `runtime.status` plus Proxystatus | nein |
| `config.get` | `{revision?}` | `config.read` | Master `config.get` | nein |
| `config.history` | `{limit?}` | `config.read` | Master `config.history` | nein |
| `config.diff` | `{from_revision,to_revision}` | `config.read` | zwei `config.get`, lokaler Diff | nein |
| `config.propose` | siehe 3.3 | `config.propose` + Pfadpolicy | Master `config.propose` | ja |
| `config.rollback` | siehe 3.4 | `config.rollback` | Master `config.rollback` | ja |
| `actuator.request` | siehe 3.5 | `actuator.request` + Controller-Scope | Master `actuator.request` | ja |
| `operation.get` | `{operation_id}` | `operation.read` | Master `operation.get` | nein |
| `audit.list` | `{limit?}` | `audit.read` | Lokal | nein |
| `node.drain` | siehe 3.6 | `node.drain` + Node-Scope | Master `node.drain` | ja |
| `node.state.set` | siehe 3.7 | `node.drain` + Node-Scope | Master `node.state.set` | ja |
| `node.admission.approve` | siehe 3.8 | `node.admit` | Master `node.admission.approve` | ja |
| `terminal.execute` | `{command}` | `terminal.execute` | Master `terminal.execute` | ja |

Ein unbekannter Actionname liefert `invalid_payload`; er wird nicht an MQTT,
eine Datenbankfunktion oder eine Shell weitergereicht.

### 3.1 `user.create`

```json
{
  "username": "operator-02",
  "password": "secret-from-secure-input",
  "display_name": "Operator 02",
  "email": "operator-02@example.invalid",
  "enabled": true,
  "role_name": "operator",
  "site_id": "site_main"
}
```

Pflicht: `username`, `password`. Optional: die übrigen Felder. Passwort- und
Benutzernamenregeln stammen unverändert aus `auth_management` und
`token_security`; das WSS-Gate darf sie nicht duplizieren. Rollenvergabe wird
mit `authorize_role_grant` auf die verwaltete Site begrenzt. Passwörter und
Tokens dürfen weder im Audit noch im Log erscheinen.

### 3.2 API-Tokens

Create:

```json
{
  "name": "e2e-client",
  "permissions": ["node.read", "config.read"],
  "site_scopes": ["site_main"],
  "expires_at": "2026-07-19T00:00:00Z"
}
```

`name` ist Pflicht. Rechte und Sites dürfen nur Teilmengen des ausstellenden
Principals sein. `expires_at` ist optional und muss vom bestehenden Auth-Core
als gültiger UTC-Zeitpunkt akzeptiert werden. `api_tokens.revoke` akzeptiert
nur `{ "token_id": "<UUID>" }` und darf standardmäßig nur einen eigenen Token
widerrufen. Der Klartext eines neu erzeugten Tokens erscheint einmalig im
direkten `command.result`, niemals im Event-Fanout, Snapshot, Audit oder
persistierten Idempotency-Ergebnis. Ein Retry mit demselben Key erzeugt keinen
zweiten Token und liefert nur dieselbe Token-ID/Metadaten plus
`secret_already_delivered=true`. Ging die einmalige Antwort verloren, muss der
Client diesen Token widerrufen und mit einem neuen Key neu ausstellen.

Bei `permissions` und `site_scopes` gilt eindeutig: Feld **weggelassen** = vom
Principal erben; explizit `[]` = leerer Scope. Der v0015-Dispatcher muss dafür
auf Feldanwesenheit statt auf Python-`or` prüfen. Damit kann ein Least-
Privilege-Client nicht versehentlich einen breiteren Token erzeugen.

### 3.3 `config.propose`

```json
{
  "base_revision": 41,
  "changes": [
    {"operation": "update", "path": "rules.0.threshold", "value": 76}
  ],
  "reason": "WSS v0015 E2E",
  "rollout_targets": ["fn-01"]
}
```

Pflicht: nichtnegative `base_revision` und nichtleeres `changes`. Jeder Change
verwendet exakt die vom bestehenden `authorize_config_changes` und
Master-Validator unterstützte Operation/Pfadform; v0015 führt keine zweite
Patchsprache ein. `reason` und `rollout_targets` sind optional. Die Master-
Authority validiert, erzeugt die nächste monotone Revision, persistiert den
Commit und koordiniert den Rollout. Der Proxy darf die Config nicht selbst
committen.

### 3.4 `config.rollback`

```json
{
  "base_revision": 42,
  "target_revision": 41,
  "reason": "E2E restore",
  "rollout_targets": ["fn-01"]
}
```

Beide Revisionen sind positive Integer. Ein Rollback erzeugt eine **neue**
monotone Revision mit semantisch restauriertem Inhalt; er setzt den
Revisionszähler nicht zurück.

### 3.5 `actuator.request`

```json
{
  "controller_uuid": "62671112-766c-59ed-b0ff-30a49d48f747",
  "actuator_id": 1,
  "value": true,
  "idempotent": false,
  "device_command_id_supported": false
}
```

Pflicht: `controller_uuid`, `actuator_id`, `value`. Die beiden booleschen
Felder sind optional und standardmäßig `false`. Owner-, Controller-, Safety-
und Aktuatorvalidierung bleiben im bestehenden Master-/Workerpfad. Der
E2E-Erfolg benötigt `CONFIRMED`, `write_succeeded=true`,
`readback_checked=true` und `readback_verified=true`.

### 3.6 `node.drain`

```json
{"node_id": "fn-01", "reason": "maintenance", "deadline_s": 30}
```

`node_id` ist Pflicht. `reason` und eine fachlich auf 0–86400 Sekunden
begrenzte `deadline_s` sind optional. Der Master entscheidet und fenced den
Übergang.

### 3.7 `node.state.set`

```json
{"node_id": "fn-01", "desired_state": "MAINTENANCE", "reason": "service"}
```

Pflicht: `node_id`, `desired_state`. Der gemeinsame v0014-Validator erlaubt
`ACTIVE`, `DRAINING`, `MAINTENANCE` und `QUARANTINED`; für einen Drain mit
Deadline ist fachlich `node.drain` zu bevorzugen. Admission-State und observed
State dürfen nicht direkt durch den Proxy gesetzt werden.

### 3.8 `node.admission.approve`

```json
{
  "node_id": "fn-02",
  "certificate_identity": "urn:automation:site_main:worker:fn-02",
  "reason": "v0015 protocol simulator"
}
```

Pflicht: `node_id`; `certificate_identity` und `reason` sind aus Gründen der
v0014-Parität optional. Wird eine Identität mitgegeben, muss sie mit dem zuvor
registrierten Node und der mTLS-Peeridentität übereinstimmen. Der fn-02-E2E
gibt sie verpflichtend mit. Die Master-Authority ist die einzige Instanz, die
`APPROVED` festlegt.

### 3.9 `terminal.execute`

```json
{"command": "operation show 0432efae-1086-42e3-aeff-f036949ca4a9"}
```

Das Feld `command` ist Pflicht und wird ausschließlich an den bestehenden
Master-Domain-Parser übergeben. Erlaubt bleibt dessen feste Grammatik:

```text
nodes list
node drain <node-id>
node approve <node-id>
node maintenance <node-id>
node quarantine <node-id>
node resume <node-id>
config revision
config history
operation show <operation-id>
```

Es gibt keine Shell, kein `eval`, keine Pipes, Redirects, Subprozesse oder
frei konstruierbare MQTT-Topics.

## 4. Read-Payloads und Grenzen

* `users.list.limit`, `api_tokens.list.limit`: optional 1–200, Standard 100.
* `config.history.limit`: optional 1–200, Standard 50.
* `audit.list.limit`: optional 1–200, Standard 100.
* `config.get.revision`: optional positiver Integer; ohne Feld Head-Revision.
* `config.diff.from_revision` und `to_revision`: positive Integer.
* `node.get.node_id`: nichtleerer, normalisierter Node-Identifier.
* `operation.get.operation_id`: UUID.
* Alle anderen Read-Actions akzeptieren ausschließlich `{}`.

Grenzen müssen mit den bestehenden v0014-Clamps übereinstimmen. Der
Dispatcher gibt keine Credential-Hashes, Sessiontokens, API-Token-Hashes,
Private Keys oder fremden geheimen Klartext zurück.

## 5. Idempotenz- und Timeout-Regeln

Für alle in der Tabelle als Mutation markierten Actions gilt:

1. Der gemeinsame Dispatcher registriert die Kombination aus Site,
   Principal, Action und `idempotency_key` dauerhaft oder atomar mit dem
   fachlichen Outbox-Eintrag.
2. Derselbe Key mit semantisch identischer Payload liefert dieselbe Operation
   beziehungsweise dasselbe terminale Ergebnis.
3. Derselbe Key mit abweichender Payload liefert `conflict`.
4. Ein abgelaufener TTL-Befehl wird nicht mehr ausgeführt und liefert
   `expired`.
5. Ist ein gestarteter Legacy-Aufruf nicht sicher abbrechbar, wird
   `accepted/pending` zurückgegeben. Ein äußerer AsyncIO-Timeout darf keine
   zweite Mutation erzeugen.

Idempotenz-Fingerprints enthalten niemals CSRF-, Session- oder Bearerwerte.
Bei geheimen Payloadfeldern, insbesondere `user.create.password`, wird nur ein
keyed HMAC über die kanonische Payload gespeichert; der HMAC-Schlüssel liegt
außerhalb der Datenbank als rotierbares Secret. Passwort, Tokenklartext und
ungekeyte, offline erratbare Hashes sind verboten.

## 6. Implementierungs- und Abnahmeregel

Die maschinenlesbaren Basishüllen stehen in den JSON-Schemas dieses Ordners.
Die reale v0015-Implementierung ergänzt eine zentrale Action-Registry mit
Payloadvalidator und Dispatcheradapter; HTTPS-Route und WSS-Adapter rufen
dieselbe Registry auf. Für jede Zeile in Abschnitt 3 sind mindestens folgende
Tests Pflicht:

* gültige Minimalpayload;
* unbekanntes Feld und fehlendes Pflichtfeld;
* fehlendes Recht beziehungsweise falscher Resource-/Site-Scope;
* REST-/WSS-Ergebnisparität;
* bei Mutation: fehlender Key, Retry, Key/Payload-Konflikt, CSRF-Fehler und
  Auditnachweis.

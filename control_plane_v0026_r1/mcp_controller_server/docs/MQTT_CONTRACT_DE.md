# MQTT-Vertrag

## Protokoll und Identität

- Envelope: `automation.control.v1`, `schema_version=1`
- Quelle: `proxy/<source_instance_id>`
- Ziel: `master/<master_instance_id>`
- Transport: MQTT 3.1.1 über TLS mit verifiziertem Servernamen und Clientzertifikat
- Publish/Subscribe: QoS 1, keine retained Control-Nachrichten
- Default-Site: `site-main`

Die MQTT-Clientimplementierung ist byte-identisch mit der im gelieferten Master
verwendeten `_lightweight_mqtt_client.py`. Ihr SHA-256 ist
`8e8a5a8bb86b24dbff5de80667af9fcc054572901a6a31dfd6d46b9203379e33`.

## Topics

| Richtung | Topic |
|---|---|
| Kommando zum Master | `automation/v1/{site}/master/{master}/commands/{event_type mit / statt .}` |
| ACK an MCP-Identität | `automation/v1/{site}/proxy/{source}/acks/{master}/{message_id}` |
| normalisierte Events | `automation/v1/{site}/events/{event_type}/{master}` |

Abonnements sind auf den exakten ACK-Präfix der konfigurierten Identität und auf
Master-Events begrenzt. Topic-Tokens werden gegen
`^[a-z0-9][a-z0-9._-]{0,127}$` geprüft; Wildcards und Separatoren können nicht
injiziert werden.

## Erlaubte Control-Kommandos

| MCP-Funktion | Control-Event | Master-Permission |
|---|---|---|
| Runtime lesen | `runtime.status` | `node.read` |
| Nodes lesen | `nodes.list` | `node.read` |
| Konfiguration lesen | `config.get` | `config.read` |
| Konfigurationshistorie | `config.history` | `config.read` |
| Operation lesen | `operation.get` | `operation.read` |
| Aktuator planen/committen | `actuator.request` | `actuator.request` |
| Konfiguration planen/committen | `config.propose` | `config.propose` |
| Node drain planen/committen | `node.drain` | `node.drain` |
| Node-State planen/committen | `node.state.set` | `node.drain` |

Nicht angeboten werden `terminal.execute`, `config.rollback`,
`node.admission.approve`, ein generischer Eventname oder ein generisches Topic.

## ACK-Korrelation und Retry

Ein ausgehendes Kommando erhält eine UUID als `message_id` und
`correlation_id`. Der Bridge akzeptiert ein ACK nur, wenn gleichzeitig gilt:

- Topic entspricht Site, eigener Proxy-Identität, Master und `causation_id`;
- Envelope-Quelle ist der konfigurierte Master;
- Envelope-Ziel ist die konfigurierte Proxy-Identität oder Broadcast;
- `causation_id` entspricht der offenen `message_id`;
- `correlation_id` entspricht dem ausgehenden Envelope;
- `payload.command` entspricht dem gesendeten Event;
- `payload.result` ist ein Objekt.

Bei ausbleibendem ACK wird bis zum Timeout derselbe Envelope mit derselben
Nachrichten- und Idempotenz-ID erneut publiziert. Ein Transport-Publish ist nicht
gleichbedeutend mit fachlichem Erfolg; nur `command.ack.payload.result` beendet
den Aufruf.

## Node-lokale Konfiguration

`config.propose` akzeptiert den aktuellen kanonischen Relay-Vertrag:

- `target_node_id`
- `operation=scope_operations`
- `operation_format=config_scope_operations_v1`
- `scope=node:<id>` oder `cross:<id>`
- `base_authority_epoch`
- `base_scope_revision`
- `base_scope_fingerprint`
- eine begrenzte Liste `replace_field`, `upsert_entity` oder `delete_entity`

`upsert_entity.value_hash` und optionale erwartete Hashes müssen exakt passen.
Legacy-Pfadoperationen können nur im Master-Global-Format validiert werden und
werden nicht mit kanonischen Node-Local-Feldern vermischt.

## Telemetrie

Der Server liest ausschließlich normalisierte Master-Events. `sensor.value`
liefert typischerweise `controller_uuid`, `sensor_id`, `value`, `quality` und
`source_node_id`. Neueste Sensorwerte und Events liegen nur in einem begrenzten
RAM-Cache und werden beim Neustart verworfen. Für dauerhafte Historie muss ein
Master-seitig autorisiertes Read-Modell ergänzt werden; ein DB-Bypass ist nicht
zulässig.

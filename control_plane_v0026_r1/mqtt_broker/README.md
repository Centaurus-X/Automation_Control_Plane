# MQTT Broker 0.14.0

Eigenständiges Mosquitto-2.1-Paket für die Control Plane. Es stellt nur MQTTS
auf Port 8883 bereit, verlangt Client-Zertifikate und ordnet die Zertifikat-CN
über eine geschlossene ACL den Rollen Master, Proxy, den Lab-Workern `fn-01`
und `fn-02` sowie dem Health-Monitor zu. Der Broker besitzt ein persistentes
Docker-Volume.

Die unveränderlichen Quelldateien liegen unter `config/`. `setup.sh` erzeugt
daraus vor jedem Start `runtime-config/` mit Mosquitto-UID 1883 und
restriktiven Rechten. Dadurch werden ACL-Dateien weder world-readable gemountet
noch durch den Container im Quellbaum umgeschrieben.

## Lab

```bash
./test_static.sh
MQTT_BIND_ADDRESS=192.168.0.82 ./deploy_test.sh
MQTT_BIND_ADDRESS=192.168.0.82 ./test.sh
```

Das Lab-Skript erzeugt idempotent eine ausschließlich für Tests bestimmte PKI.
Die Worker-Dateien liegen danach unter `test-pki/clients/worker-fn-01/` und
`test-pki/clients/worker-fn-02/`.

## Produktion

```bash
export MQTT_CA_SOURCE=/secure/ca.crt
export MQTT_BROKER_CERT_SOURCE=/secure/mqtt-broker.crt
export MQTT_BROKER_KEY_SOURCE=/secure/mqtt-broker.key
export MQTT_HEALTH_CERT_SOURCE=/secure/mqtt-health.crt
export MQTT_HEALTH_KEY_SOURCE=/secure/mqtt-health.key
./scripts/install_production_pki.sh
MQTT_BIND_ADDRESS=192.168.0.82 ./deploy.sh
```

Das Serverzertifikat muss `mqtt-broker` als DNS-SAN enthalten. Die Health-CN
muss exakt `urn:automation:site-main:monitor:mqtt-health` sein. Testmaterial
wird vom Produktionspfad verweigert.

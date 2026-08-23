# MQTT secret contract

Runtime files under `secrets/` are generated or installed locally and are never
part of the release archive:

- `mqtt_ca.crt`
- `mqtt_broker.crt`, `mqtt_broker.key` (serverAuth; SAN `mqtt-broker`)
- `mqtt_health.crt`, `mqtt_health.key` (clientAuth; health-check identity)

Production deployment additionally supplies the Master and Proxy client
certificates directly to their independent packages. All client certificates
must chain to `mqtt_ca.crt`; the ACL maps the exact certificate CN to one role.

`generate_test_pki.sh` creates a lab-only CA and clients below `test-pki/`.
Production scripts reject its marker. Never copy the test CA private key to a
production host.

Docker Compose bindet lokale File-Secrets mit den Host-Dateirechten ein. Direkt
vor dem Containerstart setzt `scripts/prepare_compose_secrets.sh` deshalb den
Host-Benutzer als Eigentümer, GID `1883` und Modus `0640`. Der Schlüssel bleibt
nur für den deployenden Benutzer und die Mosquitto-Runtime lesbar.

# Secret-Dateien

`setup.sh` kopiert DB-Passwörter/CA, erzeugt drei lokale Pepper und erwartet
produktiv folgende Zertifikatsquellen:

```text
PROXY_HTTPS_CERT_SOURCE
PROXY_HTTPS_KEY_SOURCE
MQTT_CA_FILE
PROXY_MQTT_CERT_FILE
PROXY_MQTT_KEY_FILE
```

Das provisionierte HTTPS-Serverzertifikat wird standardmäßig unter zwei
Compose-Secret-Namen read-only für HTTPS und WSS eingebunden. Es muss deshalb
SANs für alle tatsächlich verwendeten HTTPS-/WSS-Hostnamen beziehungsweise
IP-Adressen besitzen. Eine getrennte WSS-PKI kann über
`PROXY_WSS_CERT_FILE`/`PROXY_WSS_KEY_FILE` in einer angepassten Deployment-
Konfiguration bereitgestellt werden; die Runtime erzeugt niemals selbst
Produktionsschlüssel.

Testzertifikate werden ausschließlich mit den expliziten Testschaltern des
`deploy_test.sh` als CA-signiertes Leaf erzeugt. Die Lab-CA liegt unter
`test-pki/wss/ca.crt`; CA-Key, Leaf-Key und Manifest bleiben im generierten
`test-pki/wss/`-Laufzeitzustand. `secrets/` und `test-pki/` sind aus Git,
Docker-Build-Kontext und Releasearchiv ausgeschlossen.

Vor Migration und Start setzt `scripts/prepare_compose_secrets.sh` Eigentümer
und Rechte auf Host-UID, Runtime-GID `65532` und `0640`. Lokale Compose-Secrets
bleiben damit für den unprivilegierten Gateway-Prozess lesbar, aber nicht
weltweit zugänglich.

# Secret-Dateien

`setup.sh` kopiert DB-Passwörter und die PostgreSQL-CA aus dem benachbarten
`postgres_system`. Produktives MQTT benötigt:

```text
MQTT_CA_FILE
MASTER_MQTT_CERT_FILE
MASTER_MQTT_KEY_FILE
```

Die Zielwerte landen ausschließlich im ignorierten Verzeichnis `secrets/` und
werden durch `.dockerignore` nicht an den Build-Kontext übertragen.

Unmittelbar vor Migration und Start normalisiert
`scripts/prepare_compose_secrets.sh` die Dateien auf Host-UID, Runtime-GID
`65532` und `0640`. Dadurch kann der unprivilegierte Containerbenutzer lesen,
ohne private Schlüssel oder Passwörter weltweit freizugeben.

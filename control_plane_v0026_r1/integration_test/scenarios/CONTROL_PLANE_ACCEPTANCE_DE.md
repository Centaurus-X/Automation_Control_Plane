# Worker-/Modbus-Ende-zu-Ende-Abnahme

Voraussetzung: Control Plane auf `192.168.0.82`, Worker `fn-01` auf
`192.168.0.80`, Simulator-Endpunkt auf `192.168.0.3:5020` und eine im separaten Worker-
Release konfigurierte mTLS-Identität.

1. `./test_running_all.sh` ist grün und MQTT, PostgreSQL, Master, Proxy sowie
   MCP Controller sind healthy.
2. MCP verbindet sich ausschließlich als
   `urn:automation:site-main:proxy:mcp-controller-01`, kann seinen ACK-Pfad und
   Master-Events lesen, aber keine Worker-/Broadcast-Kommandotopics schreiben.
3. Worker verbindet sich als `urn:automation:site-main:worker:fn-01`.
4. Im Lab wird der broker-/zertifikatsgebundene konfigurierte Node automatisch
   `APPROVED`; unbekannte Identitäten bleiben `PENDING`/abgewiesen.
5. In Produktion genehmigt ein `site_admin` den Worker ausdrücklich.
6. Der nächste Heartbeat enthält Version, Revision, Fingerprint und Health.
7. Master sendet die gewünschte Config-Revision; der externe Worker bestätigt denselben
   Fingerprint und aktiviert nur die freigegebene Simulatorroute.
8. Ein Sensorwert von `192.168.0.3:5020` erscheint als Site-Event und im WSS-
   Stream des berechtigten Benutzers.
9. Ein Actuator-Request mit neuem `Idempotency-Key` wird persistiert, an den
   Owner geroutet und erst nach Hardware-Readback `CONFIRMED`.
10. Derselbe Request/Key erzeugt keinen zweiten Hardware-Write.
11. Eine Config-Änderung mit veralteter `base_revision` wird als Konflikt
    abgewiesen; verbotene Secret-/TLS-Pfade bleiben Deny.
12. Worker-Zertifikat `fn-01` kann kein Topic von `fn-02` beschreiben.
13. Nach Worker-Verbindungsabbruch läuft die Lease ab; neue Remote-Writes werden
    nicht an den Offline-Owner versendet.

Für jeden Schritt müssen Request-/Operation-ID, Authority-Term, Config-Revision
und Zeitstempel protokolliert werden. Ein unklarer Hardwarezustand ist
`UNCERTAIN`, niemals stillschweigend `CONFIRMED`.

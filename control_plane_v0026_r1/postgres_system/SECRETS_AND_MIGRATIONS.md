# Secrets und Migrationen

`scripts/generate_secrets.sh` erzeugt beim ersten Lab-Setup zufällige
PostgreSQL-/Replikations-/App-Passwörter. Bestehende Dateien werden nie
überschrieben. Das Verzeichnis `secrets/` ist nicht Teil eines Releases.
Bei einem orchestrierten Upgrade uebergibt
`scripts/handoff_live_secrets.sh` ausschliesslich das bereits vom laufenden
Primary gemountete Runtime-Material an den neuen Release-Ordner. Zuvor prueft
`scripts/verify_role_credentials.sh` die PostgreSQL-Rollen per TLS; die
Streaming-Replikation muss ebenfalls gesund sein. Secret-Inhalte werden weder
als Argumente noch als Logausgabe verwendet. Ohne laufenden Primary bleibt der
Fresh-Pfad unveraendert und erzeugt neue Werte.

Produktive TLS-Quellen werden über die fünf in `README.md` genannten
Umgebungsvariablen installiert. Der private Test-CA-Key liegt in
`test_ca_private/`; dieses Verzeichnis wird weder in den Container gemountet
noch paketiert.

Die Anwendungs-Schemata gehören bewusst den jeweiligen Servicepaketen:

- `master_node/master_node/deployments/migrations`
- `proxy_gateway/proxy_gateway/deployments/migrations`

Das PostgreSQL-Paket erzeugt nur Datenbank, Rollen, TLS- und
Replikationsgrundlage. Dadurch kann kein DB-Containerstart unbemerkt
Applikationsschema ändern.

Nach Erzeugung beziehungsweise Installation setzt
`scripts/prepare_compose_secrets.sh` alle Compose-File-Secrets auf Host-UID,
PostgreSQL-GID `999` und Modus `0640`. Dies ist erforderlich, weil lokales
Compose die Host-Rechte der Secret-Bind-Mounts beibehält.

# Code-Audit und Patchbericht v0011

Datum: 2026-07-15

## Befund

Die Control Plane v0010 konnte eine lokale Worker-Steuerung indirekt
destabilisieren, obwohl der Worker den Feldbus autonom besitzt. Drei
zusammenwirkende Ursachen wurden lokalisiert.

### 1. Zu großer beworbener Controller-Scope

Der Worker leitete seine Registration ausschließlich aus der statischen
`site_controller_map` ab. Dadurch meldete er auch Controller, die im aktiven
Simulatorprofil nicht gestartet waren. Der Master interpretierte diese
Controller als lokal autoritativ beziehungsweise routbar.

v0011 verwendet die vom Worker ausdrücklich beworbene Schnittmenge aus Route
und aktiver Allowlist. In `automation_authority.py` wird `observe_only` nur
noch gesetzt, wenn ein genehmigter Worker genau diese Controller-UUID
beworben hat. Eine statische Route allein genügt nicht.

### 2. Globale Auswertung unvollständiger Facts

`global_automation.py` wertete bei jedem einzelnen Sensorereignis alle Regeln
aus. Der GenericRuleEngine erzeugt für fehlende Sensorwerte bewusst
`safety_on_missing`-Aktionen. Fehlende Timer können zusätzlich `else_do`
erzeugen. Auf einem globalen Partial-Snapshot wurden diese Aktionen bisher
sofort an die Config-Authority übergeben.

Das erklärt die beobachtete Sequenz: Der erste lokale 524-Task wurde noch
erzeugt, anschließend änderten globale Revisionen States und weitere lokale
Tasks blieben aus.

v0011 führt die Policy `incomplete_signal_policy=defer` ein. Jede Aktion einer
Regel mit mindestens einem fehlenden Eingangssignal wird verworfen, bis der
globale Snapshot vollständig ist. Der Master bleibt dabei global autoritativ,
erfindet aber aus fehlenden Daten keine Zustandsänderung.

### 3. Fehlende Mode-Begrenzung beim Action-Plan

Die RuleEngine-Konfiguration fordert `required_mode_id=2`. Der Master wendete
eine Aktion bisher dennoch auf jede durch `state_ids` oder `p_ids` passende
Zeile an. Dadurch konnten automatische Regeln auch manuelle States 11, 20 und
21 verändern.

`config_authority.py` löst nun Engine und `required_mode_id` über die
`rule_id` auf und ändert nur passende Mode-Zeilen. Der bestehende immutable
Revision-/Single-Writer-Pfad bleibt unverändert.

## Bewahrte Grenzen

- Master bleibt einzige globale Config-Authority;
- Worker bleibt untergeordnete lokale Steuerungs-Authority;
- MQTT-Synchronisation bleibt bidirektional, asynchron und eventual
  consistent;
- lokale Regeln werden am Master `observe_only` ausgewertet;
- explizite Proxy-/Benutzer-Aktoraufträge bleiben möglich;
- automatische Master-Doppelwrites auf Worker-lokale Controller bleiben
  unterdrückt;
- keine Hardware- oder Modbus-Abhängigkeit im Master;
- Worker-Code ist nicht Bestandteil dieses Archivs.

## Regressionstests

Die neue Matrix prüft:

- beworbener versus nur statisch gerouteter Controller;
- Offline-Beibehaltung lokaler Authority;
- Defer für fehlende Sensor- und Timer-Facts;
- keinen Commit von Partial-Snapshot-Safety-Aktionen;
- `required_mode_id` mit unveränderten manuellen States;
- bestehende globale und explizite Aktorrouten.

Alle Master-, Proxy-, MQTT-, PostgreSQL-, Lifecycle-, Upgrade- und
Release-Strukturtests bestanden lokal. Reale Docker-/LAN-/Modbus-Tests bleiben
Teil der Zielhost-Abnahme.

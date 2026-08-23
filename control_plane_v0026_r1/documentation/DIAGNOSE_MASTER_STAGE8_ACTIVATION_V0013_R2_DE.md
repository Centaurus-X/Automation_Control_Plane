# Diagnose: Master Stage-8-Aktivierungsfehler v0013-r2

Stand: 17. Juli 2026  
Korrekturstand: `v0013-r2`

## 1. Reales Fehlerbild

Der Zielhost erreichte Stufe 8 mit gesunden MQTT-, PostgreSQL- und Proxy-
Containern. Der Master-Prozess lief, blieb jedoch `unhealthy`. Seine
semantische Readiness zeigte dauerhaft:

```text
bootstrap.state=seeding
global_revision=0
global_fingerprint=null
scope_count=0
config_ready=false
```

Das ist kein Worker-, Zertifikats-, Modbus- oder Cross-Capability-Fehler. Die
initiale Master-v2-Konfiguration wurde noch vor dem ersten PostgreSQL-Commit
abgebrochen.

## 2. Reproduzierte Root Cause

Der vollständige Produktionspfad lautet:

```text
initial_config_v2.json
→ Node-/Cross-Materialisierung
→ plan_activations(...)
→ atomarer PostgreSQL-Commit
```

Die Lab-Konfiguration materialisiert korrekt drei Scopes:

```text
node:fn-01
node:fn-02
cross:fn-01.default
```

Mehrere Shards enthalten dieselbe unowned `processes`-Zeile als notwendige
Kompatibilitäts-/Dependency-Definition. Die eigentlichen Laufzeitanker sind
die disjunkten `process_states` und `rules`. Der alte Aktivierungsplaner
behandelte dennoch jede projizierte `processes`-Zeile als separat aktive
Execution-Entity. Dadurch brach der Seed reproduzierbar vor jedem SQL-Write
ab:

```text
ValueError: execution entity is assigned to multiple scopes: process:1
```

Die PostgreSQL-Tabellen blieben folgerichtig bei Revision 0 ohne Entities und
Scopes.

## 3. Warum die bisherigen Tests das nicht erkannten

- Der Bootstrap-Test entfernte für seinen Basispfad alle Cross-Regeln.
- Der Topologietest prüfte die drei Shards, rief danach aber nicht den
  Aktivierungsplaner auf.
- Der Config-Authority-Test verwendete keinen v2-Materializer.
- Im Buildumfeld stand kein Docker-/PostgreSQL-Lauf zur Verfügung.

Jeder Test war für seinen isolierten Gegenstand grün, aber die konkrete Kante
`vollständige Lab-Config → drei Shards → Activation-Map` fehlte.

## 4. Minimal-invasive Korrektur

`scope_activation.py` unterscheidet jetzt zwischen Execution-Ankern und
replizierten Dependency-Zeilen:

- `rules` und `process_states` bleiben immer Activation-Entities;
- eine `processes`-Zeile wird nur dann Activation-Entity, wenn sie einen
  expliziten `owner_node_id` besitzt;
- eine explizit besessene Prozesszeile in zwei Scopes wird weiterhin
  fail-closed abgelehnt;
- unowned, bytegleich projizierte Prozessdefinitionen erhalten keine eigene
  Epoch. Ihre disjunkten State-Zeilen bleiben vollständig gefencet.

Die globale Konfiguration und die Node-/Cross-Shards werden dadurch nicht
umgeschrieben. Worker-v0082-Fingerprints und der Modbus-Endpunkt
`192.168.0.3:5020` bleiben unverändert.

## 5. Bootstrap- und Diagnostikhärtung

Zusätzlich werden unerwartete Seed-/Reload-Exceptions jetzt innerhalb des
Master-Bootstraps abgefangen und begrenzt sichtbar gemacht:

- `runtime.bootstrap.state=failed` statt dauerhaftem `seeding`;
- Fehlerklasse und bereinigte Kurzmeldung in `bootstrap.last_error`;
- Top-Level-`last_error` wird gesetzt;
- ein abgelehnter Leader-Role-Handler erhöht `maintenance_errors` und setzt
  `orchestrator.last_error`;
- Stufe 8 gibt bei einem ungesunden Master automatisch Readiness-JSON und den
  Master-Logtail aus.

Der Master bleibt dabei fail-closed und öffnet keinen Worker-Ingress, solange
der Config-Seed nicht vollständig validiert und committed wurde.

## 6. Neue Regressionsgates

Der Korrekturstand prüft nun ausdrücklich:

1. den exakten vollständigen Config-Authority-Seed mit allen drei Scopes;
2. 20 eindeutige Rule-/State-Activation-Anker;
3. zulässige unowned Process-Dependencies in mehreren Scopes;
4. Ablehnung eines explizit besessenen Prozesses in zwei Scopes;
5. vollständigen Cross-Bootstrap bis `config_ready=true`;
6. sichtbare, fail-closed Seed-Exceptions;
7. sichtbare Ablehnung des Leader-Role-Handlers.

Das Master-Image trägt zusätzlich das OCI-Label
`org.opencontainers.image.revision=v0013-r2`. Die laufende Abnahme lehnt ein
altes v0013-Master-Image ohne dieses Label ab.

## 7. Releasegrenze

Die statischen und funktionalen Regressionsgates sind lokal ausführbar. Der
erneute reale Docker-/no-GIL-/mTLS-/PostgreSQL-/Worker-/Modbus-/WSS-Lauf bleibt
bis zur Ausführung auf dem Zielhost `NOT_RUN`. Die normative siebenstufige
Online-Scope-Migration bleibt unabhängig davon `BLOCKED`.

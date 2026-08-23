# Third-party notices

Die Runtime installiert exakt:

- Uvicorn 0.51.0 — BSD-3-Clause
- Click 8.2.1 — BSD-3-Clause
- h11 0.16.0 — MIT

Die Abhängigkeiten sind nicht im Release-ZIP vendort; sie werden beim Image-Build
aus dem konfigurierten Python-Package-Index geladen. Die jeweiligen Upstream-
Lizenztexte und Paketmetadaten gelten für die installierten Artefakte.

Das Basisimage ist `python:3.14.6-slim-bookworm` und unterliegt den Python- und
Debian-Lizenzen des konkreten Images.

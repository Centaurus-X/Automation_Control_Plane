# Changelog

## 0.28.0 / v0028-r1 — 2026-08-23

- Promoted the verified Linux/Helm portability patch into a complete successor release.
- Corrected quoted Helm `required` expressions in Master and MQTT workload image references.
- Added reserved `global` values-tree compatibility to all nine strict subchart schemas.
- Kept source charts and vendored umbrella dependencies byte-identical after regeneration.
- Preserved all embedded v0026-r1 Control Plane components and external Runtime bindings.
- Added v0028-r1 provenance, validation, audit, release notes and deterministic package identity.

See `RELEASE_NOTES_V0028_R1_DE.md` for the detailed engineering record.

## 0.27.3 / v0027-r3 — 2026-08-23

- Published the byte-exact v0027-r3 release source and preserved v0026-r1 baseline.
- Added deterministic placement and Plan v2 with nine Saga steps.
- Added Helm, Argo CD, Flux and outbound-agent execution paths with one write owner.
- Added four-eyes/per-wave approval, pause/resume and evidence export.
- Added agent lease/epoch/HMAC fencing and expanded observability.
- Added professional product, architecture, security and contribution documentation.

See `RELEASE_NOTES_V0027_R3_DE.md` for the detailed engineering record.

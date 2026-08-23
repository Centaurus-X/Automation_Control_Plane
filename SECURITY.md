# Security policy

Automation Control Plane coordinates privileged deployment and automation
paths. Use coordinated disclosure. Do not publish exploit details, tokens,
certificates, kubeconfigs, plant information or target topology in public issues.

## Supported version

| Version | Support |
|---|---|
| `0.27.x` | Current pre-production line; best effort |
| Older releases | Historical evidence only |

## Report a vulnerability

Use **Security → Report a vulnerability**. Include affected component/version,
attacker prerequisites, safe lab reproduction, authority/deployment impact,
sanitized evidence and suggested mitigation. If private reporting is
unavailable, contact the Centaurus-X maintainer and agree on a private channel.

High-priority examples include approval or role bypass, CSRF/session failure,
plan/evidence tampering, write-owner confusion, stale lease/fencing acceptance,
agent HMAC bypass, arbitrary command execution, secret disclosure, namespace
escape, image-digest bypass and a path that grants Fleet field-I/O authority.

## Operational baseline

- Keep mutation disabled until target acceptance is complete.
- Use external secret stores, restrictive file modes, target-scoped tokens and production PKI.
- Pin images and external artifacts by digest.
- Protect Fleet journal/evidence and back up PostgreSQL before migration.
- Use namespace-scoped RBAC and verify negative access tests.
- Put broader UI/API exposure behind authenticated TLS/OIDC.
- Exercise failure and recovery only in authorized non-production environments.

No independent penetration test, safety certification or compliance
certification is claimed.

# Contributing

Changes to the Control Plane can affect identity, deployment, durability and
hardware-authority boundaries. Prefer small, reviewable, fail-closed changes
with explicit recovery evidence.

## Validation

```bash
./test_release.sh
sha256sum -c SHA256SUMS
```

Target/provider changes also require the relevant acceptance tests from
`kubernetes_orchestration/documentation/KUBERNETES_TARGET_ACCEPTANCE_DE.md`.

## Project rules

- No classes, decorators or lambdas; use `functools.partial()` where binding is needed.
- Preserve the byte-identity boundary of `control_plane_v0026_r1` unless a separately versioned release explicitly changes it.
- Keep Fleet outside Node/SEM/Safety/hardware authority.
- Enforce exactly one write owner per target.
- Never add free-form shell, `shell=True`, `pods/exec` or cluster-wide RBAC.
- Bind every mutation to plan, identity, fencing and idempotency evidence.
- Test failure, retry, pause/resume, rollback and manual-recovery paths.
- Never commit tokens, kubeconfigs, certificates, keys, plant data or runtime state.

## Pull request checklist

- [ ] State owner, trust boundary and failure behavior are explicit.
- [ ] Plan/audit/evidence compatibility is documented.
- [ ] `./test_release.sh` passes on Linux.
- [ ] Relevant real-target gates are recorded as PASS or explicit SKIP.
- [ ] No secrets, generated state or mutable image tags are included.
- [ ] Documentation and changelog are current.

Contributions are submitted under GPL-3.0-only. Discuss substantial work with
the maintainer before investing heavily, especially where separate commercial
licensing may require a written contribution agreement.

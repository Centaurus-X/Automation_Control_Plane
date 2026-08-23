## Purpose

Describe the orchestration/authority problem and the smallest change that solves it.

## Ownership and recovery

- State owner / trust boundary:
- Plan, fencing and idempotency impact:
- Failure / pause / resume / rollback behavior:
- Worker authority and hardware-safety impact:

## Validation

- [ ] Added success, rejection and recovery tests.
- [ ] `./test_release.sh` passes on Linux.
- [ ] External target gates are recorded as PASS or explicit SKIP.
- [ ] No secrets, runtime state or mutable image tags are included.
- [ ] Documentation and changelog are current.

# Implementation review — edge-ingress P4 — round 2

<!-- codex-impl-review-status: finalized -->

## Round-1 verification

- **Registry identity mismatches do not stop apply — resolved.** Blocking lifecycle preconditions replace the warning-only check and enforce the generated zone and tunnel identity.
- **Invalid registry values silently become public DNS — incomplete in round 2; resolved below** (one registry snapshot for validation and hash).
- **Retained import IDs obstruct interrupted replacement recovery — resolved.** Previously adopted imports are removed. The remaining relay import awaits the first apply and subsequent deletion; the runbook covers recreation and importing an unsaved remote replacement.
- **Older checkouts cannot safely consume migrated state — resolved.** Forward-only migration is documented, with a backup and reverse state moves.
- **The claimed Terraform/Python parity test does not exist — resolved.** DNS consumes the renderer's records, eliminating the duplicate mode evaluator.
- **P4 replacement rehearsal is neither documented nor currently schema-valid — incomplete in round 2; resolved below** (sampling before the apply; the standard names the exception).
- **Operational instructions still target the removed resource — resolved.**

## Findings

All five round-2 findings were accepted and fixed (aca9fd5f, 4e49a1a5); none is contested.

### Hash the same registry snapshot that was validated
**Location:** scripts/edge/render.py:703
**Severity:** important
**Resolution (accepted):** `main()` reads the registry once and passes that text to `load()` (parse and
validate) and to `render_dns()` (hash). `test_main_hashes_the_text_it_validated` saves the file between
validation and hashing; it fails on the previous code (hash of the edited file) and passes now.

### Rollback procedures omit the required regeneration
**Location:** docs/runbooks/edge-ingress.md:64
**Severity:** important
**Resolution (accepted):** the runbook states that every registry edit is followed by `just edge-render`
with the registry and both generated files committed together; add-host step 4 (`lan_dns`), to-tunnel
(render, apply from the branch, wait out the TTL, then merge), force_tunnel and both rollback rows
include it.

### Start DNS sampling before the replacement
**Location:** docs/runbooks/edge-ingress.md:119
**Severity:** important
**Resolution (accepted):** the rehearsal starts a sampler in a second terminal before each apply and
keeps it until both resolvers show the new answer for longer than the TTL; it logs each resolver's
status (NOERROR, NXDOMAIN) with the answer, and a cached-over gap is recorded as not observed.

### Empty test profiles unexpectedly write production DNS
**Location:** scripts/edge/render.py:702
**Severity:** nit
**Resolution (accepted):** DNS generation is gated on `--test-profile` being absent, and an empty or
non-object profile is refused. `test_a_test_profile_never_writes_dns` fails on the previous code.

### Update the standard for the fixed-response exception
**Location:** docs/standards/edge-ingress.md:59
**Severity:** nit
**Resolution (accepted):** the auth table and the MUST-reject list admit `auth: none` on a private host
only for a fixed `respond` backend.

## Deferred to live verification

After merge: P4's first apply (27 moves, the relay import, the edge-canary create, 0 changes, 0
destroys, with the runbook's "Applying" inputs), the deletion of relay-import.tf in the next PR, and
the edge-canary rehearsal with its evidence recorded on the P4 PR before any real host changes mode.

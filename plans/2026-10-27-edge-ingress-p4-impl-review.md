# Implementation review — edge-ingress P4 — round 2

<!-- codex-impl-review-status: pending -->

## Summary

- Five round-1 findings are resolved; registry validation binding and rehearsal documentation remain incomplete. Three important findings below should be fixed before merge.
- No additional defect found in generated JSON number/boolean types, deterministic ordering, ordinary host removal, or the private fixed-response exception: even declared routes cannot reach a real backend when `respond` is selected.
- `--check` covers both production outputs. Both are computed before writing, with Caddy written first; interrupted writes after a registry change leave DNS stale or unparsable. The separate registry reads and empty test-profile handling need correction.
- Accepted the author’s verified tests, clean generated outputs, migration plan, tamper cases, CRLF compatibility and formatting results without repeating them. Additional in-memory probes confirmed the registry-read race and empty-profile behavior; filesystem writes were mocked.
- The first apply, subsequent deletion of `relay-import.tf`, and live canary rehearsal evidence remain post-apply obligations, not prerequisites for merging the corrected implementation. Record the rehearsal before any real host changes mode.

## Round-1 verification

- **Registry identity mismatches do not stop apply — resolved.** Blocking lifecycle preconditions replace the warning-only check and enforce the generated zone and tunnel identity.
- **Invalid registry values silently become public DNS — incomplete.** Normal generation validates the registry, but `main()` subsequently rereads it for hashing; a concurrent edit can bind previously generated records to an unvalidated registry.
- **Retained import IDs obstruct interrupted replacement recovery — resolved.** Previously adopted imports are removed. The remaining relay import explicitly awaits the first apply and subsequent deletion; the runbook covers recreation and importing an unsaved remote replacement.
- **Older checkouts cannot safely consume migrated state — resolved.** Forward-only migration is documented, with a backup and reverse state moves. The quoted Bash prefix substitution correctly preserves each `["hostname"]` suffix, including the canary that pre-P4 configuration will delete.
- **The claimed Terraform/Python parity test does not exist — resolved.** DNS now consumes the renderer’s records, eliminating the duplicate mode evaluator. The added tests cover record shapes and shared mode selection; HCL digest compatibility is supported by the supplied plan evidence.
- **P4 replacement rehearsal is neither documented nor currently schema-valid — incomplete.** The canary’s private mode is safely admitted and transitions are documented, but sampling starts too late to measure the replacement gap, and the standard still prohibits the newly allowed exception.
- **Operational instructions still target the removed resource — resolved.** Keycloak commands, the Cloudflare README and the Trueswarm note reference the new addresses. Separate omissions in the regeneration steps are raised below.

## Findings

### Hash the same registry snapshot that was validated

**Location:** scripts/edge/render.py:703  
**Severity:** important  
<!-- codex: round-2: main() validates the registry through load() at line 693, then rereads the file after rendering to obtain its digest; an editor save between those reads can produce records from the old registry carrying the new, potentially invalid registry's hash, passing all three DNS preconditions without forging the JSON. Read the registry once and use that same immutable text for parsing, validation and hashing, with a regression test covering a change between the current two reads. -->

### Rollback procedures omit the required regeneration

**Location:** docs/runbooks/edge-ingress.md:64  
**Severity:** important  
<!-- codex: round-2: The per-host tunnel rollback, force_tunnel instructions and rollback table edit the registry and immediately apply, which now fails the registry-hash precondition; the add-host procedure likewise changes lan_dns after its last render. Add just edge-render after every registry edit in these procedures, commit the registry and both generated outputs, and retain the planned rollback ordering of DNS apply, cache expiry, then merging the proxy change. -->

### Start DNS sampling before the replacement

**Location:** docs/runbooks/edge-ingress.md:119  
**Severity:** important  
<!-- codex: round-2: The foreground tofu apply completes before the sampling loop starts, so the documented commands cannot measure a replacement gap that ends during apply; dig +short also cannot distinguish NXDOMAIN from other empty responses. Start timestamped sampling of both resolvers in another terminal before applying, capture DNS status and answers through recovery and cache expiry, and explicitly record when caching masks a gap rather than reporting an unobserved gap as zero. -->

### Empty test profiles unexpectedly write production DNS

**Location:** scripts/edge/render.py:702  
**Severity:** nit  
<!-- codex: round-2: DNS suppression tests the decoded profile's truthiness, so --test-profile containing {} or null writes dns-records.json, including the production default path when only --out points to a fixture destination. Gate DNS generation on args.test_profile being absent, reject malformed or empty profiles explicitly, and test that supplying a profile never writes the DNS artifact. -->

### Update the standard for the fixed-response exception

**Location:** docs/standards/edge-ingress.md:59  
**Severity:** nit  
<!-- codex: round-2: The standard still says validation MUST reject auth: none on every private host, and its auth table restricts none to public content, contradicting the accepted canary change. Document the narrowly scoped fixed-respond exception in both places while retaining the prohibition for real upstream backends. -->
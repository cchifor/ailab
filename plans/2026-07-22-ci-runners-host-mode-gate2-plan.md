# AgentForge v2 — "Gate 2 / CI runners" host-mode formalization + PREFLIGHT #2

## Codex Review
- The repo-only scope and host-mode decision are sound, but the AgentForge workflow mismatch must remain an explicit activation blocker, not merely an observation.
- `AutoAddPolicy` is unsafe and does not handle changed host keys; use verified host fingerprints and strict checking.
- Probe Docker as the `runner` service account, make capacity a hard gate, and give every remote operation bounded timeouts and independent results.
- The Gitea API online check must be mandatory for an activation health gate; host files and an active service cannot prove that Gitea considers each runner online.
- Keep transport, parsing, and evaluation separate, and expand tests around missing fields, sudo failures, timeouts, API failures, and secret-safe diagnostics.

## Context
The AgentForge v2 gated activation (`plans/2026-07-13-iac-activation-plan.md`, Stage 0–5) depends on a
CI-runner pool to build+push the bootstrap-class images (Stage 0) and the workload images (Stage 4) that
the whole activation pins by digest. ADR 0019 §Phasing originally listed **"k8s-native CI runners"** (KEDA
ScaledJob on the Kata pool) under P2. The user has **overridden that to host-mode**: reuse the EXISTING
Gitea `act_runner` pool (ADR 0013 VMs, ADR 0017 forge migration) — `ci-runner-1..5`, vmid 4101–4105, IPs
**.14–.18**, label **`self-hosted-hv:host`**.

**Live state verified 2026-07-22 (read-only):** all 5 runners `gitea-act-runner.service` active, docker
29.6.2 up, registered to `git.chifor.me` with label `['self-hosted-hv:host']`, registry.chifor.me/v2/ →
200, one runner actively serving a live `platform-e2e` job. Pool is **healthy** — no remediation needed.

**Why host-mode (not k8s-native ScaledJob/Kata):** the image builds run privileged host docker
(`deploy/*.Dockerfile` → `docker build`/`push`); a Kata/gVisor sandbox pool deliberately CANNOT host
privileged DinD (ADR 0019 §"fail closed onto Kata nodes … never gVisor, which can't host privileged
DinD"). A k8s-native runner would duplicate a working pool and can't do the one job Stage 0 needs. So the
host-mode pool is the correct and already-proven mechanism.

## Gap analysis (what's missing vs. already done)
Already correct/complete (NO change): `kubernetes/infra/runners/` tofu (IP map already .14–.18; IPs are
`lifecycle.ignore_changes=[initialization]` so edits are doc-only); `ansible/roles/gitea_runner` (full
host-mode role — label schema `host`, capacity 1, MemoryMax 10G, reclaim+cleanup timers, IPv4-first DNS);
`inventory/hosts.yml` (all 5 in `gitea_runners` at .14–.18); agentforge `.gitea/workflows/images.yml`
(`runs-on: ${{ vars.RUNNER_LABEL }}`, host-mode docker → registry.chifor.me).

Missing / to deliver (all **repo-only, no VM mutation, mergeable**):
1. **PREFLIGHT #2 health-gate** — no read-only gate exists to assert the pool is fit before an activation
   stage relies on it (the plan's Stage-0/Stage-4 image builds). Mirror the existing
   `scripts/check-nested-virt.py` + `just nested-virt-verify` pattern.
2. **Decision record** — nothing captures host-mode as the CHOSEN path over (a) `docker://` and (b) the P2
   k8s-native ScaledJob/Kata proposal. Add an ADR 0019 addendum + a runbook section.
3. **Stale-IP corrections** — `.47/.48/.49 + .33/.34` survive in `CLAUDE.md` (inventory table), ADR 0013
   (2 lines), and `variables.tf` header comment. Live pool is .14–.18.

## Deliverables

### D1 — `scripts/check-ci-runners.py` + `just ci-runners-preflight` (PREFLIGHT #2) [TDD]
Read-only SSH probe (paramiko, `ubuntu` user, key `~/.ssh/id_ed25519`, `AutoAddPolicy` — REQUIRED: the
renumbered VMs present changed host keys), mirroring `check-nested-virt.py` structure/exit-codes. Default
host list = the 5 runner IPs (.14–.18), overridable via argv. **One combined remote probe per host** emits
`key=value` lines; a **pure `evaluate_host()`** turns them into pass/fail (unit-testable without SSH).

<!-- codex: BLOCKER: AutoAddPolicy accepts previously unknown keys but does not recover from a changed known-host key, and silently trusting new keys permits interception. Use strict verification with a dedicated known_hosts file containing operator-verified fingerprints; report mismatches with manual remediation guidance and never mutate trust state during the gate. -->

Per-host asserts (all must pass):
- `daemon` — `gitea-act-runner.service` is `active`.
- `docker` — `sudo -n docker version --format {{.Server.Version}}` non-empty (ubuntu isn't in the docker
  group; only `runner` is → must use sudo).

<!-- codex: Running Docker as root through sudo proves daemon availability but not that the service account can execute builds. Probe `sudo -n -u runner docker version` and fail distinctly on sudo denial or runner socket permission failure. -->

- `label` — `/home/runner/act-runner/.runner` (sudo) `labels` contains `self-hosted-hv:host` AND `address`
  == the Gitea instance (host-mode + registered).

<!-- codex: The .runner file contains registration credentials. Extract only the required fields with a defined JSON parser, suppress raw content and stderr, normalize the address consistently, and fail closed on invalid JSON, missing fields, or duplicate probe keys. -->

- `registry` — `curl -sS -o /dev/null -w %{http_code} https://registry.chifor.me/v2/` == 200 (anon pull /
  push target reachable — image builds pull+push here).

<!-- codex: A 200 response proves the anonymous registry handshake and network/TLS path, not robot-secret validity or push authorization. Do not describe it as an end-to-end push check unless credentials are validated safely. -->

- `gitea` — `curl ... https://git.chifor.me/` returns a **non-000** HTTP code (TLS+L7 egress; the API
  `/version` is Authelia-gated → 403, so assert "got an HTTP response", not a specific code).

<!-- codex: Accepting 403 can be correct for a deliberately Authelia-gated endpoint, but accepting every non-000 code also accepts 5xx failures. Define the endpoint and an explicit status policy, add connect and total timeouts, and rely on the mandatory authenticated API check below for actual runner health. -->

- `capacity` — informational: `sudo grep capacity config.yaml` (report value; not a hard gate).

<!-- codex: Capacity is part of the declared runner configuration and affects fitness. Read it from the absolute config path and hard-fail unless it is the expected numeric value 1; a relative grep can inspect the wrong file or silently return nothing. -->

Exit 0 = all hosts pass; exit 1 = any host FAIL/unreachable (an unreachable host is a gate FAIL, not a
skip — matches `check-nested-virt.py`). Prints `[ OK ]/[FAIL] <ip>: …` per host + `ci-runners preflight:
PASS/FAIL` summary. `.env` `NODE_ROOT_PASSWORD` is NOT used (runner VMs are key-only `ubuntu`); no secrets
read/printed.

<!-- codex: Specify bounded connect, authentication, command, and read timeouts and collect each subcheck result even when another command fails. Disable password fallback and interactive prompts, sanitize exceptions, and never log private-key material, authorization headers, environment values, raw .runner content, or unfiltered command output. Document the shared ubuntu key and Paramiko environment as explicit prerequisites. -->

**Optional Gitea-API online cross-check (env-gated):** if `GITEA_TOKEN` is set, GET
`/api/v1/orgs/cchifor/actions/runners` and warn on any pool runner not `online`. Skipped (not failed) when
absent, so the gate is self-contained over SSH.

<!-- codex: BLOCKER: Make this check mandatory. An active process and static registration file cannot prove that Gitea sees a schedulable runner. Absence of the token, API/auth/schema failure, or any expected ci-runner-1..5 registration being missing or offline must fail the gate; unrelated stale registrations may warn. Verify the endpoint, inheritance behavior, response schema, and least-privilege token scope against the deployed Gitea version, especially because these appear to be instance-level runners queried through an org endpoint. Keep the token local, out of argv and SSH, and fully redacted. -->

**TDD:** `scripts/tests/test_check_ci_runners.py` (stdlib `unittest`, no new dep; run
`python -m unittest`). Cases: all-good→OK; daemon inactive→FAIL; docker empty→FAIL; wrong/`docker://`
label→FAIL; unregistered (no address)→FAIL; registry!=200→FAIL; gitea 000→FAIL; gitea 403→OK; malformed
probe line tolerated. Write tests first, then the script.

<!-- codex: Separate transport, `parse_probe_output()`, and pure `evaluate_host()` boundaries, with a main guard so importing tests performs no I/O. Add cases for missing and duplicate required keys, invalid .runner data, sudo denial, per-command nonzero status, timeout/unreachable transport, Gitea 5xx, missing token, API 401/403, malformed API data, and expected runner missing/offline. Malformed lines may be ignored only when all required unambiguous fields remain present. Use an explicit unittest discovery command for this test directory. -->

### D2 — Decision record (ADR 0019 addendum + runbook)
- **ADR 0019** — append an "Update (2026-07-22): CI runners = host-mode (P2 override)" note: the §Phasing
  "k8s-native CI runners" item is RESOLVED by reusing the host-mode `act_runner` pool (ADR 0013/0017), not
  a KEDA ScaledJob/Kata runner; rationale (privileged DinD can't run on Kata/gVisor); gated by PREFLIGHT #2
  (`just ci-runners-preflight`). Preserve original text (addendum, not rewrite).
- **`docs/runbooks/ci-runners.md`** — new section "8. AgentForge v2 image-build CI (host-mode) + PREFLIGHT
  #2": host-mode is the chosen path (over `docker://` and over P2 k8s-native); the `images.yml` build path
  runs on this pool; Gitea **org Actions** config it needs (`RUNNER_LABEL=self-hosted-hv` var +
  `REGISTRY_USERNAME`/`REGISTRY_PASSWORD` robot secrets for registry.chifor.me push); the PREFLIGHT #2
  procedure (what it checks, exit codes, when to run — before Stage-0/Stage-4 image builds); stale-IP
  correction note.

<!-- codex: Add the mandatory GITEA_TOKEN prerequisite, required scope, safe storage, and redaction rules. Also document that host-mode Docker access is root-equivalent: only trusted repositories and protected events may target this label, forked or untrusted pull requests must not receive host execution or registry secrets, and workflow logs must never echo credentials. -->

### D3 — Stale-IP corrections
- `CLAUDE.md` inventory row → `.14 / .15 / .16 / .17 / .18`.
- `docs/decisions/0013-ci-self-hosted-runners.md` lines ~14, ~79 → correct to .14–.18 with a renumber note
  (preserve history; annotate, don't silently rewrite the decision).
- `kubernetes/infra/runners/variables.tf` header comment (lines ~129–140) → .14–.18.

<!-- codex: Add a repository-wide targeted search for every retired address to verification, then classify any remaining match as intentional history or an unresolved stale reference. The three listed files should not be assumed exhaustive without that check. -->

## Non-goals / gated items (report to main, do NOT do)
- **No `tofu apply` / `just gitea-runners`** — the role is complete and the IPs are `ignore_changes`; no VM
  mutation. If a future re-register is ever needed it's a gated apply.
- **No agentforge-repo change** — `images.yml` already host-mode. (Observation for main: current `main`
  `images.yml` builds orchestrator/sandbox/p1-worker, not `openbao-bootstrap`/`worker` per activation-plan
  Stage 0 — that lives on the agentforge `feat/p2-unlock` line, separate PR, out of ailab scope.)

<!-- codex: The scope boundary is appropriate, but this is a blocking cross-repository dependency. Record an owner, expected revision or PR, and an explicit activation prerequisite so Gate 2 cannot be reported ready while the required Stage-0/Stage-4 workflow is absent from AgentForge main. -->

- **No merge** — push branch to `gitea` remote; main loop stages the merge.

## Verification
- `python -m unittest` green (D1 logic).
- `just ci-runners-preflight` run live (read-only) → expect PASS on all 5; capture output for the report.
- `python scripts/check-ci-runners.py` re-run to confirm idempotent/read-only.
- `tofu -chdir=kubernetes/infra/runners fmt -check` unaffected (comment-only edit); no `plan` needed
  (comment change is not applied — `ignore_changes`).

<!-- codex: Verification must also prove the mandatory API path, expected runner-name mapping, strict host-key behavior, secret-free captured output, and that repeated runs do not change known_hosts or other local state. Add explicit test discovery and stale-IP search commands. -->

<!-- codex-review-status: complete -->

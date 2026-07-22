# AgentForge v2 — "Gate 2 / CI runners" host-mode formalization + PREFLIGHT #2

## Context
The AgentForge v2 gated activation (`plans/2026-07-13-iac-activation-plan.md`, Stage 0–5) depends on a
CI-runner pool to build+push the bootstrap-class images (Stage 0) and the workload images (Stage 4) that
the whole activation pins by digest. ADR 0019 §Phasing originally listed **"k8s-native CI runners"** (KEDA
ScaledJob on the Kata pool) under P2. The user has **overridden that to host-mode**: reuse the EXISTING
Gitea `act_runner` pool (ADR 0013 VMs, ADR 0017 forge migration) — `ci-runner-1..5`, vmid 4101–4105, IPs
**.14–.18**, label **`self-hosted-hv:host`**.

**Live state verified 2026-07-22 (read-only):** all 5 runners `gitea-act-runner.service` active, docker
29.6.2 up, registered to `git.chifor.me` with label `['self-hosted-hv:host']`, registry.chifor.me/v2/ →
200. The Gitea org runners API (`/api/v1/orgs/cchifor/actions/runners`, verified with a short-lived
read-only token) reports all 5 (`ci-runner-1..5`, ids 9–13) `"status":"online"` (label name
`self-hosted-hv`). Pool is **healthy** — no remediation needed.

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
Read-only probe mirroring `check-nested-virt.py` structure/exit-codes. Default host list = the 5 runner IPs
(.14–.18) mapped to their expected runner names (`ci-runner-1..5`), overridable via argv. Code is split
into clean layers so the decision logic is unit-testable without SSH/network:
- **transport** — `run_probe(host)` (paramiko SSH) and `query_gitea_runners(token)` (HTTP); the only I/O.
- **parse** — `parse_probe_output(text) -> dict` (tolerant `key=value` parser).
- **pure eval** — `evaluate_host(fields) -> HostResult` and `evaluate_api(runners, expected) -> ApiResult`.
- `if __name__ == "__main__"` guard so importing the module (tests) performs **no I/O**.

**SSH transport.** `ubuntu` user, key `~/.ssh/id_ed25519`, `look_for_keys=False`, `allow_agent=False`, **no
password fallback**, bounded `connect(timeout=...)` + `exec_command(timeout=...)`. Host-key policy follows
the repo's established probe convention (`scripts/node-ssh.py`, `scripts/check-nested-virt.py`):
`AutoAddPolicy` with the system `known_hosts` NOT loaded — so a renumbered VM's changed key never raises
`BadHostKey`, and the user's `known_hosts` is never mutated (idempotent/read-only local state).

**One combined remote probe per host** emits independent `key=value` lines (each subcheck has its own
`|| echo <key>=FAIL` so one failing command never blanks the others). Per-host asserts (ALL must pass):
- `daemon` — `gitea-act-runner.service` is `active` (`systemctl is-active`).
- `docker` — probe as the **`runner` service account** (`sudo -n -u runner docker version --format
  {{.Server.Version}}`): proves the account that actually runs jobs can reach the socket, not just root.
  Distinct failure lines for sudo-denied vs socket-permission vs daemon-down.
- `label`/`registered` — parse `/home/runner/act-runner/.runner` **remotely with `python3 -json`**,
  emitting ONLY `label=` (must contain `self-hosted-hv:host`) and `address=` (must equal the Gitea
  instance URL). The token/uuid fields are NEVER read out or printed; invalid JSON / missing fields ⇒
  `label=FAIL`/`address=FAIL` (fail-closed).
- `registry` — `curl --connect-timeout N --max-time N -s -o /dev/null -w %{http_code}
  https://registry.chifor.me/v2/` == 200 (proves the **anonymous pull path + TLS** to the registry the
  builds push/pull; NOT a push-auth/robot-secret check — worded as such).
- `gitea` — `curl --connect-timeout N --max-time N ... https://git.chifor.me/api/v1/version`; status
  policy: reachable = code ∈ {200, 401, 403} (the API requires sign-in → 403 is expected & fine); FAIL on
  `000` (no TLS/L7) or any `5xx`.
- `capacity` — read from the **absolute** config path (`sudo -n cat /home/runner/act-runner/config.yaml`);
  hard-gate that `capacity` parses to the expected integer (`EXPECTED_CAPACITY = 1`, a module constant);
  unreadable/mismatch ⇒ FAIL.

**Gitea-API online cross-check — DEFAULT-ON (authoritative fitness signal).** A live daemon + a static
`.runner` file cannot prove Gitea sees a *schedulable* runner. So the gate also queries
`GET {gitea}/api/v1/orgs/cchifor/actions/runners` (schema verified live:
`{"runners":[{"name","status","busy","disabled","labels":[{"name"}]}],"total_count"}`) and asserts **every
expected `ci-runner-1..5` is present with `status=="online"`** (`busy` is fine). Token source: env
`GITEA_TOKEN` (fallback `AF_GITEA_TOKEN`); scope `read:organization` (org owner/admin). The token is kept
local — **never** placed in argv, an SSH command, or any log line; all error output is redacted.
Fail-closed: **missing token, API/HTTP/auth/schema failure, or any expected runner missing/offline ⇒ gate
FAIL.** Unexpected/stale extra registrations ⇒ WARN only (don't fail on unrelated org runners). An explicit
`--skip-api` flag (logged loudly as "API online-check SKIPPED — host-side only") is the one documented
escape hatch for environments without a token; skipping is an explicit operator decision, never silent.

Exit 0 = all hosts pass host-side AND (unless `--skip-api`) the API online-check passes; exit 1 = any
FAIL/unreachable (an unreachable host is a gate FAIL, matching `check-nested-virt.py`). Prints
`[ OK ]/[FAIL] <name> <ip>: …` per host + `ci-runners preflight: PASS/FAIL` summary. No `.env`
`NODE_ROOT_PASSWORD` (runner VMs are key-only `ubuntu`); no secrets printed. `just ci-runners-preflight`
wraps `python scripts/check-ci-runners.py`.

**TDD** (`scripts/tests/test_check_ci_runners.py`, stdlib `unittest`, no new dep). Write tests first. Cases:
all-good→OK; daemon inactive→FAIL; docker empty / sudo-denied→FAIL; wrong or `docker://` label→FAIL;
unregistered (no/other address)→FAIL; invalid `.runner` JSON→FAIL; missing OR duplicate required probe
keys→FAIL; per-command nonzero surfaced→FAIL; registry≠200→FAIL; gitea `000`/`5xx`→FAIL; gitea 403→OK;
capacity≠1 / unreadable→FAIL; API: missing token→FAIL, API 401/403→FAIL, malformed API JSON→FAIL, an
expected runner missing→FAIL, an expected runner `offline`→FAIL, extra stale runner→WARN-not-FAIL,
all-online→OK. Run: `python -m unittest discover -s scripts/tests -p "test_*.py"`.

### D2 — Decision record (ADR 0019 addendum + runbook)
- **ADR 0019** — append an "Update (2026-07-22): CI runners = host-mode (P2 override)" note: the §Phasing
  "k8s-native CI runners" item is RESOLVED by reusing the host-mode `act_runner` pool (ADR 0013/0017), not
  a KEDA ScaledJob/Kata runner; rationale (privileged DinD can't run on Kata/gVisor); gated by PREFLIGHT #2
  (`just ci-runners-preflight`). Preserve original text (addendum, not rewrite).
- **`docs/runbooks/ci-runners.md`** — new section "8. AgentForge v2 image-build CI (host-mode) + PREFLIGHT
  #2": host-mode is the chosen path (over `docker://` and over P2 k8s-native); the `images.yml` build path
  runs on this pool; Gitea **org Actions** config it needs (`RUNNER_LABEL=self-hosted-hv` var +
  `REGISTRY_USERNAME`/`REGISTRY_PASSWORD` robot secrets for registry.chifor.me push). PREFLIGHT #2
  procedure: what it checks, exit codes, when to run (before Stage-0/Stage-4 image builds), and the
  **`GITEA_TOKEN` prerequisite** (scope `read:organization`; how to mint via `gitea admin user
  generate-access-token`; store out-of-repo; never echo). **Security note:** host-mode docker is
  **root-equivalent on the VM** — only trusted repos / protected events may target `self-hosted-hv:host`;
  forked/untrusted PRs must NOT receive host execution or the registry secrets; workflow logs must never
  echo credentials. Plus the stale-IP correction note.

### D3 — Stale-IP corrections
- `CLAUDE.md` inventory row → `.14 / .15 / .16 / .17 / .18`.
- `docs/decisions/0013-ci-self-hosted-runners.md` lines ~14, ~79 → correct to .14–.18 with a renumber note
  (preserve history; annotate, don't silently rewrite the decision).
- `kubernetes/infra/runners/variables.tf` header comment (lines ~129–140) → .14–.18.
- **First run a repo-wide `grep` for every retired address** (`\.47|\.48|\.49|\.33|\.34` and the old
  vmid/IP prose) and classify each remaining hit as intentional history (leave, annotated) vs. unresolved
  stale ref (fix). The three files above are the known set but not assumed exhaustive.

## Non-goals / gated items (report to main, do NOT do)
- **No `tofu apply` / `just gitea-runners`** — the role is complete and the IPs are `ignore_changes`; no VM
  mutation. If a future re-register is ever needed it's a gated apply.
- **BLOCKING cross-repo activation prerequisite (not just an observation):** agentforge `main`
  `.gitea/workflows/images.yml` builds `orchestrator`/`sandbox`/`p1-worker`, but the activation plan's
  Stage 0 needs `openbao-bootstrap` + `worker` images built on this pool. **Owner: main loop / agentforge
  repo (separate PR, out of ailab scope).** Gate 2 (this ailab work) formalizes the RUNNERS; it must NOT be
  reported as "activation-ready" until the agentforge workflow actually builds the Stage-0/Stage-4 images
  (or a separate build path is confirmed — recent ailab `pin(openbao): bootstrap image` commits imply one
  exists on the `feat/p2-unlock` line; **verify during impl** and record the exact source).
- **No merge** — push branch to `gitea` remote; main loop stages the merge.

## Verification
- `python -m unittest discover -s scripts/tests -p "test_*.py"` green (D1 logic incl. API + host-key +
  secret-free paths).
- `just ci-runners-preflight` run live (read-only) → expect PASS on all 5 (with a token) and a clean
  `--skip-api` run; capture output for the report; confirm the API path maps names→online correctly.
- Re-run `python scripts/check-ci-runners.py` → confirm idempotent/read-only and that `~/.ssh/known_hosts`
  is byte-identical before/after (no local trust-state mutation).
- Confirm captured output contains NO token, key material, `.runner` raw content, or auth headers.
- Repo-wide `grep` for retired addresses returns only intentional-history hits after D3.
- `tofu -chdir=kubernetes/infra/runners fmt -check` unaffected (comment-only edit); no `plan` needed
  (comment change is not applied — `ignore_changes`).

<!-- codex-review-status: complete -->

# Relay researcher drill run R0: Relay's half of witness drills A-C, before R1

Status: **procedure, 2026-11-17. Merging this file changes nothing live.** It is the exact procedure for
drill run R0 that the owner asked for. Every step is an operator action on the cluster, OpenBao, the
budget ledger, the Router admin API or git, run only with the owner's direct confirmation for R0.

Where another runbook gives a command, this file names it and does not change it. A command that no
runbook gives is marked **NEW (not in the runbook)**; the owner reviews those before use. Values that
git cannot give are `<operator fills: …>` placeholders, each saying where to read the value.

Sources, at ailab `main` `8229304e` and Relay `e77bd09a` (the live hub v0.10.4; `rel4:` below) and
`c0fe50f0` (the ledger SQL and README; `rel:` below):

- [relay-researcher-remaining-live-steps.md](relay-researcher-remaining-live-steps.md), "the checklist":
  step 0's session and helpers, step 4's client Pod and `q`, step 5's building blocks and drills;
- [relay-worker-budget-authority.md](relay-worker-budget-authority.md), "the budget runbook": "Changing
  the witness" (`:375-399`) and "Drills before production `ready=true`" (`:411-481`);
- [relay-researcher-qualification-campaign.md](relay-researcher-qualification-campaign.md), "the
  campaign runbook": §3.0-§3.1 (signing), §3.4 (hub files), §5.1 (clocks), §5.6 and O1;
- [relay-candidate-run-generator.md](relay-candidate-run-generator.md), "the generator runbook", and
  `scripts/relay-candidate-run.py`;
- [relay-researcher-hub-upgrade.md](relay-researcher-hub-upgrade.md) H6 and H7.

## 1. Purpose and the owner's decision

The budget runbook runs Relay's half of witness drills A-C "when it is included, before Relay's first
allocation" (`relay-worker-budget-authority.md:418-419`). Relay's budget authority starts only when a
profile source exists, which before the release approval is H7's candidate file (Relay
`rel4:server/main.ts:326-352`; hub runbook H6 and H7), and H7 is included by a signed run's per-run PR.
R1 is the first allocation. Owner decision, 2026-11-17 (checklist open point 3), word for word:

> 3: drill run R0 before R1. Sign R0, merge its per-run PR (H7 live), never grant or create it (it
> doesn't count toward the 8), run Relay's half of A, B and C with the read-only pool-readiness checks,
> then reset and sign R1. Please write R0's exact procedure (trigger, captures, timings) as a reviewed
> runbook PR.

So R0 is a signed candidate run whose hub half goes live and whose agent half never does. It has no
grant, no worker, no environment, no allocation, no journal install and no budget scope. Owner decision
O1 (campaign runbook `:584`, `:748`) already says a run aborted before its create does not count toward
the eight; R0 is such a run by design.

R0 reuses the witnesses of the Router drills (checklist step 5): the stale initial witness A and the
current witness B. There is **no new epoch rotation and no new issuance record entry**. Every drill
injection is a drill capture only (`relay-worker-budget-authority.md:428-438`).

## 2. What the live hub can observe (from Relay source)

These findings set R0's observable. Two of them differ from the owner's wording and need the owner's
confirmation before R0 is signed (§4).

**F1. The pool-readiness listing cannot reach the ledger during the candidate campaign.** The read-only
preflight the owner named is `GET /api/worker-pools` (operator role, `rel4:server/workers/routes.ts:263-266`;
the web UI's Create Worker form reads it, `web/src/workers/CreateWorker.tsx:105-106`). For each template,
`inspectPoolReadiness` first requires the ordinary release qualification registry:

- "Candidate admission cannot replace or supply ordinary release qualification": with a candidate file
  loaded, the readiness check uses the wrapped `qualified` mode (`rel4:server/workers/pool-readiness.ts:100-122`,
  `server/environments/kubernetes-qualification.ts:29-38`);
- that mode is a `KubernetesQualificationRegistry` only when `RELAY_WORKER_QUALIFICATION_FILE` is set
  (`rel4:server/main.ts:260-273`). It is general activation, step 11 (hub runbook H7's table), and no
  ailab component sets it. Otherwise it stays `"refuse-fixture"`;
- so every template gets `runtime-qualification-unavailable`, and the ledger preflight
  (`pool-readiness.ts:174-211`) runs only `if (!reason && …)` (`:157`). It is never called.

During R0 the listing therefore reports `available: false`, reason `runtime-qualification-unavailable`,
for every witness state. That is not a ledger observation. The same holds through the whole candidate
campaign. Its own limits, for the record: at most eight probes per listing and none after ten seconds
(`pool-readiness.ts:336-339`), one probe at a time per tenant and four tenants per host (`:28-37`), and the
shared 120 requests per minute per issuer and tenant (`rel:deploy/worker-budget-ledger/README.md:336-340`).
`scripts/relay-admin-call.py` (ailab #1799) has no operation for this route anyway: its allowlist is
`me`, `list-installations`, `get-installation` and `enrollment-token`.

**F2. The hub's isolated budget observer does reach the ledger, read-only.** When control metrics are
configured (H6) and the budget authority has started (H7), the hub opens a separate one-connection pool,
`application_name` `relay-worker-budget-observation` (`rel4:server/main.ts:429-442`,
`server/workers/inference/budget-observer.ts:20-48`). On every authorized scrape of
`/metrics/agent-control-plane` it reads the mounted witness file, parses it, and runs
`worker_budget.current_epoch(<mounted ids>)` as the Relay login `relay_budget_allocator`
(`budget-observer.ts:53-73`; the scrape calls it through `collectBudget`, `server/main.ts:444-448`). It
creates no preparation, authorization or preflight. The scrape exports
(`server/agent-sessions/metrics.ts:128-148`):

| Series (job `relay-control-plane`) | Meaning |
| --- | --- |
| `relay_control_worker_budget_authority_started` | 1 when the budget authority started (H7) |
| `relay_control_worker_budget_authority_start_failed` | 1 when ledger files are configured but no authority started |
| `relay_control_worker_budget_observer_started` | 1 when the observer opened |
| `relay_control_worker_budget_check_succeeded` | 1 when the latest check returned the mounted epoch |
| `relay_control_worker_budget_check_timestamp_seconds` | time of the latest check, failures included; 0 if none |

Prometheus scrapes it every 30 s (`kubernetes/components/relay-control-monitoring/collector.yaml`).
`RelayWorkerBudgetCheckFailed` fires after 2 min of a failed or stale check
(`kubernetes/components/relay-worker-monitoring/relay-worker-rules.yaml`). By its own description the
check "cannot distinguish refusal from database failure". So R0 pairs every observer reading with
ledger-side controls as the same login, in the same window (§7).

**F3. A drill witness does not stop the hub from starting.** The ledger and observer read the witness
lazily, on each query (`rel4:server/workers/inference/budget-ledger.ts:245-272`); startup reads only
the DSN and the CA (`budget-ledger.ts:276-324`, `budget-startup.ts:27-43`). With an empty or malformed
witness the hub still reports `authority_started` 1 and `observer_started` 1, and every check fails at
the witness parse (`budget-observer.ts:59`). That parse sits inside the check's `try`, so a parse
failure is stamped like any other failed check: `{succeeded: false, checkedAt: Date.now()}`
(`budget-observer.ts:57-72`, the stamp at `:71`), and `check_timestamp_seconds` still advances. With the
stale A, the parse succeeds and `current_epoch` raises 42501.

**F4. Nothing else touches the ledger in R0.** The authority's refresh loop selects only workers that
have a budget preparation (`rel4:server/workers/inference/budget-preparation.ts:572-580`), and there is
none. So the budget runbook's Relay pass, "`worker_budget_ledger_unavailable`, allocation refused"
(`relay-worker-budget-authority.md:455`), cannot occur: that log line comes from the authority's own
ledger client (`server/main.ts:343-347`), which only an allocation path or a reached preflight would
use. R0 must never create one.

**F5. An expired run stops the authority only at the next hub start.** The candidate document is read
once, at startup. `CandidateHubAuthority`'s constructor verifies the run (`rel4:server/workers/candidate/authority.ts:98-110`),
and `verifyCandidateRun` refuses `now >= expiresAt` (`packages/worker-runtime/candidate-signatures.ts:213-226`).
A hub that started before `expiresAt` keeps its candidate authority, its budget authority and its
observer. A hub that starts at or after `expiresAt` logs `worker_candidate_unavailable` with reason
`authority-invalid` (`candidate/load.ts:45-48`, `server/main.ts:297-304`), has no profile source, starts
no budget authority (`server/main.ts:337`) and no observer. Its metrics then read `start_failed` 1,
`observer_started` 0, `check_succeeded` 0 and `check_timestamp_seconds` 0: no observation at all. §9
plans around it.

## 3. Where R0 sits in the order

R0 runs **after** H6, the candidate journal and File B are read back (checklist, "After this
checklist", items 1-3), and **before** R1 (item 5). The E6 rehearsal (item 4) may come before or after
R0. R0 signs a real run, so File B's `tokenPolicyDigest` must exist first (hub runbook H7: "File B comes
before any run is signed").

**The gate during R0.** By then the production gate is open: checklist steps 7 and 8 set `ready=true`
and enabled the Router plugin, and H6 requires both (`relay-researcher-hub-upgrade.md:324-327`). R0
follows the drill protocol (`relay-worker-budget-authority.md:423-459`):

- each drill installs its witness with the gate withdrawn ("Changing the witness" steps 1-2 and 4-6);
- it re-opens the gate only for that drill's observation, proves it open, observes, and withdraws;
- it restores B and proves B before the next drill.

After drill C, the owner re-opens the production gate (§8). Its end state is exactly the state before
R0. The Router is not rolled in R0: its mounted witness stays B throughout and serves as an in-window
control. That holds because both consumers mount `witness.json` as a `subPath` file
(`kubernetes/components/llm-router-worker-budget-reader/deployment.yaml:39-41`,
`kubernetes/components/relay-worker-budget-client/deployment.yaml:32-34`), which never updates in a
running Pod (`relay-worker-budget-authority.md:376-378`; checklist, "Starting state"). A force-sync
changes the Secret, not a running Pod's file: only a roll does. Every W_R and O_R captures
`wsha_router` and `wsha_relay` to show it.

## 4. Owner confirmations before R0 is signed

Record each on ailab #1753. Without them R0 is not signed.

1. **The observable (F1, F2).** The pool-readiness listing cannot see the ledger in candidate mode. R0
   observes Relay through the hub's budget observer metrics (§7), with ledger-side controls.
2. **The Relay pass (F4).** For A, B and C, Relay's pass is: two consecutive observer checks, both after
   the gate was proven open, with `check_succeeded` 0 while `authority_started` and `observer_started`
   are 1; the hub's mounted witness is the drill's; the same-login control on B's IDs succeeds before
   and after. For A, also `current_epoch(<A ids>)` as `relay_budget_allocator` fails with 42501. This
   replaces the runbook's "`worker_budget_ledger_unavailable`, allocation refused", which needs an
   allocation, as owner decision 4 did for the Router's drills B and C.
3. **The agent tree.** The generator always writes the agent half, and with the first run onto main's
   enrollment agent tree it also writes the agent's one-time candidate structure (generator runbook,
   "One-time prerequisites"). R0's per-run PR therefore carries that structure and R0's agent values, as
   generator output the merge gate compares byte for byte. `relay-researcher-agent` stays suspended from
   before the merge until the reset PR has merged, so the live agent never runs them, and the reset
   reverts them (§10). R1's `generate` then writes the same structure again.
4. **A shortened run.** If a drill cannot start by its latest start (§5), it is not run in R0. The
   missing drills need another drill run, signed fresh and also never granted, which needs its own
   owner approval.

## 5. Timeline

`t_s` is R0's `issuedAt`. The run is valid until `expiresAt = t_s + 120 min` (campaign §3.1, 7,200 s).
The hub rollout and its readback must finish by `t_s + 60 min`, or R0 aborts before any drill
(campaign `:104-106`; the generator's `check --base` fails past it). A hub roll took about 1 to 3
minutes so far (H4a: about 1 minute of downtime, plus the pre-migration backup, which is bounded at
1,800 s). The table assumes 3 minutes per roll.

| From `t_s` | Step | Target | Limit |
| --- | --- | --- | --- |
| before | §6.1: preconditions, branch, Flux suspension, client Pod, Prometheus forward | no clock | |
| 0 | §6.2: sign, compose, generate, Secret, commit, push, open the PR | 10 min | |
| | CI, `check --base`, merge, Flux, hub roll 0, H7 readback with Relay's control on B | 35 min | `t_s + 60 min`, else abort (§9) |
| 35 | drill A: W 6, O 4, R 7 min | 52 min | latest start `t_s + 85 min` |
| 52 | drill B: W 6, O 4, soft delete 4, R 7 min | 73 min | latest start `t_s + 85 min` |
| 73 | drill C: W 6, O 4, R 7 min | 90 min | latest start `t_s + 85 min` |
| 90 | §8: production re-open (owner), end readback | 95 min | |
| | no hub roll starts after this | | `t_s + 105 min` |
| 120 | `expiresAt` | | |
| later | §10: reset PR, its hub roll, cleanup | same day | |

R0 rolls the hub eight times: the per-run PR's rollout, six drill rolls (A, B and C each install and
restore) and the reset's rollout. Each is a Recreate outage of the hub with a pre-migration backup:
dev-worker connectors and relay-bus keys pause for the roll. Announce the window. R0 does not roll the
Router.

Expected alerts: `RelayWorkerBudgetCheckFailed` fires whenever the gate stays withdrawn or a drill
witness stays mounted for more than 2 min; `RelayWorkerBudgetStartupFailed` fires again after the reset
until R1's H7 readback, as after H6.

Write `t_s`, `expiresAt`, `t_s + 60`, `t_s + 85` and `t_s + 105` into the stopwatch log at signing, from
the generator summary (`issuedAt`, `expiresAt`, `abortBeforeGrantIfHubNotReadBackBy`).

## 6. Before the drills

### 6.1 Before signing (no clock running)

**The session.** Run the checklist's step 0 block (the helpers `cap`, `radmin`, `lsql`, `lwrite`,
`wsha_*`, `plugins`), step 4's client Pod and `q`, and step 5's building blocks (`withdraw.sql`,
`open-B.sql`, `plugin`, `force_sync`, `wait_hashes`, `rlog`, `roll_router`, `ledger_state`). They are
unchanged. Step 5 deleted the client Pod, so create it again with step 4's manifest and `wait`. If this
session's `$ev` has no `withdraw.sql`, `open-B.sql` or `witness-B.json`, write them again from step 5's
blocks and the offline record; `sha256sum "$ev/witness-B.json"` must equal B's hash in the record.

**R0's helpers.** All NEW (not in the runbook):

```sh
# The Prometheus API on 127.0.0.1:9090 (the form of dsh.md's port-forward), for the whole session.
# Its own variable: step 5's plugin() sets the global `pf`.
kubectl --context admin@ai -n monitoring port-forward svc/kube-prometheus-stack-prometheus 9090:9090 >"$ev/pf.log" 2>&1 &
r0_prom_pid=$!
# rcheck: one JSON object, the hub's budget series as Prometheus last scraped them. It fails unless
# exactly one relay-control-plane target answers.
rcheck() {
  local -; set -o pipefail
  curl -sS --fail --connect-timeout 5 --max-time 10 -G http://127.0.0.1:9090/api/v1/query \
    --data-urlencode 'query={__name__=~"up|relay_control_worker_budget_(authority_started|authority_start_failed|observer_started|check_succeeded|check_timestamp_seconds)",job="relay-control-plane"}' \
  | jq -ec 'if .status != "success" then error("query failed") else
      (.data.result | group_by(.metric.instance)
       | if length != 1 then error("expected one relay-control-plane target, got \(length)") else
           (.[0] as $r | ($r | map({(.metric.__name__ | sub("^relay_control_worker_budget_"; "")): (.value[1] | tonumber)}) | add)
            + {instance: $r[0].metric.instance, at: $r[0].value[0]}) end) end'
}
# relay_check <0|1> <epoch seconds, fractional>: bounded wait (3 minutes by the clock, each query at
# most 10 s) for two successive observer checks (two scrapes at most 45 s apart, so none was missed in
# between) with `up`, `authority_started` and `observer_started` 1 and `check_succeeded` equal to the
# first argument. A check counts only if it was stamped more than 5 s after the given time: the
# observer stamps a check when it ends, and one check takes at most its 1 s connection and 2 s
# statement timeouts (Relay budget-observer.ts:25-33, :69-71). Returns 0 when seen and captured, 2 at
# once if a counted check shows anything else, 1 if no two successive checks arrive or a capture fails.
# Each check is one 30 s scrape.
relay_check() {
  local -; set -o pipefail
  local want="$1" since="$2" last= n=0 end=$((SECONDS + 180)) j t
  while [ "$SECONDS" -lt "$end" ]; do
    if j="$(rcheck)" && t="$(jq -er '.check_timestamp_seconds' <<<"$j")" &&
       jq -e --argjson s "$since" '.check_timestamp_seconds > $s + 5' <<<"$j" >/dev/null && [ "$t" != "$last" ]; then
      if jq -e --argjson w "$want" '.up == 1 and .authority_started == 1 and .observer_started == 1 and .check_succeeded == $w' <<<"$j" >/dev/null; then
        cap relay-check echo "$j" || return 1
        # A gap of more than one scrape interval since the previous counted check restarts the count.
        if [ "$n" -ge 1 ] && jq -e --argjson p "$last" '.check_timestamp_seconds - $p <= 45' <<<"$j" >/dev/null; then n=2; else n=1; fi
        last="$t"; [ "$n" -ge 2 ] && return 0
      else
        cap relay-check-unexpected echo "$j"; return 2
      fi
    fi
    sleep 5
  done
  cap relay-check-not-observed echo "want=$want since=$since last=${j:-none}"; return 1
}
ralerts() { curl -sS --fail --connect-timeout 5 --max-time 10 -G http://127.0.0.1:9090/api/v1/query --data-urlencode 'query=ALERTS{alertname=~"RelayWorkerBudget.*"}' \
              | jq -c '[.data.result[] | {alert: .metric.alertname, state: .metric.alertstate}]'; }
# The hub Pod: one Running Pod, its init containers' exit codes, the candidate pin and document ConfigMap.
hpod() {
  kubectl --context admin@ai -n relay get pod -l app=relay -o json | jq -c '.items[] | {pod: .metadata.name, uid: .metadata.uid, phase: .status.phase,
    init: [.status.initContainerStatuses[]? | {name, exit: .state.terminated.exitCode}],
    relay: (first(.status.containerStatuses[]? | select(.name=="relay") | {ready, restartCount}) // null),
    candidateSha256: ([.spec.containers[] | select(.name=="relay") | .env[]? | select(.name=="RELAY_WORKER_CANDIDATE_SHA256") | .value][0]),
    document: ([.spec.volumes[] | select(.name=="worker-candidate-document") | .configMap.name][0])}'
}
# hlog: the hub's budget and candidate log lines. Fails if the log cannot be read; no line is not a failure.
hlog() {
  l="$(kubectl --context admin@ai -n relay logs deploy/relay -c relay)" || return 1
  printf '%s\n' "$l" | grep -E 'worker_(budget|candidate)_' || true
}
# roll_relay: keeps the outgoing hub's lines, then rolls it (Recreate, with its backup) and waits.
roll_relay() {
  cap relay-log-before-roll hlog || { echo "log capture failed: not rolling"; return 1; }
  cap relay-pod-before-roll hpod &&
  kubectl --context admin@ai -n relay rollout restart deploy/relay &&
  cap relay-rollout kubectl --context admin@ai -n relay rollout status deploy/relay --timeout=10m
}
# The hub's worker rows: R0 must leave every count unchanged and its workerId absent.
hub_counts() {
  kubectl --context admin@ai -n relay exec -i relay-postgres-0 -c postgres -- \
    psql -X -At -v ON_ERROR_STOP=1 -U postgres -d relay -f - <<'SQL'
SELECT 'workers', count(*) FROM workers
UNION ALL SELECT 'worker_grants', count(*) FROM worker_grants
UNION ALL SELECT 'agent_environments', count(*) FROM agent_environments
UNION ALL SELECT 'worker_candidate_sessions', count(*) FROM worker_candidate_sessions
UNION ALL SELECT 'worker_budget_preparations', count(*) FROM worker_budget_preparations
UNION ALL SELECT 'worker_inference_credentials', count(*) FROM worker_inference_credentials
UNION ALL SELECT 'R0 workerId', count(*) FROM workers WHERE id = '<operator fills: R0 workerId, from R0's signing input>';
SQL
}
```

**Pre-checks.** A failed one stops R0 before signing.

- **Order (§3).** H6 is read back (hub runbook H6), the candidate journal is on `10.96.0.42`, and File B
  is committed and read back, with its `tokenPolicyDigest` recorded.
- **Owner confirmations (§4)** are on #1753, and the owner confirms the window.
- **Production gate.** `cap gate-before ledger_state` shows `ready = t` with B's IDs, and all five
  history counts 0. `cap plugins-before plugins` shows `worker-budget-authority` `active`.
- **Witness.** `wsha_bao`, both `wsha_secret`, `wsha_router` and `wsha_relay` all show B's hash (H6
  mounted B in Relay).
- **Both client logins.** Step 4's `q` reads each login's password from OpenBao on stdin
  (`af/relay-budget/allocator` and `af/relay-budget/router`, field `password`, budget runbook "Credential
  delivery"); step 4's proofs already used both. Check them again from the new client Pod (NEW
  statements): `cap login-allocator q relay_budget_allocator "SELECT current_user"` returns
  `relay_budget_allocator`, and `cap login-router q relay_budget_router "SELECT current_user"` returns
  `relay_budget_router`.
- **Hub.** `cap hub-before hpod`: one Running Pod with no `candidateSha256`.
  `cap relay-metrics-before rcheck`: `up` 1, `authority_start_failed` 1, `observer_started` 0 (H6's
  expected state, no profile source yet). `cap hub-counts-before hub_counts`.
- **Clocks.** Campaign §5.1 for signing, and the checklist step 5 NTP readback for the nodes of
  `relay/relay-*`, `llm-router/llm-router-*` and `relay-budget-authority/relay-budget-postgres-0`.
  §5.1 includes the operator host (`chronyc tracking`, offset under 0.25 s), which matters here:
  `relay_check`'s `since` is the operator host's clock, while the hub stamps its checks with its own.
  With both within 0.25 s of NTP, the skew is far below `relay_check`'s 5 s margin. Run §5.1 again
  before the first drill; an operator host offset of 1 s or more stops R0.
- **The per-run branch** is prepared (generator runbook §4a, "The first run's PR ... is H7's inclusion
  PR"): the `relay-worker-candidate` inclusion line in `kubernetes/apps/apps/relay/kustomization.yaml`
  and the H7 group removed from `scripts/tests/test_relay_v060_activation_gate.py`. The agent tree stays
  as on main: R0's run files and the agent's one-time candidate structure come from `generate` at §6.2.
- **The signing input** follows campaign §3.0 steps 2-5, with R0's own fresh `runId`, worker and launch
  IDs and session-permit key. R0 sends no prompt; record the text its `promptSha256` was computed from.
  R1's input later differs from R0's only in the fields §3.0 step 5 lists.
- **The agent stays out.** Campaign §3.5 step 1, without its scale to zero (R0's agent values never
  reach the cluster, so the running agent keeps its current configuration). The default context is a
  different cluster (checklist, "Rules for every step"), so every `flux` command here names the context
  (NEW form): `flux --context admin@ai suspend kustomization relay-researcher-agent -n flux-system`.
  Then (NEW):

  ```sh
  kubectl --context admin@ai -n flux-system get kustomization relay-researcher-agent -o jsonpath='{.spec.suspend}{"\n"}'   # true
  cap agent-before kubectl --context admin@ai -n relay-infra-control get deploy relay-infrastructure-agent \
    -o jsonpath='{.metadata.generation} {.status.readyReplicas}{"\n"}'
  ```

  Check `spec.suspend` again immediately before the merge (§6.2 step 6).

### 6.2 Sign and roll the hub (`t_s` to `t_s + 60 min`)

1. **Sign** with campaign §3.1's signer command, unchanged. Record `runDigest`, the file `sha256`,
   `t_s` and `expiresAt`. R0 runs no journal `verify` or `install` (§3.2) and no budget scope (§3.3): it
   has no session and no allocation.
2. **Compose and generate**: generator runbook §1 (the composer), then §2's `generate` on the per-run
   branch **without** `--journal-ca` and `--operator-out`. R0 needs no control, trust or pin ConfigMap,
   because its agent half never runs. The summary shows `"oneTimeStructure": "added"`. Paste it into
   the PR body.
3. **The session-key Secret**: generator runbook §3's first two commands (create, then compare the two
   hashes), before the merge. That runbook says "`kubectl --context admin@ai` throughout", but its
   commands omit the flag: add `--context admin@ai` to each `kubectl` that talks to the cluster (the
   `--dry-run=client` and `--local` stages do not).
4. **Open the PR** right after signing, labelled `no-automerge` (campaign §2, S-A). Its title names
   R0 as a drill run that is never granted.
5. **Merge gate**: generator runbook §4a, unchanged. `check --base origin/main --run-digest <R0
   runDigest>` exits 0 with `"firstRun": true`, `"oneTimeStructure": "added"` and
   `"hubReadbackDeadlinePassed": false`: the run files, the agent values and the agent's one-time
   structure equal the generator's output on main. Review every `reviewByHand` path: the inclusion line
   and the activation-gate edit. CI is green.
6. **Merge**, after the `spec.suspend` read of §6.1 shows `true` once more. Flux applies the hub half.
   Optionally, to start it at once (NEW):
   `flux --context admin@ai reconcile source git flux-system -n flux-system && flux --context admin@ai reconcile kustomization apps -n flux-system`.
   Flux does not apply the agent half: `relay-researcher-agent` is suspended.
7. **H7 readback**, H7's list (generator runbook §5, campaign §3.4 step 4), with these forms (NEW):

   ```sh
   cap relay-rollout-0 kubectl --context admin@ai -n relay rollout status deploy/relay --timeout=10m
   cap hub-h7 hpod        # Running; every init exit 0, candidate-session-key included; R0's document.sha256; relay-worker-candidate-<d16>
   cap relay-log-h7 hlog  # no worker_candidate_unavailable, no worker_budget_authority_unavailable
   cap relay-metrics-h7 rcheck   # authority_started 1, authority_start_failed 0, observer_started 1
   cap mounted-relay-h7 wsha_relay                                    # B
   cap alerts-h7 ralerts  # RelayWorkerBudgetStartupFailed not firing
   ```

8. **Relay's positive control on B, before the drills.** The gate is open (production), and B is
   mounted: `since=$(date -u +%s.%N); relay_check 1 "$since"` returns 0. Then
   `cap control-allocator-h7 q relay_budget_allocator "SELECT worker_budget.current_epoch('<B instanceId>', '<B recoveryEpoch>', '<B witnessDigest>')"`
   (NEW statement, step 4's `q`) returns `<B recoveryEpoch>`.

**Stop.** A step fails, or step 8 is not done by `t_s + 60 min`: §9, "Abort". **Never grant or
create**, now or at any later point of R0.

## 7. Relay's drill building blocks

These follow the checklist's W, O, R and F (step 5) with the hub in place of the Router. Every capture
goes through `cap`. Statements run through step 4's `q`; the statements as `relay_budget_allocator`
are NEW.

- **W_R, install a witness state in Relay; authority stays withdrawn.**
  1. Check the clock: it is no later than `t_s + 85 min` for a drill's install, and no later than
     `t_s + 105 min` for any roll. Otherwise §9.
  2. Withdraw: `lwrite withdraw`, then `plugin false` (`HTTP 200`, `disabled`). Relay has no worker
     allocation to stop: `hub_counts` is unchanged.
  3. `sleep 20`.
  4. Write the drill's state to OpenBao, as each drill says.
  5. `force_sync`, then `wait_hashes <expected sha256>`: it must return 0.
  6. `roll_relay`.
  7. `cap hub-after-roll hpod` (Running, every init exit 0, R0's pin unchanged);
     `cap relay-log-after-roll hlog` (no `worker_candidate_unavailable`, no
     `worker_budget_authority_unavailable`); `cap mounted-relay wsha_relay` shows the expected sha256;
     `cap mounted-router wsha_router` still shows B.
- **O_R, re-open, observe Relay, withdraw.** The drill protocol, steps 2-5.
  1. `lwrite open-B` (COMMITTED), then `plugin true`. The Router still mounts B, so this is `HTTP 200`
     `active` in every drill, B and C included (unlike checklist B2).
  2. **Prove the gate open**, immediately before observing:
     - `cap plugins plugins`;
     - `cap control q relay_budget_router "SELECT worker_budget.current_epoch('<B instanceId>', '<B recoveryEpoch>', '<B witnessDigest>')"`
       (the protocol's control, checklist O3) returns `<B recoveryEpoch>`;
     - `cap control-allocator q relay_budget_allocator "<the same statement>"` returns `<B recoveryEpoch>`.
       This is Relay's own login and `clients` row. It rules out the other cause of a 42501: a login
       without a `clients` mapping (Relay `rel:deploy/worker-budget-ledger/001-authority.sql:68-75`).
     - `since=$(date -u +%s.%N)`.
  3. **Observe Relay.** `relay_check 0 "$since"` returns 0: two successive observer checks after the
     gate was proven open, both failing, with the authority and the observer started. For drill A,
     also `cap refusal-allocator q relay_budget_allocator "SELECT worker_budget.current_epoch('<A instanceId>', '<A recoveryEpoch>', '<A witnessDigest>')"`
     fails with `42501: Worker budget authority unavailable`: the statement the observer runs with
     Relay's mounted file.
  4. **The in-window Router control.** `cap router-log rlog` shows `Worker budget authority available`:
     the Router, on B, is served by the same ledger in the same window.
  5. `cap control-again …` and `cap control-allocator-again …`: both still return `<B recoveryEpoch>`.
     `cap alerts ralerts`.
  6. Withdraw: `lwrite withdraw`, then `plugin false`.
- **R_R, restore B in Relay and prove it.** "Restoring B", after a drill whose readback passed.
  1. W_R with step 4 = `bao kv put -mount=af relay-budget/witness witness=- < "$ev/witness-B.json"` and
     B's sha256 expected.
  2. `lwrite open-B`, `plugin true` (`HTTP 200`, `active`), then `since=$(date -u +%s.%N)`.
  3. `relay_check 1 "$since"` returns 0: Relay's positive control on B after the drill.
     `cap router-log rlog` shows `available`, and `cap control-allocator …` returns `<B recoveryEpoch>`.
  4. Withdraw: `lwrite withdraw`, then `plugin false`.
- **F_R, the failure path. It never re-opens.**
  1. Withdraw: `lwrite withdraw`, then `plugin false`.
  2. `cap ledger-state ledger_state` and `cap hub-counts hub_counts`.
  3. If the authority row is not B's, a ledger history count is not 0, or a hub count differs from
     §6.1's, stop and escalate to the owner. Restore nothing (`relay-worker-budget-authority.md:615-622`).
  4. Otherwise write B to OpenBao, `force_sync` and `wait_hashes <B sha256>`. Before `t_s + 105 min`,
     also `roll_relay` and `cap mounted-relay wsha_relay` (B). If `wsha_router` is not B, `roll_router`
     as in checklist step 5's R.
  5. Stop. Go to §10. Re-opening needs the owner's review of the failure.

## 8. The drills

**Each drill's history comparison.** Before W_R, capture `cap ledger-before-<X> ledger_state` and
`cap hub-before-<X> hub_counts`, with `<X>` the drill's letter. After its R_R, capture the same two as
`ledger-after-<X>` and `hub-after-<X>`. The after captures must show the same authority IDs (B's), every
ledger history count 0, and the same hub counts as the before captures; only `ready` may differ, since
R_R ends withdrawn. Any other difference is F_R step 3: stop and escalate.

**Drill A (Relay), stale witness.**

- **A1.** W_R, with step 4 = a rollback to a live OpenBao version that holds A. After checklist step 5
  that is version 1 or 3, but KV keeps a bounded number of versions, so find it first (NEW):

  ```sh
  for v in $(bao kv metadata get -format=json -mount=af relay-budget/witness \
             | jq -r '.data.versions | to_entries[] | select(.value.deletion_time == "" and (.value.destroyed | not)) | .key' | sort -n); do
    printf '%s %s\n' "$v" "$(bao kv get -mount=af -version="$v" -field=witness relay-budget/witness | sha256sum | cut -d' ' -f1)"
  done
  ```

  Then `bao kv rollback -mount=af -version=<the highest version listed with A's sha256> relay-budget/witness`,
  the budget runbook's drill A form. If no listed version has A's hash, write A from the offline record
  instead: `printf '%s' '<operator fills: A's exact witness JSON, from the offline record>' > "$ev/witness-A.json"`
  (NEW; A is public identity evidence), check `sha256sum "$ev/witness-A.json"` against
  `8e5a4104…527b`, then `bao kv put -mount=af relay-budget/witness witness=- < "$ev/witness-A.json"`.
  Either way A's sha256 `8e5a41044815b67975b794d9d82dc3535bd900fbd9e74d1f4c0c05611c13527b` is expected,
  and nothing goes into the offline record.
- **A2.** O_R, with the A refusal in step 3. Pass:
  - Relay mounts A (`wsha_relay`), the gate is proven open, and the Router on B is `available`;
  - two fresh observer checks report `check_succeeded` 0, with `authority_started` and
    `observer_started` 1;
  - `current_epoch(<A ids>)` as `relay_budget_allocator` fails with 42501, while the same login's
    control on B's IDs succeeds before and after.
- **A3.** R_R.

**Drill B (Relay), missing witness.**

- **B1.** W_R, with step 4 = `printf '' | bao kv put -mount=af relay-budget/witness witness=-` (checklist
  B1). The expected sha256 is `e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855`. The hub
  starts with the empty file (F3).
- **B2.** O_R. There are no mounted IDs to query. Pass: the gate is proven open, the Router on B is
  `available`, and two fresh observer checks report `check_succeeded` 0 with `authority_started` and
  `observer_started` 1. The empty file fails the witness parse before any query (F3).
- **B3. Soft delete, Relay's half.** The gate is withdrawn (O_R step 6). The form is checklist B3's,
  with the hub Pod in place of the Router's:
  1. `cap relay-pod-before hpod`.
  2. `bao kv delete -mount=af relay-budget/witness`, then `force_sync`.
  3. The bounded waits and hashes of checklist B3:
     `cap es-relay kubectl --context admin@ai -n relay wait --for=condition=Ready=false externalsecret/relay-worker-budget --timeout=3m`,
     the same for `llm-router/llm-router-worker-budget`, `cap secret-hash-relay wsha_secret relay relay-worker-budget`
     and `cap secret-hash-router wsha_secret llm-router llm-router-worker-budget`.
  4. `cap relay-pod-after hpod` and `cap mounted-relay-after wsha_relay`.

  Pass: both ExternalSecrets go not Ready; both Secrets remain with the empty witness's hash (ESO
  Retain); the hub Pod's UID and its mounted witness are unchanged. Nothing changes until a deliberate
  roll. If a wait times out, read the ExternalSecret's status and events before treating it as a failed
  readback.
- **B4.** R_R. A put after a soft delete writes a new live version, and both ExternalSecrets are Ready
  again.

**Drill C (Relay), malformed witness.**

- **C1.** W_R, with step 4 = `printf '%s' '{"version":1}' | bao kv put -mount=af relay-budget/witness witness=-`
  (checklist C1). The expected sha256 is that of `printf '%s' '{"version":1}' | sha256sum`.
- **C2.** O_R. Pass: as B2. The file is JSON but not a `BudgetRecoveryWitness`, so the parse fails
  (F3).
- **C3.** R_R.

**Production re-open (the owner confirms it after C3's readback).** Commands as checklist steps 7 and 8:
`lwrite open-B`, then `plugin true`. Readback:

- `cap plugins-end plugins` (`active`), and `cap router-log-end rlog` (`available`);
- `since=$(date -u +%s.%N); relay_check 1 "$since"` returns 0;
- `cap final ledger_state`: B's IDs, `ready = t`, every history count 0;
- `cap hub-counts-end hub_counts`: equal to §6.1's, R0's `workerId` absent;
- all five witness hashes are B's.

## 9. Abort and expiry

**Abort before the merge** (a pre-check fails, CI is red, `check --base` fails, or `t_s + 60 min` is
near). Close the PR unmerged and delete the session-key Secret
(`kubectl --context admin@ai -n relay delete secret relay-worker-candidate-key-<d16>`, NEW). Resume the
agent (§10 step 4), and remove the client Pod and the port-forward (§10 step 5). Nothing on the hub changed, so there is nothing to reset. A new R0 is a new signed
run.

**Abort after the merge** (the H7 readback fails, or is not done by `t_s + 60 min`). Run no drill. Never
grant or create. Go to §10.

**A drill readback fails.** F_R, then §10.

**A hub roll does not finish** (`rollout status` times out after 10 minutes). The gate is already
withdrawn and stays so. Read the Pod's status and events before anything else: the hub is down. Stop
the drills, escalate to the owner, and continue with F_R step 2 once the hub is back.

**Expiry (F5).**

- The hub that started before `expiresAt` keeps its authority and observer after it. An observation
  already under way stays valid.
- A hub roll that starts after `t_s + 105 min` is not made. If the roll of a drill's restore would come
  later, write B in OpenBao and the Secrets (`force_sync`, `wait_hashes`), keep the gate withdrawn, and
  let the reset's rollout mount B in Relay (§10). Once `wsha_relay` shows B after that rollout, the owner
  runs §8's production re-open without its `relay_check` line: a hub without candidate files has no
  observer. Relay's positive control on B then moves to R1's H7 readback (§12).
- An unplanned hub restart at or after `expiresAt`, or a drill roll that ends after it, gives a hub
  with `start_failed` 1 and `observer_started` 0. Nothing it reports is a drill observation. Restore B
  as above and go to §10.
- An unplanned Router restart mounts whatever its Secret holds. If `wsha_router` is not B at any
  capture, that window's Router control is void: restore B with `roll_router` (checklist step 5's R)
  before the next drill.

**Never** rewrite the authority row to match a witness (`relay-worker-budget-authority.md:398-399`).

## 10. Reset, then cleanup

R0's per-run PR is reverted as a whole: the generator's `reset` for `run/` and the agent files (R0's
agent values and the one-time structure), the inclusion line and the activation-gate group. R1's per-run
PR is then H7's inclusion PR again, and its `generate` writes the one-time structure again, as the
generator runbook describes. All of this section is NEW, except where a line says otherwise.

1. **The reset PR.** On a branch from `origin/main`:

   ```sh
   git revert --no-edit <R0's squash-merge commit on main>
   python3 scripts/relay-candidate-run.py reset   # placeholder run/; "oneTimeStructure": "absent" (reverted)
   git status --porcelain                         # empty: the revert already restored those bytes
   python3 scripts/relay-candidate-run.py check   # exit 0, "state": "placeholder", "oneTimeStructure": "absent"
   git diff <R0's squash-merge commit>^ HEAD -- kubernetes/components/relay-worker-candidate \
     kubernetes/apps/apps/relay/kustomization.yaml scripts/tests/test_relay_v060_activation_gate.py \
     kubernetes/apps/relay-researcher/agent        # empty, unless another merged PR touched these paths
   ```

   A conflict, or a non-empty diff caused by a later change, is reviewed by hand. Run the repository's
   tests, and open the PR. It is merged once CI is green, the same day as R0.
2. **Its rollout.** Flux rolls the hub without the candidate files. Readback:
   - `cap hub-reset hpod`: one Running Pod with no `candidateSha256`;
   - `cap relay-log-reset hlog`: no `worker_candidate_unavailable` (no candidate is configured);
   - `cap relay-metrics-reset rcheck`: `authority_start_failed` 1 and `observer_started` 0, H6's state;
     `RelayWorkerBudgetStartupFailed` fires again until R1's H7 readback;
   - `cap mounted-relay-reset wsha_relay`: B.
3. **The session-key Secret.** Once that rollout no longer mounts it (generator runbook, "After the
   campaign"): `kubectl --context admin@ai -n relay delete secret relay-worker-candidate-key-<d16>`.
4. **The agent.** Campaign §3.10 step 8, with the context named (NEW form):
   `flux --context admin@ai resume kustomization relay-researcher-agent -n flux-system`. Then `cap agent-after …` with
   §6.1's command: the same generation as `agent-before`, and Ready. The agent never ran R0's values.
5. **The client Pod.** `kubectl --context admin@ai -n relay-budget-authority delete pod relay-budget-operator-client`
   (checklist step 5). Stop the port-forward: `kill "$r0_prom_pid"`.
6. **End readback.** `cap ledger-end ledger_state`, `cap plugins-final plugins`,
   `cap hub-counts-final hub_counts` (equal to §6.1's) and the five witness hashes (B). The ledger row
   has B's IDs and every history count 0. The gate is as R0 left it:
   - after §8's production re-open, or an abort that ran no drill: `ready = t` and the plugin `active`;
   - after F_R, or a restore left to the reset's rollout (§9): `ready = f` and the plugin `disabled`,
     until the owner has reviewed the failure and re-opened with §8's commands. Any re-open after the
     reset skips §8's `relay_check` line, because a hub without candidate files has no observer;
     Relay's positive control on B is then R1's (§12).

   R0 leaves no grant, worker, environment, allocation, budget scope, journal row, Router key or session
   key.

## 11. Evidence

Post on ailab #1753, as the checklist's step 5 does, one comment per drill, titled "Witness drill A
(Relay)", "Witness drill B (Relay)" and "Witness drill C (Relay)". Build each from that drill's files in
`$ev/cap`:

- the gate captures of O_R step 2, from the same window as the observation;
- the observer captures (`relay-check`), with their check timestamps;
- for A, the 42501 refusal and the controls;
- the hub's log lines kept before each roll, the hub Pod captures, and the mounted hashes in both
  consumers;
- the SQL outputs and the before and after diffs of `ledger_state` and `hub_counts`.

Post one more comment, "Drill run R0": the run digest, `t_s` and `expiresAt`, the per-run and reset PR
numbers and merge commits, the stopwatch log (each step and roll against §5), the H7 and reset
readbacks, the owner's re-open, and any drill not run. Never post DSNs, passwords, tokens, the
session-permit key or the collector token (`relay-worker-budget-authority.md:467-481`).

The owner, or the reviewer of checklist step 6, accepts the three drill comments on #1753 before R1 is
signed.

## 12. What R1 re-checks

R1 is signed fresh after §10's readback and the owner's acceptance of R0. R1's per-run PR is H7's
inclusion PR again. Before signing R1, set up what these checks need, as R0's §6.1 does: the
checklist's step 0, 4 and 5 blocks, R0's helpers, the client Pod and the Prometheus port-forward. Remove
the client Pod and the port-forward once R1's grant decision is made. In R1's H7 readback, before the grant, run R0's §6.2 steps 7 and 8 with R1's pin: the
H7 list, then Relay's positive control on B (`relay_check 1`, and `current_epoch(<B ids>)` as
`relay_budget_allocator`). This is R1's pre-grant control that the budget authority Relay just started
accepts the current witness. If it fails, never grant: abort R1 before the grant. Per O1, it then does
not count toward the eight.

The pool-readiness listing reports `runtime-qualification-unavailable` in R1 as well (F1). It is
neither a gate nor a failure until the qualification registry exists (step 11).

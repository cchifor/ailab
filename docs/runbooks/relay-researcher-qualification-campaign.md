# Relay researcher qualification campaign: the per-run runbook (plan, inactive)

Status: **plan only. Nothing here is applied, and merging changes no cluster object.** It is item E5
of the campaign plan rev 2 ([relay#277 comment 124936](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-124936)):
the nineteen researcher scenarios on the owner-approved signed candidate runs (U11). It sits after step
H7 of [relay-researcher-hub-upgrade.md](relay-researcher-hub-upgrade.md) and steps Q1-Q6 of
[relay-researcher-installation.md](relay-researcher-installation.md). Every write is an operator action
with the owner's confirmation for that exact run. Anything marked **UNVERIFIED** was not confirmed in
code; §8 lists them all.

Code references:

| Key | Source | Used for |
| --- | --- | --- |
| `rel:` | Relay v0.10.3 `c0fe50f0` | hub, agent, runtime, operator CLIs |
| `main:` | Relay `origin/main` `5b1c0c1ed` (its `scripts/qualification` is unchanged since `ac3fe1141`) | drivers and converters |
| `519:` | Relay PR #519 head `b2a7f5944` | the defaultCommunication observer |
| `h4:` | ailab #1783 (held), `relay-researcher-hub-upgrade.md` at `09bc7c7b` | File B before H7 |

Pin one driver checkout for the whole campaign and its conversion (`main:scripts/qualification/README.md`:
the converters recompute driver digests from the checkout they run in), plus the #519 checkout.

## 1. Campaign overview

### Run map (rev 2)

| Run | Rows and gates | Ends by | Lease L | Prompt |
| --- | --- | --- | --- | --- |
| R1 | first session (nativeRuntime); #1, #2, #14; window #3-#7; defaultCommunication attempt 1; #8 last | guard exit 72 at T; grant `expiresAt` on a whole second | 90 s | P1, marker `relay-comm-<24 hex>` |
| R2 | #12 replay (bounded disruption after the replay turn), then #11 revocation last | revocation | 120 s | tool-free one-line first prompt |
| R5 | defaultCommunication attempt 2 before the stop; #13 cleanup; #15 matrix (CREATE only) and #16 egress on the empty slot | normal stop | 90 s | P5, new marker |
| R3 (+R4) | #17 storage, then one lease-expiry drill with `routerTraffic: required` giving #10 and #18 (needs E3) | hub cut, exit 71 | 60 s | multi-step, local tools only |
| R4 | only if E3 is not ready: #17 + #18 | hub cut | 60 s | as R3 |
| R7 | #9 delayed start (needs E1), immediately before R6 | the launch never starts | 90 s | never sent |
| R6 | #19 recovery fence, last | installation-wide halt | 90 s | never sent |

**Order:** R1 → R2 → R5 → R3 (+R4) → R4 if needed → R7 → reserve retries → R6. Hold one reserve
for an R6 retry. **Count:** 7 runs + 1 reserve, or 6 + 2 once E3 is validated (O1).

### Preconditions before R1

Each is read back; a missing one stops the campaign before any signing.

| Precondition | Where | State on 2026-11-16 |
| --- | --- | --- |
| H6: Router connection, control plane, monitoring | hub runbook H6 | not applied |
| Q1-Q6: internal TLS, qualification egress `f6d3187c…282f`, Q5 policy UID, Q6 drill config | installation runbook | not applied |
| Candidate journal on `10.96.0.42`, authority row `ready` | [relay-worker-candidate-journal.md](relay-worker-candidate-journal.md) | not applied |
| File B: ledger step 7 `qualifications` row; its `tokenPolicyDigest` goes into every run's `router.tokenPolicyDigests` | h4: "H7", "Enablement order at a glance" | waits for U1/O3 |
| H7 candidate component (hub `RELAY_WORKER_CANDIDATE_FILE`, `_SHA256`, `_SESSION_KEY_FILE`) | hub runbook H7 | **not staged**; its design decides §2's staging |
| Agent one-time candidate values: candidate volume, `policy.bootstrapSecrets: true`, journal egress | installation runbook, "Beyond enrollment" item 3 | not staged |
| PR #519 merged or pinned (its v0.10.3 recipe fetches `main`) | `519:docs/qualification/default-communication-observation.md` | open |
| E1 delayed start, E2 window + storage converters, E3 lease → two rows (optional), E4 replay disruption | pending Relay PRs | **pending** |
| `submittedPod` capture, `subject.json` builder, `commandSha256` derivation, session extraction | §8, items 1-4 | **missing; found while writing this runbook** |
| E6 rehearsal passed (§4) | this runbook | not run |
| Dedicated researcher channel, admin browser state, operator kube identity (VAP write, `pods/exec`, NetworkPolicy CAS, SA impersonation incl. a foreign-slot SA, `pods/log` in `relay-internal-tls`) | plan §8 | not checked |
| Probe images (egress; storage with `dd` and `sleep`) and reviewed allow/deny targets | plan R5, R4 | not built |
| O1-O5 answered | §7 | open |

**Freeze from R1 to the canary:** hub, Router, agent and runtime images and database identities;
namespaces and their UIDs; policy and egress digests; CA bundles; the agent's complete
`PodRuntimeBinding` (including the `candidateJournal` alias); the driver checkout and the #519
checkout. A change voids every row already measured (`rel:packages/worker-runtime/measurement-subject.ts:73-118`).

## 2. What needs a reviewed git PR, and what is operator-local

| Step | Tool | Writes | Git PR? |
| --- | --- | --- | --- |
| Sign the run | operator signer (§3.1); **no Relay CLI signs** (`rel:deploy/worker-candidate-journal/README.md`: "does not implement an operator signing ceremony") | none | no |
| Journal install, inspect, withdraw | `rel:scripts/candidate-worker-authority.ts` | journal DB | no |
| Journal service re-render | `kubernetes/optional/relay-candidate-journal/render.py service` + `kubectl` | `relay-candidate-journal` objects (optional bundle, in no Flux inventory) | no; operator-reviewed render |
| Budget scope | `rel:scripts/candidate-budget-scopes.ts` | ledger DB | no |
| **Hub candidate files** | H7 component | relay Deployment env, ConfigMap, Secret | **yes, every run**: `_SHA256` and the document pin bytes that exist only after signing |
| **Agent candidate values** | `kubernetes/apps/relay-researcher/agent/config.json` (`candidateRuntime.trustSha256`, `bindingSha256`, ConfigMap names) | agent ConfigMap and restart | **yes, every run**: the trust document contains the signed run |
| Candidate ConfigMaps and holder Secret | `kubectl` (`rel:deploy/infrastructure-agent/values.yaml`: "All inputs are operator-installed") | control namespace, slot | no |
| Grant, create, stop, remove | hub API | hub DB | no |
| Per-launch admission bundle | `rel:scripts/prepare-candidate-policy.ts` + `kubectl`, in place | VAPs, gate, quota | no; in-place ceremony under Flux suspension |
| Drivers, conversion | `main:scripts/qualification/*` | per driver | no |

**Plainly: every run needs one ailab PR inside its 2 h window**, because the hub's
`RELAY_WORKER_CANDIDATE_SHA256` and the agent's `candidateRuntime.trustSha256` pin the signed run,
which cannot exist before `issuedAt` (§3.1). Proposed staging, for the operator and owner (§7, S1):

- **S-A, one mechanical PR per run (default).** H7's PR also adds a generator that writes, from the
  signed run and its public files, exactly the run-bound values: the hub document ConfigMap and its
  `_SHA256`, the session-key Secret name, and the agent's `candidateRuntime` values. Everything else
  is reviewed once in H7. Prepare the branch before signing; open it with label `no-automerge` right
  after §3.4; the operator merges once CI passes and the diff equals the generator's output for the
  recorded run digest. **Deadline: merged and read back by `issuedAt` + 60 min, or abort before the
  grant** (no tokens spent).
- **S-B, in place, then commit.** The installation runbook's rule ("Flux and the in-place ceremonies":
  suspend, apply, confirm, commit the final objects, resume) applied to `relay-researcher-agent` and to
  `apps` (the hub's Kustomization, as in hub step H3). It removes review latency from the window, but
  leaves every app unreconciled for up to 2 h and puts the run-bound bytes in git only afterwards.

## 3. The per-run ceremony

`kubectl --context admin@ai` throughout. Relay CLIs run from the pinned Relay checkout on a trusted
Linux operator host (procfs required), as `node --import tsx scripts/...`. Hub SQL reads run as
`postgres` in `relay-postgres-0`, database `relay`, tenant `00000000-0000-4000-8000-000000000001`.
`t_s` is `issuedAt`, `t0` the create, `T` the absolute expiry. A failed readback stops the run.

### 3.0 Before signing (no clock running)

1. **Previous run released.** `GET /api/workers/<previous workerId>` shows `removed` with
   `outstandingEnvironments` 0, the journal slot is settled (§3.10), the slot holds no Pod, Flux
   `relay-researcher-installation` is resumed and the gate and 0-Pod quota are git's. Otherwise the
   create fails: admission needs a free execution slot (`rel:server/environments/kubernetes-admission.ts`,
   "No free execution slot").
2. **Fresh identities.** A new `workerId` every run: the budget installer refuses any worker that
   already has an allocation (`rel:server/workers/inference/budget-scope-install.ts:81`). New
   `environmentId` and `sessionId` (lowercase UUIDv4), a grant-approval `operationId` and a create
   `operationId`. The signed `grantId` and launch `operationId` are derived by the hub
   (`rel:server/workers/service.ts:765-770,1538-1542`); compute them from the pinned checkout
   (verified offline against `c0fe50f0`'s `derivedUuid`):

   ```sh
   cat > ids.mts <<'EOF'
   import { derivedUuid } from "./server/workers/grants/leases.ts";
   const [tenant, approverUserId, approveOp, workerId, createOp] = process.argv.slice(2);
   console.log(JSON.stringify({ grantId: derivedUuid("grant", tenant, approverUserId, approveOp),
     operationId: derivedUuid("worker-environment", workerId, createOp) }));
   EOF
   node --import tsx ids.mts <tenant> <approver user UUID> <approve op> <workerId> <create op>
   ```

3. **Prompt and hashes.** Exact bytes, no trailing newline: `printf '%s' '<text>' > P.txt; sha256sum P.txt`
   gives `promptSha256` (the holder hashes the UTF-8 text, `rel:holder/src/worker_candidate.rs:434-441`).
   `commandSha256` is SHA-256 of canonical `{executable, argv, cwd}` of the final native command, which
   contains `sessionId` (`rel:holder/src/worker_candidate.rs:527-545`, `holder/src/release.rs:105-134`).
   **UNVERIFIED: no operator tool computes it** (§8, item 3). A wrong hash refuses native start.
4. **Keys.** A fresh session-permit Ed25519 key per run (run-scoped:
   `rel:docs/proposals/worker-candidate-qualification.md`, "Signing keys"). One holder journal key for
   the whole campaign (see §8, item 7). The issuer, agent (= installation), holder, journal-reply and
   permit keys must all differ (`rel:packages/worker-runtime/candidate-signatures.ts:174-181`).
5. **Unsigned run.** From the previous run's unsigned JSON, change only `runId`, the worker/launch IDs,
   `promptSha256`, `commandSha256`, `leaseSeconds`, `sessionPermitPublicKey` and `owner`; a diff in any
   other field breaks the freeze. Common values: one worker, `maxActiveWorkers: 1`,
   `maxGenerations: 1`, `maxLifetimeSeconds: 900`, `providerTokenCap: 1000000`,
   `providerRequestCap: 8`, `allowedScenarios` = all 19, `egressPolicySha256` = `f6d3187c…282f`,
   `router.tokenPolicyDigests` = [File B digest].
6. **Configs** for every CLI and driver of the run, with only the run SHA-256 left to fill; the per-run
   PR branch (S-A); clock checks (§5.1); an operator ready on the session page (§5.3); a stopwatch log.

### 3.1 Sign (`t_s`)

`issuedAt = notBefore = now`, `expiresAt = now + 7,200,000`, `cleanupDeadline = expiresAt + 86,400,000`.
Never a future `notBefore`: the schema allows it (`rel:packages/contracts/worker-candidate.ts:127-141`),
but the hub, journal and budget-scope installers all refuse `now < notBefore`
(`rel:packages/worker-runtime/candidate-signatures.ts:199-201`), so it only shrinks the window. The
signed bytes are `relay.candidate-run/v1\n` + canonical JSON of the envelope without `signature`
(`rel:candidate-signatures.ts:122-130`). This signer was verified offline against `c0fe50f0`'s
`verifyCandidateRun` with throwaway keys; keep the issuer key off disk:

```sh
cat > sign-candidate-run.mts <<'EOF'
import { readFileSync } from "node:fs";
import { createPrivateKey, sign } from "node:crypto";
import { candidateTranscript, verifyCandidateRun } from "./packages/worker-runtime/candidate-signatures.ts";
const [unsignedPath, issuerPublic] = process.argv.slice(2);
const doc = JSON.parse(readFileSync(unsignedPath, "utf8"));
const now = Date.now();
Object.assign(doc.payload, { issuedAt: now, notBefore: now, expiresAt: now + 7_200_000,
  cleanupDeadline: now + 7_200_000 + 86_400_000 });
delete doc.signature;
doc.signature = sign(null, candidateTranscript(doc), createPrivateKey(readFileSync(0))).toString("base64url");
const raw = JSON.stringify(doc);
verifyCandidateRun(raw, new Map([[doc.issuerKeyId, issuerPublic]]));
process.stdout.write(raw);
EOF
<issuer PEM from the secret manager> | node --import tsx sign-candidate-run.mts unsigned.json <issuer public key> > run.json
sha256sum run.json
node --import tsx scripts/candidate-worker-authority.ts verify /operator/<run>/journal-operator.json <config sha256>
```

`verify` prints `runDigest` and `expiresAt`; record both with `t_s`.

### 3.2 Journal: re-render with exactly this run.json, install, restart

1. New service config (only `run.sha256` changes), then render and review:
   `python3 -B kubernetes/optional/relay-candidate-journal/render.py service /operator/<run>/service-render.json <sha> > /operator/<run>/service-rendered.json`.
   The diff against the previous render may touch only the public ConfigMap
   (`relay-candidate-journal-public-<config sha[:16]>`), its pins and the annotations; the Service stays
   on `10.96.0.42`.
2. `node --import tsx scripts/candidate-worker-authority.ts install /operator/<run>/journal-operator.json <sha>`
   (operator login). It refuses an expired run in the database clock too
   (`rel:deploy/worker-candidate-journal/002-signed-clients.sql:47`).
3. `kubectl apply -f /operator/<run>/service-rendered.json`; the render has `replicas: 0`, so then
   `kubectl -n relay-candidate-journal scale deploy/relay-candidate-journal --replicas=1` and wait for
   Ready. This stops the previous run's journal service, which is why §3.0 step 1 comes first. The
   service verifies the run before listening (`rel:scripts/candidate-journal-service.ts:140`).
4. Readback: Pod Ready, no refusal in its log, installation runbook Q2's
   `pinned relay-candidate-journal/relay-candidate-journal 10.96.0.42`.

### 3.3 Budget scope: a new scope per run

```sh
node --import tsx scripts/candidate-budget-scopes.ts verify  /operator/<run>/budget-scope.json <sha>
node --import tsx scripts/candidate-budget-scopes.ts install /operator/<run>/budget-scope.json <sha>
```

The output names one scope: `tokenLimit` 1,000,000 and `requestLimit` 8 (`floor(cap / workers)`).
Never install over retained debits (`rel:server/workers/inference/budget-scope-install.ts:81`); the
fresh `workerId` guarantees it. There is no withdraw verb: the scope stops admitting at the run's
`expiresAt` (`rel:deploy/worker-budget-ledger/003-scoped-requests.sql:67-68`).

### 3.4 Hub candidate files and restart

1. Create the session-permit Secret in `relay` from the private key, never via git.
2. The run-bound change (S-A PR or S-B): the `CandidateHubDocument`
   (`rel:server/workers/candidate/authority.ts:40-70`: raw run, issuer keys, hub/router/journal pins,
   pool, binding, the exact installed egress policy, profiles), its `RELAY_WORKER_CANDIDATE_SHA256`,
   `RELAY_WORKER_CANDIDATE_SESSION_KEY_FILE`.
3. The merge is a Recreate rollout: `backup-database` (bounded at 1800 s), `backup-artifacts`,
   `migrate`, then `relay` (`kubernetes/apps/apps/relay/relay.yaml`). Relay is down meanwhile.
4. Readback (H7's): budget startup `started`, `RelayWorkerBudgetStartupFailed` clear, no
   `worker_budget_authority_unavailable` and **no `worker_candidate_unavailable`**: a file that does not
   verify (expired, not yet valid, wrong key) only logs that warning and the hub starts without
   candidate authority (`rel:server/main.ts:288-305`). **Never create while it is logged:** an
   unrecognized worker takes ordinary admission, and the lifetime can freeze before the refusal
   (`rel:server/workers/service.ts:1543-1570`). Connectors and relay-bus keys answer again.

### 3.5 Agent candidate values, agent held disabled

1. `flux suspend kustomization relay-researcher-agent -n flux-system`; scale the agent to 0 replicas
   (**UNVERIFIED** as the mechanism; `rel:docs/infrastructure-candidate-policy.md` only says "keep agent
   polling and launch admission disabled").
2. Create the run's immutable control ConfigMap in `relay-infra-control` (`binding.json`, `config.json`
   = the `CandidateAgentTrust` with the signed run, `ca.pem`, `network-policy.json`;
   `rel:deploy/infrastructure-agent/templates/deployment.yaml:67-71`) and the run's slot trust and pin
   ConfigMaps under new names (immutable). The holder Secret is the campaign's one Secret.
3. Land the agent values (`candidateRuntime.configMap`, `bindingSha256`, `trustSha256`,
   `trustConfigMap`, `pinConfigMap`) with the same S-A PR, or in place (S-B).

### 3.6 Grant, then create (`t0`)

With the admin browser session (cookie file mode 0600) and `Origin: https://relay.chifor.me`:

```sh
curl -sS --fail -b "$COOKIES" -H 'Origin: https://relay.chifor.me' -H 'Content-Type: application/json' \
  -d @/operator/<run>/grant.json https://relay.chifor.me/api/workers/grants
curl -sS --fail -b "$COOKIES" -H 'Origin: https://relay.chifor.me' -H 'Content-Type: application/json' \
  -d '{"operationId":"<create op>","expectedRevision":<revision from GET /api/workers/<workerId>>}' \
  https://relay.chifor.me/api/workers/<workerId>/start
```

- `grant.json` is `ApproveWorkerGrant` (`rel:packages/contracts/worker-grants.ts:48-104`) with
  `maxLifetimeSeconds: 900`, `leaseSeconds` = the run's L (60-120), `inferenceTokenBudget` at most
  1,000,000, scratch ephemeral workspace. The hub refuses a grant above the run's lease, lifetime or
  token share (`rel:server/workers/candidate/authority.ts:392-422`). The receipt's `grantId` must equal
  the signed one.
- `expiresAt`: normally `run.expiresAt`. **R1:** a whole second `G`; create no earlier than
  `G − 900 s`, so `T = least(t0 + 900 s, G) = G` (`rel:server/workers/service.ts:1505-1520`); every second
  of delay comes off the lifetime.
- **Latest create:** `t0 + 900 s ≤ run.expiresAt` or selection refuses
  (`rel:server/workers/candidate/authority.ts:274-292`); plan `t0 ≤ t_s + 95 min` (§4).
- Readback: `SELECT original_deadline FROM workers WHERE id='<workerId>';` and the intent's
  `absoluteExpiresAt`. Record `T`; every threshold below derives from it. A create that returned an
  error may already have frozen the lifetime and debited the allocation: treat the run as spent.

### 3.7 Per-launch admission bundle (after `t0`, inside the lifetime)

`flux suspend kustomization relay-researcher-installation -n flux-system`, then
(`rel:docs/infrastructure-candidate-policy.md`):

1. Read the persisted intent:
   `SELECT id, environment_id, generation, kind, state, digest, request FROM agent_environment_operations WHERE id='<signed operationId>';`
2. `node_modules/.bin/tsx scripts/prepare-candidate-policy.ts /operator/<run>/candidate-policy.json <sha> /operator/<run>/candidate-bundle`
   with that stored intent; `report.json.launchIntents[0].requestDigest` must equal `digest`.
3. Compile with the Go verifier (prebuild it and pass `RELAY_K8S_CEL_BINARY` and its `_SHA256`, so no
   build runs inside the lifetime).
4. Apply in `report.json.transition.steps` order
   (`rel:packages/infrastructure-agent/candidate-policy-preparation.ts:392-406`): verify intents and
   digests; verify the existing gate and the 0-Pod quota; `20-authority-staging-quota.json`; provision
   and verify the authority objects; `11-candidate-contract-guards.json`; verify every policy and binding
   on the server; `12-enrollment-gate-replacement.json` in place (never delete the gate); verify the gate
   and the CNI/UID/authority evidence; `21-proposed-execution-quota.json` (one Pod) last.

### 3.8 Agent enable: Pod Running, session issued

Scale the agent to 1. Readbacks: the Pod in `relay-researcher-1` (`-l relay.infrastructure/managed=true`)
Running with both containers and zero restarts; `GET /api/workers/<workerId>` shows
`session-observed`; `SELECT permit, binding FROM worker_candidate_sessions WHERE (permit->>'workerId')='<workerId>';`
has a non-null `binding`. The agent retries the session exchange for only 20 s, then reports
`candidate-session-unsettled` (`rel:packages/infrastructure-agent/executor.ts:1062,1103`): an
unknown slot (§3.10). **create → Pod Running must stay well under 5 min; abort the run past 5 min.**

Driver inputs come from here: `session` and `bindingProvenance` (`{request, reply}`) from that row
(**UNVERIFIED**, §8 item 2); `submittedPod` from an API audit request-body capture of the Pod create
(`main:docs/qualification/worker-runtime-acceptance.md`: "leave acceptance pending" without one;
§8 item 1).

### 3.9 Drivers

From the pinned driver checkout: `node --import tsx scripts/qualification/<driver>.ts <config> <config-sha256> <report> [seconds]`.
Each reserves its report exclusively and never overwrites one. Thresholds come from `T` and the
current lease end `E_L` (read from the guard log), never from `t0` estimates:

| Rule | Source |
| --- | --- |
| Every report must **start** in `[session.notBefore, T)` to convert | `main:scripts/qualification/researcher-runtime-v2-observations.ts:73-91`; `researcher-pool-v2-observations.ts:730-733` |
| Drivers using the acceptance config refuse once the session or run has expired, **including cleanup settlement and #519** | `main:packages/worker-runtime/acceptance.ts:151-178`; `main:scripts/qualification/worker-cleanup-settlement.ts:117` |
| Partition and lease drills: at least 30 s before `T` at start and at the cut; wall/monotonic drift ≤ 1 s | `main:scripts/qualification/researcher-hub-partition-observation.ts:645,825` |
| Lease expiry needs exit 71 before `T`: cut no later than `T − L − 30 s` | `main:scripts/qualification/researcher-lease-expiry-observation.md`; §6 R3 |
| Revocation: last published lease ends ≥ 30 s before `T`; latest send ≈ `T − 30 s − L` | `main:scripts/qualification/researcher-revocation-observation.ts:357-361` |
| Measurement window must finish before `T − L` (lease publisher sampling up to 60 s) | `main:packages/worker-runtime/acceptance.ts:784-800` |
| Expiry capture runs passively to `T + 60 s`; nothing runs after it | `main:packages/worker-runtime/expiry-observation.ts:15,50-53` |
| A drill that removes the Pod's evidence: remove only after its terminal capture | lease and partition drills refuse `pod-absent` |

### 3.10 Teardown, settlement, withdrawal

1. Start the cleanup observer **before** the stop and while the session is valid, if the run measures
   #13: `worker-cleanup-observation.ts <cfg> <sha> <out> 900`.
2. Remove: `POST /api/workers/<workerId>/remove` with
   `{"operationId":"<new>","expectedRevision":<rev>,"workspaceDisposition":"retain"}` (`retain`
   destroys ephemeral environments, `rel:packages/contracts/worker-grants.ts:171-181`).
3. Settlement for #13 must also start before `T`: `worker-cleanup-settlement.ts <cfg> <sha> <out> 600`,
   with the transition report pinned. So #13 needs the removal finished before `T`; it cannot come
   from R1.
4. Journal slot: the agent settles it with UID-bound evidence. Check it:
   `node --import tsx scripts/candidate-worker-authority.ts inspect /operator/<run>/journal-inspect.json <sha> /operator/<run>/slot.json`
   (agent-service login; `slot.json` = `{runDigest, workerId, environmentId, generation, operationId}`).
   Hub: `GET /api/workers/<workerId>` shows `removed`, `outstandingEnvironments` 0.
5. **Unknown slot** (admitted, bound or started, never settled): a timer, a hub restart or a missing
   Pod is not proof of absence (`rel:docs/worker-candidate-hub.md`, "Permit exchange and retry"). Read the
   exact Pod by name and UID, the namespace UID and the guard's termination records; delete a surviving
   Pod only with a `preconditions.uid` DeleteOptions. No operator settle command or UI exists at
   `c0fe50f0` (`candidate-worker-authority.ts` has only verify/install/inspect/withdraw): **stop the
   campaign and escalate**, because the next create needs the slot released (§3.0).
6. Withdraw: `node --import tsx scripts/candidate-worker-authority.ts withdraw /operator/<run>/journal-operator.json <sha>`.
   The budget scope lapses by itself; the hub files are replaced by the next run's §3.4.
7. Slot back to git: confirm every driver's `outstandingRecovery` is empty and no Pod remains, then
   `flux resume kustomization relay-researcher-installation -n flux-system` (it restores the enrollment
   gate and the 0-Pod quota) and `flux resume kustomization relay-researcher-agent -n flux-system` once
   the agent's git values match the live ones.

### 3.11 Before the next run

Convert every report of this run with the pinned checkout and the run's `subject.json`
(`PodMeasurementSubjectV2`, `rel:packages/worker-runtime/measurement-subject.ts:99-178`):

```sh
node --import tsx scripts/qualification/researcher-runtime-v2-observations.ts \
  /abs/acceptance.json <sha> /abs/subject.json <sha> /abs/raw-report.json <sha> /abs/v2-rows.json [/abs/transition.json <sha>]
node --import tsx scripts/qualification/researcher-pool-v2-observations.ts \
  /abs/acceptance.json <sha> /abs/subject.json <sha> /abs/pool-report.json <sha> /abs/v2-row.json [/abs/subresource.json <sha>]
```

Each intended row must come out `observed`. Window and storage reports need E2's converters, the
lease drill's second row E3's. **Do not sign the next run until this run's rows convert**, and package
its evidence (§5.5).

## 4. Timing budget and the E6 rehearsal

The installers need the run valid, so everything up to the create fits in the 2 h; post-`T` waits
(settlement, egress, recovery reconciliation) are bounded by `cleanupDeadline` (+24 h) instead.

| Step (from `t_s`) | Target | Hard limit |
| --- | --- | --- |
| 3.1 sign, verify | 5 min | |
| 3.2 journal render, install, restart | 8 min | |
| 3.3 budget scope | 3 min | |
| 3.4 PR merged, Flux, hub rollout (backup ≤ 1800 s) and readback | 30 min | `t_s + 60 min`, else abort before the grant |
| 3.5 agent objects and values | 5 min, alongside 3.4 | |
| 3.6 grant, create | 2 min | create ≤ `t_s + 95 min` (`t0 + 900 s ≤ expiresAt`, plus 10 min of teardown) |
| 3.7-3.8 bundle, enable, Pod Running, session | ≤ 5 min after `t0` | 5 min, else abort |
| drivers | to `T` (+60 s capture) | `T ≤ t0 + 900 s` |
| 3.10 Pod removed, slot settled | 10 min | before `expiresAt` (UNVERIFIED necessity, §8 item 9) |

Target: create by `t_s + 55 min`, done by about `t_s + 85 min`; 35 min of slack, nearly all for the PR.

**E6 rehearsal checklist.** Time each step; record the measured value next to the target; nothing is
signed on the live hub.

- [ ] Sign, `verify`, budget `verify` and the journal render with throwaway keys (offline).
- [ ] Journal service restart on a disposable namespace or the rehearsal journal: apply, scale, Ready.
- [ ] Per-run PR: open → CI green → merge → Flux apply → agent restart, on a no-op branch.
- [ ] Hub rollout duration, from the last H-step rollouts' init-container timings (`backup-database`
      dominates).
- [ ] `prepare-candidate-policy.ts` + prebuilt verifier + transition steps on Relay's disposable
      Kubernetes 1.35 fixture (`rel:docs/infrastructure-candidate-policy.md`, "Disposable local API-server evidence").
- [ ] Runtime image cached on `relay-worker-1`; Pod start under `relay-gvisor` measured outside the slot.
- [ ] **create → Pod Running well under 5 min** (bundle + enable + scheduling + start), summed.
- [ ] Each driver's real duration (window, storage staging, admission matrix, egress, replay) to fill
      the latest-start formulas in §6.
- [ ] P1/P3 call counts on the provider-qualification harness (paid; O5) and native approval prompts.
- [ ] `commandSha256`, session extraction and `submittedPod` capture proven end to end (§8, 1-3).

## 5. Cross-cutting checks

### 5.1 Clocks (observers allow ≤ 1 s)

Run at §3.0 and again just before §3.6. Budget each clock within 250 ms of NTP.

```sh
chronyc tracking | grep -E 'System time|Leap status'          # operator host: Normal, offset < 0.25 s
for n in 192.168.0.52 192.168.0.47 192.168.0.48 192.168.0.49 192.168.0.41 192.168.0.42 192.168.0.43; do
  talosctl -n "$n" time; done                                 # relay-worker-1, agent nodes, CPs
date -u +%s.%N; kubectl -n relay exec relay-postgres-0 -- psql -U postgres -Atc 'select extract(epoch from clock_timestamp())'; date -u +%s.%N
curl -sI https://relay.chifor.me/ready | grep -i '^date:'      # the revocation driver's hub Date check
```

The DB value must sit between the two operator stamps, within 0.5 s. Use the Talos client that
matches the cluster (v1.14). A larger skew stops every skew-sensitive driver; do not sign.

### 5.2 Flux

Suspend `relay-researcher-installation` from §3.7 until §3.10 step 7, and `relay-researcher-agent`
from §3.5 (installation runbook, "Flux and the in-place ceremonies"; drivers replace the gate, quota
and egress policy in place, and Flux would silently undo them). Never resume over an unreverted live
change. Q4 must never run inside a window.

### 5.3 Native tool approvals

The Codex launcher pins `approval_policy: "on-request"`, `approvals_reviewer: "user"`,
`sandbox_mode: "read-only"` (`rel:packages/codex-launcher/main.ts:230-232`). Keep the operator on the
worker's session page from §3.8 to `T`, ready to answer permission requests; approval latency counts
against every threshold. How often local-tool prompts (P3, P4) ask is **UNVERIFIED** (E6).

### 5.4 Router headroom

Each attempt reserves `I + maxTokens` = 922,000 + 8,192 = **930,192** against the run's 1,000,000; the
next is admitted only while charged ≤ **69,808** (llm-router `inference-budget/index.ts:48`,
`worker-budgets.ts:270`). Requests are never refunded; an interrupted attempt is charged the full
reservation. Codex does not retry (`rel:packages/codex-launcher/main.ts:223-224`). Rules:

- read `GET /api/workers/<id>/environments/<environmentId>/inference-meter` (`consumedTokens`) before
  every prompt (its coverage for candidate workers is **UNVERIFIED**);
- send a prompt only if `consumedTokens` + its planned turn stays under 60,000;
- the call that can be interrupted (lease cut, partition, disruption) is always the run's last.

### 5.5 Evidence per run (U5)

U5 keeps evidence in git as forge attachments, as done for the provider qualification
([relay#277 comment 124090](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-124090)).
Per run, one directory: the signed run and every public config with its SHA-256, the journal render,
the per-launch bundle, driver configs and raw reports, converter outputs, the driver checkout commit,
clock checks and the stopwatch log. Never private keys, DSNs, browser state, kubeconfigs, Router keys or
bootstrap tokens. Build `tar --sort=name --owner=0 --group=0 --numeric-owner --mtime='<run date> 00:00Z' -czf …`,
write `SHA256SUMS`, check for secrets, and attach both to a relay#277 comment naming the run digest.
Keep the originals until the canary.

### 5.6 Stop rules

| Situation | Action |
| --- | --- |
| Any §3.0-§3.5 readback fails, a clock is out, or the PR misses `t_s + 60 min` | abort before the grant; the run is wasted but no tokens are spent (whether it counts toward 8 is O1's) |
| Create returned an error | treat lifetime and allocation as spent; remove and reconcile |
| create → Pod Running > 5 min, or no `session-observed` | stop, remove, reconcile; the run is wasted |
| A driver misses its latest start | skip that row in this run; never stretch a threshold |
| An interrupted call (charged 930,192) | no more prompts; zero-request drivers may continue |
| Unknown journal slot | stop the campaign until reconciled (§3.10) |
| A `failed` row traceable to the hub, agent or runtime image | stop the campaign: a fix changes the subject and voids every row; owner |
| A row fails to convert | fix the evidence path before the next run |
| More than one lost terminating run | new owner approval (O1) |

## 6. Runs

Each table lists the steps after §3.8. Latest starts use `D_x`, the E6-measured duration of step x.
"Spare" rows are taken only when their latest start still holds.

### R1: first session, window, communication 1, absolute expiry

Signed: L 90, lifetime 900, grant `expiresAt` = whole second `G`, P1 with a fresh
`relay-comm-<openssl rand -hex 12>`, names the researcher channel, asks for a broadcast and a reply
with the message ID (`519:docs/qualification/default-communication-observation.md:68-86`). 2-4 requests.
Must be the campaign's first candidate launch.

| Step | Driver / action | Latest start, stop/go |
| --- | --- | --- |
| 1 | RBAC (#14): `researcher-pool-rbac-observation.ts`, read-only, concurrent | at once; 10 min budget |
| 2 | Acceptance A [#1, #2]: `worker-runtime-acceptance.ts preflight <cfg> <sha>`, then `observe <cfg> <sha> <out>` | before the window |
| 3 | Window #3-#7: `operator-measurement-window.ts prepare <cfg> <sha> <plan>`, review its `planDigest`, `observe <cfg> <sha> <planDigest> <out>`; exactly the five probes (`main:packages/worker-runtime/operator-window-policy.ts:171-175`) | observe by `T − L − D_window`; else skip, spares in R3/R5 |
| 4 | #519 `preflight <cfg> <sha>`, `observe <cfg> <sha> <out> <seconds>` started **before** P1; send P1 through session control; nothing else until it exits | P1 by `T − 300 s`; headroom check first |
| 5 | Acceptance B [#8]: `observe` with only `absolute-expiry-active-deadline`; both containers Running | by `T − 60 s`; passive to `T + 60 s`; nothing after it |
| 6 | Remove, journal slot settled (§3.10); no #13 here | after the capture |

Go/no-go: harness exit 72 at `T`. A lease lapse (71) or lease rejection (73) reaching the guard
first is a non-observed outcome (`main:docs/qualification/worker-runtime-acceptance.md:251-259`), and
#8 then needs the reserve.

### R2: replay, then revocation

Signed: L 120, a tool-free one-line first prompt. 2-3 requests (first prompt, replay exactly 1, none
after the revoke). Needs R1's proof of session control and of per-key usage reads.

| Step | Driver / action | Latest start, stop/go |
| --- | --- | --- |
| 1 | Optional spare acceptance [#1, #2] | before step 2 |
| 2 | Send the signed first prompt; wait for the answer | headroom check |
| 3 | Replay #12: `worker-session-replay-observation.ts <cfg> <sha> <out> <seconds>`; config selects only #12; unsigned replay prompt ≤ 8 KiB, the driver appends `relay-replay-<24 hex>`. **PENDING E4:** the bounded hub-rule cut (≥ 30 s, inside the current lease, loss and recovery observed, exact restore, Flux suspended), done after the replay turn | by `T − 150 s − D_replay`; refuses with < 10 s left |
| 4 | Revocation #11: `researcher-revocation-observation.ts <cfg> <sha> <out>` while idle; one irreversible `POST /api/workers/grants/<grantId>/revoke` | send by `T − 150 s` (`T − 30 s − L`); hub `Date` within 1 s |
| 5 | Remove, settle (§3.10) | last |

### R5: communication 2, cleanup, matrix, egress

Signed: L 90, P5 with a new marker. 2-4 requests.

| Step | Driver / action | Latest start, stop/go |
| --- | --- | --- |
| 1 | Spares: RBAC, acceptance [#1, #2] | at once |
| 2 | #519 observe, P5, wait for the observer | P5 by `T − 600 s` |
| 3 | Cleanup observer, then remove (§3.10) | remove by `T − 360 s` |
| 4 | Settlement #13 with the transition report | start by `T − 240 s` |
| 5 | Matrix #15: `researcher-pool-admission-observation.ts <cfg> <sha> <out>`; slot Pod absent; dry runs only; CREATE paths only, UPDATE paths stay `unmeasured` | start by `T − 60 s − D_egress-start` |
| 6 | Egress #16: `researcher-pool-egress-observation.ts <cfg> <sha> <out>`; zero Pods; replaces the gate in place under CAS; 600 s window; never overlapping step 5 | start before `T`; finish before `cleanupDeadline` |

### R3 (+R4): storage, then one lease-expiry drill

Signed: L 60, multi-step prompt with local tools only (for example sequential `sleep 10` steps).
3-5 requests; the cut-off call is charged 930,192 and is the last.

| Step | Driver / action | Latest start, stop/go |
| --- | --- | --- |
| 1 | Pod `native-started`, ≥ 2 lease publications and a renewal | |
| 2 | Storage #17: `researcher-storage-policy-staging.ts <cfg> <sha> <out>`; the researcher Pod is the only Pod; quota `pods` hard = used = 1; raises the quota to 2, adds a no-egress policy, replaces the gate; 600 s budget. **PENDING E2** for its converter | start by `T − D_storage − 240 s`; else skip #17; restore confirmed before step 3 |
| 3 | Send the prompt (headroom check), then the lease drill: `researcher-lease-expiry-observation.ts <cfg> <sha> <out>` with `routerTraffic: required`, egress and endpoints from Q6 | cut by `T − L − 30 s` = `T − 90 s`; exit 71 before `T` (72 is `inconclusive`) |
| 4 | **PENDING E3:** convert the drill to #10 and #18 | |
| 5 | After the terminal capture: restore checked, remove; spare #13 if settlement can start before `T` | |

### R4: storage and hub partition (only without E3)

Signed: as R3. Steps 1-2 as R3, then send the prompt and run
`researcher-hub-partition-observation.ts <cfg> <sha> <out>` with `routerTraffic: required`: at least one
Router request must complete after the cut, so leave headroom under 69,808 for it. Exit 71 or 72 both
count. Cut by `T − 30 s` at the latest (the driver refuses later).

### R7: delayed start (PENDING E1)

Signed: L 90, prompt never sent, 0 requests. **PENDING E1**, against its documented interface: a
pre-session observer bound to the signed run, the admission evidence, the submitted Pod and its UID,
and the guard's `--absolute-expires-at-ms`; its converter emits `sessionDigest` and
`journalProvenanceDigest` as null with `sessionAuthority` under `unmeasured`
(`rel:packages/worker-runtime/measurement-subject.ts:221-225`); a reviewed hold, release and cleanup
procedure rehearsed outside the candidate slot. The launch ends `candidate-session-unsettled`, an
unknown slot to reconcile by hand; R6 cannot be created until it is released (§3.0).

### R6: recovery fence, last

Signed: L 90, prompt never sent, 0 requests.

| Step | Driver / action | Latest start, stop/go |
| --- | --- | --- |
| 0 | Prerequisites: a passing `worker-budget-restore.ts --router-root <llm-router> --work-dir <dir> --report <file>` (Node 24, clean trees); an older Relay snapshot that predates this Pod (INFERRED: §3.4's own pre-migration dump qualifies); a 0600 management token for the disposable hub | before signing |
| 1 | `researcher-recovery-fence-observation.ts <cfg> <sha> <out>`, `denialWaitMs` ≤ 10 min, `reconcileWaitMs` ≤ 30 min | **before** the restored hub is pointed at the installation; before `T` |
| 2 | Freeze external authority; restore the older snapshot as a disposable hub; point it at the installation | |
| 3 | Halt (`fence_stale`), orphan deletion, reconciliation; then explicit operator recovery | bounded by `cleanupDeadline` |

Keep the last reserve until R6 passes.

## 7. Open decisions

Owner decisions from rev 2, **not answered here**:

- **O1:** baseline 7 runs + 1 reserve (6 + 2 once E3 is validated); more than one lost terminating run
  needs a new approval. Also open: does a signed run aborted before the create count toward the 8?
- **O2:** accept what I = 922,000 means per run (930,192 reserved per call, 69,808 headroom, about six
  calls, an interrupted call ends the run's calls), or a smaller I with fresh provider-enforcement
  evidence.
- **O3:** D, the canary's deadline. Proposal: 2026-11-27T18:00Z, if File B lands by 2026-11-20.
- **O4 (optional):** one no-call run with three workers (R5, R7, R6), each below one reservation.
- **O5:** a few paid rehearsal calls for E6.

Raised by this runbook, for the operator and owner:

- **S1:** S-A or S-B for the per-run hub and agent change (§2), decided with H7's component design.
- **S2:** the mechanism that keeps the agent from claiming the create before the bundle (§3.5).

## 8. UNVERIFIED

1. **`submittedPod` capture.** Every acceptance-config driver needs the exact Pod create request body;
   the agent does not export it and the only route is API audit `requestObject`. No audit policy is in
   this repository (Talos default assumed), so request bodies are likely **not** recorded today.
2. **Session and `bindingProvenance` files.** No operator route or documented extraction; this runbook
   reads `worker_candidate_sessions.permit` and `.binding` (both jsonb; verification canonicalizes,
   so key order should not matter).
3. **`commandSha256`.** No operator tool; it must reproduce the holder's final command.
4. **`subject.json`.** Only the library function `createPodMeasurementSubject` builds it; no CLI exists,
   so §3.11 cannot run yet.
5. The agent-disable mechanism (scale to 0), and whether the create needs a live agent.
6. Per-run PR latency and the hub rollout duration (`backup-database` up to 1800 s).
7. Slot trust/pin ConfigMap and holder Secret layout; reusing one holder Secret, since run N's guards
   freeze slot Secret mutations.
8. Between runs: Flux resume restores the enrollment gate and 0-Pod quota; leftover
   `relay-candidate-<installationId>-<n>` policies of earlier runs are harmless.
9. Whether the agent can still settle the journal slot after `run.expiresAt` (hence "before `expiresAt`").
10. Settling an unknown slot: no operator command or UI at `c0fe50f0`.
11. The inference-meter route's coverage for candidate workers.
12. How often local-tool prompts raise native approval requests.
13. The R1 whole-second `T` (supported by `least(original, grant)`; the 71/73 race remains).
14. The agent's journal egress values (`networkPolicy.candidateJournalCidrs`, port 8443).
15. R6's older snapshot taken from §3.4's own pre-migration dump.
16. All durations in §4 and every `D_x`, until E6 measures them.

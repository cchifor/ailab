# Relay researcher qualification campaign: the per-run runbook (plan, inactive)

Status: **plan only. Nothing here is applied, and merging changes no cluster object.** It is item E5
of the campaign plan rev 2 ([relay#277 comment 124936](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-124936)):
the nineteen researcher scenarios on the owner-approved signed candidate runs (plan U11). It sits after step
H7 of [relay-researcher-hub-upgrade.md](relay-researcher-hub-upgrade.md) and steps Q1-Q6 of
[relay-researcher-installation.md](relay-researcher-installation.md). Every write is an operator action
with the owner's confirmation for that exact run. Anything marked **UNVERIFIED** still needs host or
live measurements; §8 records the source verdicts, the remaining checks and the gaps G0-G6.

Code references:

| Key | Source | Used for |
| --- | --- | --- |
| `rel:` | Relay v0.10.3 `c0fe50f0` | agent, runtime, the journal/budget/policy operator CLIs; the hub except its candidate loader |
| `rel4:` | Relay v0.10.4 `e77bd09a` (release PR #526, Gitea release v0.10.4) | the hub image: `rel:` plus the fraction-safe candidate loader (§8 G0); worker images stay v0.10.3 |
| `main:` | Relay `origin/main` `eb9b1f07` (fetched 2026-11-18; E1, the O8 SQL, R7's Secret hold #536 and the composer #533 merged) | the ceremony tools, drivers and converters |
| `ailab:` | ailab `319fc3b2` | the hub rollout/backup and Flux installation manifests |
| `h4:` | `relay-researcher-hub-upgrade.md` at ailab `e5a2f3b9` | File B before H7 |

Pin one driver checkout for the whole campaign and its conversion (`main:scripts/qualification/README.md`:
the converters recompute driver digests from the checkout they run in). PR #519 (defaultCommunication)
is in `main` since `908b0c104`, E1 since `1ea5ecc9`, and R7's bootstrap-Secret check since `eb6033fd`
(Relay #536). The hub-document composer (`scripts/compose-candidate-hub-document.ts`, §3.4) comes with
Relay #533. One checkout at or after `9e09962f` carries all of them. An older checkout cannot convert R7:
the delayed-start converter requires the driver's `bootstrapSecret` facts, which only the #536 driver records.
The ceremony tools, drivers, converters and qualification documents cited below are unchanged between
`9e09962f` and `eb9b1f07`.

## 1. Campaign overview

### Run map (rev 2)

| Run | Rows and gates | Ends by | Lease L | Prompt |
| --- | --- | --- | --- | --- |
| R1 | first session (nativeRuntime); #1, #2, #14; window #3-#7; defaultCommunication attempt 1; #8 last | guard exit 72 at T; grant `expiresAt` on a whole second | 90 s | P1, marker `relay-comm-<24 hex>` |
| R2 | #12 replay (bounded disruption after the replay turn), then #11 revocation last | revocation | 120 s | tool-free one-line first prompt |
| R5 | defaultCommunication attempt 2 before the stop; #13 cleanup; #15 matrix (CREATE only) and #16 egress on the empty slot | normal stop | 90 s | P5, new marker |
| R3 | #17 storage (staging driver), then one lease-expiry drill with `routerTraffic: required` giving #10 and #18 (E3, #527) | hub cut, exit 71 | 60 s | multi-step, local tools only |
| R4 | reserve only: #18 again if R3's partition row is not `observed` | hub cut | 60 s | as R3 |
| R7 | #9 delayed start (E1, Relay #532), immediately before R6 | the launch never starts | 120 s (#536) | never sent |
| R6 | #19 recovery fence, last | installation-wide halt | 90 s | never sent |

**Order:** R1 → R2 → R5 → R3 → reserve retries (R4 if needed) → R7 → O8 reconciliation → R6. Hold one
reserve for an R6 retry. **Count:** 6 runs + 2 reserves (O1): E3 merged as Relay #527, so R3 gives both
#10 and #18. R7 leaves the pool's only slot blocked (§6 R7), so every retry that R7 does not need goes
before it, and any run after R7 (R6, or a retry) needs the reviewed reconciliation first (O8). Drill run
R0 comes before R1: signed, its hub half live, never granted or created, so it does not count
([relay-researcher-r0-relay-drills.md](relay-researcher-r0-relay-drills.md)).

### Preconditions before R1

Each is read back; a missing one stops the campaign before any signing.

| Precondition | Where | In git on 2026-11-16 |
| --- | --- | --- |
| H6: Router connection, control plane, monitoring | hub runbook H6 | not in the live overlay |
| Q1-Q6: internal TLS, qualification egress `f6d3187c…282f`, Q5 policy UID, Q6 drill config | installation runbook | Q3 (#1802) and Q4 (#1803) merged: the terminators and the installation tree are in Flux, and the installation selects the qualification egress; Q5's readback and Q6 are the operator's |
| Candidate journal on `10.96.0.42`, authority row `ready` | [relay-worker-candidate-journal.md](relay-worker-candidate-journal.md) | Flux `relay-candidate-journal` (base at zero replicas, Service on `.42`) and the one-replica flip: held PRs ("Activation under Flux"); the authority row is the operator's ceremony |
| File B: ledger step 7 `qualifications` row; its `tokenPolicyDigest` goes into every run's `router.tokenPolicyDigests` | h4: "H7", "Enablement order at a glance" | not generated; D is fixed with the owner when it is (O3, §7) |
| H7 candidate component (hub `RELAY_WORKER_CANDIDATE_FILE`, `_SHA256`, `_SESSION_KEY_FILE`) and the S-A generator | hub runbook H7; [relay-candidate-run-generator.md](relay-candidate-run-generator.md) | staged, not included: R1's per-run PR includes it |
| Agent one-time candidate values: candidate mounts and volume, `policy.bootstrapSecrets: true`, the candidate RBAC, journal egress | installation runbook, "Beyond enrollment" item 3; the generator's "One-time prerequisites" | staged in `scripts/relay-candidate-run.py`: `generate` writes them with the first run (R0's PR, R1's again after R0's reset), `reset` removes them |
| E2 window + storage converters (#523), E3 lease → two rows (#527), E4 replay disruption (#524), defaultCommunication observer (#519) | Relay `main` | merged |
| E1 delayed start: pre-session observer, converter branch, hold procedure | Relay #532; bootstrap-Secret hold #536 | merged (`1ea5ecc9`, `eb6033fd`); R7 also needs O7's audit policy and O8's reconciliation |
| Hub v0.10.4: the `c0fe50f0` candidate loader refuses every real binding | §8 G0; fixed by #525, released as v0.10.4, pinned by ailab #1789 | **live** since 2026-11-16 10:08Z (`…@sha256:d1482bc1…57a7`, schema 79; ailab #1789 comment 125493) |
| Ceremony tools and driver fixes (G1-G5): signer, `commandSha256`, `subject.json`, `submittedPod`, the matrix's installed-bundle check, fraction-safe parsing | §8; Relay #523, #528, #529, #530 | merged; `submittedPod` by reconstruction needs O7 |
| `deployment.json` and `subject.json` composed and frozen (§3.0 step 7) | this runbook | after the v0.10.4 rollout |
| E6 rehearsal passed (§4) | this runbook | not run |
| Dedicated researcher channel, admin browser state, operator kube identity (VAP write, `pods/exec`, NetworkPolicy CAS, SA impersonation incl. a foreign-slot SA, `pods/log` in `relay-internal-tls`) | plan §8 | operator readback |
| Probe images (egress; storage with `dd` and `sleep`) and reviewed allow/deny targets | plan R5, R4 | not in git |
| Owner decisions O1-O8, S3, I4, N1, N2 | §7 | decided; S2's create path is verified, queued-create survival and timing/readbacks remain in E6 |

**Freeze from R1 to the canary:** hub, Router, agent and runtime images and database identities;
namespaces and their UIDs; policy and egress digests; CA bundles; the agent's complete
`PodRuntimeBinding` (including the `candidateJournal` alias); `deployment.json` and `subject.json`;
the one driver checkout. A change voids every row already measured (`rel:packages/worker-runtime/measurement-subject.ts:73-118`).

## 2. What needs a reviewed git PR, and what is operator-local

| Step | Tool | Writes | Git PR? |
| --- | --- | --- | --- |
| Sign the run | `main:scripts/sign-candidate-run.ts`, offline (§3.1); `rel:` has no signer (`rel:deploy/worker-candidate-journal/README.md`: "does not implement an operator signing ceremony") | none | no |
| Journal install, inspect, withdraw | `rel:scripts/candidate-worker-authority.ts` | journal DB | no |
| Journal service re-render | `kubernetes/optional/relay-candidate-journal/render.py service` + `kubectl` | `relay-candidate-journal` objects: the public ConfigMap, Deployment and client NetworkPolicy, in no Flux inventory (the protocol Service is Flux's; §3.2 applies the render without it) | no; operator-reviewed render |
| Budget scope | `rel:scripts/candidate-budget-scopes.ts` | ledger DB | no |
| **Hub candidate files** | `main:scripts/compose-candidate-hub-document.ts` (the document), then `scripts/relay-candidate-run.py generate` (`run/` of the H7 component `kubernetes/components/relay-worker-candidate`) | relay Deployment env, the run's ConfigMap; the session-key Secret by `kubectl`, never git | **yes, every run**: `_SHA256` and the document pin bytes that exist only after signing |
| **Agent candidate values** | `scripts/relay-candidate-run.py generate`, into the agent tree: runtime `config.json` `candidateRuntime` (`trustSha256`, `trustConfigMap`, `pinConfigMap`; `bindingSha256` stays byte-identical, the binding is frozen) and `deployment.yaml`'s candidate volume (the run's control ConfigMap) | agent ConfigMap and Deployment | **yes, every run**, applied in place first (the agent must stay disabled through the create, §3.5) and committed in the same per-run PR (S-A) or afterwards (S-B); `relay-researcher-agent` stays suspended until §3.10 step 8 |
| Candidate ConfigMaps and holder Secret | `kubectl create -f` of the generator's `--operator-out` ConfigMaps; the holder Secret once (`rel:deploy/infrastructure-agent/values.yaml`: "All inputs are operator-installed") | control namespace, slot | no |
| Grant, create, stop, remove | hub API | hub DB | no |
| Per-launch admission bundle | `rel:scripts/prepare-candidate-policy.ts` + `kubectl`, in place | VAPs, gate, quota | no; in-place ceremony under Flux suspension |
| Drivers, conversion | `main:scripts/qualification/*` | per driver | no |

**Plainly: every run needs one ailab PR, and its hub half must merge inside the run's 2 h window**,
because the hub's `RELAY_WORKER_CANDIDATE_SHA256` pins the signed run, which cannot exist before
`issuedAt` (§3.1). The agent half (`candidateRuntime` values) is applied in place under the
installation runbook's rule ("Flux and the in-place ceremonies": suspend, apply, confirm, commit the
final objects, resume), because the agent must not claim the create before the per-launch bundle is
installed. Staging of the hub half (§7, O6: S-A, with S-B as the fallback and for the canary):

- **S-A, one mechanical PR per run (approved).** The generator `scripts/relay-candidate-run.py`
  ([relay-candidate-run-generator.md](relay-candidate-run-generator.md)) writes, from the signed run
  and the composed hub document, exactly the run-bound values: the hub document ConfigMap and its
  `_SHA256`, the session-key Secret name, and the agent's `candidateRuntime` values and candidate
  volume. Everything else is reviewed once in H7. Prepare the branch before signing; open it with
  label `no-automerge` right after §3.1, so CI and review run during §3.2-§3.3; the operator merges
  once those readbacks and CI pass and the diff equals the generator's output for the recorded run
  digest: `python3 scripts/relay-candidate-run.py check --base origin/main --run-digest <runDigest>` exits 0
  (CI checks the committed values' self-consistency; R1's PR is H7's inclusion PR, whose other
  changes the check lists for review by hand). **Deadline: hub rollout read back by `issuedAt` +
  60 min, or abort before the grant** (no tokens spent).
- **S-B, in place, then commit.** The same rule applied to `apps` (the hub's Kustomization, suspended
  as in hub step H3): the run's ConfigMap and the `relay` Deployment from the branch's render, applied
  server-side, then the same generated files committed (generator runbook §4b). It removes review
  latency from the window, but leaves every app unreconciled for up to 2 h and puts the run-bound hub
  bytes in git only afterwards.

## 3. The per-run ceremony

`kubectl --context admin@ai` throughout. Relay's journal, budget-scope and policy CLIs
(`scripts/candidate-worker-authority.ts`, `candidate-budget-scopes.ts`, `prepare-candidate-policy.ts`)
and the ID snippet below run from a clean checkout of the release commit `c0fe50f0` on a trusted Linux
operator host (procfs required), as `node --import tsx scripts/...`. The ceremony tools
(`sign-candidate-run.ts`, `candidate-command-sha256.ts`, `compose-measurement-subject.ts`,
`qualification/candidate-submitted-pod.ts`), drivers and converters run from the pinned driver
checkout; the ceremony tools are checked against `c0fe50f0`'s own verifiers
(`main:docs/qualification/candidate-ceremony-tools.md`). Hub SQL reads run as
`postgres` in `relay-postgres-0`, database `relay`, tenant `00000000-0000-4000-8000-000000000001`.
`t_s` is `issuedAt`, `t0` the create, `T` the absolute expiry. A failed readback stops the run.

### 3.0 Before signing (no clock running)

1. **Previous run released.** `GET /api/workers/<previous workerId>` shows `removed` with
   `outstandingEnvironments` 0, the journal slot is settled (or R7's retained `admitted` entry has O8's
   readbacks, §3.10 step 5), the slot holds no Pod, ConfigMap quota headroom ≥ 2 in
   both namespaces (`kubectl get resourcequota relay-enrollment-quota -n <ns>`), Flux
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

3. **Prompt and hashes.** Exact bytes, no trailing newline. Write the text with a quoted heredoc (safe
   for any quote), drop the final newline, check the byte count, then hash; the holder hashes the UTF-8
   text as sent (`rel:holder/src/worker_candidate.rs:434-441`):

   ```sh
   cat > P.raw <<'PROMPT'
   <exact prompt text>
   PROMPT
   head -c -1 P.raw > P.txt; wc -c P.txt; tail -c 1 P.txt | od -c; sha256sum P.txt   # = promptSha256
   ```

   `commandSha256` is SHA-256 of canonical `{executable, argv, cwd}` of the final native command, which
   contains `sessionId` (`rel:holder/src/worker_candidate.rs:527-545`, `holder/src/release.rs:105-134`).
   A wrong hash refuses native start. Compute it offline from the frozen binding:

   ```sh
   node --import tsx scripts/candidate-command-sha256.ts \
     /operator/binding.json <binding sha256> <profileTemplateDigest> <sessionId>
   ```

   For the researcher image the command is `/opt/relay/harness/codex/node
   /opt/relay/harness/codex/relay-codex.mjs --home /run/relay-holder/state/native-homes/<sessionId>`
   in `/workspace` (`main:docs/qualification/candidate-ceremony-tools.md`, "`commandSha256`").
4. **Keys.** A fresh session-permit Ed25519 key per run (run-scoped:
   `rel:docs/proposals/worker-candidate-qualification.md`, "Signing keys"). One holder journal key for
   the whole campaign (§3.5). The issuer, agent (= installation), holder, journal-reply and
   permit keys must all differ (`rel:packages/worker-runtime/candidate-signatures.ts:174-181`).
5. **Signing input.** `signing-input.json` (`kind: relay.candidate-run-signing-input`): `issuerKeyId`,
   `validitySeconds: 7200`, `cleanupSeconds: 86400` and the whole payload except its four times
   (`main:docs/qualification/candidate-ceremony-tools.md`, "Signing a run"). From the previous run's,
   change only `runId`, the worker/launch IDs, `promptSha256`, `commandSha256`, `leaseSeconds`,
   `sessionPermitPublicKey` and `owner`; a diff in any other field breaks the freeze. Common values:
   one worker, `maxActiveWorkers: 1`, `maxGenerations: 1`, `maxLifetimeSeconds: 900`,
   `providerTokenCap: 1000000`, `providerRequestCap: 8`, `allowedScenarios` = all 19,
   `egressPolicySha256` = `f6d3187c…282f`, `router.tokenPolicyDigests` = [File B digest], the Router CA
   = the researcher template's pin `3d109e83…c4c3` (the agent refuses any other), the hub image = the
   running v0.10.4 digest.
6. **Configs** for every CLI and driver of the run, with only the run SHA-256 left to fill; the per-run
   PR branch (S-A); clock checks (§5.1); an operator ready on the session page (§5.3); a stopwatch log.
7. **Once, before R1: `deployment.json` and `subject.json`.** After the v0.10.4 hub rollout, write
   `deployment.json` (a `MeasurementDeployment`, `main:packages/worker-runtime/measurement-subject.ts:84-96`)
   from readbacks, as the field table in `main:docs/qualification/candidate-ceremony-tools.md`
   ("`subject.json`") says: `tenantId`; `clusterUid` (namespace `kube-system`'s UID); `hub` origin, CA
   (the image's `hub-ca` artifact), the running hub image digest and a declared `databaseIdentity`;
   `router` origin, CA `3d109e83…c4c3`, image and a declared `databaseIdentity`; the control and
   execution namespaces with their UIDs; and the four contract digests (SHA-256 of each reviewed
   manifest at the frozen commit). The two `databaseIdentity` UUIDs and the four contract digests are
   declarations no code checks: choose them once and never change them. Then
   `node --import tsx scripts/compose-measurement-subject.ts /operator/binding.json <sha> /operator/deployment.json <sha> /operator/subject.json`.
   Both files are frozen until the canary; every run is checked against them in §3.1.

### 3.1 Sign (`t_s`)

`issuedAt = notBefore = now`, `expiresAt = now + 7,200,000`, `cleanupDeadline = expiresAt + 86,400,000`.
Never a future `notBefore`: the schema allows it (`rel:packages/contracts/worker-candidate.ts:127-141`),
but the hub, journal and budget-scope installers all refuse `now < notBefore`
(`rel:packages/worker-runtime/candidate-signatures.ts:199-201`), so it only shrinks the window. The
signed bytes are `relay.candidate-run/v1\n` + canonical JSON of the envelope without `signature`
(`rel:candidate-signatures.ts:122-130`). The offline signer sets the four times itself and verifies
the result with `verifyCandidateRun` against the expected issuer key before writing it; it also refuses
any payload key that is not canonical unpadded base64url, which the holder's strict decoder would
refuse. Keep the issuer key off disk (`-` reads it from standard input):

```sh
<issuer PEM from the secret manager> | node --import tsx scripts/sign-candidate-run.ts \
  /operator/<run>/signing-input.json <sha256> - <issuer public key> /operator/<run>/run.json
node --import tsx scripts/compose-measurement-subject.ts /operator/binding.json <sha> /operator/deployment.json <sha> \
  /operator/<run>/subject-check.json /operator/<run>/run.json <run sha256>
cmp /operator/subject.json /operator/<run>/subject-check.json
node --import tsx scripts/candidate-worker-authority.ts verify /operator/<run>/journal-operator.json <config sha256>   # rel: checkout
```

The signer prints `runDigest` and `sha256` (the file pin for the journal, budget-scope and hub configs)
and never overwrites a file; on any refusal it prints only `Refusal: <code>` and exits 2. The subject
check refuses (`subject-run-mismatch:<fields>`) a run whose hub, Router, tenant, cluster, namespace,
image, runtime, template or CA pins differ from the frozen subject, and `cmp` confirms the frozen file
is unchanged. `verify` prints `runDigest` and `expiresAt`; record both with `t_s`.

### 3.2 Journal: re-render with exactly this run.json, install, restart

1. New service config (only `run.sha256` changes), then render and review:
   `python3 -B kubernetes/optional/relay-candidate-journal/render.py service /operator/<run>/service-render.json <sha> > /operator/<run>/service-rendered.json`.
   The diff against the previous render may touch only the public ConfigMap
   (`relay-candidate-journal-public-<config sha[:16]>`), its pins and the annotations; the Service stays
   on `10.96.0.42`.
2. `node --import tsx scripts/candidate-worker-authority.ts install /operator/<run>/journal-operator.json <sha>`
   (operator login). It refuses an expired run in the database clock too
   (`rel:deploy/worker-candidate-journal/002-signed-clients.sql:47`).
3. Apply the render without its protocol Service, which only Flux manages
   (`kubernetes/apps/relay-candidate-journal/service.yaml`; the tests keep it equal to the render's copy):
   `python3 -c 'import json,sys; d=json.load(sys.stdin); d["items"]=[o for o in d["items"] if (o["kind"], o["metadata"]["name"]) != ("Service", "relay-candidate-journal")]; json.dump(d, sys.stdout)' < /operator/<run>/service-rendered.json | kubectl apply -f -`
   (an opted-in metrics Service, `relay-candidate-journal-metrics`, is still applied). The render has `replicas: 0`, so then
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

1. Compose the document (`main:scripts/compose-candidate-hub-document.ts`, which constructs the hub's
   `CandidateHubAuthority` from it with the permit key before writing; Relay #533), run
   `python3 scripts/relay-candidate-run.py generate` on the per-run branch, and create the session-permit
   Secret `relay-worker-candidate-key-<runDigest[:16]>` in `relay` from the private key, never via git
   ([relay-candidate-run-generator.md](relay-candidate-run-generator.md) §1-§3). Without the Secret the
   hub Pod waits in `ContainerCreating`.
2. The run-bound change (merge the S-A PR, or apply in place under S-B): the `CandidateHubDocument`
   (`rel:server/workers/candidate/authority.ts:40-70`: raw run, issuer keys, hub/router/journal pins,
   pool, binding, the exact installed egress policy, profiles), its `RELAY_WORKER_CANDIDATE_SHA256`,
   `RELAY_WORKER_CANDIDATE_SESSION_KEY_FILE`. Write the document as **compact JSON** (no indentation):
   the hub reads at most 65,536 bytes (`rel:packages/worker-runtime/candidate-files.ts:32,40`), and the
   document with the real binding measures about 56.8 KB compact, so an indented one does not fit.
   These are the generator's `run/` files: `document.json` in ConfigMap
   `relay-worker-candidate-<runDigest[:16]>`, its pin, and the Secret's name. The component's init
   container copies the key into a Memory `emptyDir` as UID 1000 with mode 0400, because no Secret
   volume meets the reader's `secret` rule for a non-root hub.
3. The merge is a Recreate rollout: `backup-database` (bounded at 1800 s), `backup-artifacts`,
   `migrate`, then `relay` (`kubernetes/apps/apps/relay/relay.yaml`). Relay is down meanwhile.
4. Readback (H7's): the init container `candidate-session-key` exited 0, the Pod's
   `RELAY_WORKER_CANDIDATE_SHA256` is the generator summary's `document.sha256`, budget startup `started`, `RelayWorkerBudgetStartupFailed` clear, no
   `worker_budget_authority_unavailable` and **no `worker_candidate_unavailable`**: a file that does not
   verify (expired, not yet valid, wrong key) only logs that warning and the hub starts without
   candidate authority (`rel:server/main.ts:288-305`). **This readback needs the v0.10.4 hub
   (`rel4:server/workers/candidate/load.ts:40`): `c0fe50f0`'s loader refuses every document with a real
   binding (§8 G0).** **Never create while it is logged:** an
   unrecognized worker takes ordinary admission, and the lifetime can freeze before the refusal
   (`rel:server/workers/service.ts:1543-1570`). Connectors and relay-bus keys answer again.

### 3.5 Agent candidate values, agent held disabled

1. `flux suspend kustomization relay-researcher-agent -n flux-system`, then
   `kubectl -n relay-infra-control scale deploy/relay-infrastructure-agent --replicas=0`
   and wait until every Pod of that Deployment is gone before §3.6. **Verified:** the running daemon
   polls and executes offers (`rel:packages/infrastructure-agent/daemon.ts:713-719`); with no agent
   process it cannot claim the create. The hub checks the stored installation is `ready` at the current
   recovery generation, not a heartbeat (`rel:server/workers/service.ts:1398-1410`), and persists the
   create intent without contacting the agent (`rel:server/environments/kubernetes-admission.ts:524-565`).
   Keep the enrollment gate and 0-Pod quota until §3.7; queued-create survival until restart remains
   **UNVERIFIED**, as do shutdown and restart latency (§8 U2, U12).
2. Create the run's immutable control ConfigMap in `relay-infra-control` (`binding.json`, `config.json`
   = the `CandidateAgentTrust` with the signed run, `ca.pem`, `network-policy.json`;
   `rel:deploy/infrastructure-agent/templates/deployment.yaml:67-71`) and the run's immutable slot
   trust (key `config.json`) and pin (key `sha256`) ConfigMaps under new names
   (`rel:packages/infrastructure-agent/candidate-volumes.ts:44-75`): `kubectl create -f` of the
   generator's `--operator-out` files, named `relay-candidate-control-`, `-trust-` and
   `-pin-<runDigest[:16]>`. The holder Secret `relay-candidate-holder` (keys
   `holder-key.pem`, `ca.pem`) is provisioned once, before the first guards. **Verified:** the Secret
   guard matches CREATE/UPDATE/DELETE by every principal in the execution namespace, including the
   operator; its only allowance is CREATE/DELETE of that run's bootstrap Secret names by the
   installation account (`rel:packages/infrastructure-agent/candidate-policy-preparation.ts:224-240,311-326`).
   Thus re-applying the holder Secret is denied while the guard is installed; before it is installed,
   preserving the key is an operator responsibility.
   Reuse it without applying it again: every signed run must retain the same holder public key and
   journal CA pin (`rel:packages/worker-runtime/supervisor/candidate-config.ts:113-138`; §8 U4).
3. Apply the agent values in place, still at 0 replicas. They are runtime fields, not Helm values
   (`rel:packages/infrastructure-agent/config.ts:113-125`, strict): `config.json`'s
   `candidateRuntime.trustSha256`, `trustConfigMap`, `pinConfigMap` (the file paths, `holderSecret` and
   `bindingSha256` stay; assert `bindingSha256` equals the previous run's, since the binding is frozen), and `deployment.yaml`'s candidate volume naming the run's control ConfigMap. Render
   `kubectl kustomize kubernetes/apps/relay-researcher/agent` from the per-run branch, set the
   Deployment's `spec.replicas` to 0 in that render, apply it. The same values reach git through the
   per-run PR (the generator's two agent files; it refuses a changed `bindingSha256` or
   `holderSecret`); `relay-researcher-agent` stays suspended until §3.10 step 8.

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
  of delay comes off the lifetime. **Verified:** the grant accepts Unix milliseconds
  (`rel:packages/contracts/worker-grants.ts:71-72`); choose `G % 1000 = 0` and confirm the intent's
  absolute expiry equals `G`. This does not guarantee exit 72 (§8 U8).
- **Latest create:** `t0 + 900 s ≤ run.expiresAt` or selection refuses
  (`rel:server/workers/candidate/authority.ts:274-292`); plan `t0 ≤ t_s + 95 min` (§4).
- Readback: `SELECT original_deadline FROM workers WHERE id='<workerId>';` and the intent's
  `absoluteExpiresAt`. Record both: **`T` is the intent's absolute expiry**, which may precede
  `original_deadline` when the grant clamps it (`rel:server/workers/service.ts:1627-1630`). Every
  threshold below derives from `T`. A create that returned an
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
unknown slot (§3.10). **create → Pod Running must stay well under 5 min; abort the run past 5 min**
(a chosen campaign limit from rev 2's E6, not a code timeout: it keeps at least 10 of the 15 minutes
for the drivers).

Driver inputs come from here: `session` = that row's `permit`, `bindingProvenance` = its `binding`,
the journal proof `{request, reply}` the coordinator stores (`rel:server/workers/candidate/coordinator.ts:279-282`).
**Verified:** export each column as one JSON object (no SQL headings or row wrapper), then hash the
final file bytes and put those hashes in the driver's `session.sha256` and `bindingProvenance.sha256`.
The file pins check bytes before parsing (`rel:packages/worker-runtime/acceptance.ts:164-191`);
signatures canonicalize object keys, so jsonb key order and whitespace do not invalidate them
(`rel:packages/worker-runtime/candidate-signatures.ts:122-130,148-156`;
`rel:packages/worker-runtime/candidate-journal-protocol.ts:40-66,282-320`; §8 U1).
`submittedPod` is rebuilt offline once the session exists (owner decision O7, §7):

```sh
node --import tsx scripts/qualification/candidate-submitted-pod.ts \
  /operator/<run>/submitted-pod-config.json <sha> /operator/<run>/submitted-pod.json
```

The config (`kind: relay-candidate-submitted-pod-reconstruction`) pins the run's `trust`, `binding` and
`session` files and the installed bundle's `30-rendered-contracts.json` as `bundleContracts`. The tool
varies only `activeDeadlineSeconds` and keeps the Pod whose digest equals the session permit's
hub-signed `journalAdmission.podDigest`, so a match is the body the agent posted
(`main:scripts/qualification/candidate-submitted-pod.ts:1-40`); anything else is refused. R7 has no
session: there the same tool takes `journalAdmission` (the journal's `admitted` entry) in place of
`session`, and R7 also needs the create's API audit event for the Pod UID (§6 R7).

### 3.9 Drivers

From the pinned driver checkout: `node --import tsx scripts/qualification/<driver>.ts <config> <config-sha256> <report> [seconds]`.
Each reserves its report exclusively and never overwrites one. Thresholds come from `T` and the
current lease end `E_L` (the latest `published` event's `leaseExpiresAtMs` in
`kubectl logs <pod> -c supervisor`, `main:scripts/qualification/researcher-hub-partition-observation.ts:169-173`),
never from `t0` estimates:

| Rule | Source |
| --- | --- |
| Every report must **start** in `[session.notBefore, T)` to convert | `main:scripts/qualification/researcher-runtime-v2-observations.ts:87-105`; `researcher-pool-v2-observations.ts:1004-1007` |
| Drivers using the acceptance config refuse once the session or run has expired, **including cleanup settlement and #519** | `main:packages/worker-runtime/acceptance.ts:151-178`; `main:scripts/qualification/worker-cleanup-settlement.ts:117` |
| Partition and lease drills: at least 30 s before `T` at start and at the cut; wall/monotonic drift ≤ 1 s | `main:scripts/qualification/researcher-hub-partition-observation.ts:695,840` |
| Lease expiry needs exit 71 before `T`: cut no later than `T − L − 30 s` | `main:scripts/qualification/researcher-lease-expiry-observation.md`; §6 R3 |
| Revocation: last published lease ends ≥ 30 s before `T`; latest send ≈ `T − 30 s − L` | `main:scripts/qualification/researcher-revocation-observation.ts:357-361` |
| Measurement window: the code's deadline is `min(run.expiresAt, T)` and lease sampling lasts up to 60 s; this runbook adds the margin "finish before `T − L`" so the sampled lease is not clamped to `T` | `main:packages/worker-runtime/acceptance.ts:472-476,794-795` |
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
   `c0fe50f0` (**Verified:** `rel:scripts/candidate-worker-authority.ts:43-53`;
   `main:docs/qualification/delayed-start-slot-reconciliation.md:38-58`): **stop the campaign and
   escalate** for a reviewed reconciliation, not an improvised journal write (§8 U6).
   **R7's reviewed exception:** follow O8 to release the hub reservation; retain and inspect the
   journal entry as `admitted`, `workloadUid: null`. O8 does not settle that entry, and the journal's
   reservation check is per run, so it does not block a different signed run
   (`main:docs/qualification/delayed-start-slot-reconciliation.md:211-238`;
   handler and transition checks in §8 U6). Other unknown states remain stopped.
6. Withdraw: `node --import tsx scripts/candidate-worker-authority.ts withdraw /operator/<run>/journal-operator.json <sha>`.
   The budget scope lapses by itself; the hub files are replaced by the next run's §3.4.
7. ConfigMap quota: both namespaces cap ConfigMaps at 8 (`installation/containment.yaml`). Delete the
   previous run's control ConfigMap and stale generated agent config once the agent no longer mounts
   them. Run N's slot trust and pin ConfigMaps stay frozen by its guards until run N+1's
   `11-candidate-contract-guards.json` replaces those policies in place (same names,
   `relay-candidate-<installationId>…`). **Verified:** the names are stable and the ConfigMap guard
   freezes only the two names in the new definition
   (`rel:packages/infrastructure-agent/candidate-policy-preparation.ts:224-240,327-337`); delete the
   old ConfigMaps only after the new guards and bindings are read back in §3.7 step 4 (§8 U4).
8. Slot back to git: confirm every driver's `outstandingRecovery` is empty and no Pod remains, then
   `flux resume kustomization relay-researcher-installation -n flux-system`. **Verified:** git declares
   the deny-all gate and 0-Pod quota (`ailab:kubernetes/apps/relay-researcher/installation/admission-deny.yaml:35-38`;
   `ailab:kubernetes/apps/relay-researcher/installation/containment.yaml:71-90`). Wait for successful
   reconciliation at the intended revision and read back both objects; resume alone is not that
   readback. Extra candidate guards remain (`prune: false`,
   `ailab:kubernetes/apps/clusters/ai/relay-researcher-installation.yaml:25-33`); their scope is described in
   §8 U5. Then `flux resume kustomization relay-researcher-agent -n flux-system` once
   the per-run PR has merged and git equals the live agent (replicas included).

### 3.11 Before the next run

Convert every report of this run with the pinned checkout and the run's `subject.json`
(`PodMeasurementSubjectV2`, `rel:packages/worker-runtime/measurement-subject.ts:99-178`):

```sh
node --import tsx scripts/qualification/researcher-runtime-v2-observations.ts \
  /abs/acceptance.json <sha> /abs/subject.json <sha> /abs/raw-report.json <sha> /abs/v2-rows.json [/abs/transition.json <sha>]
node --import tsx scripts/qualification/researcher-pool-v2-observations.ts \
  /abs/acceptance.json <sha> /abs/subject.json <sha> /abs/pool-report.json <sha> /abs/v2-row.json [/abs/subresource.json <sha>]
```

Each intended row must come out `observed`. The same two commands convert every report
(`main:scripts/qualification/README.md`):

- **Runtime converter:**
  - acceptance A and B;
  - the measurement window: five rows, with the window's own acceptance config, whose SHA-256 must equal
    the report's `acceptanceSha256`;
  - lease expiry (#10), revocation and replay;
  - cleanup settlement, with its transition report as the extra pair.
- **Pool converter:**
  - RBAC;
  - storage staging (#17);
  - the admission matrix, with the subresource report as the extra pair when there is one;
  - egress;
  - the same lease-expiry report again (#18), the hub partition and the recovery fence.

The #519 defaultCommunication reports are not V2 rows: they go to their own independent review for
`ReleaseApproval.defaultCommunication` (`main:docs/qualification/default-communication-observation.md`).
R7's delayed-start report converts through the runtime converter's delayed-start branch (Relay #532). **Do not sign the next
run until this run's rows convert**, and package its evidence (§5.5).

## 4. Timing budget and the E6 rehearsal

The installers need the run valid, so everything up to the create fits in the 2 h. Journal
inspection and settlement stay allowed after `expiresAt` (`rel:deploy/worker-candidate-journal/001-journal.sql:26-27`:
`cleanupDeadline` is "not a cleanup authorization expiry"), so post-`T` waits (settlement, egress,
recovery reconciliation) only need to finish by `cleanupDeadline` for the converters.

| Step (from `t_s`) | Target | Hard limit |
| --- | --- | --- |
| 3.1 sign, verify | 5 min | |
| 3.2 journal render, install, restart | 8 min | |
| 3.3 budget scope | 3 min | |
| 3.4 PR (opened after 3.1) merged, Flux, hub rollout (backup ≤ 1800 s), readback | 30 min | `t_s + 60 min`, else abort before the grant |
| 3.5 agent objects and in-place values | 5 min, alongside 3.4 | |
| 3.6 grant, create | 2 min | code: `t0 + 900 s ≤ expiresAt`; chosen margin: create ≤ `t_s + 95 min` |
| 3.7-3.8 bundle, enable, Pod Running, session | ≤ 5 min after `t0` | chosen (rev 2 E6): 5 min, else abort |
| drivers | to `T` (+60 s capture) | `T ≤ t0 + 900 s` |
| 3.10 Pod removed, slot settled | 10 min | chosen: before `expiresAt`; code allows later |

Target: create by `t_s + 55 min`, done by about `t_s + 85 min`; 35 min of slack, nearly all for the PR.

**E6 rehearsal checklist.** Time each step; record the measured value next to the target; nothing is
signed on the live hub.

- [ ] Sign, `verify`, budget `verify` and the journal render with throwaway keys (offline).
- [ ] Journal service restart on a disposable namespace or the rehearsal journal: apply, scale, Ready.
- [ ] Per-run PR: open → CI green → merge → Flux apply → hub rollout, on a no-op branch; and the
      in-place agent apply at 0 replicas. Keep UTC timestamps for each transition and the §3.4
      readback; compare signing → readback with the 60 min abort limit (§8 U3).
- [ ] Hub rollout duration, from the last H-step rollouts' init-container timings (`backup-database`
      dominates). Record each init attempt's start, finish, exit status and restart count, plus Pod
      Ready and §3.4 readback times; a timeout per attempt does not bound the whole rollout (§8 U3).
- [ ] `prepare-candidate-policy.ts` + prebuilt verifier + transition steps on Relay's disposable
      Kubernetes 1.35 fixture (`rel:docs/infrastructure-candidate-policy.md`, "Disposable local API-server evidence").
- [ ] Runtime image cached on `relay-worker-1`; Pod start under `relay-gvisor` measured outside the slot.
- [ ] **create → Pod Running well under 5 min** (bundle + enable + scheduling + start), summed.
      Rehearse §3.5's scale-to-zero and wait for no agent Pods before create; confirm the hub stores
      the intent while the slot stays empty. Read back the stored installation state and create
      operation through the hold: `ready`, `pending`, no claim or offer, with the same intent retained;
      record any expiry or state change. Then measure restart through the §3.8 readbacks (§8 U2).
- [ ] Each driver's real duration (window, storage staging, admission matrix, egress, replay) to fill
      the latest-start formulas in §6.
- [ ] P1/P3/P4 call counts on the provider-qualification harness (paid; within O5's six-call cap)
      and native approval prompts; record per-prompt tool and approval counts and response delays
      as in §5.3 (§8 U7).
- [ ] The ceremony tools end to end on a rehearsal launch: `commandSha256` accepted by the holder,
      session files passing the drivers' pins (U1), `submittedPod` reconstructed, and `subject.json`
      accepted by both converters.
- [ ] R2's disruption on a rehearsal worker: arm, cut, hold 55 s, exact restore, recovery
      (`main:docs/qualification/session-replay-hub-disruption.md`).
- [ ] R7's hold and release on a rehearsal Pod outside the candidate slot (E1's procedure): the held
      Pod survives past `T`, release-to-exit latency, the audit event's shape (O7: the probe and the
      extraction rehearsal in [relay-researcher-r7-audit-policy.md](relay-researcher-r7-audit-policy.md)),
      exact-UID cleanup, and the agent at zero keeping the bootstrap Secret past its expiry `E`
      (Relay #536).
- [ ] U9: exactly one agent -> journal connection on TCP 8443, admitted by
      `relay-infrastructure-agent-journal-egress` (the chart's `10.96.0.42/32` rule admits nothing under
      Cilium), and no agent egress to the journal on any other port.

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

Suspend `relay-researcher-installation` from §3.7 until §3.10 step 8, and `relay-researcher-agent`
from §3.5 (installation runbook, "Flux and the in-place ceremonies"; drivers replace the gate, quota
and egress policy in place, and Flux would silently undo them, as it would the per-launch bundle).
Driver procedures that suspend and resume Flux around their own in-place change (R2's disruption) run
inside this window: confirm it is suspended instead, and never resume before §3.10 step 8. Never
resume over an unreverted live change. Q4 must never run inside a window.

### 5.3 Native tool approvals

The Codex launcher pins `approval_policy: "on-request"`, `approvals_reviewer: "user"`,
`sandbox_mode: "read-only"` (`rel:packages/codex-launcher/main.ts:230-232`). Keep the operator on the
worker's session page from §3.8 to `T`, ready to answer permission requests; approval latency counts
against every threshold. How often local-tool prompts (P3, P4) ask is **UNVERIFIED** (§8 U7).
In E6's approved prompt trials, retain the exact prompt and session transcript; count local tool
requests and the permission requests displayed on the session page, recording each tool/command,
request time, operator response and resume time. Report approvals per tool request and per prompt
trial, plus total and longest wait. Repeat this log in R3 step 3 and R4 if used; budget the measured
waits into their cut deadlines. A trial with no requests does not establish a zero rate for later runs.

### 5.4 Router headroom

Each attempt reserves `I + maxTokens` = 922,000 + 8,192 = **930,192** against the run's 1,000,000; the
next is admitted only while charged ≤ **69,808** (llm-router `inference-budget/index.ts:48`,
`worker-budgets.ts:270`). Requests are never refunded; an interrupted attempt is charged the full
reservation. Codex does not retry (`rel:packages/codex-launcher/main.ts:223-224`). Rules:

- read `GET /api/workers/<id>/environments/<environmentId>/inference-meter` (`consumedTokens`) before
  every prompt (`rel:server/workers/routes.ts:160-210`; 409 `inference_meter_unavailable` until the
  credential is metered);
- send a prompt only if `consumedTokens` + its planned turn stays under 60,000;
- the call that can be interrupted (lease cut, partition, disruption) is always the run's last.

### 5.5 Evidence per run (plan U5)

The campaign plan's U5 keeps evidence in git as forge attachments, as done for the provider qualification
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
| Any §3.0-§3.5 readback fails, a clock is out, or the PR misses `t_s + 60 min` | abort before the grant; no tokens are spent, and a run aborted before its create does not count toward the 8 (O1) |
| Create returned an error | treat lifetime and allocation as spent; remove and reconcile |
| create → Pod Running > 5 min, or no `session-observed` | stop, remove, reconcile; the run is wasted |
| A driver misses its latest start | skip that row in this run; never stretch a threshold |
| An interrupted call (charged 930,192) | no more prompts; zero-request drivers may continue |
| Unknown journal slot | stop until reviewed reconciliation; only R7's O8 procedure retains an `admitted`, null-UID journal entry (§3.10 step 5) |
| A `failed` row traceable to the hub, agent or runtime image | stop the campaign: a fix changes the subject and voids every row; owner |
| A row fails to convert | fix the evidence path before the next run |
| More than one lost terminating run | new owner approval (O1) |

## 6. Runs

Each table lists the steps after §3.8. Latest starts use `D_x`, the E6-measured duration of step x.
"Spare" rows are taken only when their latest start still holds.

### R1: first session, window, communication 1, absolute expiry

Signed: L 90, lifetime 900, grant `expiresAt` = whole second `G`, P1 with a fresh
`relay-comm-<openssl rand -hex 12>`, names the researcher channel, asks for a broadcast and a reply
with the message ID (`main:docs/qualification/default-communication-observation.md:68-86`). 2-4 requests.
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
first is a non-observed outcome (`main:docs/qualification/worker-runtime-acceptance.md:290-298`), and
#8 then needs the reserve.

### R2: replay, then revocation

Signed: L 120, a tool-free one-line first prompt (P2). 2-3 requests (P2, replay exactly 1, none after
the revoke). Needs R1's proof of session control. R2 runs **no** #519 observer (the communication
attempts are R1 and R5), so skip that half of step 1 in the procedure below. The whole sequence is
`main:docs/qualification/session-replay-hub-disruption.md` (E4, #524): inputs pinned before signing
(replay config with its `disruption` entry; disruption config with `holdSeconds: 55`,
`leaseMarginSeconds: 30`, `expiryReserveSeconds: 270`, `hubRuleIndex: 0`, `routerRuleIndex: 1`), the
disruption kubeconfig's rights, and the recovery cases. The replay refuses before prompting without a
settled usage baseline (`main:packages/worker-runtime/session-replay-observation.ts:400-419`).

| Step | Driver / action | Latest start, stop/go |
| --- | --- | --- |
| 1 | Optional spare acceptance [#1, #2] | before step 2 |
| 2 | Send P2; wait for the answer; `worker-session-replay-observation.ts --preflight <replay cfg> <sha>` until `ready: true` | preflight ready by `T − 470 s`, else skip to step 6 |
| 3 | Confirm `relay-researcher-installation` is still suspended (since §3.7; the procedure's own suspend is then a no-op); save the live egress policy; terminal A: `researcher-hub-disruption-observation.ts` armed | `T − 460 s` |
| 4 | Terminal B: replay #12 with `seconds = min(300, T − 210 s − now)` (≥ 235); the disruption cuts after the replay turn and a fresh renewal, holds 55 s, restores exactly | replay start by **`T − 450 s`**; cut by `latestCutAt` = `T − 325 s`; restore by `T − 270 s` |
| 5 | Confirm the restore (resourceVersion and `.spec` equal the saved file); **do not resume Flux** here, unlike the procedure's step 8: the per-launch bundle is live until teardown (§5.2); replay `observed` with `disruptionJoin.status: joined` | replay done by `T − 180 s` |
| 6 | Cleanup observer, then revocation #11: `researcher-revocation-observation.ts <cfg> <sha> <out>` while idle; one irreversible `POST /api/workers/grants/<grantId>/revoke` | send before **`T − 150 s`**; hub `Date` within 1 s |
| 7 | Remove, settle (§3.10) | last |

### R5: communication 2, cleanup, matrix, egress

Signed: L 90, P5 with a new marker. 2-4 requests.

| Step | Driver / action | Latest start, stop/go |
| --- | --- | --- |
| 1 | Spares: RBAC, acceptance [#1, #2] | at once |
| 2 | #519 observe, P5, wait for the observer | P5 by `T − 600 s` |
| 3 | Cleanup observer, then remove (§3.10) | remove by `T − 360 s` |
| 4 | Settlement #13 with the transition report | start by `T − 240 s` |
| 5 | Matrix #15: `researcher-pool-admission-observation.ts <cfg> <sha> <out>`; slot Pod absent; dry runs only; CREATE paths only, UPDATE paths stay `unmeasured`; the config adds the policy `definition` and the installed bundle's `installedReport` (`main:scripts/qualification/researcher-pool-admission-observation.md`) |  start by `T − D_matrix − 60 s`, so egress still starts before `T` |
| 6 | Egress #16: `researcher-pool-egress-observation.ts <cfg> <sha> <out>`; zero Pods; replaces the gate in place under CAS; 600 s window; never overlapping step 5 | start before `T`; finish before `cleanupDeadline` |

### R3: storage, then one lease-expiry drill (#10 and #18)

Signed: L 60, multi-step prompt with local tools only (for example sequential `sleep 10` steps).
3-5 requests. A hub-only cut leaves the Router rule in place, so calls keep flowing until the worker
exits: land the cut during a local step, keep `consumedTokens` ≤ 69,808 minus one turn so the next
call after the cut is admitted and completes (`routerTraffic: required`), and accept that the call in
flight at exit 71 is charged 930,192 and is the run's last.

| Step | Driver / action | Latest start, stop/go |
| --- | --- | --- |
| 1 | Pod `native-started`, ≥ 2 lease publications and a renewal | |
| 2 | Storage #17: `researcher-storage-policy-staging.ts <cfg> <sha> <out>`; the researcher Pod is the only Pod; quota `pods` hard = used = 1; raises the quota to 2, adds a no-egress policy, replaces the gate; 600 s budget; converts with the pool converter | start by `T − D_storage − 240 s`; else skip #17; restore confirmed before step 3 |
| 3 | Send the prompt (headroom check), then the lease drill: `researcher-lease-expiry-observation.ts <cfg> <sha> <out>` with `routerTraffic: required`, egress and endpoints from Q6 | cut by `T − L − 30 s` = `T − 90 s`; exit 71 before `T` (72 is `inconclusive`) |
| 4 | Convert the one report twice: runtime converter → #10, pool converter → #18. #18 is `observed` only with the drill's own `restored` and worker Router traffic observed after the cut; a traffic miss lowers only #18 (`main:scripts/qualification/researcher-lease-expiry-observation.md`) | |
| 5 | After the terminal capture: restore checked, remove; spare #13 if settlement can start before `T` | |

### R4: hub partition (reserve only, if R3's #18 is not `observed`)

Signed: as R3. Step 1 as R3 (storage only if #17 is also missing), then send the prompt and run
`researcher-hub-partition-observation.ts <cfg> <sha> <out>` with `routerTraffic: required`: at least one
Router request must complete after the cut: land the cut during a local step and leave headroom under
69,808 for that call; a call still in flight at the exit is charged 930,192 and is the last. Exit 71 or
72 both count. Cut by `T − 30 s` at the latest (the driver refuses later).

### R7: delayed start (E1, Relay #532)

Signed: L 120 (the maximum), and the worker grant is approved with `leaseSeconds: 120` too: the
bootstrap lease takes the grant's value. Prompt never sent, 0 requests. The full procedure, inputs and
rehearsal are `main:docs/qualification/researcher-delayed-start-observation.md`; in summary:

- **Hold.** Cordon `relay-worker-1` before enabling the agent, so the created Pod stays Pending and
  unscheduled. Uncordon at `T + 5 s` (never before `T + 1 s`).
- **Keep the bootstrap Secret (Relay #536).** A running agent's idle sweep deletes the held Pod's
  bootstrap Secret once its enrollment expires, at `E`, long before `T`. The Pod's `bootstrap` volume
  is not optional, so a Pod released after that can never start. So (procedure steps 3a-3b, 6, 9):
  - read the create's `execute` receipt (`present`, the Pod UID, `candidate-session-unsettled`) and the
    enrollment's `secret_object_uid` (`bootstrapSecretUid`) and `expires_at` (`E`) from the hub
    database; never scale the agent down before the receipt is recorded, because O8 needs it;
  - scale `relay-infrastructure-agent` to 0 no later than `E − 60 s`, and wait until its Pod is gone,
    before `E`;
  - if the receipt is not recorded by `E − 60 s`, abort R7: no #9 row, and the run still counts
    toward the 8, because its create was made (O1). The procedure scales the agent down anyway. O8
    cannot settle this state as written, because its preconditions require that `execute` receipt
    (Relay `delayed-start-slot-reconciliation.md`, "Preconditions", "The Pod"). So:
    - scale the agent back to 1 (the Secret no longer matters once R7 is aborted);
    - repeat O8's step 2 queries;
    - run O8 only if they show the `execute` receipt with the Pod `present` and its UID. Otherwise
      stop with the slot blocked, record the hub, journal and Pod state, and take it to the owner.
      Releasing the slot then needs a new reviewed reconciliation before R6 or any R7 retry;
  - read the Secret's UID back after the scale and again right before the release; it must equal
    `bootstrapSecretUid`. If it is gone, stop before `T` and record why. The driver also checks it at
    its first read after `T` and ends `inconclusive` (`bootstrap-secret-missing-before-release`), so
    its kubeconfig needs named `get` on that Secret (it prints only the UID);
  - keep the agent at 0 through the release and the capture. `RelayInfrastructureAgentUnavailable`
    fires after 10 minutes at zero; it is expected, record it in the run log. Flux stays suspended for
    both Kustomizations: a resumed Flux would scale the agent back;
  - scale the agent back to 1 only after the report and the evidence are written, then follow O8 from
    its step 2. O8's step 1 is the check that R7 is finished (report written, evidence kept, R6 not
    signed), which this point satisfies.
- **Driver.** A pre-session observer bound to the signed run, the hub-admitted intent, the journal's
  `admitted` entry (its `podDigest` covers the submitted Pod), the submitted Pod, the guard's
  `--absolute-expires-at-ms` and the created Pod's UID. That UID comes from the create's own
  `RequestResponse` API audit event, so R7 needs the audit policy (O7). It observes:
  - a positive control before `T` (Pending, no node);
  - a read at or after `T` that still finds no node;
  - after the release, the same UID's harness exiting 72 with an `absolute-expiry` guard record and no
    `started` record.
- **Converter.** `sessionDigest` and `journalProvenanceDigest` are null, with `sessionAuthority`,
  `journalBinding` and the other fixed facets under `unmeasured` (`rel:packages/worker-runtime/measurement-subject.ts:221-225`).
- **Afterwards.** The launch ends `candidate-session-unsettled`. The hub's create operation stays
  `unknown` and keeps the pool's single execution slot, and `rel:` has no settle path, so R6's create
  fails until a reviewed hub reconciliation releases it (O8). Delete the held Pod only by exact UID.
  **Verified:** the instruction to settle the journal by hand is refuted by
  O8: keep its `admitted`, null-UID entry and record `inspect`; O8 releases the hub slot without
  changing the journal (`main:docs/qualification/delayed-start-slot-reconciliation.md:211-238`;
  §3.10 step 5, §8 U6).

### R6: recovery fence, last

Signed: L 90, prompt never sent, 0 requests.

| Step | Driver / action | Latest start, stop/go |
| --- | --- | --- |
| 0 | Prerequisites: a passing `worker-budget-restore.ts --router-root <llm-router> --work-dir <dir> --report <file>` (Node 24, clean trees); an older Relay snapshot that predates this Pod (**Verified:** R6's §3.4 pre-migration dump precedes its fresh create and meets that ordering condition; `ailab:kubernetes/apps/apps/relay/relay.yaml:54-63,174-178`; `main:scripts/qualification/researcher-recovery-fence-observation.md:24-27`; capture and restore checks in §8 U10); a 0600 management token for the disposable hub | harness and token before signing; capture the dump at §3.4, before create |
| 1 | `researcher-recovery-fence-observation.ts <cfg> <sha> <out>`, `denialWaitMs` ≤ 10 min, `reconcileWaitMs` ≤ 30 min | **before** the restored hub is pointed at the installation; before `T` |
| 2 | Freeze external authority; restore the older snapshot as a disposable hub; point it at the installation | |
| 3 | Halt (`fence_stale`), orphan deletion, reconciliation; then explicit operator recovery | bounded by `cleanupDeadline` |

Keep the last reserve until R6 passes.

## 7. Owner decisions

All recorded on relay#277 (comments 125299, 125360 and 125437). They are approvals in principle:
each live step (the audit-policy PR, the reconciliation, every run's apply and grant, the paid
rehearsal calls) still goes to the owner, through the operator, before it happens.

| Decision | Approved (2026-11-16) |
| --- | --- |
| O1 | 6 runs + 2 reserves; more than one lost terminating run needs a new approval; a run aborted before its create does not count toward the 8 |
| O2 | I = 922,000: 930,192 reserved per call, about six calls per run, an interrupted call ends the run's calls |
| O3, D | fixed with the owner when File B is generated: at least 5 days after File B, at a working-hours instant whose 48 h window avoids a weekend. If the canary would overrun File B's expiry, reissue File B before signing more runs |
| O4 | no three-worker no-call run |
| O5 | up to 6 paid rehearsal calls for E6, inside the existing caps |
| O6 | S-A: one generated, mechanical ailab PR per run, aborting before the grant if the hub is not read back by `t_s + 60 min`; S-B (in place under Flux suspension, then the same bytes committed) as the fallback; the canary always uses S-B (§2) |
| O7 | `submittedPod` by offline reconstruction (§3.8) for R1-R6, plus an API server audit policy for R7 only, as its own owner-held PR |
| O8 | a reviewed hub database reconciliation between R7 and R6, its SQL drafted and reviewed in advance (§6 R7) |
| S3 | v0.10.4, the hub-only loader fix, including the hub rollout |
| I4 | the frozen binding, ailab #1782 |
| N1, N2 | `notifications` under a written owner exception; two release approvals (A1 for the canary, A2 for H4b) |

**S2:** queueing the create with no agent is verified in source (§3.5, §8 U2). Survival until restart
remains **UNVERIFIED**; E6 must demonstrate the stored state through the hold, the no-Pod readback
and restart within the timing budget (§8 U12).

## 8. Gaps and UNVERIFIED steps

**Gaps found in code while writing this runbook, and how each is closed:**

- **G0, the hub cannot load a candidate document.** Closed: released and rolled out.
  - **The bug.** `c0fe50f0`'s loader parses the whole `CandidateHubDocument` with the integer-only
    candidate parser (`rel:server/workers/candidate/load.ts:38`; `rel:packages/worker-runtime/candidate-signatures.ts:107`).
    The document's `binding` carries `supervisorConfig.renewAfterFraction`, a fraction by schema
    (`rel:packages/worker-runtime/supervisor/config.ts:36`; 0.5 for the researcher). So every real
    document ends `document-invalid`.
  - **The fix.** Relay #525 adds a fraction-safe parser used only by the hub loader for this pinned,
    unsigned document; signed run and session documents stay integer-only.
  - **The release.** Released as v0.10.4 (`rel4:`), hub-only: the worker images, the binding and the I4
    pins stay v0.10.3.
  - **Rolled out.** ailab #1789 (`bda5b3fd`) has run it since 2026-11-16 10:08Z: image
    `registry.chifor.me/relay/control-plane@sha256:d1482bc101ecf13667a75bfc1892f78b37bed7cf2fed8f9ce42f8c4513d057a7`,
    schema 79. This is `deployment.json`'s `hub.image`, frozen from R1 to the canary.
- **G1, #15 matrix.** Closed by Relay #529. The driver now regenerates the installed bundle at its own
  `generatedAt`, requires the pinned bytes, and takes `installedReport`. The per-launch policy pins the
  seconds left when the bundle was generated (`rel:packages/infrastructure-agent/pod-renderer.ts:248-255`).
- **G2, `submittedPod`.** Closed by Relay #530, the offline reconstruction against the hub-signed
  `podDigest` (§3.8); owner decision O7. ailab sets no API audit policy, so an audit capture is not
  available today.
- **G3, `subject.json`.** Closed by Relay #528 (`compose-measurement-subject.ts`, §3.0 step 7).
- **G4, `commandSha256`, and the signer.** Closed by Relay #528 (`candidate-command-sha256.ts`,
  `sign-candidate-run.ts`).
- **G5, fraction-safe parsing in drivers.** Closed:
  - both converters read `subject.json` through `main:scripts/qualification/runtime-v2-subject.ts` (Relay #523);
  - the admission drivers read the policy definition with `parseOperatorJson` (Relay #529).
- **G6, delayed start (E1).** Closed by Relay #532 (`1ea5ecc9`); the bootstrap Secret's early deletion
  that would have kept the held Pod from ever starting is handled by Relay #536 (`eb6033fd`: L 120, the
  agent at zero before `E`, the Secret UID read back and checked by the driver). R7 still needs the API
  audit policy on the control planes (O7: merged as ailab #1790 and applied one control plane at a time
  by [relay-researcher-r7-audit-policy.md](relay-researcher-r7-audit-policy.md)) and leaves R6's slot
  blocked until the reviewed reconciliation (O8).

**Source verdicts and remaining UNVERIFIED measurements:**

1. U1 — **Verified:** jsonb exports preserve the signed values. The coordinator stores the permit and
   `{request, reply}` proof (`rel:server/workers/candidate/coordinator.ts:268-282,316-356`), and session
   and journal signatures use canonical JSON (`rel:packages/worker-runtime/candidate-signatures.ts:122-130,148-156`;
   `rel:packages/worker-runtime/candidate-journal-protocol.ts:40-66,282-320`). File pins are byte hashes,
   not those signature digests (`rel:packages/worker-runtime/acceptance.ts:164-191`). In §3.8 export
   each non-null column separately as JSON, hash the final bytes, and set those pins; never reuse a
   pre-export byte hash. E6 retains the end-to-end driver check while the session is valid.
2. U2 — **Verified:** no live agent is needed to queue the create. The hub requires stored installation
   state `ready` and current recovery generation (`rel:server/workers/service.ts:1398-1410`), then
   inserts the intent (`rel:server/environments/kubernetes-admission.ts:524-565`); the daemon alone
   polls and executes it (`rel:packages/infrastructure-agent/daemon.ts:713-719`). §3.5 now waits for
   every agent Pod to disappear, keeps Flux suspended and retains the closed gate/0-Pod quota until
   §3.7. **UNVERIFIED:** survival of the queued create until restart. These paths do not establish
   whether hub-side intent/offer expiry or pre-claim transitions to `failed`/`unknown` can intervene,
   or whether a background heartbeat check moves the installation off `ready`. E6 must read back
   the installation's `ready` state and the same `pending`, unclaimed create throughout the hold,
   then confirm it is offered and executed on restart (§4); shutdown/restart timing remains U12.
3. U3 — **UNVERIFIED:** measure E6's S-A rehearsal from PR open through CI green, merge, Flux apply,
   init attempts, Pod Ready and §3.4 readback. Keep the UTC transition log and init-container
   `startedAt`, `finishedAt`, exit codes and restart counts; use the last H-step rollouts as the
   initial sample. Repeat the log for every run's §3.4, comparing `t_s` → readback with the 60 min
   abort limit. The 1800 s timeout is for a `backup-database` attempt, not a measured rollout bound
   (`ailab:kubernetes/apps/apps/relay/relay.yaml:54-63`). No source check settles PR or scheduling latency.
4. U4 — **Verified:** guard names depend on installation ID, not run digest
   (`rel:packages/infrastructure-agent/candidate-policy-preparation.ts:224-240`). The ConfigMap guard
   denies CREATE/UPDATE/DELETE for precisely the current trust/pin names; the Secret guard permits
   only the signed bootstrap names, CREATE/DELETE, by the installation account. Both guards apply
   to every principal in the execution namespace, including the operator
   (`rel:packages/infrastructure-agent/candidate-policy-preparation.ts:224-240,311-337`).
   Thus §3.10 step 7 deletes old ConfigMaps only after the next guards' readback. The holder Secret
   stays frozen across replacements: provision it once and retain the holder public key and CA pin
   in every run (`rel:packages/worker-runtime/supervisor/candidate-config.ts:113-138`).
5. U5 — **Verified:** the Flux tree declares a deny-all gate and zero Pods
   (`ailab:kubernetes/apps/relay-researcher/installation/admission-deny.yaml:35-38`;
   `ailab:kubernetes/apps/relay-researcher/installation/containment.yaml:71-90`). Resume requires successful
   reconciliation and live readback in §3.10 step 8. It does not remove extra guards (`prune: false`,
   `ailab:kubernetes/apps/clusters/ai/relay-researcher-installation.yaml:25-33`). Numbered Pod guards match
   the exact old Pod name and only CREATE/UPDATE; the next one-launch bundle replaces `-0` in place
   (`rel:packages/infrastructure-agent/candidate-policy-preparation.ts:250-270,297-309`). They do not
   obstruct a differently named next Pod. Secret/ConfigMap guards still enforce U4; do not interpret
   resume as removal of that freeze or permission to rotate the holder Secret.
6. U6 — **Verified:** there is no operator settle command or UI (`rel:scripts/candidate-worker-authority.ts:43-53`;
   `main:docs/qualification/delayed-start-slot-reconciliation.md:38-58`). The agent's deletion
   settlement requires retained UID-bound proof and a bound/started journal
   entry (`rel:packages/infrastructure-agent/candidate-admission.ts:811-865`). The unsupported R7
   instruction to settle by hand is **refuted**: O8 deliberately retains `admitted`, `workloadUid: null`
   and releases only the hub reservation (`main:docs/qualification/delayed-start-slot-reconciliation.md:211-238`).
   The journal's HTTP handler verifies the signed request and dispatches `admit` to the agent adapter
   (`rel:packages/worker-runtime/candidate-journal-https.ts:87-112`;
   `rel:packages/worker-runtime/candidate-journal-protocol.ts:98-134`). The adapter calls
   `worker_candidate.transition` (`rel:packages/worker-runtime/candidate-journal.ts:42-75`), which
   locks the requested run/launch and checks active reservations only within that `run_digest`
   (`rel:deploy/worker-candidate-journal/001-journal.sql:75-114`). No layer adds an installation-,
   holder- or slot-wide active-entry check. After §3.2 installs the next run and re-renders the service,
   R7's retained entry therefore does not block that different signed run.
   §3.0, §3.10 and R7 now allow exactly that reviewed exception; other unknown states stop for review.
7. U7 — **UNVERIFIED:** source fixes the approval settings (§5.3), not the requests produced by a
   particular prompt. In E6's approved P1/P3/P4 trials (within O5) and R3 step 3 (R4 if used), use the
   session page and retained transcript to count tool requests, permission requests and prompt trials, recording
   each requested command and request/response/resume times. Report approvals per tool request and
   per trial, total wait and longest wait; feed those delays into the cut deadlines (§5.3).
8. U8 — **Verified:** grant expiry is Unix milliseconds (`rel:packages/contracts/worker-grants.ts:71-72`)
   and the hub takes `least(original, grant)` (`rel:server/workers/service.ts:1505-1520`). For a fresh
   R1 worker, `G % 1000 = 0` and create at/after `G − 900 s` make `T = G`; selection still requires
   the unclamped original deadline within the signed run (`rel:server/workers/candidate/authority.ts:274-292`).
   §3.6 now reads `T` from the intent, separately from `original_deadline`
   (`rel:server/workers/service.ts:1627-1630`). R1 step 5 still must observe exit 72; whole-second
   alignment does not remove the 71/73 race (`rel:packages/worker-runtime/expiry-observation.ts:373-388`).
9. U9: decided 2026-11-17. The chart values are `networkPolicy.candidateJournalCidrs: ["10.96.0.42/32"]`,
   port 8443 (inert under Cilium, required by the chart), and ailab's selector policy
   `relay-infrastructure-agent-journal-egress` admits agent -> journal on TCP 8443
   ([relay-candidate-run-generator.md](relay-candidate-run-generator.md)). E6 must show exactly one
   agent -> journal connection on 8443 (the rehearsal list in §4).
10. U10 — **Verified:** R6's own §3.4 dump meets the required ordering: backup completes before hub
    startup and the fresh §3.6 create (`ailab:kubernetes/apps/apps/relay/relay.yaml:54-63,174-178`), whereas
    the drill needs an older snapshot missing a live managed environment
    (`main:scripts/qualification/researcher-recovery-fence-observation.md:24-27,61-66`). R6 step 0 now
    distinguishes pre-signing prerequisites from this later capture. At §3.4 retain the receipt's
    `generation`, `createdAt`, `dumpSha256` and `podUid`, and its completed backup generation
    (`ailab:kubernetes/apps/apps/relay/backup.yaml:171-198,224-229`). In R6 step 2 confirm those bytes
    restore in the disposable hub and its inventory lacks R6's fresh environment; read the report's
    nonempty `facts.orphans` afterwards (`main:scripts/qualification/researcher-recovery-fence-observation.ts:618-636`).
    Source establishes eligibility, not a successful restore of a future dump.
11. U11 — **UNVERIFIED:** in E6, run §5.1's exact commands with the operator host's installed `chronyc`
    and the cluster-matching Talos client; retain versions, exit codes, UTC timestamps and output for
    every listed node. Confirm the output exposes synchronization and offset, not just a displayed
    clock; if it does not, the operator must supply and rehearse an equivalent offset readback before
    signing. Repeat at each run's §3.0 and immediately before §3.6; apply §5.1's 250 ms/0.5 s limits.
12. U12 — **UNVERIFIED:** in E6 record monotonic start/finish times and results for every §4 step and
    the window, storage, matrix, egress and replay drivers. Include agent shutdown and restart through
    session issuance, and record failures/retries separately. Fill each `D_x` in §6 from the measured
    elapsed duration with an explicit operator-chosen margin; verify the latest-start formulas and
    create → Pod Running < 5 min before R1. Repeat timings in each run's stopwatch log (§5.5).

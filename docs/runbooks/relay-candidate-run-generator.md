# Relay researcher candidate runs: the per-run generator (S-A, plan, inactive)

Status: **staged, inactive. Nothing here is applied, and merging changes no cluster object.** Owner
decision O6 ([relay#277 comment 125437](https://git.chifor.me/cchifor/relay/issues/277#issuecomment-125437)):
every signed candidate run of the researcher qualification campaign gets **one generated, mechanical
ailab PR (S-A)**; the operator merges it once CI passes and its diff equals the generator's output for
the recorded run digest; **if the hub is not read back by `t_s + 60 min`, abort before the grant**. The
fallback is S-B: the same bytes applied in place under Flux suspension, then committed. The campaign's
per-run ceremony is [relay-researcher-qualification-campaign.md](relay-researcher-qualification-campaign.md)
(§2, §3.4, §3.5); the hub component this fills is H7 of
[relay-researcher-hub-upgrade.md](relay-researcher-hub-upgrade.md#h7-candidate-canary-and-qualification-files-each-its-own-reviewed-component-and-pr).

`kubectl --context admin@ai` throughout. `t_s` is the run's `issuedAt`.

## The pipeline, per run

| Step | Tool | Output | Where |
| --- | --- | --- | --- |
| Sign (§3.1) | Relay `scripts/sign-candidate-run.ts` | `run.json`, its `runDigest` and file `sha256` | operator host |
| Compose the hub document | Relay `scripts/compose-candidate-hub-document.ts` | `document.json` (canonical compact JSON, about 57 KB of the hub reader's 65,536 bytes) | operator host |
| Generate | ailab `scripts/relay-candidate-run.py generate` | the five run-bound git files, three public ConfigMaps, a summary | the per-run branch; the operator directory |
| Create the run's objects | `kubectl create` | the session-key Secret (`relay`), the control ConfigMap (`relay-infra-control`), the slot's trust and pin ConfigMaps (`relay-researcher-1`) | cluster, never git |
| S-A PR, or S-B apply then commit | Gitea; `kubectl apply --server-side` | hub rollout; agent values | git, cluster |
| Readback (H7's) | metrics, log, Pod status | | before the grant, by `t_s + 60 min` |

### 1. Compose the hub document (Relay, offline)

From the pinned driver checkout (Relay `main`), with the run's session-permit private key (the
one the run's `sessionPermitPublicKey` names; a 0600 file or `-` for standard input):

```json
{
  "version": 1,
  "kind": "relay.candidate-hub-document-input",
  "hubOrigin": "https://relay.chifor.me",
  "run": {"path": "/operator/<run>/run.json", "sha256": "<run file sha256>"},
  "binding": {"path": "/operator/binding.json", "sha256": "fbf5cdb3559885683512dd4bb6c91a3f981e18f6571d6bcef001f0945ce6ae39"},
  "pool": {"path": "/operator/pool.json", "sha256": "<sha256>"},
  "egressPolicy": {"path": "/operator/qualification-egress.json", "sha256": "<sha256>"},
  "issuerKeys": [{"id": "<issuerKeyId>", "publicKey": "<raw base64url issuer public key>"}],
  "profiles": [{"templateDigest": "abffc97e804e8069d289d58b945d9623afc9fda2400036d69d51df42fea9f195",
                "profileTemplateDigest": "<the researcher worker profile template digest>"}]
}
```

```sh
node --import tsx scripts/compose-candidate-hub-document.ts /operator/<run>/hub-input.json <sha256> \
  /secure/<run>/permit.pem /operator/<run>/document.json
```

- `pool.json` is the hub's stored pool `definition` for the pool revision the grant names
  (`SELECT definition FROM agent_environment_pools WHERE id='<pool>' AND revision=<rev>;`, as
  `postgres` in `relay-postgres-0`), frozen for the campaign like the binding. The hub compares it
  exactly at admission (`CandidateHubAuthority.permits`).
- `qualification-egress.json` is the installed qualification-stage policy
  (`kubectl -n relay-researcher-1 get networkpolicy relay-researcher-egress -o json`, or
  `installation/qualification-egress.yaml` as JSON); the tool stores its measured form, digest
  `f6d3187c…282f`, which must equal the run's `egressPolicySha256`.
- The tool runs the hub's own parser and `CandidateHubAuthority` constructor on the bytes before
  writing them, with the permit key: a document it writes loads on the v0.10.4 hub, at that moment.
  It prints `runDigest`, `sha256` and `bytes`, never overwrites, and refuses with `Refusal: <code>`.

### 2. Generate (ailab, offline)

On the per-run branch (prepared before signing, §3.0 step 6):

```sh
python3 scripts/relay-candidate-run.py generate \
  --run /operator/<run>/run.json --run-sha256 <run file sha256> \
  --document /operator/<run>/document.json --document-sha256 <document sha256> \
  --journal-ca /operator/journal-ca.pem --operator-out /operator/<run>/objects
```

It writes exactly these files and nothing else (`d16` = the first 16 hex of the run digest):

| File | Run-bound values |
| --- | --- |
| `kubernetes/components/relay-worker-candidate/run/document.json` | the composed document, byte for byte |
| `.../run/kustomization.yaml` | ConfigMap `relay-worker-candidate-<d16>` in `relay` (immutable, no hash suffix) |
| `.../run/deployment.yaml` | `RELAY_WORKER_CANDIDATE_SHA256` = sha256 of `document.json`; that ConfigMap; Secret `relay-worker-candidate-key-<d16>` |
| `kubernetes/apps/relay-researcher/agent/config.json` | `candidateRuntime`: `trustSha256`, `trustConfigMap` = `relay-candidate-trust-<d16>`, `pinConfigMap` = `relay-candidate-pin-<d16>`; `bindingSha256` = `fbf5cdb3…ae39` (the frozen I4 binding), `holderSecret` = `relay-candidate-holder` and the four file paths never change |
| `kubernetes/apps/relay-researcher/agent/deployment.yaml` | the `candidate` volume's ConfigMap = `relay-candidate-control-<d16>` (one line) |

and, with `--operator-out` (a new directory; public data only), the three ConfigMaps the operator
creates: `control-configmap.json` (`relay-candidate-control-<d16>` in `relay-infra-control`:
`binding.json` = the frozen binding bytes, `config.json` = the `CandidateAgentTrust`, `ca.pem` = the
journal CA the run pins, `network-policy.json`), `slot-trust-configmap.json`
(`relay-candidate-trust-<d16>`, the same `config.json`) and `slot-pin-configmap.json`
(`relay-candidate-pin-<d16>`, key `sha256` = `trustSha256`, no newline). The trust bytes are canonical
JSON plus a final newline: what Relay's candidate policy preparation measures, so the Pod annotation
`candidate-trust-sha256` it pins in the per-launch guards equals the agent's `trustSha256`.

Before writing anything, it refuses (`Refusal: <code>`, exit 2) a run or document that disagrees with
the repository: a non-canonical run or a non-compact document; a document whose `run` is not the
run's exact text, or whose issuer keys, destination pins, binding, egress policy, pool target or
profiles differ from the run and the repository; a run whose hub image is not the one `relay.yaml`
deploys, whose namespaces, tenant, hub or journal origin, installation, policy, image, source commit
or role templates are not this installation's; the installation tree not on the qualification
egress stage; a document over 65,536 bytes; a run outside its validity window; and an agent tree
without the one-time candidate structure or whose identity, slot, runtime digests, templates, local
policy digest or installation key disagree with the run.

The summary names the run digest, the five names, `trustSha256`, every written file's SHA-256 and
`abortBeforeGrantIfHubNotReadBackBy` (`t_s + 60 min`). Paste it into the PR body.

### 3. Create the run's objects (never in git)

```sh
kubectl -n relay create secret generic relay-worker-candidate-key-<d16> \
    --from-file=session-key.pem=/secure/<run>/permit.pem --dry-run=client -o yaml \
  | kubectl label --local -f - velero.io/exclude-from-backup=true -o yaml \
  | kubectl create -f -
kubectl -n relay get secret relay-worker-candidate-key-<d16> -o jsonpath='{.data.session-key\.pem}' \
  | base64 -d | sha256sum; sha256sum /secure/<run>/permit.pem             # equal
kubectl create -f /operator/<run>/objects/control-configmap.json
kubectl create -f /operator/<run>/objects/slot-trust-configmap.json
kubectl create -f /operator/<run>/objects/slot-pin-configmap.json
```

Create the Secret **before** the hub rollout: the Pod waits in `ContainerCreating` without it (the
volume is not optional), and the hub is down meanwhile (Recreate). The slot ConfigMaps must exist
before the per-launch bundle's guards freeze them (§3.7). The holder Secret `relay-candidate-holder`
(`holder-key.pem`, `ca.pem`) is created once for the campaign.

### 4a. S-A: the per-run PR

Commit the five files, open the PR right after signing with label `no-automerge` (campaign runbook
§2), and paste the summary. CI's `script-tests` runs `scripts/tests/test_relay_candidate_run.py`,
which regenerates every run-bound value from the committed `run/document.json` (it carries the signed
run) and fails on any difference in them. **The merge gate** is stricter, and the reviewer and the
operator run it on the PR branch: it runs the generator on the base revision's files with the
committed run and requires the branch to equal that output and to change nothing else:

```sh
git fetch origin main && git merge --no-edit origin/main     # the branch must contain the base tip
python3 scripts/relay-candidate-run.py check --base origin/main --run-digest <runDigest recorded at t_s>
# exit 0, "equal": true, "firstRun": false, "hubReadbackDeadlinePassed": false
```

It reads both revisions from git's tree objects (uncommitted changes never count) and refuses with
`base-not-in-branch` when main has moved on since the branch was cut (merge it and re-run; the run's
values are checked against the new base, so a hub image bump there is caught), and with `run-expired`
for a run past its `expiresAt` (merging it would only roll the hub to log
`worker_candidate_unavailable`). Past `t_s + 60 min` it fails (exit 1, `hubReadbackDeadlinePassed:
true`): abort the run before the grant instead of merging.

R1's PR is H7's inclusion PR: its base still has the placeholder `run/`, so the check reports
`"firstRun": true`, checks the generated values for self-consistency and lists every other changed
path under `reviewByHand` (the overlay line, the activation-gate edit and, if it lands there, the
agent's one-time structure), which are reviewed like any H-step change.

The operator merges once CI, the §3.2-§3.3 readbacks and that check pass. Flux then rolls the hub
(Recreate: `backup-database` up to 1800 s, `backup-artifacts`, `migrate`).

### 4b. S-B: in place, then commit the same bytes

Under `flux suspend kustomization apps -n flux-system` (as in hub step H3), render the hub overlay
from the per-run branch with the pinned kustomize of `scripts/manifest-lint.sh` (the CI render), keep
only the run's ConfigMap and the `relay` Deployment (the overlay also renders SOPS-encrypted Secrets,
which only Flux can apply), and apply them server-side; then commit the same five files and resume
once git equals the live objects:

```sh
docker run --rm -v "$PWD:/repo:ro" -w /repo <KUSTOMIZE_IMAGE of scripts/manifest-lint.sh> build kubernetes/apps/apps/relay \
  | python3 -c 'import sys,yaml; print(yaml.safe_dump_all([d for d in yaml.safe_load_all(sys.stdin) if d and (d["kind"], d["metadata"]["name"]) in {("ConfigMap", "relay-worker-candidate-<d16>"), ("Deployment", "relay")}]))' \
  > /operator/<run>/hub-apply.yaml
kubectl apply --server-side --field-manager=kustomize-controller -f /operator/<run>/hub-apply.yaml
```

### 4c. The agent half, in place in either mode (campaign runbook §3.5)

The agent must not claim the create before the per-launch bundle is installed, so its values are
applied in place at 0 replicas, from the same branch, before the grant:

```sh
flux suspend kustomization relay-researcher-agent -n flux-system
kubectl -n relay-infra-control scale deploy/relay-infrastructure-agent --replicas=0
docker run --rm -v "$PWD:/repo:ro" -w /repo <KUSTOMIZE_IMAGE of scripts/manifest-lint.sh> build kubernetes/apps/relay-researcher/agent \
  | python3 -c 'import sys,yaml; d=[x for x in yaml.safe_load_all(sys.stdin) if x]; [x["spec"].update(replicas=0) for x in d if x["kind"] == "Deployment"]; print(yaml.safe_dump_all(d))' \
  > /operator/<run>/agent-apply.yaml
kubectl apply --server-side --field-manager=kustomize-controller -f /operator/<run>/agent-apply.yaml
```

Readback: the Deployment has `replicas: 0` and its `candidate` volume names
`relay-candidate-control-<d16>`; the `config.json` of the ConfigMap its `config` volume names has the
SHA-256 of the branch's `kubernetes/apps/relay-researcher/agent/config.json`; the three run
ConfigMaps of step 3 exist. Scale the agent to 1 only after the per-launch bundle (§3.7-§3.8), and
resume `relay-researcher-agent` only once the per-run PR has merged and git equals the live agent
(§3.10 step 8).

### 5. Readback (H7's), by `t_s + 60 min`

- The hub Pod's init container `candidate-session-key` terminated with exit code 0; the `relay`
  container's `RELAY_WORKER_CANDIDATE_SHA256` equals the summary's `document.sha256`, and its
  `worker-candidate-document` volume names `relay-worker-candidate-<d16>`.
- The log has **no `worker_candidate_unavailable`** (its `reason` would be
  `document-file-untrusted`, `document-invalid`, `signer-file-untrusted`, `signer-invalid` or
  `authority-invalid`), and no `worker_budget_authority_unavailable`; budget startup `started`;
  `RelayWorkerBudgetStartupFailed` clear.
- Otherwise: **never grant or create**. Past `t_s + 60 min`, abort the run before the grant (it does
  not count toward the eight, owner decision O1).

## Why the session key is copied by an init container

Relay v0.10.4 reads the document with `readCandidateFile(path, {sha256, trustedUid: 0})` and the key
with `readCandidateFile(path, {secret: true})` (`packages/worker-runtime/candidate-files.ts`): an
absolute canonical path opened with `O_NOFOLLOW` whose `/proc/self/fd` realpath is the same path, a
regular file with one link and at most 65,536 bytes, an owner in `[0, trustedUid ?? process UID]`, and
a mode without group/other write (`0o022`), or for a secret without any group/other bit (`0o077`).
The hub runs as UID 1000, GID 1000, Pod `fsGroup` 1000.

- **Document, a ConfigMap subPath file:** kubelet's atomic writer creates `..<timestamp>/document.json`
  as root; with `fsGroup` it changes the group to 1000 and ORs `0440` into the mode of a read-only
  volume (`pkg/volume/volume_linux.go`), so `defaultMode: 0444` stays `0444`. A subPath mount
  bind-mounts the resolved file: the container sees a regular file at the mount path, not the
  `..data` symlink, one link, `root:1000 0444`. Accepted. A directory mount would put the symlink
  at the path and fail `O_NOFOLLOW`.
- **Key, not from a Secret volume:** every Secret file is owned by root, and no volume option sets
  another owner. Under `fsGroup` it is at least `0440` (refused: group bits); without the group bits
  UID 1000 cannot read a root-owned `0400` file. So the init container `candidate-session-key` (the
  Pod's UID 1000, read-only root, no capabilities) runs `install -m 0400` from the Secret, which only
  it mounts, into a Memory `emptyDir`: `1000:1000 0400`, one link. Accepted; the relay container
  mounts that directory read-only.

Offline evidence (2026-11-16): Relay v0.10.4 `e77bd09a`'s own `loadCandidateHubAuthority`, unmocked, in
the pinned node image as UID 1000 with these exact attributes (a single-file bind mount standing in
for the subPath), loaded a document composed by `compose-candidate-hub-document.ts` with the real
binding: `root:1000 0444` document plus the copied `1000:1000 0400` key → loaded; the key straight
from a Secret under `fsGroup` (`root:1000 0440`) → `signer-file-untrusted`; from a Secret without
`fsGroup` (`root:root 0400`) → `signer-file-untrusted`; the document through a directory mount (the
`..data` symlink) → `document-file-untrusted`.

## One-time prerequisites

- **H7's inclusion** is the first run's (R1's) per-run PR: it also adds
  `- ../../../components/relay-worker-candidate` to `kubernetes/apps/apps/relay/kustomization.yaml` and
  removes the H7 group from `scripts/tests/test_relay_v060_activation_gate.py`. Later runs change only
  the five generated files.
- **The agent's one-time candidate structure** (installation runbook, "Beyond enrollment" item 3),
  which the generator requires and never writes: `config.json` as imported at I5 with
  `policy.bootstrapSecrets: true`; in `deployment.yaml` the chart's four `candidate` mounts
  (`/run/relay/candidate-binding.json`, `-trust.json`, `-ca.pem`, `-network-policy.json`, subPaths
  `binding.json`, `config.json`, `ca.pem`, `network-policy.json`, read-only) and the `candidate`
  volume, in block style with `name:` on the line after `configMap:`, `defaultMode: 0444`,
  `optional: false` and those four items; plus the journal egress and the agent's namespace and
  NetworkPolicy reads. Without a run's control ConfigMap the structure cannot mount, so it lands
  with R1's PR at the latest (listed under `reviewByHand` there), under the agent Kustomization's
  suspension; the generator then owns only the values above.
- **The qualification egress stage** (installation runbook Q4) selected in the installation tree.

The installation's I5 checks accept exactly this overlay while a run is committed, so R1's PR and every
later run's PR pass them unchanged. `scripts/relay-researcher-bundle.py` (and with it
`just relay-researcher-reproduce`) compares the committed agent config with this generator's
`candidate_config()` of the I5 import, which sets `policy.bootstrapSecrets` true and adds the run's
`candidateRuntime` and nothing else, and renders the pinned chart with the matching candidate values.
It accepts the campaign only while this generator's `check` passes on the committed tree. Its tests
pin the chart's candidate RBAC (the two namespaces' reads, the slot egress policy read, the slot's
bootstrap Secrets), the four mounts and the volume, and one journal egress rule (private CIDRs off the
LAN, port 8443). Any other difference in the agent tree still fails.

**U9, decided by the owner on 2026-11-17.** The chart values are `networkPolicy.candidateJournalCidrs:
["10.96.0.42/32"]` and `candidateJournalPort: 8443`, set with the rest of the one-time candidate
structure in R1's PR. Under Cilium that rule admits nothing: an `ipBlock` never selects a Cilium-managed
Pod, and the Service address is translated to the journal Pod before policy. The chart still requires it
(it refuses `candidateRuntime` without `candidateJournalCidrs`). What admits agent -> journal is ailab's
own `relay-infrastructure-agent-journal-egress` in `kubernetes/apps/relay-researcher/agent/network-policy.yaml`:
egress from the agent Pod to the `relay-candidate-journal` Pods (namespace `relay-candidate-journal`, label
`app: relay-candidate-journal`) on TCP 8443 only, the selector the slot's qualification egress uses. It
is already committed, outside the chart comparison, like the agent's API-server rule.

## After the campaign

Relay's design removes the candidate authority before the ordinary canary: one PR drops the
component from the overlay and runs `python3 scripts/relay-candidate-run.py reset` (the placeholder
`run/`). Delete each run's session-key Secret once a later rollout no longer mounts it, and the
control and slot ConfigMaps as the campaign runbook's §3.10 step 7 says.

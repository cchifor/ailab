# kube-apiserver audit policy for the researcher delayed-start run (R7)

Owner decision O7 (relay#277 comment 125437): `submittedPod` comes from the offline reconstruction
(Relay #530) for runs R1-R6, and R7 alone gets an API server audit policy, in its own owner-held PR. This
runbook applies that policy, verifies it, extracts R7's event, and takes the policy out again. Merging
changes nothing live: the control planes' machine config is OpenTofu, applied by hand. Applying it and
removing it are operator steps, each approved by the owner.

## Why R7 needs it

R7 observes `delayed-start-after-absolute-expiry-zero-launch` with Relay #532's
`scripts/qualification/researcher-delayed-start-observation.ts`
(`docs/qualification/researcher-delayed-start-observation.md` in Relay). The candidate Pod is held
unscheduled past its absolute expiry, so it never redeems bootstrap and never gets a session. Without a
session, the only record of which Pod the agent created is the API server's own answer to the create.
The driver's `createAudit` input is that answer: the `ResponseComplete` audit event of the create, logged
at `RequestResponse`. The preflight checks that:

- the user is `system:serviceaccount:relay-infra-control:relay-infrastructure-agent`, with no
  `impersonatedUser`;
- `objectRef` is `pods`, `v1`, namespace `relay-researcher-1`, the submitted Pod's name, no subresource;
- `responseStatus.code` is 201;
- `requestObject` and `responseObject` match the submitted Pod, up to documented Kubernetes defaults;
- `responseObject.metadata.uid` becomes the created Pod's UID, which every later read must return.

Talos's default policy logs everything at `Metadata`: no request or response body, so no UID
(`docs/runbooks/relay-researcher-qualification-campaign.md` §8 G2).

## The policy

`kubernetes/infra/machine-config/controlplane.yaml.tftpl`, `cluster.apiServer.auditPolicy`, pinned by
`scripts/tests/test_talos_apiserver_audit_policy.py`:

| # | Rule | Effect |
| --- | --- | --- |
| 1 | `Metadata` for `secrets`, `serviceaccounts/token` and `authentication.k8s.io` `tokenreviews` | No body for Secret or token material, whatever a later rule says. Same level and stages as rule 3, so nothing changes today. |
| 2 | `RequestResponse` for `create` of core `pods` in `relay-researcher-1`, `omitStages: [RequestReceived]` | **The change.** One `ResponseComplete` event per create, with `requestObject` and `responseObject`. A create never has `ResponseStarted`. A handler panic is still logged (`Panic`), as it is today. |
| 3 | `Metadata` | Talos's default, unchanged. Every other request keeps its level and its stages. |

- **Before this change**, the effective policy was Talos's default, `rules: [{level: Metadata}]`.
  Config contract v1.11.2 generates it into this very field, and the field merges as "replace". The
  block is therefore the whole policy, not rules appended after the default. The deprecated v1alpha1
  field stays until the contract migration (P8). Under contract v1.11.2 a `KubeAuditPolicyConfig`
  document conflicts with the generated field, and `talosctl` refuses it ("audit policy config is already
  set in v1alpha1 config").
- **"For R7 only" means only R7 consumes it.** While it is live, every Pod create in the slot is
  logged with its body: R1-R6's creates, refused attempts (for example the probe below), and R7's.
  The rule cannot be bounded in time. A candidate Pod body carries no secret: the bootstrap material
  is a Secret volume reference, and Relay's renderer never sees its bytes. Secret creates in the slot
  stay at `Metadata` (rule 1).
- **Not limited to the agent's user.** The policy could add
  `users: [system:serviceaccount:relay-infra-control:relay-infrastructure-agent]`, but then the
  operator could verify it only by minting a token for the agent. Rule matching uses the
  authenticated user, not an impersonated one, so a `kubectl --as=<agent>` probe lands at `Metadata`
  (checked on the disposable API server below). Admission already restricts the
  slot to the agent: the enrollment gate refuses every Pod, and the per-launch policies admit exactly
  the launch's Pod. The driver checks the user itself.
- **Where it is written.** Each control plane writes only the requests it served, to
  `/var/log/audit/kube/kube-apiserver.log`, with Talos's fixed flags `--audit-log-maxsize=100` (MB),
  `--audit-log-maxbackup=10` and `--audit-log-maxage=30` (days). Rotated files sit beside it as
  `kube-apiserver-<time>.log`. The agent reaches the API through the `kubernetes` Service, so any of
  the three can serve the create: always read all three.

## When to apply and when to remove

- **Apply before the E6 rehearsal**, so that E6 checks the live policy. Never apply during a run window.
- **Keep it through R7 and any R7 retry on a reserve run.**
- **Remove it after the canary**, in a quiet window: nothing consumes the event after that, and the
  campaign freezes every subject pin until the canary.
- Each apply and each removal restarts kube-apiserver once on every control plane, one at a time.
- The alternative, applying just before R7 and removing right after it, costs two more rolling
  restarts inside the campaign, and E6 would not see the live policy.

## Apply, one control plane at a time

Applying it restarts **kube-apiserver only**, on the node it is applied to. The static pod's config
version annotation moves, so the kubelet replaces the container. There is no reboot, and etcd,
kube-controller-manager and kube-scheduler keep running. Talos 1.14 applies every config change live,
and `talos.tf` uses `apply_mode = "no_reboot"`. The other two API servers keep answering while one restarts:
expect refused connections to that one node only, for about a minute (the measured figure in
[roots-of-trust-rotation.md](roots-of-trust-rotation.md)).

**Never apply this with an untargeted apply.** `talos_machine_configuration_apply.cp` covers all three
control planes in one apply, so all three API servers would restart at once.

`just plan` and `just apply` drive the `tofu/` root (the Proxmox storage), not `kubernetes/infra`.
Run OpenTofu in `kubernetes/infra` of the main checkout, on the Windows workstation in Git Bash, where
its state lives. `tofu_cp` below changes into that directory instead of passing `-chdir`, because
`MSYS_NO_PATHCONV=1` would hand the Windows binary an unconverted POSIX path.
Native `jq.exe` ends its lines with CRLF, so the commands strip `\r` wherever a jq value is reused.

```bash
OUT="$(cd "$(git rev-parse --git-common-dir)/.." && pwd -P)/kubernetes/infra/_out"
export TALOSCONFIG="$OUT/talosconfig" MSYS_NO_PATHCONV=1
T="$OUT/talosctl-1142.exe"; K="kubectl --context admin@ai"
tofu_cp() { (cd "$OUT/.." && "$HOME/.tofubin/tofu.exe" "$@"); }
W="$OUT/r7-audit"; mkdir -p "$W"
CPS="192.168.0.41 192.168.0.42 192.168.0.43"; ALL=192.168.0.41,192.168.0.42,192.168.0.43
declare -A KEY=([192.168.0.41]=cp1 [192.168.0.42]=cp2 [192.168.0.43]=cp3)
declare -A POD=([192.168.0.41]=kube-apiserver-talos-cp1 [192.168.0.42]=kube-apiserver-talos-cp2 [192.168.0.43]=kube-apiserver-talos-cp3)
# start time of $1's kube-apiserver container, asked through ANOTHER control plane (the VIP may sit on $1)
started(){ local o; for o in $CPS; do [ "$o" != "$1" ] && break; done
           $K --server "https://$o:6443" -n kube-system get pod "${POD[$1]}" -o jsonpath='{.status.containerStatuses[0].state.running.startedAt}'; }
rules()  { "$T" -n "$1" get auditpolicyconfigs audit-policy -o jsonpath='{.spec.config.rules}'; echo; }
# probe <cp>: a server-side dry-run Pod create in the slot, sent to that control plane only. Nothing is
# persisted (dry run), and the slot's admission refuses it anyway. Prints that CP's audit events for it.
probe()  { local name="r7-audit-probe-${1##*.}-$(date +%s)" id
           $K --server "https://$1:6443" -n relay-researcher-1 create --dry-run=server -f - <<POD 2>&1 | tail -1
apiVersion: v1
kind: Pod
metadata: {name: $name}
spec:
  automountServiceAccountToken: false
  securityContext: {runAsNonRoot: true, runAsUser: 65532, seccompProfile: {type: RuntimeDefault}}
  containers:
  - name: probe
    image: registry.k8s.io/pause:3.10
    securityContext: {allowPrivilegeEscalation: false, capabilities: {drop: [ALL]}}
    resources: {limits: {cpu: 10m, memory: 16Mi, ephemeral-storage: 16Mi}}
POD
           sleep 2
           "$T" -n "$1" read /var/log/audit/kube/kube-apiserver.log > "$W/probe.log"
           # a create's RequestReceived event has no name yet: find the request by name, list it by auditID
           id=$(grep -F "\"name\":\"$name\"" "$W/probe.log" | jq -r .auditID | tr -d '\r' | sort -u)
           grep -F "\"auditID\":\"$id\"" "$W/probe.log" |
             jq -c '{level, stage, code: .responseStatus.code, body: (.requestObject != null), dryRun: (.requestURI | test("[?&]dryRun="))}'
           rm -f "$W/probe.log"; }
```

**Before the first control plane:**

```bash
"$T" -n $ALL etcd status                                    # 3 members, same raft index, no errors
for c in $CPS; do echo "$c $($K --server https://$c:6443 get --raw /readyz)"; done   # ok on all three
for c in $CPS; do echo "$c $(rules $c)"; done              # one rule, level Metadata, on all three
for c in $CPS; do probe $c; done
#   per CP two lines, Metadata, body false: stage RequestReceived (code null), then ResponseComplete (403)
for c in $CPS; do "$T" -n $c ls -l /var/log/audit/kube; done   # keep: the backups' times give the rotation rate
```

**For each control plane in turn** (`cp=192.168.0.41`, then `.42`, then `.43`):

```bash
"$T" -n $ALL etcd status                                    # 3/3 in sync, before EVERY control plane
tofu_cp plan -target="talos_machine_configuration_apply.cp[\"${KEY[$cp]}\"]"
#   the only change: that CP's config_patches gain the cluster.apiServer.auditPolicy block (plus the
#   template comment). machine_configuration_input must not change (it would show as a sensitive
#   value), and nothing else may change. Otherwise: stop.
before=$(started $cp)
tofu_cp apply -target="talos_machine_configuration_apply.cp[\"${KEY[$cp]}\"]"
until s=$(started $cp) && [ -n "$s" ] && [ "$s" != "$before" ]; do sleep 3; done
until [ "$($K --server https://$cp:6443 get --raw /readyz 2>/dev/null)" = ok ]; do sleep 3; done
rules $cp                                                   # the three rules of the table above
"$T" -n $cp read /system/config/kubernetes/kube-apiserver/auditpolicy.yaml   # the file it loaded: the same rules
"$T" -n $ALL etcd status                                    # still 3/3 (etcd was not touched)
probe $cp
#   exactly ONE line: {"level":"RequestResponse","stage":"ResponseComplete","code":403,"body":true,"dryRun":true}
#   (403: the slot's admission refused it; 404 if the namespace does not exist yet. Either way the
#   policy is in effect.) Two Metadata lines: the policy is not loaded; stop.
```

Wait about 5 minutes and check that `$K get --raw /readyz` stays `ok` through the VIP before the next
control plane. After the third, `tofu_cp plan` with no target shows no changes.

**Readback for the owner:** per control plane, the `rules` output (three rules), the restarted
kube-apiserver's new `startedAt`, `etcd status` 3/3, and the `probe` line above.

## Verify: probe, then the E6 rehearsal

The `probe` above is the live check. It is a harmless dry-run create in the slot, recorded by this
policy on each control plane in turn. The rehearsal Pod of the E6 rehearsal's R7 item ("the audit
event's shape (O7)") lives in `relay-delayed-start-rehearsal`, which this policy does not cover. So
E6 does two things:

- It runs `probe` on every control plane.
- It rehearses the extraction below on one probe. Set `POD` to the probe's name. In the jq filter,
  replace `(.requestURI | test("[?&]dryRun=") | not)` with `(.requestURI | test("[?&]dryRun="))`.
  Expect exactly one line, `403`, from the probed control plane and none from the others.

That rehearses reading all three logs and the selection. The `201` shape and #532's checks of it
were run against a disposable API server (below). The live `201` is first seen in R7 itself, at
least two minutes before `T`.

## Extract R7's event

Do this in R7's step 4 ("capture the inputs before T"), as soon as the agent's create has returned.
`POD` is the submitted Pod's name.

```bash
POD=$(jq -r .metadata.name /operator/r7/submitted-pod.json | tr -d '\r'); NS=relay-researcher-1
for c in $CPS; do
  "$T" -n $c read /var/log/audit/kube/kube-apiserver.log | grep -F "\"name\":\"$POD\"" > "$W/r7-$c.jsonl"
done
for c in $CPS; do
  jq -r --arg ns "$NS" --arg pod "$POD" 'select(.verb == "create" and .stage == "ResponseComplete"
      and .objectRef.resource == "pods" and .objectRef.subresource == null and .objectRef.namespace == $ns
      and .objectRef.name == $pod and (.requestURI | test("[?&]dryRun=") | not))
    | "\(.auditID) \(.level) \(.responseStatus.code) \(.responseObject.metadata.uid // "-") \(.user.username) \(.impersonatedUser.username // "-")"' \
    "$W/r7-$c.jsonl" | sed "s/^/$c /"
done | tee "$W/r7-hits.txt"
```

- **Expect exactly one line**:
  `<cp> <auditID> RequestResponse 201 <uid> system:serviceaccount:relay-infra-control:relay-infrastructure-agent -`.
- **Other outcomes:**
  - A line that is not `201` is a refused attempt; look for the `201`.
  - No line on any control plane: the create was not recorded. Check `rules` on all three, and
    `"$T" -n $c ls -l /var/log/audit/kube` for a rotation since the create; read any newer
    `kube-apiserver-<time>.log` the same way. Without the event, R7 cannot bind its Pod: abort before `T`.
  - Two `201` lines for one name: stop. Something deleted and re-created the Pod.
- **Write the file.** Copy that one line unchanged, as the API server logged it:

```bash
read -r CP ID _ < <(grep ' 201 ' "$W/r7-hits.txt")
grep -F "\"auditID\":\"$ID\"" "$W/r7-$CP.jsonl" > /operator/r7/create-audit-event.json
wc -l < /operator/r7/create-audit-event.json                # 1
```

**Check it with #532's own code**, from the pinned driver checkout. First the strict parser the
preflight uses: 1 MiB, no duplicate keys, safe integers, depth 16. Then the preflight itself, which
applies every `createAudit` check above once the config pins this file:

```bash
node --import tsx --input-type=module -e 'import { readFileSync } from "node:fs";
  import { parseKubernetesObservationJson } from "./packages/worker-runtime/candidate-signatures.ts";
  const e = parseKubernetesObservationJson(readFileSync(process.argv[1], "utf8"));
  console.log(e.level, e.stage, e.verb, e.objectRef.namespace, e.objectRef.name, e.responseStatus.code,
    e.user.username, e.impersonatedUser ? "IMPERSONATED" : "-", e.responseObject.metadata.uid)' \
  /operator/r7/create-audit-event.json
# RequestResponse ResponseComplete create relay-researcher-1 <POD> 201 system:serviceaccount:relay-infra-control:relay-infrastructure-agent - <uid>
sha256sum /operator/r7/create-audit-event.json             # the config's createAudit.sha256
node --import tsx scripts/qualification/researcher-delayed-start-observation.ts preflight /operator/r7/delayed-start.json <config-sha256>
```

Then compare the hub's pinned UID (`infrastructure_environment_objects.object_uid` for the environment)
with the event's UID, and record both. Retain `create-audit-event.json` with R7's evidence. Delete `$W`'s
`r7-*.jsonl` afterwards: they hold other requests' metadata.

**How long the event survives on the node.**
- **Active file.** The event stays in the active file until that file reaches 100 MB and rotates.
- **Backups.** It then sits in a backup until ten newer backups push it out. A backup is also deleted
  at a later rotation once it is more than 30 days past its own rotation time.
- **This cluster's rate.** The `ls -l` taken before the apply gives the rotation period. After the
  event leaves the active file, expect it to last about ten rotation periods.
- **Extract within minutes, before `T`.** The node copy is only a source. The pinned file is the evidence.

## Event shape: checked against a disposable API server

On 2026-11-16, before this PR, a Docker-only `registry.k8s.io/kube-apiserver:v1.35.0` with etcd 3.6.6
was run (ailab runs 1.35.9). It had no kubelet, scheduler or controller-manager. It used the policy
exactly as `talosctl` v1.14.2 renders it from this template onto a contract-v1.11.2 config, the
namespaces' Pod Security labels, the `relay-gvisor` RuntimeClass and the enrollment gate. Relay #532 was
at its head `5957f067a`. It merged as `1ea5ecc9` with the same driver, comparator and parser.

- **The R7 class.** A Pod create in the slot with a real ServiceAccount token produced **one** event:
  `RequestResponse`, `ResponseComplete`, no `RequestReceived`. About 22 KB, user the agent, no
  `impersonatedUser`, `objectRef` `{resource: pods, namespace, name, apiVersion: v1}`, code 201,
  `responseObject.metadata.uid` set. The extraction above, run on that log, wrote a file byte-identical
  to the captured line.
- **#532's full preflight** (`prepareResearcherDelayedStart`, its fixture inputs with this real event as
  `createAudit`, the Pod completed with the fields the real renderer sets) **accepted it**.
  `createdPodUid` equalled the response UID.
- **The real renderer's Pod.** The Pod from `prepareCandidatePolicy`, clamped and frozen as the agent
  posts it, matched both the logged `requestObject` and `responseObject` under `submittedPodMatches`.
- **Defaulting.** `requestObject` is the request **after decoding and defaulting, before admission**.
  - Added: `dnsPolicy`, `schedulerName`, `terminationMessagePath` and `terminationMessagePolicy`, an
    empty `status`, and `requests` copied from `limits` where only limits were set.
  - Dropped: `false` host namespaces and empty lists.
  - Admission's changes appear only in `responseObject`: `serviceAccountName`, `priority`,
    `preemptionPolicy`, and `uid`, `resourceVersion`, `generation`, `creationTimestamp` and
    `managedFields`.
  - #532's comparator accepts each of these. A real Pod therefore matches only if, like the renderer's,
    it sets `requests` and every list it relies on.
- **Every other class stayed `Metadata` with all its stages:**
  - a Secret create in the slot (its value appears nowhere in the log);
  - TokenRequests (no token in the log);
  - a Pod create in another namespace;
  - reads and watches.

  The probe produced one `RequestResponse` event with code 403 and a `Status` response. Under
  Talos's default policy it produced the two `Metadata` events documented above.
- **A user-scoped rule** (`users: [<agent>]`) logged the agent's own create at `RequestResponse`. The
  same create made by an administrator with `--as=<agent>` stayed at `Metadata`.

What stays for the live cluster: the policy on Talos itself (the readbacks above), the real agent's
create, and which control plane serves it.

## Rollback and removal

- **Planned removal (after the canary), or rollback of a healthy apply:** revert the template block in
  a PR, then roll it exactly like the apply: per control plane, plan and apply with `-target`, wait
  for the container, `readyz`, `etcd status`. `rules` is back to one rule, level `Metadata`, and `probe`
  prints two `Metadata` lines. Each control plane restarts kube-apiserver once more.
- **A control plane whose kube-apiserver does not come back after the apply:** stop the roll. The
  Talos API does not depend on kube-apiserver, so on **that** control plane put back the default
  policy. It replaces the whole field, exactly as generated: checked with `talosctl machineconfig patch`
  v1.14.2 against a contract-v1.11.2 config.

  ```bash
  printf 'cluster:\n  apiServer:\n    auditPolicy:\n      apiVersion: audit.k8s.io/v1\n      kind: Policy\n      rules:\n        - level: Metadata\n' > "$W/audit-default.yaml"
  "$T" -n $cp patch mc --patch @"$W/audit-default.yaml" --mode=no-reboot
  ```

  Then wait for `readyz` and check `etcd status` and `rules`. Read the failed container's log through another
  control plane: `$K --server https://<other>:6443 -n kube-system logs ${POD[$cp]} --previous`. Freeze
  `kubernetes/infra` applies until the template is reverted or fixed: the next apply would re-add the block.

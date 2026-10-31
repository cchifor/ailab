#!/usr/bin/env bash
# Live sandbox-boundary CANARY harness — the ADR-0019 §Verification gate proof for
# `privilege_hardening: v1.1`. It runs the boundary MATRIX against the ACTUAL flipped sandbox on the
# `ai` cluster and asserts every ADR-0018/0019 threat-model breach vector FAILS:
#
#   STATIC   — kata RuntimeClass present; the two egress CiliumNetworkPolicies + the namespace
#              default-deny present; the sandbox ns is SEPARATE from the orchestrator + restricted-PSA;
#              the sandbox SA is tokenless with NO RoleBinding (no OpenBao/kube-apiserver reach).
#   ADMISSION— the pinned pod-shape VAPs actually REJECT breaches (privileged, automount-token,
#              hostNetwork, a NON-kata runtimeClass, a hostPath volume) via server-side dry-run — so
#              Kata + the credential-free shape are ENFORCED, not merely what our canary happens to use.
#   EGRESS   — an AGENT-tier canary: every denied target (world / metadata / alt-DNS / other-pool broker
#              / OpenBao) is unreachable AND its OWN pool broker (by pod IP) IS reachable (the positive
#              control that defeats a netless false-pass). A TEST-tier canary: ZERO egress — even the
#              broker is unreachable (the `test_cmd`/`setup_cmd` `--network none` equivalent).
#   KATA/PROC— the agent canary's guest kernel differs from the host node's (microVM isolation) and its
#              /proc/1/environ carries no orchestrator credential (separate PID namespace).
#
# DISPOSABLE + SAFE: runs ONLY against the canary's own identity tenant-zero/boundary-canary (pool canary,
# PVC af-sbx-ws-tenant-zero-boundary-canary; no tenant, orchestrator, seat or capability exists for it)
# with a fresh random job-id subPath. Every other workspace is REFUSED, with no override: platform-dev
# drives the real cchifor/ailab + cchifor/platform repos, and the retired playground's PVC is deleted. Both
# canary Jobs are TORN DOWN afterwards. A green run is the standing proof v1.1 is safe; ANY breach is a
# P0 — STOP and escalate with the reproduction. (The retired playground's PV/PVC pairs were deleted on
# 2026-10-31, #1505.)
#
# Brokers: the agent tier's POSITIVE control is a Ready pod of AF_CANARY_BROKER (default
# broker-openai-codex, the one broker whose CNP admits the canary identity, rule 1c), and its new
# NEGATIVE control is a Ready pod of AF_CANARY_DENY_BROKER (default broker-anthropic-max1, which does not
# admit it): reaching that pod's IP is a BREACH of the per-identity broker ingress.
#
# Usage:  scripts/verify-sandbox-boundary.sh            # full matrix against admin@ai / boundary-canary
#         KUBECTL_CONTEXT=admin@ai scripts/verify-sandbox-boundary.sh
#         AF_CANARY_KEEP=1 scripts/verify-sandbox-boundary.sh   # keep the canary Jobs for inspection
#         AF_CANARY_IMAGE=<repo>@sha256:<64 hex> ...           # pin the sandbox image explicitly
#         AF_CANARY_PREFLIGHT_ONLY=1 ...   # resolve workspace, image and brokers, print them, stop
# Test seams (scripts/tests/test_verify_sandbox_boundary.py): AF_CANARY_KUBECTL and AF_CANARY_CURL replace the
# kubectl and curl binaries;
# AF_CANARY_FAKE_RUN=<dir> reads the two canary logs from <dir>/{agent,test}.log and the agent pod's
# "<node> <runtimeClass>" from <dir>/agent.meta instead of running Jobs. A fake run is NEVER a proof: it
# says so, and it ends with exit 3 even when every assertion passes.
set -uo pipefail
cd "$(dirname "$0")/.."

KCTX="${KUBECTL_CONTEXT:-admin@ai}"
NS="agentforge-sandbox"
ORCH_NS="agentforge"
BROKER_NS="agentforge-broker"
ORG="tenant-zero"
WS="boundary-canary"
POOL="canary"
POS_BROKER="${AF_CANARY_BROKER:-broker-openai-codex}"
DENY_BROKER="${AF_CANARY_DENY_BROKER:-broker-anthropic-max1}"
K=("${AF_CANARY_KUBECTL:-kubectl}" --context "$KCTX")

pass=0 fail=0
ok()   { echo "  PASS  $*"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $*"; fail=$((fail+1)); }
info() { echo "  ..    $*"; }
die()  { echo "FATAL: $*" >&2; exit 2; }

# ---- 0. preflight + disposable-target guard ------------------------------------------------
# The canary mounts a REAL workspace PVC and writes a job-id subPath, so it runs ONLY on its own
# disposable identity. This is checked before any kubectl call, and there is deliberately no override.
if [ -n "${AF_CANARY_WORKSPACE:-}" ] && [ "$AF_CANARY_WORKSPACE" != "$WS" ]; then
  die "REFUSING workspace '$AF_CANARY_WORKSPACE' — the canary runs only on its own identity $ORG/$WS (no override)"
fi
"${K[@]}" version -o json >/dev/null 2>&1 || die "kubectl context $KCTX unreachable"
PVC="af-sbx-ws-${ORG}-${WS}"
"${K[@]}" -n "$NS" get pvc "$PVC" >/dev/null 2>&1 || die "workspace PVC $PVC not found in $NS"

# The sandbox image: AF_CANARY_IMAGE, else the AF_SANDBOX_IMAGE every live orchestrator (af-tenant-*
# namespaces) runs, else the committed canary's digest. A failed discovery, disagreeing orchestrators or
# a non-digest value is FATAL, never a silent fallback: a green run on a stale image proves nothing
# about the image the agents use.
COMMITTED_IMAGE="$(grep -oE 'registry[^ ]*sandbox@sha256:[0-9a-f]+' kubernetes/apps/infrastructure/agentforge-sandbox/egress-canary.yaml | head -1)"
if [ -n "${AF_CANARY_IMAGE:-}" ]; then
  IMAGE="$AF_CANARY_IMAGE"; IMAGE_SRC="AF_CANARY_IMAGE"
else
  # "declared:" marks every AF_SANDBOX_IMAGE env entry, so one set through valueFrom (or set empty) is seen
  # and refused, never mistaken for "no orchestrator here".
  found="$("${K[@]}" get deploy -A -o jsonpath='{range .items[*]}{.metadata.namespace}/{.metadata.name}{"\t"}{range .spec.template.spec.containers[*].env[?(@.name=="AF_SANDBOX_IMAGE")]}{"declared:"}{.value}{end}{"\n"}{end}' 2>/dev/null)" \
    || die "image discovery failed: could not list Deployments (kubectl error)"
  live="$(printf '%s\n' "$found" | awk -F'\t' '$1 ~ /^af-tenant-/ && $2 ~ /^declared:/ { v = $2; sub(/^declared:/, "", v); print $1 "\t" v }')"
  while IFS=$'\t' read -r dep val; do
    [ -n "$dep" ] || continue
    [[ "$val" =~ ^[^@[:space:]]+@sha256:[0-9a-f]{64}$ ]] \
      || die "orchestrator $dep declares AF_SANDBOX_IMAGE without a literal digest-pinned value ('$val': valueFrom, empty or a tag)"
  done <<< "$live"
  if [ -z "$live" ]; then
    IMAGE="$COMMITTED_IMAGE"; IMAGE_SRC="committed fallback (no live orchestrator sets AF_SANDBOX_IMAGE)"
  else
    [ "$(printf '%s\n' "$live" | cut -f2 | sort -u | wc -l | tr -d ' ')" = "1" ] \
      || die "live orchestrators disagree on AF_SANDBOX_IMAGE: $(printf '%s\n' "$live" | tr '\t\n' '= ')"
    IMAGE="$(printf '%s\n' "$live" | head -1 | cut -f2)"
    IMAGE_SRC="live: $(printf '%s\n' "$live" | cut -f1 | tr '\n' ' ')"
  fi
fi
[[ "$IMAGE" =~ ^[^@[:space:]]+@sha256:[0-9a-f]{64}$ ]] || die "sandbox image '$IMAGE' ($IMAGE_SRC) is not digest-pinned"
# The digest must still be in the registry. Zot GCs digests nothing re-pulls, and a CP-rendered pool keeps
# the digest it was rendered with (#1519): without this, a GC'd image surfaces 5 minutes later as "the
# canary never ran". Only an HTTP 200 for the manifest passes; anything else (404, auth, unreachable) stops.
_reg="${IMAGE%%/*}"; _rest="${IMAGE#*/}"
_code="$("${AF_CANARY_CURL:-curl}" -s -o /dev/null -m 20 -w '%{http_code}' \
  -H 'Accept: application/vnd.oci.image.index.v1+json, application/vnd.oci.image.manifest.v1+json, application/vnd.docker.distribution.manifest.v2+json, application/vnd.docker.distribution.manifest.list.v2+json' \
  "https://${_reg}/v2/${_rest%@*}/manifests/${_rest#*@}" 2>/dev/null)"
[ "$_code" = "200" ] || die "sandbox image '$IMAGE' ($IMAGE_SRC) is not in the registry (manifest HTTP ${_code:-none}); pass a present digest as AF_CANARY_IMAGE"

# Broker targets. A pod's fingerprint is its uid, container restart counts and Ready transition time: any
# restart, replacement or unready spell between two reads changes it.
POD_FIELDS='{.metadata.name}{"|"}{.status.podIP}{"|"}{.metadata.deletionTimestamp}{"|"}{.metadata.uid}{"|"}{range .status.containerStatuses[*]}{.restartCount}{","}{end}{"|"}{range .status.conditions[?(@.type=="Ready")]}{.status}{"/"}{.lastTransitionTime}{end}'
POD_PICK='$2 != "" && $3 == "" && $6 ~ /^True\// { print $1, $2, $4 "|" $5 "|" $6; exit }'
# "<pod> <ip> <fingerprint>" of the first Ready, non-terminating pod of app.kubernetes.io/name=$1 whose IP is not $2.
ready_broker() {
  "${K[@]}" -n "$BROKER_NS" get pods -l "app.kubernetes.io/name=$1" -o jsonpath="{range .items[*]}${POD_FIELDS}{\"\\n\"}{end}" 2>/dev/null \
    | awk -F'|' -v skip="${2:-}" "\$2 != skip && ${POD_PICK}"
}
# The same for ONE named pod; nothing if it is gone, terminating or not Ready.
pod_now() {
  "${K[@]}" -n "$BROKER_NS" get pod "$1" -o jsonpath="${POD_FIELDS}{\"\\n\"}" 2>/dev/null | awk -F'|' "${POD_PICK}"
}
echo "== sandbox-boundary canary =="
echo "context=$KCTX  ns=$NS  workspace=$ORG/$WS  pool=$POOL  pvc=$PVC"
echo "image=$IMAGE  source=$IMAGE_SRC"
read -r BROKER_POD BROKER_IP _ <<< "$(ready_broker "$POS_BROKER")"
# Never the positive pod: with AF_CANARY_DENY_BROKER set to the admitting broker this picks its OTHER
# replica, which the canary can reach, so the run must FAIL (the mutation check in the runbook).
read -r DENY_POD DENY_IP DENY_FP <<< "$(ready_broker "$DENY_BROKER" "${BROKER_IP:-}")"
[ -n "${BROKER_IP:-}" ] && info "positive control: $POS_BROKER pod $BROKER_POD ($BROKER_IP)" \
  || { bad "no Ready $POS_BROKER pod (cannot run the positive control)"; BROKER_IP=""; }
[ -n "${DENY_IP:-}" ] && info "negative control: $DENY_BROKER pod $DENY_POD ($DENY_IP)" \
  || { bad "no Ready $DENY_BROKER pod (cannot run the denied-broker probe)"; DENY_IP=""; }
if [ -n "$BROKER_IP" ] && [ "$BROKER_IP" = "$DENY_IP" ]; then
  bad "the positive and denied broker targets are the same IP ($BROKER_IP)"; DENY_IP=""
fi
if [ -n "${AF_CANARY_PREFLIGHT_ONLY:-}" ]; then
  echo "PREFLIGHT workspace=$ORG/$WS pool=$POOL pvc=$PVC image=$IMAGE source=$IMAGE_SRC broker=${BROKER_IP:-none} deny=${DENY_IP:-none} failures=$fail"
  [ "$fail" -eq 0 ]; exit $?
fi
echo

# ---- 1. STATIC boundary --------------------------------------------------------------------
echo "[1] static boundary"
"${K[@]}" get runtimeclass kata >/dev/null 2>&1 && ok "kata RuntimeClass present" || bad "kata RuntimeClass MISSING"
for cnp in sandbox-agent-egress sandbox-test-zero-egress; do
  "${K[@]}" -n "$NS" get ciliumnetworkpolicy "$cnp" >/dev/null 2>&1 \
    && ok "CiliumNetworkPolicy $cnp present" || bad "CNP $cnp MISSING"
done
"${K[@]}" -n "$NS" get networkpolicy default-deny-all >/dev/null 2>&1 \
  && ok "default-deny-all NetworkPolicy present" || bad "default-deny-all MISSING"
[ "$NS" != "$ORCH_NS" ] && ok "sandbox ns ($NS) is separate from the orchestrator ($ORCH_NS)" \
  || bad "sandbox ns == orchestrator ns"
psa="$("${K[@]}" get ns "$NS" -o jsonpath='{.metadata.labels.pod-security\.kubernetes\.io/enforce}' 2>/dev/null)"
[ "$psa" = "restricted" ] && ok "sandbox ns PSA enforce=restricted" || bad "sandbox ns PSA enforce=$psa (want restricted)"
amount="$("${K[@]}" -n "$NS" get sa agentforge-sandbox -o jsonpath='{.automountServiceAccountToken}' 2>/dev/null)"
[ "$amount" = "false" ] && ok "sandbox SA automountServiceAccountToken=false" || bad "sandbox SA automount=$amount"
# the tokenless SA must have NO RoleBinding/ClusterRoleBinding subject anywhere (no OpenBao/apiserver reach).
# A failed list is a FAIL: an unreadable answer is not an empty one.
if subjects="$("${K[@]}" get rolebindings,clusterrolebindings -A -o jsonpath='{range .items[*]}{range .subjects[*]}{.kind}/{.namespace}/{.name}{"\n"}{end}{end}' 2>/dev/null)"; then
  sarefs="$(printf '%s\n' "$subjects" | grep -c "^ServiceAccount/${NS}/agentforge-sandbox$")"
  [ "$sarefs" = "0" ] && ok "sandbox SA has NO Role/ClusterRoleBinding (no apiserver/OpenBao RBAC)" \
    || bad "sandbox SA is bound by $sarefs RoleBinding subject(s)"
else
  bad "could not list Role/ClusterRoleBindings — the tokenless-SA check is unproven"
fi
echo

# ---- 2. NEGATIVE admission (the pinned pod-shape VAPs REJECT breaches) ----------------------
echo "[2] negative admission (server dry-run — breaches MUST be rejected)"
JOBID="canary$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-26)"   # 6+26 = 32 hex-ish
render_canary() {  # $1=name $2=trust-class $3=job-id $4=broker-ip $5=probe-args-file $6=denied-broker-ip
  cat <<YAML
apiVersion: batch/v1
kind: Job
metadata: { name: $1, namespace: $NS, labels: { app.kubernetes.io/managed-by: agentforge, app.kubernetes.io/component: sandbox } }
spec:
  parallelism: 1
  completions: 1
  backoffLimit: 0
  activeDeadlineSeconds: 120
  ttlSecondsAfterFinished: 300
  podReplacementPolicy: Failed
  template:
    metadata:
      labels:
        agentforge.io/job-id: "$3"
        agentforge.io/trust-class: $2
        agentforge.io/org: $ORG
        agentforge.io/workspace: $WS
        agentforge.io/pool: $POOL
        app.kubernetes.io/managed-by: agentforge
        app.kubernetes.io/component: sandbox
    spec:
      restartPolicy: Never
      runtimeClassName: kata
      serviceAccountName: agentforge-sandbox
      automountServiceAccountToken: false
      terminationGracePeriodSeconds: 10
      securityContext: { runAsNonRoot: true, runAsUser: 65532, runAsGroup: 65532, fsGroup: 65532, seccompProfile: { type: RuntimeDefault } }
      volumes:
        - { name: workspace, persistentVolumeClaim: { claimName: $PVC } }
        - { name: home, emptyDir: { sizeLimit: 256Mi } }
      containers:
        - name: sandbox
          image: $IMAGE
          workingDir: /workspace
          env:
            - { name: AF_CANARY_BROKER_IP, value: "$4" }
            - { name: AF_CANARY_DENY_BROKER_IP, value: "${6:-}" }
          command: ["/bin/sh","-c"]
          args:
$(sed 's/^/            /' "$5")
          securityContext: { runAsNonRoot: true, runAsUser: 65532, runAsGroup: 65532, allowPrivilegeEscalation: false, privileged: false, capabilities: { drop: ["ALL"] }, seccompProfile: { type: RuntimeDefault }, readOnlyRootFilesystem: true, procMount: Default }
          volumeMounts:
            - { name: workspace, mountPath: /workspace, subPath: "$3" }
            - { name: home, mountPath: /home/nonroot }
          resources: { requests: { cpu: 50m, memory: 64Mi, ephemeral-storage: 64Mi }, limits: { cpu: 500m, memory: 256Mi, ephemeral-storage: 256Mi } }
YAML
}
NOOP=$(mktemp); printf -- '- "true"\n' > "$NOOP"
BASE=$(mktemp); render_canary af-boundary-dryrun agent "$JOBID" "" "$NOOP" > "$BASE"
"${K[@]}" apply --dry-run=server -f "$BASE" >/dev/null 2>&1 \
  && ok "a compliant canary is ADMITTED" || bad "the compliant canary was rejected (shape drift?)"
dryrun_breach() {  # $1=label $2=sed-expr — MUST be rejected
  out="$(sed "$2" "$BASE" | sed "s/af-boundary-dryrun/af-boundary-breach/" | "${K[@]}" apply --dry-run=server -f - 2>&1)"
  if printf '%s' "$out" | grep -qiE 'denied|invalid|forbidden|violate|must '; then
    ok "admission REJECTS $1"
  else
    bad "admission ADMITTED $1 (BREACH): $(printf '%s' "$out" | tr '\n' ' ' | cut -c1-120)"
  fi
}
dryrun_breach "privileged:true"        's/privileged: false/privileged: true/'
dryrun_breach "automountToken:true"    's/automountServiceAccountToken: false/automountServiceAccountToken: true/'
dryrun_breach "hostNetwork:true"       's/restartPolicy: Never/restartPolicy: Never\n      hostNetwork: true/'
dryrun_breach "non-kata runtimeClass"  's/runtimeClassName: kata/runtimeClassName: runc/'
dryrun_breach "hostPath volume"        's|emptyDir: { sizeLimit: 256Mi }|hostPath: { path: /etc }|'
echo

# ---- 3. resolve a ready broker pod IP (positive-control target) ----------------------------
echo "[3] submit canaries (broker targets resolved in the preflight)"

# probe scripts (mirror the committed egress-canary.yaml / test-egress-canary.yaml). deny() uses
# `curl -sSk` (insecure): we assert NETWORK reachability, not cert trust — otherwise a reachable HTTPS
# target with an untrusted cert exits non-zero and would be MISread as "blocked", masking a breach.
AGENT_ARGS=$(mktemp); cat > "$AGENT_ARGS" <<'PROBE'
- |
  set -u; rc=0
  deny() { if curl -sSk --max-time 5 --connect-timeout 5 -o /dev/null "$2"; then echo "BREACH: reached $1 ($2)"; rc=1; else echo "ok (blocked): $1"; fi; }
  deny world-ip          "https://1.1.1.1/"
  deny metadata          "http://169.254.169.254/latest/meta-data/"
  deny alt-dns           "https://example.com/"
  deny other-pool-broker "http://agentforge-broker.agentforge-broker-otherpool.svc.cluster.local:8700/"
  deny openbao           "https://openbao.openbao.svc.cluster.local:8200/v1/sys/health"
  # NEGATIVE broker control: a broker whose CNP does NOT admit this identity, by pod IP. Reaching it is a
  # BREACH of the per-identity broker ingress; a missing IP is INCONCLUSIVE (rc=1), never a pass.
  if [ -n "${AF_CANARY_DENY_BROKER_IP:-}" ]; then deny denied-broker-ip "http://${AF_CANARY_DENY_BROKER_IP}:8700/"
  else echo "INCONCLUSIVE: no AF_CANARY_DENY_BROKER_IP"; rc=1; fi
  # POSITIVE control is MANDATORY: a netless pod blocks EVERYTHING (incl. its broker), so without
  # proving the broker reachable "all blocked" is indistinguishable from a broken sandbox. No IP or
  # an unreachable broker ⇒ INCONCLUSIVE (rc=1), never a pass.
  if [ -n "${AF_CANARY_BROKER_IP:-}" ]; then
    if curl -sSk --max-time 5 --connect-timeout 5 -o /dev/null "http://${AF_CANARY_BROKER_IP}:8700/"; then echo "BROKER REACHABLE ($AF_CANARY_BROKER_IP:8700)"; else echo "INCONCLUSIVE: broker unreachable"; rc=1; fi
  else echo "INCONCLUSIVE: no AF_CANARY_BROKER_IP"; rc=1; fi
  echo "GUEST_KERNEL: $(uname -r)"
  # grep: 0 = a marker found (BREACH), 1 = read and none found (CLEAN), anything else = could not read.
  grep -aqE 'ANTHROPIC_AUTH_TOKEN|GITEA_BOT_TOKEN|OPENBAO_TOKEN|CLAUDE_CODE_OAUTH|VAULT_TOKEN' /proc/1/environ 2>/dev/null; g=$?
  if [ "$g" -eq 0 ]; then echo "BREACH: orchestrator credential in /proc/1/environ"; rc=1
  elif [ "$g" -eq 1 ]; then echo "PROC1 CLEAN"
  else echo "INCONCLUSIVE: /proc/1/environ unreadable (grep rc=$g)"; rc=1; fi
  [ "$rc" -eq 0 ] && echo "BOUNDARY OK: agent confined"
  exit "$rc"
PROBE
TEST_ARGS=$(mktemp); cat > "$TEST_ARGS" <<'PROBE'
- |
  set -u; rc=0
  deny() { if curl -sSk --max-time 5 --connect-timeout 5 -o /dev/null "$2"; then echo "BREACH: reached $1 ($2)"; rc=1; else echo "ok (blocked): $1"; fi; }
  deny world-ip   "https://1.1.1.1/"
  deny metadata   "http://169.254.169.254/latest/meta-data/"
  deny alt-dns    "https://example.com/"
  deny broker-svc "http://broker-anthropic-max1.agentforge-broker.svc.cluster.local:8700/"
  deny openbao    "https://openbao.openbao.svc.cluster.local:8200/v1/sys/health"
  # MANDATORY discriminator: PROVE the broker is unreachable EVEN BY POD IP. No IP ⇒ INCONCLUSIVE (rc=1).
  if [ -n "${AF_CANARY_BROKER_IP:-}" ]; then deny broker-ip "http://${AF_CANARY_BROKER_IP}:8700/"
  else echo "INCONCLUSIVE: no AF_CANARY_BROKER_IP (cannot prove broker unreachable)"; rc=1; fi
  echo "GUEST_KERNEL: $(uname -r)"
  [ "$rc" -eq 0 ] && echo "BOUNDARY OK: test tier zero egress (broker proven unreachable)"
  exit "$rc"
PROBE

AGENT_JOB="af-boundary-agent-${JOBID:6:8}"
TEST_JOB="af-boundary-test-${JOBID:6:8}"

teardown() {
  [ -n "${AF_CANARY_KEEP:-}" ] && { echo "(AF_CANARY_KEEP set — leaving $AGENT_JOB / $TEST_JOB)"; return; }
  "${K[@]}" -n "$NS" delete job "$AGENT_JOB" "$TEST_JOB" --ignore-not-found --wait=false >/dev/null 2>&1 || true
}
trap teardown EXIT
rm -f "$NOOP" "$BASE"

# Submit ONE canary, wait for it to go terminal, capture its NODE (into $5) and echo its pod log.
# Runs the tiers SEQUENTIALLY so only ONE Kata microVM boots at a time — two concurrent microVMs on
# the small agent nodes was flaky (one occasionally failed to boot, yielding an empty log).
run_tier() {  # $1=job $2=trust-class $3=job-id $4=args-file $5=meta-out-file $6=denied-broker-ip → echoes the pod log;
              # writes "<nodeName> <runtimeClassName>" to $5 BEFORE the caller tears the Job down.
  local s
  render_canary "$1" "$2" "$3" "$BROKER_IP" "$4" "${6:-}" | "${K[@]}" create -f - >/dev/null 2>&1 \
    || { echo "SUBMIT_FAILED"; : > "$5"; return 1; }
  for _ in $(seq 1 60); do   # up to ~5 min for Kata boot + probes
    s="$("${K[@]}" -n "$NS" get job "$1" -o jsonpath='{.status.succeeded}/{.status.failed}' 2>/dev/null)"
    { [ "${s%/*}" = "1" ] || [ "${s#*/}" = "1" ]; } && break
    sleep 5
  done
  # capture the live pod's node + ACTUAL runtimeClassName (k8s-side Kata evidence) while it still exists.
  "${K[@]}" -n "$NS" get pods -l batch.kubernetes.io/job-name="$1" \
    -o jsonpath='{.items[0].spec.nodeName} {.items[0].spec.runtimeClassName}' 2>/dev/null > "$5"
  "${K[@]}" -n "$NS" logs "job/$1" --tail=-1 2>/dev/null || true
}
NODE_OUT=$(mktemp)
echo; echo "[4] running canaries SEQUENTIALLY (one Kata microVM at a time; ~30-90s each)"
FAKE="${AF_CANARY_FAKE_RUN:-}"
info "agent canary $AGENT_JOB ..."
if [ -n "$FAKE" ]; then
  echo "  !!    TEST MODE (AF_CANARY_FAKE_RUN=$FAKE): logs are read from files, no Job runs — NOT a boundary proof"
  AGENT_LOG="$(cat "$FAKE/agent.log" 2>/dev/null)"; cat "$FAKE/agent.meta" > "$NODE_OUT" 2>/dev/null
else
  AGENT_LOG="$(run_tier "$AGENT_JOB" agent "agent${JOBID:5:27}" "$AGENT_ARGS" "$NODE_OUT" "$DENY_IP")"
fi
read -r AGENT_NODE AGENT_RTC < "$NODE_OUT"
# The denied broker must still be the SAME pod, Ready, with the same fingerprint (uid, restarts, Ready
# transition) as when it was picked: a target that restarted or went unready during the probe could have
# "blocked" it by being down, which proves nothing about the policy.
DENY_IP_AFTER="" DENY_FP_AFTER=""
[ -n "${DENY_POD:-}" ] && read -r _ DENY_IP_AFTER DENY_FP_AFTER <<< "$(pod_now "$DENY_POD")"
[ -z "${AF_CANARY_KEEP:-}" ] && [ -z "$FAKE" ] && "${K[@]}" -n "$NS" delete job "$AGENT_JOB" --ignore-not-found --wait=false >/dev/null 2>&1
info "test canary $TEST_JOB ..."
if [ -n "$FAKE" ]; then TEST_LOG="$(cat "$FAKE/test.log" 2>/dev/null)"
else TEST_LOG="$(run_tier "$TEST_JOB" test "testc${JOBID:5:27}" "$TEST_ARGS" "$NODE_OUT")"; fi
[ -z "${AF_CANARY_KEEP:-}" ] && [ -z "$FAKE" ] && "${K[@]}" -n "$NS" delete job "$TEST_JOB" --ignore-not-found --wait=false >/dev/null 2>&1
rm -f "$NODE_OUT" "$AGENT_ARGS" "$TEST_ARGS"
echo "--- agent canary log ---"; printf '%s\n' "$AGENT_LOG" | sed 's/^/    /'
echo "--- test canary log ---";  printf '%s\n' "$TEST_LOG"  | sed 's/^/    /'
echo

# ---- 5. assert the matrix ------------------------------------------------------------------
echo "[5] egress + kata + proc matrix"
has() { printf '%s' "$1" | grep -q "$2"; }
has_line() { printf '%s\n' "$1" | grep -qxF -- "$2"; }   # the WHOLE line, literally
# EMPTY-LOG GUARD: an empty log means the canary never ran (boot/schedule failure) — treat it as a
# FAIL, never a pass. Without this, "no BREACH in an empty log" would spuriously read as "all blocked".
if [ -z "$AGENT_LOG" ]; then bad "AGENT: canary produced NO log (never ran) — inconclusive, fail closed"
elif has "$AGENT_LOG" "BREACH"; then bad "AGENT: a denied target was REACHED (egress BREACH)"
else ok "AGENT: all denied targets blocked (world/metadata/alt-dns/other-broker/openbao)"; fi
if [ -z "$DENY_IP" ]; then bad "AGENT: denied-broker probe had no target — inconclusive, fail closed"
elif [ "$DENY_IP_AFTER" != "$DENY_IP" ] || [ "$DENY_FP_AFTER" != "$DENY_FP" ]; then
  bad "AGENT: denied broker pod $DENY_POD restarted, was replaced or went unready during the probe (fingerprint '$DENY_FP' -> '${DENY_FP_AFTER:-gone}') — inconclusive"
elif has_line "$AGENT_LOG" "ok (blocked): denied-broker-ip"; then ok "AGENT: $DENY_BROKER (does not admit $ORG/$WS) UNREACHABLE by pod IP — per-identity broker ingress holds"
else bad "AGENT: no denied-broker-ip evidence in the log — the ingress-isolation probe did not run"; fi
if has "$AGENT_LOG" "BROKER REACHABLE"; then ok "AGENT: positive control — reached its pool broker (allow path works)"; else bad "AGENT: broker NOT reached — INCONCLUSIVE (netless?), not a pass"; fi
if has "$AGENT_LOG" "PROC1 CLEAN"; then ok "AGENT: /proc/1/environ carries no orchestrator credential (PID-ns isolated)"; else bad "AGENT: could not confirm /proc/1 isolation"; fi
# BOUNDARY OK is printed ONLY when rc=0 (⇒ the pod exited 0), so its presence IS the exit-0 proof.
if has "$AGENT_LOG" "BOUNDARY OK"; then ok "AGENT: BOUNDARY OK sentinel (pod exit 0)"; else bad "AGENT: no BOUNDARY OK sentinel"; fi
# KATA: k8s-side (the pod's ACTUAL runtimeClassName) AND runtime-side (guest kernel != host). BOTH
# must PROVE, and an unreadable/absent value FAILS CLOSED (a runc fallback or a masked read is never
# read as isolated). Together with step-2 negative admission (non-kata REJECTED) this is unspoofable.
[ "$AGENT_RTC" = "kata" ] && ok "KATA: the sandbox pod ran with runtimeClassName=kata (k8s-side)" \
  || bad "KATA: pod runtimeClassName='$AGENT_RTC' (want kata) — not proven isolated"
GUEST="$(printf '%s' "$AGENT_LOG" | sed -n 's/^GUEST_KERNEL: //p' | head -1)"
HOST="$("${K[@]}" get node "$AGENT_NODE" -o jsonpath='{.status.nodeInfo.kernelVersion}' 2>/dev/null)"
if [ -n "$GUEST" ] && [ -n "$HOST" ] && [ "$GUEST" != "$HOST" ]; then ok "KATA: guest kernel ($GUEST) != host ($HOST) — microVM isolation in effect"
else bad "KATA: could not PROVE guest!=host (guest='$GUEST' host='$HOST') — fail closed"; fi
# test tier (same empty-log guard)
if [ -z "$TEST_LOG" ]; then bad "TEST: canary produced NO log (never ran) — inconclusive, fail closed"
elif has "$TEST_LOG" "BREACH"; then bad "TEST: a target was REACHED — test tier is NOT zero-egress (BREACH)"
else ok "TEST: zero egress — nothing reachable (incl. the broker) = test_cmd --network none"; fi
# MANDATORY discriminator (mirrors the agent's positive control): the test tier must have PROVEN the
# broker unreachable BY POD IP — the log must show the broker-ip probe ran AND was blocked. A skipped
# broker probe (INCONCLUSIVE / no IP) or a missing evidence line is a FAIL, never a pass.
if has "$TEST_LOG" "ok (blocked): broker-ip"; then ok "TEST: broker PROVEN unreachable by pod IP (the discriminator from agent)"
elif has "$TEST_LOG" "INCONCLUSIVE"; then bad "TEST: broker probe INCONCLUSIVE (no IP) — cannot prove the discriminator, fail closed"
else bad "TEST: no broker-ip deny evidence — the zero-egress discriminator was not proven"; fi
if has "$TEST_LOG" "BOUNDARY OK"; then ok "TEST: BOUNDARY OK sentinel (pod exit 0, zero egress)"; else bad "TEST: no BOUNDARY OK sentinel"; fi
echo

echo "== RESULT: $pass passed, $fail failed =="
if [ -n "$FAKE" ]; then
  echo "TEST MODE (AF_CANARY_FAKE_RUN): no canary ran, so this is NOT a boundary proof." >&2
  [ "$fail" -eq 0 ] && exit 3 || exit 1
fi
if [ "$fail" -ne 0 ]; then
  echo "SANDBOX BOUNDARY BREACH or INCONCLUSIVE — this is a P0. Do NOT flip/keep privilege_hardening: v1.1." >&2
  exit 1
fi
echo "GREEN — the sandbox boundary held on every vector. Standing proof for privilege_hardening: v1.1."

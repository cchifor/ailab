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
# DISPOSABLE + SAFE: runs ONLY against the throwaway tenant-zero/playground workspace with a fresh random
# job-id subPath, and HARD-REFUSES platform-dev (which drives the real cchifor/ailab + cchifor/platform
# repos). Both canary Jobs are TORN DOWN afterwards. A green run is the standing proof v1.1 is safe; ANY
# breach is a P0 — STOP and escalate with the reproduction.
#
# Usage:  scripts/verify-sandbox-boundary.sh            # full matrix against admin@ai / playground
#         KUBECTL_CONTEXT=admin@ai AF_CANARY_WORKSPACE=playground scripts/verify-sandbox-boundary.sh
#         AF_CANARY_KEEP=1 scripts/verify-sandbox-boundary.sh   # keep the canary Jobs for inspection
set -uo pipefail
cd "$(dirname "$0")/.."

KCTX="${KUBECTL_CONTEXT:-admin@ai}"
NS="agentforge-sandbox"
ORCH_NS="agentforge"
BROKER_NS="agentforge-broker"
ORG="tenant-zero"
WS="${AF_CANARY_WORKSPACE:-playground}"
K=(kubectl --context "$KCTX")

pass=0 fail=0
ok()   { echo "  PASS  $*"; pass=$((pass+1)); }
bad()  { echo "  FAIL  $*"; fail=$((fail+1)); }
info() { echo "  ..    $*"; }
die()  { echo "FATAL: $*" >&2; exit 2; }

# ---- 0. preflight + disposable-target guard ------------------------------------------------
"${K[@]}" version -o json >/dev/null 2>&1 || die "kubectl context $KCTX unreachable"
# ALLOWLIST, not denylist: the canary mounts a REAL workspace PVC + writes a job-id subPath, so it may
# run ONLY against a workspace KNOWN to be disposable. 'playground' is the sacrificial workspace (ADR
# 0018 — the only non-hardened, never-real-repo workspace). Any other name (platform-dev, ailab, web,
# …) is REFUSED unless the operator sets AF_CANARY_DANGEROUS_OVERRIDE=yes and names it explicitly.
_ALLOWED_WORKSPACES="playground"
case " $_ALLOWED_WORKSPACES " in
  *" $WS "*) : ;;
  *) [ "${AF_CANARY_DANGEROUS_OVERRIDE:-}" = "yes" ] \
       || die "REFUSING workspace '$WS' — canary runs only against a disposable workspace ($_ALLOWED_WORKSPACES); set AF_CANARY_DANGEROUS_OVERRIDE=yes to force" ;;
esac
PVC="af-sbx-ws-${ORG}-${WS}"
TENANT_NS="af-tenant-${ORG}-${WS}"   # the worker (holds AF_SANDBOX_IMAGE) runs here, not the CP ns
"${K[@]}" -n "$NS" get pvc "$PVC" >/dev/null 2>&1 || die "workspace PVC $PVC not found in $NS"
# resolve the sandbox image the live worker actually uses (AF_SANDBOX_IMAGE); fall back to the
# digest pinned in the committed egress-canary.yaml so the harness still runs if the env moves.
IMAGE="$("${K[@]}" -n "$TENANT_NS" get deploy -o jsonpath='{range .items[*].spec.template.spec.containers[*].env[?(@.name=="AF_SANDBOX_IMAGE")]}{.value}{"\n"}{end}' 2>/dev/null | grep -m1 . )"
[ -n "$IMAGE" ] || IMAGE="$(grep -oE 'registry[^ ]*sandbox@sha256:[0-9a-f]+' kubernetes/apps/infrastructure/agentforge-sandbox/egress-canary.yaml | head -1)"
[ -n "$IMAGE" ] || die "could not resolve AF_SANDBOX_IMAGE (tenant ns $TENANT_NS or the committed canary)"
echo "== sandbox-boundary canary =="
echo "context=$KCTX  ns=$NS  workspace=$ORG/$WS  pvc=$PVC"
echo "image=$IMAGE"
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
rb="$("${K[@]}" get rolebindings,clusterrolebindings -A -o json 2>/dev/null \
  | grep -c "\"name\": \"agentforge-sandbox\"" || true)"
# (subjects OR the RB's own name may match; treat 0 subject-refs to the SA as the goal — check subjects)
sarefs="$("${K[@]}" get rolebindings,clusterrolebindings -A -o jsonpath='{range .items[*]}{range .subjects[*]}{.kind}/{.namespace}/{.name}{"\n"}{end}{end}' 2>/dev/null \
  | grep -c "ServiceAccount/${NS}/agentforge-sandbox" || true)"
[ "${sarefs:-0}" = "0" ] && ok "sandbox SA has NO Role/ClusterRoleBinding (no apiserver/OpenBao RBAC)" \
  || bad "sandbox SA is bound by $sarefs RoleBinding subject(s)"
echo

# ---- 2. NEGATIVE admission (the pinned pod-shape VAPs REJECT breaches) ----------------------
echo "[2] negative admission (server dry-run — breaches MUST be rejected)"
JOBID="canary$(head -c16 /dev/urandom | od -An -tx1 | tr -d ' \n' | cut -c1-26)"   # 6+26 = 32 hex-ish
render_canary() {  # $1=name $2=trust-class $3=job-id $4=broker-ip $5=probe-args-file
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
        agentforge.io/pool: planner
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
echo "[3] resolve broker endpoint + submit canaries"
BROKER_IP="$("${K[@]}" -n "$BROKER_NS" get pods -l app.kubernetes.io/component=broker \
  -o jsonpath='{range .items[?(@.status.phase=="Running")]}{.status.podIP}{"\n"}{end}' 2>/dev/null | grep -m1 . )"
[ -n "$BROKER_IP" ] && info "broker pod IP for the positive control: $BROKER_IP" \
  || { bad "no Running broker pod IP (cannot run the positive control)"; BROKER_IP=""; }

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
  # POSITIVE control is MANDATORY: a netless pod blocks EVERYTHING (incl. its broker), so without
  # proving the broker reachable "all blocked" is indistinguishable from a broken sandbox. No IP or
  # an unreachable broker ⇒ INCONCLUSIVE (rc=1), never a pass.
  if [ -n "${AF_CANARY_BROKER_IP:-}" ]; then
    if curl -sSk --max-time 5 --connect-timeout 5 -o /dev/null "http://${AF_CANARY_BROKER_IP}:8700/"; then echo "BROKER REACHABLE ($AF_CANARY_BROKER_IP:8700)"; else echo "INCONCLUSIVE: broker unreachable"; rc=1; fi
  else echo "INCONCLUSIVE: no AF_CANARY_BROKER_IP"; rc=1; fi
  echo "GUEST_KERNEL: $(uname -r)"
  if grep -aqE 'ANTHROPIC_AUTH_TOKEN|GITEA_BOT_TOKEN|OPENBAO_TOKEN|CLAUDE_CODE_OAUTH|VAULT_TOKEN' /proc/1/environ 2>/dev/null; then echo "BREACH: orchestrator credential in /proc/1/environ"; rc=1; else echo "PROC1 CLEAN"; fi
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
run_tier() {  # $1=job $2=trust-class $3=job-id $4=args-file $5=meta-out-file → echoes the pod log;
              # writes "<nodeName> <runtimeClassName>" to $5 BEFORE the caller tears the Job down.
  local s
  render_canary "$1" "$2" "$3" "$BROKER_IP" "$4" | "${K[@]}" create -f - >/dev/null 2>&1 \
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
info "agent canary $AGENT_JOB ..."
AGENT_LOG="$(run_tier "$AGENT_JOB" agent "agent${JOBID:5:27}" "$AGENT_ARGS" "$NODE_OUT")"
read -r AGENT_NODE AGENT_RTC < "$NODE_OUT"
[ -z "${AF_CANARY_KEEP:-}" ] && "${K[@]}" -n "$NS" delete job "$AGENT_JOB" --ignore-not-found --wait=false >/dev/null 2>&1
info "test canary $TEST_JOB ..."
TEST_LOG="$(run_tier "$TEST_JOB" test "testc${JOBID:5:27}" "$TEST_ARGS" "$NODE_OUT")"
[ -z "${AF_CANARY_KEEP:-}" ] && "${K[@]}" -n "$NS" delete job "$TEST_JOB" --ignore-not-found --wait=false >/dev/null 2>&1
rm -f "$NODE_OUT" "$AGENT_ARGS" "$TEST_ARGS"
echo "--- agent canary log ---"; printf '%s\n' "$AGENT_LOG" | sed 's/^/    /'
echo "--- test canary log ---";  printf '%s\n' "$TEST_LOG"  | sed 's/^/    /'
echo

# ---- 5. assert the matrix ------------------------------------------------------------------
echo "[5] egress + kata + proc matrix"
has() { printf '%s' "$1" | grep -q "$2"; }
# EMPTY-LOG GUARD: an empty log means the canary never ran (boot/schedule failure) — treat it as a
# FAIL, never a pass. Without this, "no BREACH in an empty log" would spuriously read as "all blocked".
if [ -z "$AGENT_LOG" ]; then bad "AGENT: canary produced NO log (never ran) — inconclusive, fail closed"
elif has "$AGENT_LOG" "BREACH"; then bad "AGENT: a denied target was REACHED (egress BREACH)"
else ok "AGENT: all denied targets blocked (world/metadata/alt-dns/other-broker/openbao)"; fi
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
if [ "$fail" -ne 0 ]; then
  echo "SANDBOX BOUNDARY BREACH or INCONCLUSIVE — this is a P0. Do NOT flip/keep privilege_hardening: v1.1." >&2
  exit 1
fi
echo "GREEN — the sandbox boundary held on every vector. Standing proof for privilege_hardening: v1.1."

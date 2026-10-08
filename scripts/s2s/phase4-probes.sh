#!/usr/bin/env bash
# S2S Phase 4: the MANDATORY post-flip probes of the projected-token harness identity.
#
# Plan: plans/2026-10-06-s2s-identity-openbao-plan.md, "Phase 4" and "Verification and drills".
# Runbook: docs/runbooks/s2s-identity.md, "Phase 4: activation checks" (when to run what).
#
# OWNER-RUN, after platform #2092 (`harness.enabled: true`) has rolled out, from a machine that is
# NOT a dev worker, with the `admin@ai` kube context. Git Bash on Windows or bash on Linux; needs
# kubectl, curl, base64 and sha256sum (no local python).
#
# Per gatekeeper replica (every pod of app.kubernetes.io/name=gatekeeper in strive-ailab, or each
# --replica), it execs into container `gatekeeper` and runs scripts/s2s/phase4_probe.py with the
# pod's OWN python against http://127.0.0.1:5000. The gatekeeper NetworkPolicy admits only Traefik
# and the allowedClients, so in-pod loopback is how ONE replica is tested on its own.
#
# Tokens are TokenRequests (`kubectl create token`, no stored object, 10 minutes): the harness
# ServiceAccount for audience strive-gatekeeper, the same SA for a wrong audience, and the
# namespace's `default` SA for strive-gatekeeper (a second identity: while svc-harness is the only
# k8s registry entry it stands in for "a second k8s entry claimed with the harness token"). They
# live in shell variables only and reach the pod on STDIN only: never argv, the environment, a
# file or the output. Nothing here prints a token; any JWT-shaped string in relayed output is
# redacted as well.
#
# Exit 0: every check passed. Exit 1: a check failed; `DARKEN THE HARNESS` is printed with the
# exact commands. Exit 2: bad invocation.
set -euo pipefail
set +x # never trace: tokens live in shell variables
umask 077
# Git Bash: pass `-l a/b=c`, jsonpath and URL arguments to kubectl.exe verbatim.
export MSYS_NO_PATHCONV=1

CONTEXT=admin@ai
# The harness, its ServiceAccount, the `default` SA and HelmRelease `strive` live in NS.
NS=strive-ailab
# Gatekeeper's namespace: its pods, Service/Endpoints and ConfigMap gatekeeper-registry-extras.
# strive-gatekeeper since the A.2 cutover (platform: gatekeeper in its own namespace, step 3).
# It is the shared source: scripts/s2s/test_phase4_job.py fails unless the in-cluster probe Job
# (kubernetes/apps/infrastructure/s2s-phase4-probe/gatekeeper-ns/kustomization.yaml) says the same.
GK_NS=strive-gatekeeper
GK_LABEL=app.kubernetes.io/name=gatekeeper
GK_CONTAINER=gatekeeper
HARNESS_LABEL=app.kubernetes.io/name=harness
HARNESS_SA=harness
HARNESS_DEPLOY=harness
HARNESS_CONTAINER=harness
ALT_SA=default
AUDIENCE=strive-gatekeeper
WRONG_AUDIENCE=not-strive-gatekeeper
TOKEN_TTL=10m
EXTRAS_CM=gatekeeper-registry-extras
HELMRELEASE=strive
# The Flux Kustomization that applies the HelmRelease manifest (deploy/gitops/flux/clusters/ailab/app).
FLUX_NS=flux-system
FLUX_KUSTOMIZATION=platform-app
# Its parents, outermost first: the root (applies `platform`) and `platform` (applies platform-app).
# Each re-applies its children and drops a hand-set suspend (incident AG2-1, 2026-10-08).
FLUX_PARENTS=(flux-system platform)
# A real route of the harness IngressRoute (PathPrefix(/api/harness), gatekeeper-auth BEFORE the
# rewrite; the harness serves /admin/v1/chat*): unauthenticated it must be 401, never a login 302.
EDGE_URL=https://strive.place/api/harness/admin/v1/chat
EXPECTED_REPLICAS=2
REVOCATION_BOUND=${PHASE4_REVOCATION_BOUND:-90}
# The revocation drill holds a token minted with this --duration (the API refuses under 600 s)...
HELD_TOKEN_SECONDS=${PHASE4_HELD_TOKEN_SECONDS:-600}
# ...and watches it until its own exp. A drill whose token outlives this cap (after the mint) would
# leave the rest unwatched, so it stops as INCOMPLETE (exit 3) before any probe. The held duration
# must be at or below the cap; raise the cap to cover a longer exp the API server issues.
MAX_WATCH_SECONDS=${PHASE4_MAX_WATCH_SECONDS:-900}
# ...and needs the pod removed this long before that end, to observe the refusal.
REMOVAL_MARGIN_SECONDS=${PHASE4_REMOVAL_MARGIN_SECONDS:-30}
# Consecutive attempts without an HTTP answer on one replica before the drill gives up on it.
MAX_NO_ANSWER=${PHASE4_MAX_NO_ANSWER:-3}
POLL_SECONDS=${PHASE4_POLL_SECONDS:-5}
POD_POLL_SECONDS=${PHASE4_POD_POLL_SECONDS:-2}

SCRIPT_DIR=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
PROG="$SCRIPT_DIR/phase4_probe.py"
# stdin line 1 carries the program (base64); the rest are `name=token` lines.
BOOT="import base64,sys;exec(compile(base64.b64decode(sys.stdin.readline()),'phase4_probe.py','exec'))"

DRY_RUN=0
GATEKEEPER_ONLY=0
EXPECT_REFUSED=0
REGISTRY_CHECK=1
REVOCATION=0
TENANT=phase4-probe
REPLICAS=()

usage() {
  cat <<'EOF'
usage: scripts/s2s/phase4-probes.sh [options]

  (no option)          the full post-flip check: per gatekeeper replica the registry acceptance and
                       the mandatory probes, then #2092's own checks (HelmRelease, harness pod Ready,
                       migrate completed, token mode, IngressRoute) and the edge 401
  --gatekeeper-only    only the per-replica part (every later roll of an active registry)
  --expect-refused     svc-harness must be REFUSED on each replica (cold-start, deletion and rollback
                       drills); implies --gatekeeper-only
  --no-registry-check  skip the extras-registry acceptance (a drill that deleted the ConfigMap)
  --revocation-drill   hold a pod-bound harness token, wait while the owner revokes the pod, time the
                       rejection on each replica, and watch every replica until the token expires
  --replica POD        probe only this gatekeeper pod (repeatable); default: every pod
  --tenant ID          tenant_id of the probe mint (default: phase4-probe)
  --dry-run            print the plan; no cluster or network call
  -h, --help           this text

Revocation drill knobs: PHASE4_HELD_TOKEN_SECONDS (default 600) is the held token's --duration and
must be at most PHASE4_MAX_WATCH_SECONDS (default 900), the longest the drill watches. If the API
server issues a token that outlives the cap, the drill stops as INCOMPLETE before any probe.

Exit 0 = pass, 1 = a check failed (DARKEN THE HARNESS is printed), 2 = bad invocation,
3 = revocation drill INCOMPLETE (the held token outlives the cap; nothing was probed).
EOF
}

while (($#)); do
  case "$1" in
    --dry-run) DRY_RUN=1 ;;
    --gatekeeper-only) GATEKEEPER_ONLY=1 ;;
    --expect-refused) EXPECT_REFUSED=1 GATEKEEPER_ONLY=1 ;;
    --no-registry-check) REGISTRY_CHECK=0 ;;
    --revocation-drill) REVOCATION=1 ;;
    --replica)
      [[ $# -ge 2 && -n $2 ]] || { echo "phase4-probes: --replica needs a pod name" >&2; exit 2; }
      REPLICAS+=("$2")
      shift
      ;;
    --tenant)
      [[ $# -ge 2 && $2 =~ ^[A-Za-z0-9._:-]{1,128}$ ]] || { echo "phase4-probes: --tenant needs an id ([A-Za-z0-9._:-])" >&2; exit 2; }
      TENANT=$2
      shift
      ;;
    -h | --help) usage; exit 0 ;;
    *) echo "phase4-probes: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
if ((REVOCATION && EXPECT_REFUSED)); then
  echo "phase4-probes: --revocation-drill and --expect-refused do not combine" >&2
  exit 2
fi
if ((REVOCATION)); then
  [[ $MAX_WATCH_SECONDS =~ ^[1-9][0-9]*$ ]] || { echo "phase4-probes: PHASE4_MAX_WATCH_SECONDS must be a whole number of seconds" >&2; exit 2; }
  if [[ ! $HELD_TOKEN_SECONDS =~ ^[1-9][0-9]*$ ]] || ((HELD_TOKEN_SECONDS > MAX_WATCH_SECONDS)); then
    echo "phase4-probes: PHASE4_HELD_TOKEN_SECONDS (${HELD_TOKEN_SECONDS}) must be a whole number of seconds at most PHASE4_MAX_WATCH_SECONDS (${MAX_WATCH_SECONDS}), so the drill can watch the held token until it expires" >&2
    exit 2
  fi
fi
for pod in ${REPLICAS[@]+"${REPLICAS[@]}"}; do
  [[ $pod =~ ^[a-z0-9]([-a-z0-9]*[a-z0-9])?$ ]] || { echo "phase4-probes: not a pod name: $pod" >&2; exit 2; }
done
[[ -f $PROG ]] || { echo "phase4-probes: $PROG is missing" >&2; exit 2; }

HT='' WT='' AT='' HELD=''
trap 'unset HT WT AT HELD' EXIT

FAILURES=()
ok() { printf 'PASS %s\n' "$*"; }
bad() { printf 'FAIL %s\n' "$*"; FAILURES+=("$*"); }
info() { printf 'INFO %s\n' "$*"; }
section() { printf '\n== %s\n' "$*"; }
redact() { sed -E 's/[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]*/<redacted-jwt>/g'; }
k() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }
kg() { kubectl --context "$CONTEXT" -n "$GK_NS" "$@"; }

# Freeze = top-down: the parent Kustomizations (the root, then `platform`), then the Flux
# Kustomization that applies the HelmRelease, then the HelmRelease, then the scale. Every suspend is
# needed: a parent re-applies its children from git and drops a hand-set suspend (incident AG2-1,
# 2026-10-08: a freeze of platform-app and the HelmRelease alone was undone twice within minutes);
# the Kustomization re-applies the HelmRelease manifest and so clears a hand-set HelmRelease suspend
# within one reconcile (observed 2026-10-06 17:22Z); the HelmRelease suspend stops helm-controller
# upgrades, which would put the replica count back. flux-resume.sh resumes the parents last.
freeze_commands() {
  local parent
  cat <<EOF
  1. Freeze the release top-down: the root Kustomization, then \`platform\` (each re-applies its
     children from git and drops a hand-set suspend; incident AG2-1, 2026-10-08), then the
     Kustomization $FLUX_KUSTOMIZATION (it re-applies the HelmRelease from git and clears a hand-set
     HelmRelease suspend; observed 2026-10-06 17:22Z), then the HelmRelease (Helm re-applies the
     replica count on every platform commit, so a bare scale is undone). Suspending the root holds
     every ailab GitOps change until the resume:
EOF
  for parent in "${FLUX_PARENTS[@]}"; do
    printf "       kubectl --context %s -n %s patch kustomization %s --type=merge -p '{\"spec\":{\"suspend\":true}}'\n" \
      "$CONTEXT" "$FLUX_NS" "$parent"
  done
  cat <<EOF
       kubectl --context $CONTEXT -n $FLUX_NS patch kustomization $FLUX_KUSTOMIZATION --type=merge -p '{"spec":{"suspend":true}}'
       kubectl --context $CONTEXT -n $NS patch helmrelease $HELMRELEASE --type=merge -p '{"spec":{"suspend":true}}'
  2. Stop every harness pod (its projected token dies with the pod; deleting the pod alone does NOT
     revoke: the Deployment is Recreate and brings up a fresh, valid identity):
       kubectl --context $CONTEXT -n $NS scale deployment/$HARNESS_DEPLOY --replicas=0
EOF
}

# Resume = scripts/s2s/flux-resume.sh, never by hand. It lands the platform commit through Flux,
# with a gate at each step: the GitRepository fetched it; the Kustomization applied it; the
# HelmRelease (un-suspended only if still suspended) upgraded to it. Un-suspending by hand can
# re-apply a stale HelmRelease and, mid-rollback, bring the harness back before the revert lands.
resume_commands() {
  cat <<EOF
     (never by hand: it gates each step on the commit Flux actually fetched and applied: the source,
     then the Kustomization, then the HelmRelease only if still suspended, then the parents, the
     root last; see the script's header)
       scripts/s2s/flux-resume.sh --after-$1
EOF
}

darken_commands() {
  freeze_commands
  cat <<EOF
  3. Make it durable: a platform PR setting \`harness.enabled: false\` in
     deploy/helm/values/providers/ailab.yaml (revert #2092), merged. Only AFTER it has merged, resume:
$(resume_commands revert)
EOF
}

# incomplete WHAT HOW: the drill cannot reach a verdict; exit 3, never a PASS (and no DARKEN:
# nothing was found wrong, nothing was verified either).
incomplete() {
  section "Result"
  printf 'INCOMPLETE: %s\n' "$1"
  printf '  %s\n' "$2"
  printf '  This is not a pass. Runbook: docs/runbooks/s2s-identity.md, drill 4 (revocation).\n'
  exit 3
}

finish() {
  section "Result"
  if ((${#FAILURES[@]})); then
    printf '%d check(s) failed:\n' "${#FAILURES[@]}"
    printf '  - %s\n' "${FAILURES[@]}"
    printf '\nDARKEN THE HARNESS\n'
    darken_commands
    printf '  Runbook: docs/runbooks/s2s-identity.md, "Phase 4: activation checks".\n'
    exit 1
  fi
  echo "ALL CHECKS PASSED"
  exit 0
}

# A JWT, nothing else: kubectl printed a token and no warning.
is_jwt() { [[ $1 =~ ^[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+$ ]]; }

# mint_into VAR SA AUDIENCE [extra create-token args]: a TokenRequest straight into VAR.
mint_into() {
  local __var=$1 sa=$2 aud=$3 __tok
  shift 3
  # MINT_DURATION (set `local` by a caller) overrides the default lifetime.
  if ! __tok=$(k create token "$sa" --audience "$aud" --duration "${MINT_DURATION:-$TOKEN_TTL}" "$@"); then
    bad "token: kubectl create token $sa --audience $aud failed"
    return 1
  fi
  __tok=${__tok//$'\r'/}
  __tok=${__tok//$'\n'/}
  if ! is_jwt "$__tok"; then
    bad "token: kubectl create token $sa --audience $aud did not return a JWT"
    return 1
  fi
  printf -v "$__var" '%s' "$__tok"
  return 0
}

token_lines() {
  [[ -n $HT ]] && printf 'harness=%s\n' "$HT"
  [[ -n $WT ]] && printf 'wrong_aud=%s\n' "$WT"
  [[ -n $AT ]] && printf 'alt_sa=%s\n' "$AT"
  return 0
}

# run_in_pod POD MODE: the program on stdin line 1, then the tokens; output on stdout.
run_in_pod() {
  local pod=$1 mode=$2 args
  args=(--mode "$mode" --tenant "$TENANT")
  ((REGISTRY_CHECK)) || args+=(--no-registry-check)
  {
    printf '%s\n' "$PROG_B64"
    if [[ $mode == single ]]; then printf 'held=%s\n' "$HELD"; else token_lines; fi
  } | kg exec -i "$pod" -c "$GK_CONTAINER" -- python -c "$BOOT" "${args[@]}" 2>&1
}

# discover_replicas: fills PODS with the gatekeeper replicas to probe; fails on a roll in flight.
PODS=()
discover_replicas() {
  section "Gatekeeper replicas"
  local lines name phase deleting ready all=() good=()
  if ! lines=$(kg get pods -l "$GK_LABEL" -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.phase}{"|"}{.metadata.deletionTimestamp}{"|"}{.status.containerStatuses[?(@.name=="gatekeeper")].ready}{"\n"}{end}'); then
    bad "replicas: kubectl get pods -l $GK_LABEL failed"
    return 1
  fi
  lines=${lines//$'\r'/}
  while IFS='|' read -r name phase deleting ready; do
    [[ -n $name ]] || continue
    all+=("$name")
    if [[ $phase == Running && -z $deleting && $ready == true ]]; then
      good+=("$name")
    else
      info "$name: phase=$phase deleting=${deleting:-no} ready=${ready:-?}"
    fi
  done <<<"$lines"
  if ((${#REPLICAS[@]})); then
    local want found g
    for want in "${REPLICAS[@]}"; do
      found=0
      for g in ${good[@]+"${good[@]}"}; do [[ $g == "$want" ]] && found=1; done
      if ((found)); then PODS+=("$want"); else bad "replicas: $want is not a Running, Ready gatekeeper pod"; fi
    done
  else
    if ((${#all[@]} != ${#good[@]})); then
      bad "replicas: ${#good[@]} of ${#all[@]} gatekeeper pods are Running and Ready (a roll in flight? wait for it)"
    elif ((${#good[@]} < EXPECTED_REPLICAS)); then
      bad "replicas: ${#good[@]} gatekeeper pod(s), ailab runs $EXPECTED_REPLICAS; every replica must be probed"
    fi
    PODS=(${good[@]+"${good[@]}"})
  fi
  ((${#PODS[@]})) || return 1
  ok "replicas: ${PODS[*]}"
}

print_plan() {
  section "Plan (dry run: no cluster or network call)"
  cat <<EOF
context $CONTEXT, namespace $NS, gatekeeper namespace $GK_NS; in-pod program $PROG (sha256 $PROG_SHA)
replicas: ${REPLICAS[*]:-every pod of $GK_LABEL (expected $EXPECTED_REPLICAS, all Running and Ready)}
EOF
  if ((REVOCATION)); then
    cat <<EOF
mode: revocation drill
  1. the one Ready harness pod; a pod-bound TokenRequest for SA $HARNESS_SA, audience $AUDIENCE,
     --duration ${HELD_TOKEN_SECONDS}s (PHASE4_HELD_TOKEN_SECONDS, at most the ${MAX_WATCH_SECONDS}s cap)
     (--bound-object-kind Pod: it dies with that pod, as the pod's projected token does).
     If the issued token outlives PHASE4_MAX_WATCH_SECONDS (${MAX_WATCH_SECONDS}s), the drill stops right
     there as INCOMPLETE (exit 3): no probe, no revoke prompt, never a pass.
  2. each replica: a svc-mcp mint with it -> 200
  3. wait (no write) while the owner revokes in another terminal:
$(freeze_commands)
  4. each replica every ${POLL_SECONDS}s, until the held token's own exp (decoded from it, never
     printed): the same mint. PASS when every replica refuses within
     ${REVOCATION_BOUND}s of the pod's removal, never accepts again before the exp, and the last two
     rounds are refused by every replica. No HTTP answer is never a refusal; ${MAX_NO_ANSWER} in a row
     on one replica fails the drill (it could not be observed)
EOF
  else
    cat <<EOF
mode: $(if ((EXPECT_REFUSED)); then echo "expect-refused (svc-harness must be refused)"; elif ((GATEKEEPER_ONLY)); then echo "gatekeeper-only"; else echo "activation (full)"; fi)
tokens (TokenRequest, $TOKEN_TTL, on stdin only):
  harness   = kubectl create token $HARNESS_SA --audience $AUDIENCE
  wrong_aud = kubectl create token $HARNESS_SA --audience $WRONG_AUDIENCE
  alt_sa    = kubectl create token $ALT_SA --audience $AUDIENCE
EOF
    if ((EXPECT_REFUSED)); then
      cat <<EOF
per replica (kubectl exec -i <pod> -c $GK_CONTAINER -- python -c <bootstrap> --mode refused):
  registry-no-extras   extras_sha empty (or no registry metrics on an older image)
  harness-refused      the harness token as svc-harness for svc-mcp -> 401 invalid_client
  refuse-*             svc-deepagent + harness Bearer, unregistered client, other SA, wrong audience,
                       no token -> 401 invalid_client; every refusal body identical
EOF
    else
      cat <<EOF
per replica (kubectl exec -i <pod> -c $GK_CONTAINER -- python -c <bootstrap> --mode probe):
  registry             $( ((REGISTRY_CHECK)) && echo "loaded extras_sha = mounted file = sha256(ConfigMap $EXTRAS_CM data), extras_rejected 0, every extras_refused_total 0, base_sha equal on every replica" || echo "skipped (--no-registry-check)")
  d3-policy            the mounted extras open client_credentials for svc-mcp only; subject system:serviceaccount:$NS:$HARNESS_SA
  k8s-mint[svc-mcp]    the harness's exact client_credentials request with the harness Bearer -> 200,
                       sub = azp = svc-harness, target svc-mcp, tenant, no act, ttl <= 300 s, scopes =
                       the registry grant; a fresh TokenReview counted on that replica
  d3-refused[<aud>]    client_credentials for every token_exchange-only audience -> 403 unauthorized_client
  refuse-preshared     svc-deepagent with the harness Bearer -> 401 (no fallback to the preshared path)
  refuse-unregistered  an unregistered client_id with the harness Bearer -> 401
  refuse-second-k8s    a second k8s entry with the harness Bearer -> 401 (only when one exists)
  refuse-other-sa      the default SA's token as svc-harness -> 401 (the sub precheck)
  refuse-wrong-audience, refuse-no-token -> 401; every refusal the SAME generic body
EOF
    fi
    if ((!GATEKEEPER_ONLY)); then
      cat <<EOF
#2092's own checks:
  helmrelease $HELMRELEASE Ready; exactly one harness pod Running and Ready, SA $HARNESS_SA, init
  container migrate Completed (exit 0), HARNESS_CLIENT_TOKEN_FILE set and HARNESS_CLIENT_SECRET
  absent; serviceaccount and ingressroute $HARNESS_DEPLOY present
  edge: unauthenticated GET $EDGE_URL -> 401 (not a login 302)
  reminders (not run here): the @api assistant journeys, report-ailab-pin-drift 0 torn
EOF
    fi
  fi
  printf '\non any failure the script prints DARKEN THE HARNESS and:\n'
  darken_commands
}

# probe_all MODE: the in-pod program on every replica; collects REGISTRY lines.
declare -A REG_BASE=() REG_EXTRAS=()
probe_all() {
  local mode=$1 pod out rc line
  for pod in "${PODS[@]}"; do
    section "Replica $pod ($mode)"
    if out=$(run_in_pod "$pod" "$mode"); then rc=0; else rc=$?; fi
    printf '%s\n' "$out" | redact | sed 's/^/    /'
    while IFS= read -r line; do
      line=${line%$'\r'}
      case "$line" in
        "FAIL "*) FAILURES+=("$pod: ${line#FAIL }") ;;
        "REGISTRY "*)
          REG_BASE[$pod]=$(sed -n 's/.*base_sha=\([^ ]*\).*/\1/p' <<<"$line")
          REG_EXTRAS[$pod]=$(sed -n 's/.*extras_sha=\([^ ]*\).*/\1/p' <<<"$line")
          ;;
      esac
    done < <(printf '%s\n' "$out" | redact)
    if ((rc != 0)) || ! grep -qx 'RESULT pass' < <(printf '%s\n' "$out" | tr -d '\r'); then
      grep -q '^FAIL ' < <(printf '%s\n' "$out") || bad "$pod: the in-pod probe did not complete (exit $rc)"
    fi
  done
}

registry_agreement() {
  ((REGISTRY_CHECK && !EXPECT_REFUSED)) || return 0
  section "Registry agreement"
  local cm_sha pod base=''
  if cm_sha=$(kg get configmap "$EXTRAS_CM" -o jsonpath='{.data.registry\.yaml}' | sha256sum | cut -d' ' -f1) && [[ -n $cm_sha ]]; then
    for pod in "${PODS[@]}"; do
      if [[ ${REG_EXTRAS[$pod]:-} == "$cm_sha" ]]; then
        ok "$pod: loaded extras_sha = sha256(ConfigMap $EXTRAS_CM data.registry.yaml) $cm_sha"
      else
        bad "$pod: loaded extras_sha ${REG_EXTRAS[$pod]:--} != ConfigMap $cm_sha (roll gatekeeper)"
      fi
    done
  else
    bad "registry: cannot read ConfigMap $EXTRAS_CM"
  fi
  if ((${#PODS[@]} < 2)); then
    info "base_sha agreement needs two replicas; probe the other one too"
    return 0
  fi
  local b first=1
  for pod in "${PODS[@]}"; do
    b=${REG_BASE[$pod]:--}
    ((first)) && base=$b first=0
    if [[ $b != "$base" || $b == - ]]; then
      bad "registry: base_sha missing or different across replicas (${PODS[*]})"
      return 0
    fi
  done
  ok "registry: base_sha $base on every replica"
}

harness_checks() {
  section "#2092's own checks"
  local hr lines n=0 name phase deleting ready sa mreason mexit envs
  if hr=$(k get helmrelease "$HELMRELEASE" -o jsonpath='{.status.conditions[?(@.type=="Ready")].status}') && [[ ${hr//$'\r'/} == True ]]; then
    ok "helmrelease $HELMRELEASE Ready"
  else
    bad "helmrelease $HELMRELEASE not Ready (${hr:-unreadable})"
  fi
  if ! lines=$(k get pods -l "$HARNESS_LABEL" -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.status.phase}{"|"}{.metadata.deletionTimestamp}{"|"}{.status.conditions[?(@.type=="Ready")].status}{"|"}{.spec.serviceAccountName}{"|"}{.status.initContainerStatuses[?(@.name=="migrate")].state.terminated.reason}{"|"}{.status.initContainerStatuses[?(@.name=="migrate")].state.terminated.exitCode}{"|"}{.spec.containers[?(@.name=="harness")].env[*].name}{"\n"}{end}'); then
    bad "harness: kubectl get pods -l $HARNESS_LABEL failed"
    lines=''
  fi
  lines=${lines//$'\r'/}
  while IFS='|' read -r name phase deleting ready sa mreason mexit envs; do
    [[ -n $name ]] || continue
    n=$((n + 1))
    if [[ $phase == Running && -z $deleting && $ready == True ]]; then ok "harness pod $name Running and Ready"; else bad "harness pod $name: phase=$phase deleting=${deleting:-no} ready=${ready:-?}"; fi
    if [[ $sa == "$HARNESS_SA" ]]; then ok "harness pod runs as ServiceAccount $sa"; else bad "harness pod ServiceAccount '$sa' != $HARNESS_SA"; fi
    if [[ $mreason == Completed && $mexit == 0 ]]; then ok "init container migrate Completed (exit 0)"; else bad "init container migrate: reason=${mreason:-none} exit=${mexit:-none}"; fi
    if [[ " $envs " == *" HARNESS_CLIENT_TOKEN_FILE "* ]]; then ok "harness env HARNESS_CLIENT_TOKEN_FILE set (token mode)"; else bad "harness env lacks HARNESS_CLIENT_TOKEN_FILE"; fi
    if [[ " $envs " == *" HARNESS_CLIENT_SECRET "* ]]; then bad "harness env carries HARNESS_CLIENT_SECRET"; else ok "harness env has no HARNESS_CLIENT_SECRET"; fi
  done <<<"$lines"
  ((n == 1)) || bad "harness: expected exactly one pod (Recreate, one replica), found $n"
  if k get serviceaccount "$HARNESS_SA" -o name >/dev/null; then ok "serviceaccount $HARNESS_SA present"; else bad "serviceaccount $HARNESS_SA missing"; fi
  if k get ingressroute "$HARNESS_DEPLOY" -o name >/dev/null; then ok "ingressroute $HARNESS_DEPLOY present"; else bad "ingressroute $HARNESS_DEPLOY missing"; fi
}

edge_check() {
  section "Edge"
  local code
  code=$(curl -sS -o /dev/null -w '%{http_code}' --max-time 20 "$EDGE_URL") || code=${code:-000}
  code=${code//$'\r'/}
  case "$code" in
    401) ok "unauthenticated GET $EDGE_URL -> 401" ;;
    302 | 303 | 307) bad "unauthenticated GET $EDGE_URL -> $code, a login redirect (#1959: gatekeeper-auth must answer 401 on /api/)" ;;
    *) bad "unauthenticated GET $EDGE_URL -> $code, expected 401" ;;
  esac
}

reminders() {
  section "Reminder: #2092's checks this script cannot run"
  cat <<'EOF'
  - The @api assistant journeys (tests/e2e/journeys/assistant/api-*.spec.ts, flows
    tests/e2e/flows/assistant/api-*.md) pass live against https://strive.place with the persona,
    one worker.
  - report-ailab-pin-drift shows 0 torn: from a platform checkout at main, as
    .github/workflows/ailab-pin-guard.yml runs it:
      uv run --quiet python3 scripts/ci/report-ailab-pin-drift.py --count-app-templates --fail-on-torn --fail-on-incoherent
  Both are part of the activation: a failure there darkens the harness too.
EOF
}

# single_mint POD: "MINT <status> <error>" from the held token.
single_mint() {
  local out
  out=$(run_in_pod "$1" single) || true
  printf '%s\n' "$out" | tr -d '\r' | sed -n 's/^\(MINT [0-9]* [-a-z_]*\)$/\1/p' | head -n 1
}

# held_token_exp TOKEN: the token's own `exp` claim, decoded here. Only the number is output, never
# the token or its claims.
held_token_exp() {
  local payload=${1#*.}
  payload=${payload%%.*}
  payload=${payload//-/+}
  payload=${payload//_//}
  case $((${#payload} % 4)) in
    2) payload+='==' ;;
    3) payload+='=' ;;
  esac
  printf '%s' "$payload" | base64 -d 2>/dev/null | tr -d '\r\n' | sed -n 's/.*"exp":[[:space:]]*\([0-9][0-9]*\).*/\1/p'
}

revocation_drill() {
  section "Revocation drill: the held bearer"
  local hp uid hp_lines t_start token_exp held_exp t_del='' t_gone='' now st pod res status code elapsed
  local MINT_DURATION="${HELD_TOKEN_SECONDS}s" # read by mint_into (dynamic scope)
  if ! hp_lines=$(k get pods -l "$HARNESS_LABEL" -o jsonpath='{range .items[*]}{.metadata.name}{"|"}{.metadata.uid}{"|"}{.status.conditions[?(@.type=="Ready")].status}{"|"}{.metadata.deletionTimestamp}{"\n"}{end}'); then
    bad "revocation: kubectl get pods -l $HARNESS_LABEL failed"
    return 1
  fi
  hp_lines=${hp_lines//$'\r'/}
  hp='' uid=''
  local count=0 name puid ready deleting
  while IFS='|' read -r name puid ready deleting; do
    [[ -n $name ]] || continue
    count=$((count + 1))
    [[ $ready == True && -z $deleting ]] && hp=$name uid=$puid
  done <<<"$hp_lines"
  if ((count != 1)) || [[ -z $hp ]]; then
    bad "revocation: expected exactly one Ready harness pod, found $count"
    return 1
  fi
  t_start=$(date +%s)
  mint_into HELD "$HARNESS_SA" "$AUDIENCE" --bound-object-kind Pod --bound-object-name "$hp" --bound-object-uid "$uid" || return 1
  # Observe until the token's OWN exp (what the API server issued, not what was asked for). A token
  # that outlives the cap cannot be watched to its end, and a partial watch proves nothing about
  # the rest: stop now, INCOMPLETE, before any probe and before the owner is asked to revoke.
  token_exp=$(held_token_exp "$HELD")
  if [[ ! $token_exp =~ ^[0-9]+$ ]] || ((token_exp <= t_start)); then
    bad "revocation: cannot read the held token's exp (or it is already past)"
    return 1
  fi
  if ((token_exp - t_start > MAX_WATCH_SECONDS)); then
    incomplete "token lifetime not fully observed (exp in $((token_exp - t_start))s > cap ${MAX_WATCH_SECONDS}s)" \
      "Nothing was probed and nothing was revoked. Re-run with PHASE4_MAX_WATCH_SECONDS=$((token_exp - t_start + 60)) (at least the token's lifetime), or with PHASE4_HELD_TOKEN_SECONDS at or below the cap if the API server honours it."
  fi
  held_exp=$token_exp
  info "holding a token bound to pod $hp (uid $uid); it expires at +$((token_exp - t_start))s ($(date -u -d "@$token_exp" +%H:%M:%SZ 2>/dev/null || echo "epoch $token_exp")); the drill watches until then"
  for pod in "${PODS[@]}"; do
    res=$(single_mint "$pod")
    if [[ $res == "MINT 200 -" ]]; then ok "$pod accepts the held bearer before the revocation"; else bad "$pod: the held bearer before the revocation -> ${res:-no answer}, expected MINT 200"; fi
  done
  ((${#FAILURES[@]} == 0)) || return 1

  section "Revoke now, in ANOTHER terminal (this script only watches)"
  freeze_commands
  info "waiting for pod $hp to be removed (polling every ${POD_POLL_SECONDS}s)"
  while :; do
    now=$(date +%s)
    if ((now >= held_exp - REMOVAL_MARGIN_SECONDS)); then
      bad "revocation: pod $hp was not removed ${REMOVAL_MARGIN_SECONDS}s or more before the end of the observation (the held token's expiry)"
      return 1
    fi
    if ! st=$(k get pod "$hp" --ignore-not-found -o jsonpath='{.metadata.uid}{"|"}{.metadata.deletionTimestamp}'); then
      sleep "$POD_POLL_SECONDS"
      continue
    fi
    now=$(date +%s) # when this observation returned
    st=${st//$'\r'/}
    if [[ -z $st || ${st%%|*} != "$uid" ]]; then
      t_gone=$now
      info "pod $hp removed at +$((t_gone - t_start))s"
      break
    fi
    if [[ -z $t_del && -n ${st#*|} ]]; then
      t_del=$now
      info "pod $hp deletionTimestamp ${st#*|} seen at +$((t_del - t_start))s"
    fi
    sleep "$POD_POLL_SECONDS"
  done

  # Every replica is polled until the held token's expiry. Each attempt is timed by when ITS request
  # was sent, and is exactly one of:
  #   - accepted (200): fine before the replica's first refusal, a FAIL after it;
  #   - refused (any other HTTP status; the expected one is 503, GC4): the first one is held against
  #     the bound, measured from the pod's removal;
  #   - no answer (no MINT line, or `MINT 0`: transport or exec failure): neither a refusal nor an
  #     acceptance. MAX_NO_ANSWER in a row on one replica is a FAIL: it could not be observed.
  # A round is "refused" when EVERY replica refused in it; any other round resets that streak. The
  # end state must be observed: the last two rounds before the expiry both refused.
  section "Rejection, per replica, until the held token's expiry"
  declare -A refused_at=() silent=()
  local streak=0 decided=0 all_refused t_req
  while :; do
    now=$(date +%s)
    ((now < held_exp)) || break
    all_refused=1
    for pod in "${PODS[@]}"; do
      t_req=$(date +%s)
      res=$(single_mint "$pod")
      status='' code=''
      read -r _ status code <<<"$res"
      if [[ ! $status =~ ^[1-9][0-9][0-9]$ ]]; then
        all_refused=0
        silent[$pod]=$((${silent[$pod]:-0} + 1))
        info "$pod: no answer at +$((t_req - t_gone))s after removal (${silent[$pod]} in a row; not a refusal)"
        if ((${silent[$pod]} >= MAX_NO_ANSWER)); then
          bad "could not observe $pod: ${silent[$pod]} consecutive attempts without an HTTP answer"
          decided=1
          break
        fi
        continue
      fi
      silent[$pod]=0
      if [[ $status == 200 ]]; then
        all_refused=0
        if [[ -n ${refused_at[$pod]:-} ]]; then
          bad "$pod: accepted the held bearer AGAIN at +$((t_req - t_gone))s after removal, having refused it at +$((${refused_at[$pod]} - t_gone))s"
          decided=1
          break
        fi
      elif [[ -z ${refused_at[$pod]:-} ]]; then
        refused_at[$pod]=$t_req
        info "$pod: refused at +$((t_req - t_gone))s after removal ($status $code)"
      fi
    done
    ((decided)) && break
    if ((all_refused)); then streak=$((streak + 1)); else streak=0; fi
    now=$(date +%s)
    for pod in "${PODS[@]}"; do
      if [[ -z ${refused_at[$pod]:-} ]] && ((now - t_gone > REVOCATION_BOUND)); then
        bad "$pod did not refuse the revoked bearer within the ${REVOCATION_BOUND}s bound (not refused at +$((now - t_gone))s after removal)"
        decided=1
      fi
    done
    ((decided)) && break
    sleep "$POLL_SECONDS"
  done

  if ((!decided)); then
    for pod in "${PODS[@]}"; do
      if [[ -z ${refused_at[$pod]:-} ]]; then
        bad "$pod never refused the held bearer before the end of the observation"
        continue
      fi
      elapsed=$((${refused_at[$pod]} - t_gone))
      if ((elapsed <= REVOCATION_BOUND)); then
        ok "$pod rejected the revoked bearer ${elapsed}s after the pod's removal (bound ${REVOCATION_BOUND}s${t_del:+; deletionTimestamp seen $((t_gone - t_del))s before removal})"
      else
        bad "$pod rejected the revoked bearer ${elapsed}s after the pod's removal, over the ${REVOCATION_BOUND}s bound"
      fi
      # Reached only after watching to the token's own exp: a capped watch never gets here.
      ok "$pod never accepted the revoked bearer again before its expiry"
    done
    if ((streak >= 2)); then
      ok "observed until the held token's expiry: the last $streak rounds were refused by every replica"
    else
      bad "revocation: only $streak consecutive refused rounds at the end of the observation (2 needed): the end state was not observed"
    fi
  fi
  section "Restore, once the drill is over (it lands platform main, then scales the harness to 1)"
  resume_commands drill
  cat <<EOF
  then: scripts/s2s/phase4-probes.sh   (the full post-flip check again)
EOF
}

command -v base64 >/dev/null && command -v sha256sum >/dev/null || { echo "phase4-probes: needs base64 and sha256sum" >&2; exit 2; }
PROG_SHA=$(sha256sum "$PROG" | cut -d' ' -f1)
PROG_B64=$(base64 <"$PROG" | tr -d '\r\n')

if ((DRY_RUN)); then
  print_plan
  exit 0
fi

command -v kubectl >/dev/null || { echo "phase4-probes: kubectl not found" >&2; exit 2; }
if ((!GATEKEEPER_ONLY && !REVOCATION)); then
  command -v curl >/dev/null || { echo "phase4-probes: curl not found" >&2; exit 2; }
fi

printf 'S2S Phase 4 probes: context %s, namespace %s, gatekeeper namespace %s, program sha256 %s\n' "$CONTEXT" "$NS" "$GK_NS" "$PROG_SHA"

discover_replicas || true
# Probe nothing while a roll is in flight or a named replica is missing.
((${#FAILURES[@]} == 0 && ${#PODS[@]} > 0)) || finish

if ((REVOCATION)); then
  revocation_drill || true
  finish
fi

section "Tokens (TokenRequest, ${TOKEN_TTL}; on stdin only, never printed)"
if ((EXPECT_REFUSED)) && ! k get serviceaccount "$HARNESS_SA" -o name >/dev/null 2>&1; then
  info "serviceaccount $HARNESS_SA is absent (harness rolled back): no pod can hold svc-harness; the harness-token cases are skipped"
else
  mint_into HT "$HARNESS_SA" "$AUDIENCE" && ok "harness token (audience $AUDIENCE)"
  mint_into WT "$HARNESS_SA" "$WRONG_AUDIENCE" && ok "harness token for the wrong audience ($WRONG_AUDIENCE)"
fi
mint_into AT "$ALT_SA" "$AUDIENCE" && ok "$ALT_SA ServiceAccount token (audience $AUDIENCE)"
((${#FAILURES[@]} == 0)) || finish

if ((EXPECT_REFUSED)); then
  probe_all refused
else
  probe_all probe
  registry_agreement
fi

if ((!GATEKEEPER_ONLY)); then
  harness_checks
  edge_check
  reminders
fi
finish

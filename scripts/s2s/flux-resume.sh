#!/usr/bin/env bash
# Resume the frozen strive release in the only safe order, proving at each step that Flux landed the
# intended platform commit before anything further is un-frozen.
#
# The S2S darken and drills (docs/runbooks/s2s-identity.md, "Phase 4") freeze the release: the Flux
# Kustomization flux-system/platform-app first, then the HelmRelease strive-ailab/strive. Resuming
# by hand is unsafe in two ways:
#   - Un-suspending the HelmRelease first lets helm-controller upgrade a stale spec. Mid-rollback,
#     that brings the harness back before the reverted helmrelease.yaml is applied.
#   - Un-suspending the Kustomization before its source has fetched the merged commit re-applies
#     the OLD HelmRelease. That clears its suspend, and the old spec upgrades the same way. Waiting
#     on observedGeneration proves nothing: it also moves when suspend is toggled.
#
# The script does this, and stops (exit 1) at the first gate that does not pass. Nothing after a
# failed gate is touched.
#   0. Target: --sha, else platform main now (git ls-remote).
#   1. Source: request a reconcile of GitRepository flux-system/platform, then wait until
#      .status.artifact.revision is main@sha1:<target>. Without --sha it may instead be
#      main@sha1:<platform main now>: main is protected and append-only, so a later main descends
#      from the target, and it then becomes the target. With --sha it must be exactly that commit.
#      Nothing is resumed before this.
#   2. Kustomization: un-suspend platform-app, then wait until .status.lastAppliedRevision is
#      main@sha1:<target> and Ready is True.
#   3. HelmRelease: un-suspend it ONLY if .spec.suspend still reads true (the Kustomization's
#      re-apply normally clears it, as observed on 2026-10-06 at 17:22:39Z). Then wait until Ready
#      is True and .status.history[0].chartVersion carries the target's first 12 hex
#      (reconcileStrategy: Revision gives <version>+<sha12>...).
#   4. --after-revert: deployment/harness must be gone. --after-drill: scale it to 1 and wait for
#      the rollout; then run scripts/s2s/phase4-probes.sh. --after-config (a config-only rollback or
#      re-forward that keeps the harness, drill 2 as redesigned on 2026-10-08): deployment/harness must
#      still exist and its rollout must be complete.
#   5. The parents: Kustomizations flux-system/platform (it applies platform-app) and then
#      flux-system/flux-system (the root), each resumed only if it is suspended, then Ready required.
#
# THE FREEZE IS TOP-DOWN (incident AG2-1, 2026-10-08): a parent Kustomization re-applies its children
# and drops a `kubectl patch` suspend, so a freeze of platform-app and the HelmRelease alone was undone
# twice within minutes. Freeze flux-system, then platform, then the children; this script resumes the
# children first and the parents last. Parents that are not suspended get a WARNING at step 0.
#
# OWNER-RUN, from a machine that is not a dev worker, with the `admin@ai` context and git access to
# cchifor/platform. Git Bash on Windows or bash on Linux.
#
# Exit 0: landed. Exit 1: STOP at a gate (what is resumed and what is not is printed). Exit 2: bad
# invocation.
set -euo pipefail
set +x
export MSYS_NO_PATHCONV=1

CONTEXT=admin@ai
FLUX_NS=flux-system
SOURCE=platform
KUSTOMIZATION=platform-app
NS=strive-ailab
HELMRELEASE=strive
HARNESS=harness
PLATFORM_REMOTE=https://git.chifor.me/cchifor/platform.git
TIMEOUT=${PHASE4_RESUME_TIMEOUT_SECONDS:-600}
POLL=${PHASE4_RESUME_POLL_SECONDS:-10}

usage() {
  cat <<'EOF'
usage: scripts/s2s/flux-resume.sh (--after-revert | --after-drill | --after-config) [--sha SHA] [--dry-run]

  --after-revert  after the durable revert (harness.enabled: false) has MERGED on platform main:
                  lands it, then requires deployment/harness to be gone
  --after-drill   once a drill is over: lands platform main, then scales the harness back to 1
  --after-config  a config-only rollback or re-forward that keeps the harness: lands it, then requires
                  deployment/harness to exist and be rolled out
  --sha SHA       the platform commit to land (40 hex), e.g. the revert's merge commit. The source
                  must then carry exactly that commit. Default: platform main now, or any later
                  main (main is append-only, so it descends from the target)
  --dry-run       print the steps; no cluster or git call

Each gate waits at most PHASE4_RESUME_TIMEOUT_SECONDS (default 600), polling every
PHASE4_RESUME_POLL_SECONDS (default 10). Exit 0 = landed, 1 = STOP at a gate, 2 = bad invocation.
EOF
}

MODE='' SHA='' DRY_RUN=0 PINNED=0
while (($#)); do
  case "$1" in
    --after-revert | --after-drill | --after-config)
      [[ -z $MODE ]] || { echo "flux-resume: choose one of --after-revert, --after-drill and --after-config" >&2; exit 2; }
      MODE=${1#--after-}
      ;;
    --sha)
      [[ $# -ge 2 && $2 =~ ^[0-9a-f]{40}$ ]] || { echo "flux-resume: --sha needs a full 40-hex commit" >&2; exit 2; }
      SHA=$2
      PINNED=1
      shift
      ;;
    --dry-run) DRY_RUN=1 ;;
    -h | --help) usage; exit 0 ;;
    *) echo "flux-resume: unknown argument: $1" >&2; usage >&2; exit 2 ;;
  esac
  shift
done
[[ -n $MODE ]] || { echo "flux-resume: --after-revert, --after-drill or --after-config is required" >&2; usage >&2; exit 2; }

f() { kubectl --context "$CONTEXT" -n "$FLUX_NS" "$@"; }
h() { kubectl --context "$CONTEXT" -n "$NS" "$@"; }
step() { printf '\n== %s\n' "$*"; }
stop() {
  printf '\nSTOP: %s\n' "$1"
  printf '  %s\n' "$2"
  exit 1
}

# main_sha: platform main now, or nothing (never a partial or malformed value).
main_sha() {
  local s
  s=$(git ls-remote "$PLATFORM_REMOTE" refs/heads/main 2>/dev/null | cut -f1 | head -n 1) || true
  s=${s//$'\r'/}
  if [[ $s =~ ^[0-9a-f]{40}$ ]]; then printf '%s' "$s"; fi
  return 0
}

# wait_until CHECK: run CHECK until it succeeds, for at most TIMEOUT seconds.
wait_until() {
  local deadline=$(($(date +%s) + TIMEOUT))
  while :; do
    "$1" && return 0
    (($(date +%s) >= deadline)) && return 1
    sleep "$POLL"
  done
}

ART='' KS='' HR=''
source_at_target() {
  local cur
  ART=$(f get gitrepository "$SOURCE" -o jsonpath='{.status.artifact.revision}') || return 1
  ART=${ART//$'\r'/}
  [[ $ART == *"@sha1:$SHA" ]] && return 0
  # A later main descends from the target only when the target was itself read from main.
  ((PINNED)) && return 1
  cur=$(main_sha)
  if [[ -n $cur && $cur != "$SHA" && $ART == *"@sha1:$cur" ]]; then
    printf 'platform main moved on to %s (a descendant: main is append-only); landing that\n' "$cur"
    SHA=$cur
    return 0
  fi
  return 1
}

ks_applied() {
  local susp applied ready
  KS=$(f get kustomization "$KUSTOMIZATION" -o jsonpath='{.spec.suspend}{"|"}{.status.lastAppliedRevision}{"|"}{.status.conditions[?(@.type=="Ready")].status}') || return 1
  KS=${KS//$'\r'/}
  IFS='|' read -r susp applied ready <<<"$KS"
  [[ $susp != true && $applied == *"@sha1:$SHA" && $ready == True ]]
}

hr_read() {
  HR=$(h get helmrelease "$HELMRELEASE" -o jsonpath='{.spec.suspend}{"|"}{.status.history[0].chartVersion}{"|"}{.status.conditions[?(@.type=="Ready")].status}') || return 1
  HR=${HR//$'\r'/}
}

hr_upgraded() {
  local susp chart ready
  hr_read || return 1
  IFS='|' read -r susp chart ready <<<"$HR"
  [[ $susp != true && $chart == *"${SHA:0:12}"* && $ready == True ]]
}

# The parents of platform-app, innermost first: flux-system/platform applies it, flux-system/flux-system
# (the root) applies platform.
PARENTS=(platform flux-system)
PARENT='' PARENT_STATE='' REQUESTED=''
# parent_read NAME: PARENT_STATE = suspend|lastHandledReconcileAt|Ready. A failed read is an error (return 1),
# never "not suspended": a transient API error must not drop a frozen parent from the resume list.
parent_read() {
  PARENT_STATE=$(f get kustomization "$1" -o jsonpath='{.spec.suspend}{"|"}{.status.lastHandledReconcileAt}{"|"}{.status.conditions[?(@.type=="Ready")].status}') || return 1
  PARENT_STATE=${PARENT_STATE//$'\r'/}
  [[ $PARENT_STATE == *"|"*"|"* ]]
}
# parent_reconciled: resumed AND it handled the reconcile requested at REQUESTED AND Ready. A Ready condition
# alone can be the one from before the freeze.
parent_reconciled() {
  local susp handled ready
  parent_read "$PARENT" || return 1
  IFS='|' read -r susp handled ready <<<"$PARENT_STATE"
  [[ $susp != true && $handled == "$REQUESTED" && $ready == True ]]
}

# harness_up: deployment/harness exists with at least one available replica (rollout status alone succeeds
# on a deployment scaled to zero).
harness_up() {
  local out spec avail
  out=$(h get deployment "$HARNESS" --ignore-not-found -o name) || return 1
  [[ -n ${out//$'\r'/} ]] || { HARNESS_WHY="is missing"; return 1; }
  out=$(h get deployment "$HARNESS" -o jsonpath='{.spec.replicas}{"|"}{.status.availableReplicas}') || return 1
  out=${out//$'\r'/}
  IFS='|' read -r spec avail <<<"$out"
  [[ ${avail:-0} -ge 1 ]] || { HARNESS_WHY="has no available replica (spec.replicas=${spec:-?})"; return 1; }
}
HARNESS_WHY=''

harness_gone() {
  local out
  out=$(h get deployment "$HARNESS" --ignore-not-found -o name) || return 1
  [[ -z ${out//$'\r'/} ]]
}

if ((DRY_RUN)); then
  cat <<EOF
flux-resume ($MODE), dry run: no cluster or git call. The steps, each gated (at most ${TIMEOUT}s):
  0. target = ${SHA:-platform main now: git ls-remote $PLATFORM_REMOTE refs/heads/main}
  1. kubectl --context $CONTEXT -n $FLUX_NS annotate gitrepository $SOURCE --overwrite reconcile.fluxcd.io/requestedAt=<now>
     wait: kubectl --context $CONTEXT -n $FLUX_NS get gitrepository $SOURCE -o jsonpath='{.status.artifact.revision}'
           is main@sha1:<target> (or a later platform main); NOTHING is resumed before this
  2. kubectl --context $CONTEXT -n $FLUX_NS patch kustomization $KUSTOMIZATION --type=merge -p '{"spec":{"suspend":false}}'
     wait: .status.lastAppliedRevision is main@sha1:<target> and Ready is True
  3. only if kubectl --context $CONTEXT -n $NS get helmrelease $HELMRELEASE -o jsonpath='{.spec.suspend}' is still true:
       kubectl --context $CONTEXT -n $NS patch helmrelease $HELMRELEASE --type=merge -p '{"spec":{"suspend":false}}'
     wait: Ready is True and .status.history[0].chartVersion carries <target>'s first 12 hex
EOF
  if [[ $MODE == revert ]]; then
    echo "  4. wait: kubectl --context $CONTEXT -n $NS get deployment $HARNESS --ignore-not-found is empty (the harness is gone)"
  elif [[ $MODE == config ]]; then
    echo "  4. require: kubectl --context $CONTEXT -n $NS get deployment/$HARNESS exists with .status.availableReplicas >= 1"
  else
    echo "  4. kubectl --context $CONTEXT -n $NS scale deployment/$HARNESS --replicas=1; rollout status; then scripts/s2s/phase4-probes.sh"
  fi
  for p in "${PARENTS[@]}"; do
    echo "  5. only if suspended: kubectl --context $CONTEXT -n $FLUX_NS patch kustomization $p --type=merge -p '{\"spec\":{\"suspend\":false}}'; annotate reconcile.fluxcd.io/requestedAt=<now>; wait: lastHandledReconcileAt == <now> and Ready"
  done
  exit 0
fi

command -v kubectl >/dev/null || { echo "flux-resume: kubectl not found" >&2; exit 2; }
command -v git >/dev/null || { echo "flux-resume: git not found" >&2; exit 2; }

step "0. Target commit"
[[ -n $SHA ]] || SHA=$(main_sha)
[[ $SHA =~ ^[0-9a-f]{40}$ ]] || stop "cannot read platform main (git ls-remote $PLATFORM_REMOTE refs/heads/main)" \
  "Nothing was changed: the Kustomization and the HelmRelease stay suspended."
echo "landing platform $SHA ($MODE)"
SUSPENDED_PARENTS=()
for p in "${PARENTS[@]}"; do
  parent_read "$p" || stop "cannot read Kustomization $FLUX_NS/$p" \
    "Nothing was changed: everything frozen stays frozen. Re-run once the API answers."
  if [[ ${PARENT_STATE%%|*} == true ]]; then
    SUSPENDED_PARENTS+=("$p")
  else
    echo "WARNING: Kustomization $FLUX_NS/$p is not suspended: the freeze is not top-down, so its next reconcile can clear the children's suspends (R19)."
  fi
done
[[ ${#SUSPENDED_PARENTS[@]} -gt 0 ]] && echo "suspended parents, resumed last: ${SUSPENDED_PARENTS[*]}"

step "1. Source: GitRepository $FLUX_NS/$SOURCE must carry $SHA before anything is resumed"
f annotate gitrepository "$SOURCE" --overwrite "reconcile.fluxcd.io/requestedAt=$(date -u +%Y-%m-%dT%H:%M:%SZ)" >/dev/null
wait_until source_at_target || stop "GitRepository $FLUX_NS/$SOURCE is at '${ART:-?}', not main@sha1:$SHA, after ${TIMEOUT}s" \
  "Nothing was resumed: the Kustomization and the HelmRelease stay suspended. Re-run once the source has fetched the commit."
echo "source at $ART"

step "2. Kustomization $FLUX_NS/$KUSTOMIZATION: resume, then require it applied $SHA"
f patch kustomization "$KUSTOMIZATION" --type=merge -p '{"spec":{"suspend":false}}' >/dev/null
wait_until ks_applied || stop "Kustomization $FLUX_NS/$KUSTOMIZATION reads '${KS:-?}' (suspend|lastAppliedRevision|Ready), not applied main@sha1:$SHA, after ${TIMEOUT}s" \
  "The Kustomization is resumed but did not apply the commit; the HelmRelease was not touched. Investigate, or freeze again (Kustomization first)."
echo "Kustomization applied ${KS#*|}"

step "3. HelmRelease $NS/$HELMRELEASE: resume only if still suspended, then require the upgrade to $SHA"
hr_read || stop "cannot read HelmRelease $NS/$HELMRELEASE" "The Kustomization is resumed; the HelmRelease was not touched."
if [[ ${HR%%|*} == true ]]; then
  echo "still suspended after the Kustomization applied $SHA: resuming it"
  h patch helmrelease "$HELMRELEASE" --type=merge -p '{"spec":{"suspend":false}}' >/dev/null
else
  echo "not suspended: the Kustomization's re-apply cleared it"
fi
wait_until hr_upgraded || stop "HelmRelease $NS/$HELMRELEASE reads '${HR:-?}' (suspend|chartVersion|Ready), not upgraded to ${SHA:0:12}, after ${TIMEOUT}s" \
  "Flux is resumed; the release did not reach the commit. Investigate the HelmRelease, or freeze again (Kustomization first)."
echo "HelmRelease upgraded: chart ${HR#*|}"

if [[ $MODE == revert ]]; then
  step "4. The revert: deployment/$HARNESS must be gone"
  wait_until harness_gone || stop "deployment/$HARNESS still exists after the upgrade to $SHA" \
    "Is the revert (harness.enabled: false) in that commit? Freeze again (Kustomization first) and scale the harness to 0."
  echo "deployment/$HARNESS is gone"
elif [[ $MODE == config ]]; then
  step "4. A config-only change keeps the harness: deployment/$HARNESS must be up"
  h rollout status "deployment/$HARNESS" --timeout="${TIMEOUT}s" >/dev/null || stop "deployment/$HARNESS did not finish rolling out after the upgrade to $SHA" \
    "The parents stay suspended. Investigate the harness rollout."
  harness_up || stop "deployment/$HARNESS $HARNESS_WHY after the upgrade to $SHA" \
    "A config-only rollback or re-forward keeps the harness up (harness.enabled: true). Investigate before resuming the parents; they stay suspended."
  echo "deployment/$HARNESS is up"
else
  step "4. After the drill: the harness back to one replica"
  h scale "deployment/$HARNESS" --replicas=1 >/dev/null
  h rollout status "deployment/$HARNESS" --timeout="${TIMEOUT}s"
  echo "Now run the full check: scripts/s2s/phase4-probes.sh"
fi
if [[ ${#SUSPENDED_PARENTS[@]} -gt 0 ]]; then
  step "5. The parents, innermost first: ${SUSPENDED_PARENTS[*]}"
  for PARENT in "${SUSPENDED_PARENTS[@]}"; do
    f patch kustomization "$PARENT" --type=merge -p '{"spec":{"suspend":false}}' >/dev/null
    REQUESTED=$(date -u +%Y-%m-%dT%H:%M:%SZ)
    f annotate kustomization "$PARENT" --overwrite "reconcile.fluxcd.io/requestedAt=$REQUESTED" >/dev/null
    wait_until parent_reconciled || stop "Kustomization $FLUX_NS/$PARENT reads '${PARENT_STATE:-?}' (suspend|lastHandledReconcileAt|Ready), not a fresh reconcile of $REQUESTED, after ${TIMEOUT}s" \
      "The release has landed; the remaining parents stay suspended. Investigate $PARENT."
    echo "Kustomization $FLUX_NS/$PARENT resumed, reconciled ($REQUESTED) and Ready"
  done
fi
printf '\nRESUMED: platform %s applied by the Kustomization and the HelmRelease\n' "$SHA"

#!/usr/bin/env bash
# Runs a Gitea version's own workflow parser over every Actions workflow at HEAD of every repository on the
# forge. Gitea 28.x drops a workflow it cannot parse with only a log warning ("ignore invalid workflow"), so
# that workflow's CI silently stops. 2026-11-16: platform's ci.yml used `matrix` in a job-level `if:`, which
# 1.27 accepted and 28.4.0 rejects. Run it in step 2 of docs/runbooks/gitea-upgrade.md with the TARGET
# version (28.0 or later: older trees lack modules/actions/jobparser). It only reads from the forge:
# `git ls-tree` and `git cat-file` in the Gitea pod (gitea-workflow-check/export.sh).
#
# Usage: scripts/gitea-workflow-check.sh <gitea-version>     (for example 28.4.0)
# Needs kubectl (context admin@ai), docker, curl, tar and base64. Exit 0: every workflow parses; 1: some do
# not; 2: the check itself failed.
set -euo pipefail

here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd -P)
host_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }
die() { echo "gitea-workflow-check: $*" >&2; exit 2; }
work="" exported=0

# Reads export.sh's stream on stdin and writes each workflow as a regular file under $1/<owner>/<repo>/.
# The file name is a counter plus the Git path with every unsafe character replaced, so a crafted path cannot
# leave the directory. Fails unless the stream ends with =====END.
decode_stream() {
  local out=$1 line rest repo mode path data safe n=0 complete=0
  while IFS= read -r line; do
    case $line in
      "=====FILE "*)
        rest=${line#=====FILE }; repo=${rest%% *}; rest=${rest#* }; mode=${rest%% *}; path=${rest#* }
        [[ $repo =~ ^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$ && $repo != *..* ]] || die "unexpected repository name: $repo"
        IFS= read -r data || die "stream ended inside $repo $path"
        # Drop git's quotes around an unusual name, so the .yml/.yaml suffix survives into the file name.
        [[ $path == \"*\" ]] && path=${path:1:${#path}-2}
        n=$((n + 1)); safe=$(printf '%s' "$path" | tr -c 'A-Za-z0-9._-' '_')
        mkdir -p "$out/$repo"
        printf '%s' "$data" | base64 -d > "$out/$repo/$n-$safe" || die "cannot decode $repo $path"
        ;;
      "=====EMPTY "*) ;;
      "=====END") complete=1 ;;
      *) die "from the pod: $line" ;;
    esac
  done
  [ "$complete" = 1 ] || die "the export did not finish (no =====END)"
  exported=$n
  echo "exported $n workflow files"
}

main() {
  local ver=${1:?usage: gitea-workflow-check.sh <gitea-version>} src toolchain pod rc=0
  work=$(mktemp -d)   # global: the EXIT trap runs after main has returned
  trap 'rm -rf "$work"' EXIT
  local k=(kubectl --context admin@ai -n gitea)

  # The target version's source; only go.mod, go.sum and modules/ are compiled.
  curl -fsSL "https://github.com/go-gitea/gitea/archive/refs/tags/v$ver.tar.gz" | tar -xz -C "$work" ||
    die "cannot download the source of gitea $ver"
  src="$work/gitea-$ver"
  [ -d "$src/modules/actions/jobparser" ] || die "gitea $ver has no modules/actions/jobparser (28.0 or later only)"
  toolchain=$(sed -n 's/^toolchain go//p' "$src/go.mod")
  [ -n "$toolchain" ] || toolchain=$(sed -n 's/^go //p' "$src/go.mod")

  pod=$("${k[@]}" get pods -l app.kubernetes.io/name=gitea -o jsonpath='{.items[0].metadata.name}') || die "kubectl"
  [ -n "$pod" ] || die "no Gitea pod"
  mkdir "$work/files"
  "${k[@]}" exec -i "$pod" -c gitea -- sh -s < "$here/gitea-workflow-check/export.sh" > "$work/export.txt" ||
    die "the export failed: $(grep '^=====ERROR' "$work/export.txt" || echo 'kubectl exec')"
  decode_stream "$work/files" < "$work/export.txt"

  # Exit 1 comes only from the checker's verdict. A build or run failure, or a checked count that differs from
  # the exported one, prints no "checked" line: exit 3.
  MSYS_NO_PATHCONV=1 docker run --rm \
    -v "$(host_path "$src"):/src-ro:ro" -v "$(host_path "$work/files"):/files:ro" \
    -v "$(host_path "$here/gitea-workflow-check"):/tool:ro" -v gitea-workflow-check-gomod:/go/pkg/mod \
    "golang:$toolchain" sh -c 'mkdir -p /src/cmd/wfcheck && cp /src-ro/go.mod /src-ro/go.sum /src/ &&
      cp -r /src-ro/modules /src/ && cp /tool/main.go /src/cmd/wfcheck/ && cd /src &&
      GOFLAGS=-mod=mod go run ./cmd/wfcheck /files "$1" > /tmp/out 2>&1; rc=$?
      grep -v "^go: downloading" /tmp/out
      grep -q "^checked [0-9]* workflow files" /tmp/out || exit 3; exit "$rc"' sh "$exported" || rc=$?
  [ "$rc" -le 1 ] || die "the checker did not run to the end (exit $rc)"
  return "$rc"
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then main "$@"; fi

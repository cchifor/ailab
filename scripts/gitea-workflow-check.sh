#!/usr/bin/env bash
# Runs a Gitea version's own workflow parser over every Actions workflow at HEAD of every repository on the
# forge. Gitea 28.x drops a workflow it cannot parse with only a log warning ("ignore invalid workflow"), so
# that workflow's CI silently stops. 2026-11-16: platform's ci.yml used `matrix` in a job-level `if:`, which
# 1.27 accepted and 28.4.0 rejects. Run it in step 2 of docs/runbooks/gitea-upgrade.md with the TARGET
# version. It only reads from the forge: `git ls-tree` and `git archive` in the Gitea pod.
#
# Usage: scripts/gitea-workflow-check.sh <gitea-version>     (for example 28.4.0)
# Needs kubectl (context admin@ai), docker, curl and tar. Exit 0: every workflow parses; 1: some do not.
# Every workflow directory is checked, including one Gitea ignores because `.gitea/workflows` exists.
set -euo pipefail

ver=${1:?usage: gitea-workflow-check.sh <gitea-version>}
here=$(cd "$(dirname "$0")" && pwd -P)
work=$(mktemp -d)
trap 'rm -rf "$work"' EXIT
k=(kubectl --context admin@ai -n gitea)
host_path() { if command -v cygpath >/dev/null 2>&1; then cygpath -w "$1"; else printf '%s' "$1"; fi; }

# The target version's source; only go.mod, go.sum and modules/ are compiled.
curl -fsSL "https://github.com/go-gitea/gitea/archive/refs/tags/v$ver.tar.gz" | tar -xz -C "$work"
src="$work/gitea-$ver"
[ -d "$src/modules/actions/jobparser" ] || { echo "gitea $ver has no modules/actions/jobparser" >&2; exit 2; }
toolchain=$(sed -n 's/^toolchain go//p' "$src/go.mod")
[ -n "$toolchain" ] || toolchain=$(sed -n 's/^go //p' "$src/go.mod")

# Every workflow directory at HEAD of every bare repository, as one stream of tar archives.
pod=$("${k[@]}" get pods -l app.kubernetes.io/name=gitea -o jsonpath='{.items[0].metadata.name}')
mkdir "$work/files"
"${k[@]}" exec -i "$pod" -c gitea -- sh -s <<'POD' | tar -x -i -f - -C "$work/files"
cd /data/git/gitea-repositories || exit 1
for d in */*.git; do
  dirs=""
  for dir in .gitea/workflows .github/workflows .gitea/scoped_workflows; do
    [ -n "$(git -C "$d" ls-tree -d HEAD -- "$dir" 2>/dev/null)" ] && dirs="$dirs $dir"
  done
  # shellcheck disable=SC2086 # $dirs is a list of fixed paths without blanks
  [ -n "$dirs" ] && git -C "$d" archive --format=tar --prefix="${d%.git}/" HEAD -- $dirs
done
true
POD
echo "exported $(find "$work/files" -name '*.y*ml' | wc -l) workflow files from $(find "$work/files" -mindepth 2 -maxdepth 2 -type d | wc -l) repositories"

MSYS_NO_PATHCONV=1 docker run --rm \
  -v "$(host_path "$src"):/src-ro:ro" -v "$(host_path "$work/files"):/files:ro" \
  -v "$(host_path "$here/gitea-workflow-check"):/tool:ro" -v gitea-workflow-check-gomod:/go/pkg/mod \
  "golang:$toolchain" sh -c 'mkdir -p /src/cmd/wfcheck && cp /src-ro/go.mod /src-ro/go.sum /src/ &&
    cp -r /src-ro/modules /src/ && cp /tool/main.go /src/cmd/wfcheck/ && cd /src &&
    GOFLAGS=-mod=mod go run ./cmd/wfcheck /files > /tmp/out 2>&1; rc=$?
    grep -v "^go: downloading" /tmp/out; exit "$rc"'

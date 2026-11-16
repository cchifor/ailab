# Runs INSIDE the Gitea pod (busybox sh, as the git user), fed on stdin by scripts/gitea-workflow-check.sh.
# Prints every workflow file at HEAD of every bare repository as raw Git blobs, the bytes Gitea parses:
#   =====FILE <owner>/<repo> <mode> <path>   then the blob, base64 on one line
#   =====EMPTY <owner>/<repo>                 a repository without HEAD (nothing to check)
#   =====END                                  printed only when every repository was read
# Raw blobs rather than `git archive`: archive honours `export-ignore` and resolves nothing for symlinks, so it
# could hide a file Gitea reads. Any Git failure stops the script before =====END.
set -o pipefail || exit 1
root=${1:-/data/git/gitea-repositories}
cd "$root" || exit 1
for d in */*.git; do
  [ -d "$d" ] || continue
  repo=${d%.git}
  if ! git -C "$d" rev-parse -q --verify HEAD^{commit} >/dev/null; then
    # An empty repository has no HEAD. Anything else that fails here is an error, not "empty".
    [ -z "$(git -C "$d" for-each-ref --count=1)" ] || { echo "=====ERROR $repo: HEAD unreadable"; exit 1; }
    echo "=====EMPTY $repo"
    continue
  fi
  # The workflow directories Gitea 28.x reads: WORKFLOW_DIRS (first existing one wins) and SCOPED_WORKFLOW_DIRS.
  # Every one is checked, including one Gitea ignores because .gitea/workflows exists.
  for dir in .gitea/workflows .github/workflows .gitea/scoped_workflows; do
    list=$(git -C "$d" ls-tree -r HEAD -- "$dir/") || { echo "=====ERROR $repo: ls-tree $dir"; exit 1; }
    [ -n "$list" ] || continue
    printf '%s\n' "$list" | while read -r mode type sha path; do
      [ "$type" = blob ] || continue          # a submodule is not a workflow
      case "$path" in
        *.yml|*.yaml|*.yml\"|*.yaml\") ;;     # Gitea's suffix filter; ls-tree quotes unusual names
        *) continue ;;
      esac
      printf '=====FILE %s %s %s\n' "$repo" "$mode" "$path"
      git -C "$d" cat-file blob "$sha" | base64 | tr -d '\n' || exit 1
      printf '\n'
    done || { echo "=====ERROR $repo: reading $dir"; exit 1; }
  done
done
echo "=====END"

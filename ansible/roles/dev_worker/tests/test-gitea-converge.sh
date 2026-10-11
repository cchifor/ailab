#!/usr/bin/env bash
# Convergence test for the per-slot Gitea identity (plan 2026-10-11 worker identities): runs the
# role's REAL git_identity.yml and openbao_switch.yml (the Gitea pre-flight included) through
# ansible-playbook, again and again, on one simulated worker with two users (each with both
# ~/.gitconfig and ~/.config/git/config):
#   gate false -> true -> false, each converge repeated (idempotence: changed=0) and under --check
#   (nothing changes on disk); a render failure and a failing restart (both must roll back to the last
#   verified login, re-pin it, and say ROLLED BACK AND VERIFIED); a failing restart that another
#   template changed in the same run also needed; a restart for the shared role's changes alone; an
#   interrupted switch (new template in place, credentials removed, no restart) completed by the next
#   run, rolled back when its render fails, and rolled back when the PRE-FLIGHT fails; a pre-flight
#   failure with nothing unfinished changing nothing; a replacement kubeconfig template that cannot
#   render, rolled back with every other managed file; repository identities removed except under
#   dev_worker_git_identity_keep_paths.
#
# What is simulated, and nothing else:
#   - a COPY of the role, with /etc/openbao-agent/ moved under a temp dir, /usr/local/bin/cred
#     pointed at a stub, `become`/`become_user` dropped and `owner`/`group: root|openbao-agent`
#     dropped (no root here);
#   - systemctl: a stub whose start/restart is a tiny bao agent — it renders EVERY template agent.hcl
#     names into its destination (git-credentials from a fixture KV, the others copied through), and
#     dies (is-active: failed) when a KV field is missing, a template source is missing or carries
#     the word BROKEN, or a render is told to fail, as error_on_missing_key + exit_on_retry_failure
#     make the real one; kubectl (the helmtest kubeconfig check): answers yes;
#   - cred: reads that fixture KV; hostname: the slot name; runuser (needs root): runs the command
#     as this user; sleep: returns at once.
# Linux only. Needs ansible-playbook with community.general (CI: ansible==14.0.0 in a venv,
# .gitea/workflows/dev-worker-scripts.yaml) — a missing one FAILS the suite, it never skips.
#
# Usage: bash ansible/roles/dev_worker/tests/test-gitea-converge.sh   (exit 0 = pass)
set -uo pipefail
ROLE="$(cd "$(dirname "$0")/.." && pwd)"
command -v ansible-playbook >/dev/null 2>&1 || { echo "FATAL: ansible-playbook not found"; exit 2; }
command -v python3 >/dev/null 2>&1 || { echo "FATAL: python3 not found"; exit 2; }
T="$(mktemp -d)"
trap 'rm -rf "$T"' EXIT
ME="$(id -un)"
SHARED=stub-shared-pat-0000
SLOT=stub-slot-pat-3333
BOT=dev-worker-3-bot
ETC="$T/etc/openbao-agent"
fails=0
ok() { echo "  PASS: $1"; }
bad() { echo "  FAIL: $1"; fails=$((fails + 1)); [ -z "${2:-}" ] || printf '    %s\n' "$2"; }
cd / # never inside a repository: git helpers below must not discover one

# ---- a copy of the role, transformed as documented above ----------------------------------------
python3 - "$ROLE" "$T" <<'PY' || { echo "FATAL: could not prepare the role copy"; exit 2; }
import os, shutil, sys
import yaml

role, t = sys.argv[1], sys.argv[2]
dst = os.path.join(t, "roles", "dev_worker")
for sub in ("defaults", "vars", "tasks", "templates", "handlers", "meta"):
    shutil.copytree(os.path.join(role, sub), os.path.join(dst, sub))
etc = os.path.join(t, "etc", "openbao-agent") + "/"


def strip(node):
    if isinstance(node, list):
        for x in node:
            strip(x)
    elif isinstance(node, dict):
        node.pop("become", None)
        node.pop("become_user", None)
        for k, v in list(node.items()):
            if k in ("owner", "group") and v in ("root", "openbao-agent"):
                del node[k]
            else:
                strip(v)


for sub in ("tasks", "handlers"):
    for name in os.listdir(os.path.join(dst, sub)):
        p = os.path.join(dst, sub, name)
        text = open(p, encoding="utf-8").read().replace("/etc/openbao-agent/", etc)
        data = yaml.safe_load(text.replace("/usr/local/bin/cred", os.path.join(t, "stubs", "cred")))
        strip(data)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            yaml.safe_dump(data, f, sort_keys=False, width=100000, allow_unicode=True)
for name in os.listdir(os.path.join(dst, "templates")):
    p = os.path.join(dst, "templates", name)
    text = open(p, encoding="utf-8").read()
    if "/etc/openbao-agent/" in text:
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(text.replace("/etc/openbao-agent/", etc))
PY

# ---- stubs ---------------------------------------------------------------------------------------
mkdir -p "$T/stubs" "$T/agent" "$ETC" "$T/kv/common" "$T/kv/dev-worker-3" "$T/ws/$ME"
for f in gitea_pat gitea_repo_pat gitea_package_pat; do
  printf '%s' "$SHARED-$f" >"$T/kv/common/$f"
  printf '%s' "$SLOT-$f" >"$T/kv/dev-worker-3/$f"
done
SHARED="$SHARED-gitea_pat"
SLOT="$SLOT-gitea_pat"
echo inactive >"$T/agent/state"
echo 0 >"$T/agent/fail-restarts"
echo 0 >"$T/agent/fail-renders"
echo 0 >"$T/agent/restarts"
cat >"$T/stubs/systemctl" <<'EOF'
#!/usr/bin/env python3
import datetime, os, re, sys
T = os.environ["CONVERGE_T"]
A = T + "/agent/"
ETC = T + "/etc/openbao-agent/"
rd = lambda p: open(p).read().strip()
def wr(p, s):
    with open(p, "w") as f:
        f.write(s)
def render():  # the fake bao agent: every template agent.hcl names
    wr(A + "restarts", str(int(rd(A + "restarts")) + 1))
    left = int(rd(A + "fail-renders"))
    if left > 0:  # a render failure: the agent exits
        wr(A + "fail-renders", str(left - 1))
        wr(A + "state", "failed")
        return
    hcl = open(ETC + "agent.hcl").read()
    for src, dst in re.findall(r'source\s*=\s*"([^"]+)"\s*\n\s*destination\s*=\s*"([^"]+)"', hcl):
        if not os.path.exists(src) or "BROKEN" in open(src).read():  # a template that cannot render
            wr(A + "state", "failed")
            return
        text = open(src).read()
        if src.endswith("git-credentials.ctmpl"):
            m = re.fullmatch(r'\{\{ with secret "af/data/dev-workers/([^"]+)" \}\}(.*)\{\{ end \}\}\n?', text, re.S)
            field = T + "/kv/" + m.group(1) + "/gitea_pat"
            if not os.path.exists(field):  # error_on_missing_key: the agent exits
                wr(A + "state", "failed")
                return
            text = m.group(2).replace("{{ .Data.data.gitea_pat }}", rd(field)) + "\n"
        wr(dst, text)
        os.chmod(dst, 0o600)
    wr(A + "state", "active")
    wr(A + "started", datetime.datetime.now(datetime.timezone.utc).strftime("%a %Y-%m-%d %H:%M:%S UTC"))
argv = sys.argv[1:]
verb = argv[0] if argv else ""
state = rd(A + "state")
if verb in ("daemon-reload", "daemon-reexec"):
    sys.exit(0)
if verb == "show":
    if "-p" in argv:
        prop = argv[argv.index("-p") + 1]
        print(rd(A + "started") if prop == "ExecMainStartTimestamp" else "0")
    else:
        print("Id=openbao-agent.service\nLoadState=loaded\nActiveState=%s\nSubState=running\nUnitFileState=enabled" % state)
    sys.exit(0)
if verb == "is-active":
    print(state)
    sys.exit(0 if state == "active" else 3)
if verb == "is-enabled":
    print("enabled")
    sys.exit(0)
if verb == "list-unit-files":
    print("openbao-agent.service enabled enabled")
    sys.exit(0)
if verb in ("start", "restart"):
    left = int(rd(A + "fail-restarts"))
    if verb == "restart" and left > 0:
        wr(A + "fail-restarts", str(left - 1))
        print("Job for openbao-agent.service failed.", file=sys.stderr)
        sys.exit(1)
    if verb == "restart" or state != "active":
        render()
    sys.exit(0)
if verb == "stop":
    wr(A + "state", "inactive")
    sys.exit(0)
print("stub systemctl: unexpected %r" % argv, file=sys.stderr)
sys.exit(2)
EOF
printf '#!/bin/sh\nwhile [ "$#" -gt 0 ] && [ "$1" != -- ]; do shift; done\nshift\nexec "$@"\n' >"$T/stubs/runuser"
printf '#!/bin/sh\necho dev-worker-3\n' >"$T/stubs/hostname"
printf '#!/bin/sh\necho yes\n' >"$T/stubs/kubectl"
# cred get <name> <field>, from the fixture KV (the real helper is tested in test-cred-helper.sh)
printf '#!/bin/sh\n[ "$1" = get ] && [ -f "$CONVERGE_T/kv/$2/$3" ] || { echo "cred: no field $3" >&2; exit 1; }\ncat "$CONVERGE_T/kv/$2/$3"\n' >"$T/stubs/cred"
printf '#!/bin/sh\nexit 0\n' >"$T/stubs/sleep"
chmod +x "$T/stubs/"*

# ---- the simulated worker: two users, both global config files, repository identities ------------
gitq() { git -C / -c init.defaultBranch=main "$@" >/dev/null 2>&1; }
for u in a b; do
  mkdir -p "$T/home-$u/.config/git" "$T/home-$u/.claude" "$T/home-$u/.codex" "$T/home-$u/.helmtest"
  printf '[core]\n\teditor = vi\n' >"$T/home-$u/.gitconfig"
  printf '[color]\n\tui = auto\n' >"$T/home-$u/.config/git/config"
done
for r in "$T/ws/$ME/proj" "$T/ws/$ME/kept" "$T/home-a/platform"; do gitq init -q "$r"; done
gitq -C "$T/ws/$ME/proj" config user.name Codex
gitq -C "$T/ws/$ME/proj" config user.email codex@chifor.me
gitq -C "$T/ws/$ME/kept" config user.name "Deliberate Owner"
gitq -C "$T/home-a/platform" config user.name "Constantin"

cat >"$T/ansible.cfg" <<EOF
[defaults]
roles_path = $T/roles
local_tmp = $T/.ansible-tmp
host_key_checking = False
retry_files_enabled = False
nocows = True
EOF
printf 'dev-worker-3 ansible_connection=local ansible_python_interpreter="{{ ansible_playbook_python }}"\n' >"$T/hosts"
cat >"$T/play.yml" <<EOF
- hosts: dev-worker-3
  gather_facts: false
  vars:
    dev_worker_users:
      - {name: "$ME", home: "$T/home-a"}
      - {name: "$ME", home: "$T/home-b"}
    dev_worker_workspace_mount: "$T/ws"
    dev_worker_git_identity_keep_paths: ["$T/ws/$ME/kept"]
    dev_worker_openbao_renders_kubeconfigs: true # so agent.hcl renders a non-Gitea template too
    dev_worker_platform_fields_present: false
  tasks:
    - name: Commit identity
      ansible.builtin.include_role: {name: dev_worker, tasks_from: git_identity}
    - name: Gitea credential switch
      ansible.builtin.include_role: {name: dev_worker, tasks_from: openbao_switch}
EOF

# ---- helpers ---------------------------------------------------------------------------------------
RC=0
play() { # <gate> [ansible-playbook args...]
  ANSIBLE_CONFIG="$T/ansible.cfg" CONVERGE_T="$T" PATH="$T/stubs:$PATH" \
    ansible-playbook -i "$T/hosts" "$T/play.yml" -e "dev_worker_gitea_identity_cutover=$1" "${@:2}" >"$T/log" 2>&1
  RC=$?
}
changed() { sed -n 's/^dev-worker-3 *:.* changed=\([0-9]*\).*/\1/p' "$T/log" | tail -n 1; }
login() { sed -n 's#^https://\([^:]*\):.*#\1#p' "$1" 2>/dev/null; }
pin() { git -C / config --file "$1/.gitconfig" --get credential.https://git.chifor.me.username; }
cfg() { git -C / config --file "$1" --get "$2"; }
snapshot() { (cd "$T" && find home-a home-b etc ws -type f ! -path '*/.git/objects/*' ! -path '*/.git/logs/*' -print0 |
  sort -z | xargs -0 sha256sum) >"$T/snap.$1"; }
expect_login() { # <label> <login> <token>
  local h good=1
  for h in "$T/home-a" "$T/home-b"; do
    [ "$(login "$h/.git-credentials")" = "$2" ] && grep -q ":$3@" "$h/.git-credentials" && [ "$(pin "$h")" = "$2" ] || good=0
  done
  if [ "$good" = 1 ]; then ok "$1: both users render and pin $2"; else bad "$1: both users render and pin $2" "$(tail -n 30 "$T/log")"; fi
}
expect_rc() { # <label> <0|nonzero>
  if { [ "$2" = 0 ] && [ "$RC" = 0 ]; } || { [ "$2" != 0 ] && [ "$RC" != 0 ]; }; then ok "$1 (rc=$RC)"
  else bad "$1 (rc=$RC)" "$(grep -E 'fatal|FAILED|msg' "$T/log" | tail -n 8)"; fi
}
expect_changed0() { # <label>
  if [ "$(changed)" = 0 ]; then ok "$1: changed=0"; else bad "$1: changed=$(changed)" "$(grep -E '^changed:|^TASK' "$T/log" | grep -B1 '^changed' | head -n 12)"; fi
}
expect_log() { # <label> <fixed string>
  if grep -qF -- "$2" "$T/log"; then ok "$1"; else bad "$1" "$(tail -n 12 "$T/log")"; fi
}
no_token_in_log() {
  if grep -qE "stub-shared-pat-0000|stub-slot-pat-3333" "$T/log"; then bad "$1: a token reached the ansible output"; else ok "$1: no token in the ansible output"; fi
}

echo "[1] gate false on a fresh worker"
play false
expect_rc "1a: converges" 0
expect_login "1b" dev-worker-bot "$SHARED"
[ "$(cfg "$T/home-a/.gitconfig" user.name)" = "$BOT" ] && [ "$(cfg "$T/home-b/.gitconfig" user.email)" = "$BOT@noreply.git.chifor.me" ] \
  && ok "1c: both users commit as $BOT" || bad "1c: both users commit as $BOT"
[ "$(cfg "$T/home-a/.config/git/config" color.ui)" = auto ] && [ -z "$(cfg "$T/home-a/.config/git/config" user.name)" ] \
  && ok "1d: the XDG config is left alone" || bad "1d: the XDG config is left alone"
[ -z "$(cfg "$T/ws/$ME/proj/.git/config" user.name)" ] && [ -z "$(cfg "$T/ws/$ME/proj/.git/config" user.email)" ] \
  && [ -z "$(cfg "$T/home-a/platform/.git/config" user.name)" ] && ok "1e: repository identities are removed" \
  || bad "1e: repository identities are removed" "$(grep -E 'corrected|override|inaccessible' "$T/log")"
[ "$(cfg "$T/ws/$ME/kept/.git/config" user.name)" = "Deliberate Owner" ] && ok "1f: ...except under keep_paths" || bad "1f: ...except under keep_paths"
good=1
for f in agent.hcl git-credentials.ctmpl helmtest-kubeconfig.ctmpl platform-kubeconfig.ctmpl platform-pgpass.ctmpl; do
  cmp -s "$ETC/$f" "$ETC/$f.last-good" || good=0
done
[ "$good" = 1 ] && ok "1g: every managed file is recorded as its last verified version" \
  || bad "1g: every managed file is recorded as its last verified version" "$(ls -l "$ETC")"
[ -s "$T/home-a/.helmtest/kubeconfig" ] && ok "1h: the non-Gitea template renders too" || bad "1h: the non-Gitea template renders too"
no_token_in_log "1i"

echo "[2] gate false again, and under --check"
play false
expect_rc "2a: converges" 0
expect_changed0 "2b: idempotent"
snapshot before
play false --check
expect_rc "2c: --check converges" 0
expect_changed0 "2d: --check on a converged worker"
snapshot after
cmp -s "$T/snap.before" "$T/snap.after" && ok "2e: --check changes nothing on disk" || bad "2e: --check changes nothing on disk"

echo "[3] gate true: the cut-over"
play true
expect_rc "3a: converges" 0
expect_login "3b" "$BOT" "$SLOT"
grep -q 'dev-workers/dev-worker-3"' "$ETC/git-credentials.ctmpl.last-good" && ok "3c: the slot template is the last verified one now" \
  || bad "3c: the slot template is the last verified one now"
cp "$ETC/git-credentials.ctmpl" "$T/slot.ctmpl"
no_token_in_log "3d"
play true
expect_changed0 "3e: idempotent"
snapshot before
play false --check
expect_rc "3f: --check of the rollback converges" 0
snapshot after
cmp -s "$T/snap.before" "$T/snap.after" && ok "3g: ...and changes nothing on disk" || bad "3g: ...and changes nothing on disk"
[ "$(changed)" != 0 ] && ok "3h: ...while reporting what it would change" || bad "3h: ...while reporting what it would change"

echo "[4] gate false: the rollback"
play false
expect_rc "4a: converges" 0
expect_login "4b" dev-worker-bot "$SHARED"
grep -q 'dev-workers/common"' "$ETC/git-credentials.ctmpl.last-good" && ok "4c: the shared template is the last verified one again" \
  || bad "4c: the shared template is the last verified one again"

echo "[5] a cut-over whose render fails is rolled back and verified"
echo 1 >"$T/agent/fail-renders"
play true
expect_rc "5a: the play fails" 1
expect_log "5b: ...saying the rollback was verified" "ROLLED BACK AND VERIFIED"
expect_login "5c" dev-worker-bot "$SHARED"
cmp -s "$ETC/git-credentials.ctmpl" "$ETC/git-credentials.ctmpl.last-good" && ok "5d: the last verified template is back" \
  || bad "5d: the last verified template is back"

echo "[5b] a cut-over whose pre-flight fails, with nothing unfinished, changes nothing"
snapshot before
mv "$T/kv/dev-worker-3/gitea_repo_pat" "$T/kv/held"
play true
expect_rc "5b-a: the play fails" 1
expect_log "5b-b: ...at the pre-flight, saying nothing was changed" "Nothing was changed"
snapshot after
cmp -s "$T/snap.before" "$T/snap.after" && ok "5b-c: nothing on disk changed" || bad "5b-c: nothing on disk changed"
mv "$T/kv/held" "$T/kv/dev-worker-3/gitea_repo_pat"

echo "[6] a cut-over whose restart fails is rolled back and verified"
echo 1 >"$T/agent/fail-restarts"
play true
expect_rc "6a: the play fails" 1
expect_log "6b: ...saying the rollback was verified" "ROLLED BACK AND VERIFIED"
expect_login "6c" dev-worker-bot "$SHARED"

echo "[7] an interrupted cut-over (template replaced, credentials removed, no restart) is completed"
cp "$T/slot.ctmpl" "$ETC/git-credentials.ctmpl"
rm -f "$T/home-a/.git-credentials" "$T/home-b/.git-credentials"
play true
expect_rc "7a: converges" 0
expect_login "7b" "$BOT" "$SLOT"

echo "[8] the same interruption, then the slot token unreadable: the PRE-FLIGHT fails and rolls back"
play false
expect_rc "8a: back to the shared login first" 0
cp "$T/slot.ctmpl" "$ETC/git-credentials.ctmpl"
rm -f "$T/home-a/.git-credentials" "$T/home-b/.git-credentials"
mv "$T/kv/dev-worker-3/gitea_pat" "$T/kv/held"
play true
expect_rc "8b: the play fails" 1
expect_log "8c: ...at the pre-flight" "The Gitea pre-flight failed"
expect_log "8d: ...finding the unfinished switch and verifying its rollback" "UNFINISHED switch was found"
expect_log "8e: ...ROLLED BACK AND VERIFIED" "ROLLED BACK AND VERIFIED"
expect_login "8f" dev-worker-bot "$SHARED"
cmp -s "$ETC/git-credentials.ctmpl" "$ETC/git-credentials.ctmpl.last-good" && ok "8g: the last verified template is back" \
  || bad "8g: the last verified template is back"
mv "$T/kv/held" "$T/kv/dev-worker-3/gitea_pat"

echo "[8b] the same interruption with a failing render rolls back through the switch"
cp "$T/slot.ctmpl" "$ETC/git-credentials.ctmpl"
rm -f "$T/home-a/.git-credentials" "$T/home-b/.git-credentials"
echo 1 >"$T/agent/fail-renders"
play true
expect_rc "8b-a: the play fails" 1
expect_log "8b-b: ...saying the rollback was verified" "ROLLED BACK AND VERIFIED"
expect_login "8b-c" dev-worker-bot "$SHARED"

echo "[10] a cut-over whose restart is ALSO for another changed template, and fails: rescued"
printf 'stale\n' >"$ETC/helmtest-kubeconfig.ctmpl"
echo 1 >"$T/agent/fail-restarts"
play true
expect_rc "10a: the play fails" 1
expect_log "10b: ...saying the rollback was verified" "ROLLED BACK AND VERIFIED"
expect_login "10c" dev-worker-bot "$SHARED"
# The rescue must leave every managed file at its last verified version, not at what this run laid
# down — the other template included.
good=1
for f in agent.hcl git-credentials.ctmpl helmtest-kubeconfig.ctmpl platform-kubeconfig.ctmpl platform-pgpass.ctmpl; do
  cmp -s "$ETC/$f" "$ETC/$f.last-good" || good=0
done
[ "$good" = 1 ] && ok "10d: the rescue restored every managed file to its last verified version"   || bad "10d: the rescue restored every managed file to its last verified version" "$(ls -l "$ETC")"
grep -qF 'RUNNING HANDLER' "$T/log" && bad "10e: no handler ran" "$(grep -F 'RUNNING HANDLER' "$T/log")" || ok "10e: no handler ran"

echo "[10b] on a converged worker, a restart needed only by another template fails: rescued too"
play false
expect_rc "10b-a: converged" 0
printf 'stale
' >"$ETC/platform-pgpass.ctmpl"
echo 1 >"$T/agent/fail-restarts"
play false
expect_rc "10b-b: the play fails" 1
expect_log "10b-c: ...saying the rollback was verified" "ROLLED BACK AND VERIFIED"
expect_login "10b-d" dev-worker-bot "$SHARED"

echo "[12] a replacement kubeconfig template that cannot render is rolled back with every managed file"
play false
expect_rc "12a: converged" 0
HT="$T/roles/dev_worker/templates/helmtest-kubeconfig.ctmpl.j2"
cp "$HT" "$T/helmtest.j2.good"
echo BROKEN >>"$HT"
play false
expect_rc "12b: the play fails" 1
expect_log "12c: ...saying the rollback was verified" "ROLLED BACK AND VERIFIED"
expect_login "12d" dev-worker-bot "$SHARED"
cmp -s "$ETC/helmtest-kubeconfig.ctmpl" "$ETC/helmtest-kubeconfig.ctmpl.last-good" && ! grep -q BROKEN "$ETC/helmtest-kubeconfig.ctmpl" \
  && ok "12e: the last verified kubeconfig template is back" || bad "12e: the last verified kubeconfig template is back"
[ "$(cat "$T/agent/state")" = active ] && ok "12f: the agent runs again" || bad "12f: the agent runs again"
cp "$T/helmtest.j2.good" "$HT"

echo "[11] the shared role's changes alone restart the agent, in the switch"
play false
expect_rc "11a: converged" 0
before="$(cat "$T/agent/restarts")"
play false -e dev_worker_agent_inputs_changed=true
expect_rc "11b: converges" 0
[ "$(cat "$T/agent/restarts")" -gt "$before" ] && ok "11c: the agent was restarted" || bad "11c: the agent was restarted"
expect_login "11d" dev-worker-bot "$SHARED"

echo "[9] converged again, idempotent, and --check is a no-op"
play false
expect_rc "9a: converges" 0
play false
expect_changed0 "9b: idempotent"
snapshot before
play false --check
expect_changed0 "9c: --check"
snapshot after
cmp -s "$T/snap.before" "$T/snap.after" && ok "9d: --check changes nothing on disk" || bad "9d: --check changes nothing on disk"

echo
if [ "$fails" -eq 0 ]; then echo "ALL PASS"; exit 0; fi
echo "$fails CHECK(S) FAILED"
exit 1

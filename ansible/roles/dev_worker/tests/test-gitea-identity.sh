#!/usr/bin/env bash
# The per-slot Gitea identity (plan 2026-10-11 worker identities): the cut-over gate
# dev_worker_gitea_identity_cutover and the gate-independent commit identity. Plain bash driving one
# python3 block (jinja2 + PyYAML, like scripts/tests/test_registry_zot_retention.py): the role's own
# defaults, templates and task scripts are rendered and, where they are shell, RUN against fixtures.
#
# WHY THIS EXISTS. Every way this breaks is quiet until an agent's push fails or a commit names the
# wrong worker:
#   - gate off must render today's credential line, pinned username and instructions byte for byte,
#     or an unrelated converge moves a slot;
#   - gate on must move the document, the login and the instructions TOGETHER, and the pinned
#     username must equal the login in the rendered line in both states (git skips a line whose
#     username differs from the pin and prompts instead);
#   - a switch either way must remove the other login's rendered file before the agent restarts, and
#     the health check must compare the LOGIN, not just non-emptiness;
#   - the pre-flight must stop on a wrong hostname, a missing, empty or shared-copy field — without
#     ever printing a value;
#   - the commit identity is <slot>-bot whatever the gate says, and the audit reports overrides
#     without printing values.
# Linux only (bash, git, /proc). CI: .gitea/workflows/dev-worker-scripts.yaml.
#
# Usage: bash ansible/roles/dev_worker/tests/test-gitea-identity.sh   (exit 0 = pass)
set -uo pipefail
ROLE="$(cd "$(dirname "$0")/.." && pwd)"
PY=python3; command -v python3 >/dev/null 2>&1 || PY=python
WORK="$(mktemp -d)"
SLEEPER=""
trap '[ -z "$SLEEPER" ] || kill "$SLEEPER" 2>/dev/null; rm -rf "$WORK"' EXIT

# A process of this user carrying an identity variable, for the audit's live-environment scan
# (section [H]). The value is a marker the output must never contain.
env GIT_AUTHOR_NAME=stub-env-value-7777 sleep 120 &
SLEEPER=$!

"$PY" - "$ROLE" "$WORK" "$SLEEPER" <<'PY'
import os, pwd, re, shlex, subprocess, sys

try:
    import jinja2
    import yaml
except ImportError as e:  # a missing library fails the suite: it must never pass hollow
    print("FATAL: needs python3 jinja2 and PyYAML (%s)" % e)
    sys.exit(2)

ROLE, WORK, SLEEPER = sys.argv[1], sys.argv[2], sys.argv[3]
GROUP_VARS = os.path.join(ROLE, "..", "..", "group_vars")
HOST = "dev-worker-3"
BOT = HOST + "-bot"
EMAIL = BOT + "@noreply.git.chifor.me"
# Synthetic; nothing here touches a real vault, forge or worker.
TOKENS = {"common": "stub-shared-pat-0000", HOST: "stub-slot-pat-3333"}
fails = 0


def check(cond, label, detail=""):
    global fails
    if cond:
        print("  PASS: " + label)
        return
    fails += 1
    print("  FAIL: " + label)
    if detail != "":
        print("    " + str(detail).replace("\n", "\n    "))


def to_bool(v):  # ansible's `bool` filter
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in ("1", "true", "yes", "on", "y", "t")


# trim_blocks like ansible's templar; StrictUndefined so a variable this test forgot fails loudly.
ENV = jinja2.Environment(undefined=jinja2.StrictUndefined, trim_blocks=True, keep_trailing_newline=True)
ENV.filters["bool"] = to_bool
ENV.filters["regex_replace"] = lambda s, pat, rep="": re.sub(pat, rep, s)
ENV.filters["quote"] = lambda s: shlex.quote(str(s))  # ansible's `quote` is shlex.quote
ENV.tests["match"] = lambda v, pat: re.match(pat, str(v)) is not None  # ansible's `match` test


def render(text, **v):
    return ENV.from_string(text).render(**v)


def read(rel):
    with open(os.path.join(ROLE, rel), encoding="utf-8") as f:
        return f.read()


DEFAULTS = yaml.safe_load(read("defaults/main.yml"))


def host_vars(gate):
    """The role defaults for HOST with the gate set, the templated ones resolved as ansible would."""
    v = dict(DEFAULTS, inventory_hostname=HOST, dev_worker_gitea_identity_cutover=gate)
    for _ in range(5):
        for k, val in list(v.items()):
            if k.startswith(("dev_worker_git", "dev_worker_helmtest")) and isinstance(val, str) and "{{" in val:
                v[k] = render(val, **v)
    return v


def tasks_in(rel):
    """Every task of a task file, block/rescue/always flattened, in file order."""
    out = []

    def walk(items):
        for t in items or []:
            out.append(t)
            for k in ("block", "rescue", "always"):
                walk(t.get(k))

    walk(yaml.safe_load(read(rel)))
    return out


def task(rel, prefix):
    hits = [t for t in tasks_in(rel) if str(t.get("name", "")).startswith(prefix)]
    if len(hits) != 1:
        raise SystemExit("FATAL: %s has %d tasks named %r" % (rel, len(hits), prefix))
    return hits[0]


def position(rel, prefix):
    names = [str(t.get("name", "")) for t in tasks_in(rel)]
    return next(i for i, n in enumerate(names) if n.startswith(prefix))


def run_sh(script, env=None):
    path = os.path.join(WORK, "script.sh")
    with open(path, "w") as f:
        f.write(script)
    e = dict(os.environ)
    e.update(env or {})
    r = subprocess.run(["bash", path], capture_output=True, text=True, env=e)
    return r.returncode, r.stdout, r.stderr


def leaked(text):
    return [t for t in list(TOKENS.values()) + ["stub-env-value-7777", "stub-profile-value-5555"] if t in text]


OFF, ON = host_vars(False), host_vars(True)

print("[A] the gate resolves the login and cred name; the commit identity ignores it")
check(DEFAULTS.get("dev_worker_gitea_identity_cutover") is False, "A1: the gate defaults to false")
check(OFF["dev_worker_gitea_user"] == "dev-worker-bot", "A2: gate off -> dev-worker-bot", OFF["dev_worker_gitea_user"])
check(ON["dev_worker_gitea_user"] == BOT, "A3: gate on -> <host>-bot", ON["dev_worker_gitea_user"])
check((OFF["dev_worker_gitea_cred_name"], ON["dev_worker_gitea_cred_name"]) == ("common", HOST),
      "A4: the cred name is common / <host>")
check(host_vars("true")["dev_worker_gitea_user"] == BOT and host_vars("false")["dev_worker_gitea_user"] == "dev-worker-bot",
      "A5: an -e string 'true'/'false' behaves like the boolean")
for state, v in (("off", OFF), ("on", ON)):
    check((v["dev_worker_git_user_name"], v["dev_worker_git_user_email"]) == (BOT, EMAIL),
          "A6: the commit identity is <host>-bot with the gate " + state,
          (v["dev_worker_git_user_name"], v["dev_worker_git_user_email"]))
check("dev_worker_gitea_token" not in DEFAULTS, "A7: no SOPS token default (the SOPS git path is gone)")
# A per-host switch, never a fleet one (like dev_worker_openbao_kubeconfig_cutover).
fleet = [f for f in os.listdir(GROUP_VARS) if f.endswith((".yml", ".yaml"))
         and "dev_worker_gitea_identity_cutover" in (yaml.safe_load(open(os.path.join(GROUP_VARS, f))) or {})]
check(not fleet, "A8: no group_vars file sets the gate", fleet)

print("[B] the credential template, and what the bao agent renders from it")
CTMPL = read("templates/git-credentials.ctmpl.j2")
TODAY = '{{ with secret "af/data/dev-workers/common" }}https://dev-worker-bot:{{ .Data.data.gitea_pat }}@git.chifor.me{{ end }}'
out_off, out_on = render(CTMPL, **OFF).strip("\n"), render(CTMPL, **ON).strip("\n")
check(out_off == TODAY, "B1: gate off renders the pre-gate template byte for byte", out_off)
check(out_on == TODAY.replace("dev-workers/common", "dev-workers/" + HOST).replace("dev-worker-bot", BOT),
      "B2: gate on reads af/data/dev-workers/<host> as <host>-bot", out_on)


def agent_line(ctmpl_out):
    """The bao agent's pass: the document read and the credential line it writes."""
    m = re.fullmatch(r'\{\{ with secret "af/data/dev-workers/([^"]+)" \}\}(.*)\{\{ end \}\}', ctmpl_out, re.S)
    if not m:
        raise SystemExit("FATAL: not a single `with secret` block: %r" % ctmpl_out)
    return m.group(1), m.group(2).replace("{{ .Data.data.gitea_pat }}", TOKENS[m.group(1)]) + "\n"


LINE = {"off": agent_line(out_off)[1], "on": agent_line(out_on)[1]}
check(LINE["off"] == "https://dev-worker-bot:%s@git.chifor.me\n" % TOKENS["common"], "B3: gate off writes the shared login's line")
check(LINE["on"] == "https://%s:%s@git.chifor.me\n" % (BOT, TOKENS[HOST]), "B4: gate on writes the slot login with the slot's token")

print("[C] the username pin follows the same login")
pin = task("tasks/pin_gitea_username.yml", "Pin the git.chifor.me")["community.general.git_config"]
check(pin["name"] == "credential.https://git.chifor.me.username", "C1: the pin is the git.chifor.me username")
for state, v in (("off", OFF), ("on", ON)):
    pinned = render(pin["value"], **v)
    rendered = re.match(r"https://([^:]+):", LINE[state]).group(1)
    check(pinned == rendered, "C2: gate %s pins the login the credential line carries (%s)" % (state, pinned), (pinned, rendered))

print("[D] a switch removes the other login's file; the health check reads the login")
SW = "tasks/openbao_switch.yml"
drift = task(SW, "Find credential files that are missing")["ansible.builtin.shell"]
verify = task(SW, "Verify the agent restarted")["ansible.builtin.shell"]
home = os.path.join(WORK, "home-drift")
os.makedirs(home)
cfile = os.path.join(home, ".git-credentials")
vlines = verify.splitlines()
at = next(i for i, l in enumerate(vlines) if "does not name" in l)
login_check = vlines[at - 1].strip() + "\n" + vlines[at].strip()
for label, want, other in (("cut-over", "on", "off"), ("rollback", "off", "on")):
    user = (ON if want == "on" else OFF)["dev_worker_gitea_user"]
    script = render(drift, dev_worker_users=[{"name": "u", "home": home}], dev_worker_gitea_user=user)
    checker = "set -euo pipefail\n" + render(login_check, user={"name": "u", "home": home}, dev_worker_gitea_user=user)
    if os.path.exists(cfile):
        os.remove(cfile)
    rc, out, err = run_sh(script)
    check(rc == 0 and out.strip() == "absent " + cfile, "D1 %s: a missing file forces the render" % label, (rc, out, err))
    open(cfile, "w").write(LINE[other])
    rc, out, err = run_sh(script)
    check(rc == 0 and out.strip() == "other " + cfile, "D2 %s: the other login's file is listed for removal" % label, (rc, out, err))
    check(not leaked(out + err), "D3 %s: ...and no token reaches the output" % label, leaked(out + err))
    rc, out, err = run_sh(checker)
    check(rc != 0 and "does not name " + user in err and not leaked(out + err),
          "D4 %s: the health check FAILS on the other login's non-empty file" % label, (rc, out, err))
    open(cfile, "w").write(LINE[want])
    rc, out, err = run_sh(script)
    check(rc == 0 and out == "", "D5 %s: this run's login is left alone" % label, (rc, out, err))
    rc, out, err = run_sh(checker)
    check(rc == 0, "D6 %s: the health check passes on this run's login" % label, (rc, out, err))
    open(cfile, "w").write("")
    rc, out, err = run_sh(script)
    check(rc == 0 and out.strip() == "other " + cfile, "D7 %s: an empty or foreign file is re-rendered too" % label, (rc, out, err))
sed_re = re.compile(r"sed -n '([^']+)'")
check(sed_re.findall(drift) == [s for s in sed_re.findall(verify) if "chifor" in s] and len(sed_re.findall(drift)) == 1,
      "D8: the health check extracts the login exactly as the removal does")

print("[E] structure: pre-flight first; the whole switch guarded; the rescue restores and verifies")
OB = "tasks/openbao.yml"
pre = position(OB, "Pre-flight this slot's own Gitea tokens")
for prefix in ("Lay down the kubeconfig and platform render templates", "Switch the agent onto"):
    check(pre < position(OB, prefix), "E1: the Gitea pre-flight runs before '%s'" % prefix)
check(task(OB, "Pre-flight this slot's own Gitea tokens").get("when") == "dev_worker_gitea_identity_cutover | bool",
      "E2: ...and only when the gate is on")
check(task(OB, "Switch the agent onto").get("ansible.builtin.import_tasks") == "openbao_switch.yml",
      "E3: openbao.yml imports the switch")
switch = task(SW, "Switch the agent onto")
inside = [str(t.get("name", "")) for t in switch["block"]]
order = ["Keep the current credential template", "Lay down the bao agent config", "Lay down the git-credentials render template",
         "Find credential files that are missing", "Remove the ones that name another login", "Enable + start openbao-agent",
         "Apply restarts queued elsewhere", "Restart openbao-agent onto", "Verify the agent restarted",
         "Pin the git.chifor.me credential username", "Record this credential template as the last verified one"]
idx = [next((i for i, n in enumerate(inside) if n.startswith(p)), -1) for p in order]
check(-1 not in idx and idx == sorted(idx), "E4: every mutation, the start, the restart, the check and the pin are inside the block, in order",
      list(zip(order, idx)))
check(not any("notify" in t for t in switch["block"]), "E5: no restart is left to a handler inside the switch")
restart = task(SW, "Restart openbao-agent onto")
check("dev_worker_git_login_drift.stdout_lines | length > 0" in str(restart.get("when"))
      and "dev_worker_git_ctmpl is changed" in str(restart.get("when")),
      "E6: a missing or other-login file forces the restart even when no template changed")
rescue = task(SW, "Roll back to the last verified config")["ansible.builtin.shell"]
for needle, label in (("git-credentials.ctmpl.last-good", "the last verified template"),
                      ('!= "$prev" ]; then rm -f', "removes a file that names another login"),
                      ("credential.https://git.chifor.me.username \"$prev\"", "re-pins the restored login"),
                      ("did NOT verify", "verifies the rollback before reporting it")):
    check(needle in rescue, "E7: the rescue uses " + label if needle.endswith("last-good") else "E7: the rescue " + label)

print("[F] the pre-flight, run against a stub cred and hostname")
pre_sh = task(OB, "Pre-flight this slot's own Gitea tokens")["ansible.builtin.shell"]
fix, stubs = os.path.join(WORK, "kv"), os.path.join(WORK, "stubs")
os.makedirs(stubs)
with open(os.path.join(stubs, "cred"), "w") as f:  # `cred get <name> <field>` from $FIX/<name>/<field>
    f.write('#!/bin/sh\n[ "$1" = get ] || exit 2\n[ -f "$CRED_FIX/$2/$3" ] || { echo "cred: no field $3" >&2; exit 1; }\n'
            'cat "$CRED_FIX/$2/$3"\n')
with open(os.path.join(stubs, "hostname"), "w") as f:
    f.write('#!/bin/sh\nprintf "%s\\n" "$STUB_HOSTNAME"\n')
for s in ("cred", "hostname"):
    os.chmod(os.path.join(stubs, s), 0o755)
# The task calls cred by absolute path; only that path is swapped for the stub.
pre_run = render(pre_sh, inventory_hostname=HOST).replace("/usr/local/bin/cred", os.path.join(stubs, "cred"))


def kv(slot_fields):
    for name, fields in (("common", {f: TOKENS["common"] for f in ("gitea_pat", "gitea_repo_pat", "gitea_package_pat")}),
                         (HOST, slot_fields)):
        d = os.path.join(fix, name)
        os.makedirs(d, exist_ok=True)
        for f in os.listdir(d):
            os.remove(os.path.join(d, f))
        for k, val in fields.items():
            open(os.path.join(d, k), "w").write(val)


good = {f: TOKENS[HOST] + f for f in ("gitea_pat", "gitea_repo_pat", "gitea_package_pat")}
penv = {"PATH": stubs + ":" + os.environ["PATH"], "CRED_FIX": fix, "STUB_HOSTNAME": HOST}
TOKENS.update({"slot-" + k: v for k, v in good.items()})
cases = [
    ("all three present", good, HOST, 0),
    ("hostname -s is not the inventory name", good, "dev-worker-9", 90),
    ("gitea_repo_pat missing", {k: v for k, v in good.items() if k != "gitea_repo_pat"}, HOST, 91),
    ("gitea_package_pat empty", dict(good, gitea_package_pat=""), HOST, 92),
    ("gitea_pat is the shared token", dict(good, gitea_pat=TOKENS["common"]), HOST, 93),
]
for label, fields, hn, want in cases:
    kv(fields)
    rc, out, err = run_sh(pre_run, dict(penv, STUB_HOSTNAME=hn))
    check(rc == want, "F1: %s -> exit %d" % (label, want), (rc, out, err))
    check(not leaked(out + err), "F2: %s -> no value printed" % label, leaked(out + err))

print("[G] the agent instructions")
blk = task(OB, "Publish the credential instructions")
block = blk["ansible.builtin.blockinfile"]["block"]
check("'.claude/CLAUDE.md', '.codex/AGENTS.md'" in blk["loop"], "G1: rendered into CLAUDE.md and AGENTS.md", blk["loop"])


def guide(v, target):
    return render(block, item=({"name": "u", "home": "/home/u"}, target),
                  dev_worker_openbao_renders_kubeconfigs=False, dev_worker_platform_fields_present=False, **v)


GITEA_TODAY = """## Gitea (git.chifor.me): three tokens, three jobs

All three are fields of `cred` name `common` and authenticate as the non-admin
`dev-worker-bot`; they differ only in scope. A 403 saying "token does not have at
least one of required scope(s)" usually means the wrong token, not missing access: switch to
the token below that carries the scope. Ask the operator only when none of the three does.

- `gitea_pat` (`write:repository,write:issue,read:organization,read:user`): git, PRs, issues,
  comments. It is the token in `~/.git-credentials`, so plain `git` already uses it.
- `gitea_repo_pat` (`write:organization,write:repository,read:user`): creating a repository in
  org `cchifor`. `cred exec common gitea_repo_pat GITEA_TOKEN -- <cmd>`.
- `gitea_package_pat` (`write:package,read:repository,read:user`): packages and container images
  under owner `cchifor` (`https://git.chifor.me/api/packages/cchifor/...`), reading or
  publishing (`write:package` includes `read:package`). For a Forge release:
  `cred exec common gitea_package_pat FORGE_RELEASE_TOKEN -- <cmd>` with
  `FORGE_RELEASE_USERNAME=dev-worker-bot`; the same field serves `FORGE_CATALOG_TOKEN`.
"""


def gitea_section(text):
    s = text.index("## Gitea (git.chifor.me)")
    return text[s:text.index("\n## ", s + 1)].rstrip("\n") + "\n"


g_off, g_on = guide(OFF, ".claude/CLAUDE.md"), guide(ON, ".claude/CLAUDE.md")
check(gitea_section(g_off) == GITEA_TODAY, "G2: gate off: the Gitea instructions are the pre-gate text byte for byte",
      gitea_section(g_off))
sec_on = gitea_section(g_on)
check("`cred` name `%s`" % HOST in sec_on and "cred exec %s gitea_repo_pat" % HOST in sec_on
      and "cred exec %s gitea_package_pat" % HOST in sec_on and "FORGE_RELEASE_USERNAME=%s" % BOT in sec_on
      and "`%s`;" % BOT in sec_on, "G3: gate on: all three tokens via `cred ... <host>` as <host>-bot", sec_on)
check("common" not in sec_on and "dev-worker-bot" not in sec_on, "G4: gate on: no shared name or login left", sec_on)
for state, v in (("off", OFF), ("on", ON)):
    for target, agent in ((".claude/CLAUDE.md", "claude"), (".codex/AGENTS.md", "codex")):
        g = guide(v, target)
        lines = [l.strip() for l in g.splitlines()]
        check("Worker: %s" % HOST in lines and "Agent: %s" % agent in lines and "Run: <run id>" in lines,
              "G5 gate %s, %s: the Worker/Agent/Run trailers" % (state, agent))
        check("<!-- worker: %s agent: %s run: <run id> -->" % (HOST, agent) in lines,
              "G6 gate %s, %s: the run marker line" % (state, agent))
        check("`%s`" % BOT in g and "`unknown`" in g and "approve-pin <sha>" in g and "no marker" in g,
              "G7 gate %s, %s: commit identity, `unknown` run id, the command-comment exemption" % (state, agent))

print("[H] the commit identity task and its override audit")
gi = "tasks/git_identity.yml"
ident = task(gi, "Set each user's git commit identity")
mod = ident["community.general.git_config"]
check(mod["scope"] == "file" and mod["file"] == "{{ item.0.home }}/.gitconfig" and "add_mode" not in mod,
      "H1: user.* is written to ~/.gitconfig by name, replace-all")
check("dev_worker_git_user_name" in ident["loop"] and "dev_worker_git_user_email" in ident["loop"],
      "H2: ...with the identity variables")
check(all("cutover" not in str(t.get("when", "")) for t in tasks_in(gi))
      and "cutover" not in str(task("tasks/main.yml", "Git commit identity")), "H3: not gated on the cut-over")
check(position("tasks/main.yml", "Git commit identity") < position("tasks/main.yml", "OpenBao credential plumbing")
      and "when" not in task("tasks/main.yml", "Git commit identity"), "H4: imported unconditionally, not under openbao")

audit = task(gi, "Audit what can still override")["ansible.builtin.shell"]
me = pwd.getpwuid(os.getuid()).pw_name
h, ws = os.path.join(WORK, "home-audit"), os.path.join(WORK, "ws")
os.makedirs(os.path.join(h, ".config", "git"))
os.makedirs(os.path.join(ws, me))
open(os.path.join(h, ".gitconfig"), "w").write(
    "[user]\n\tname = %s\n\temail = %s\n[includeIf \"gitdir:~/elsewhere/\"]\n\tpath = ~/elsewhere.inc\n" % (BOT, EMAIL))
open(os.path.join(h, ".config", "git", "config"), "w").write("[author]\n\temail = codex@chifor.me\n")
open(os.path.join(h, ".bashrc"), "w").write("# a profile\nexport PATH=$PATH:/x\nexport GIT_COMMITTER_NAME=stub-profile-value-5555\n")
genv = dict(os.environ, HOME=h, GIT_CONFIG_NOSYSTEM="1", GIT_TERMINAL_PROMPT="0")
for k in ("GIT_DIR", "GIT_WORK_TREE", "GIT_CONFIG_GLOBAL"):
    genv.pop(k, None)


def git(*args, cwd=None):
    subprocess.run(["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "init.defaultBranch=main", *args],
                   cwd=cwd, env=genv, check=True, capture_output=True)


proj = os.path.join(ws, me, "proj")
git("init", "-q", proj)
git("config", "user.name", "Owner Name", cwd=proj)
git("commit", "-q", "--allow-empty", "-m", "x", cwd=proj)
git("config", "extensions.worktreeConfig", "true", cwd=proj)
git("worktree", "add", "-q", os.path.join(ws, me, "wt"), cwd=proj)
git("config", "--worktree", "user.email", "wt@example.invalid", cwd=os.path.join(ws, me, "wt"))
dep = os.path.join(ws, me, "node_modules", "dep")  # depth 4: inside find's bound, so only the prune skips it
git("init", "-q", dep)
git("config", "user.name", "Pruned Dep", cwd=dep)
script = render(audit, dev_worker_users=[{"name": me, "home": h}], dev_worker_git_user_name=BOT,
                dev_worker_git_user_email=EMAIL, dev_worker_workspace_mount=ws,
                dev_worker_archive_dir=os.path.join(ws, "archive"))
rc, out, err = run_sh(script)
check(rc == 0, "H5: the audit never fails the run", (rc, err))
expect = {
    "H6: an XDG author.email (outranks user.email)": "%s/.config/git/config: author.email = codex@chifor.me" % h,
    "H7: ...and the effective author it produces": "git var GIT_AUTHOR_IDENT outside a repository is '%s <codex@chifor.me>'" % BOT,
    "H8: an includeIf in ~/.gitconfig": "%s/.gitconfig: includeif.gitdir:~/elsewhere/.path = ~/elsewhere.inc" % h,
    "H9: a repository's own user.name": "%s/.git/config: user.name = Owner Name" % proj,
    "H10: a linked worktree's config.worktree": "config.worktree: user.email = wt@example.invalid",
    "H11: a profile export, by name and line": "%s/.bashrc:3: GIT_COMMITTER_NAME is set" % h,
    "H12: a live process environment, by name": "pid %s (sleep) has GIT_AUTHOR_NAME in its environment" % SLEEPER,
}
for label, needle in expect.items():
    check(needle in out, label, out)
check("GIT_COMMITTER_IDENT" not in out, "H13: the committer identity is not reported (it is right)", out)
check(".gitconfig: user." not in out, "H14: the role's own user.* in ~/.gitconfig is not an override", out)
check("Pruned Dep" not in out, "H15: dependency trees are not walked", out)
check(not leaked(out + err), "H16: no value of a profile or process variable is printed", leaked(out + err))

print("[I] the role never sets an identity variable itself")
pat = re.compile(r"GIT_(AUTHOR|COMMITTER)_(NAME|EMAIL)\s*[=:]|(^|[^A-Za-z0-9_])EMAIL\s*=")
hits = []
for sub in ("tasks", "templates", "files"):
    for root, _, files in os.walk(os.path.join(ROLE, sub)):
        for name in files:
            p = os.path.join(root, name)
            for n, l in enumerate(open(p, encoding="utf-8", errors="replace"), 1):
                if pat.search(l):
                    hits.append("%s:%d" % (os.path.relpath(p, ROLE), n))
check(not hits, "I1: no task, template or file exports GIT_AUTHOR_*/GIT_COMMITTER_*/EMAIL", hits)

print("[J] every free-form shell in the role's tasks loads")
# ansible splits a free-form shell like an argument list, comments included, and refuses the whole
# play on an unbalanced quote ("failed at splitting arguments"): one `run's` in a comment of the
# health check did exactly that. Quote parity outside Jinja and backslash escapes is that property.


def balanced(s):
    s = re.sub(r"\\.", "", re.sub(r"\{\{.*?\}\}|\{%.*?%\}", "", s, flags=re.S))
    sq = dq = False
    for ch in s:
        if ch == "'" and not dq:
            sq = not sq
        elif ch == '"' and not sq:
            dq = not dq
    return not (sq or dq)


unbalanced = []
for name in sorted(os.listdir(os.path.join(ROLE, "tasks"))):
    for t in tasks_in(os.path.join("tasks", name)):
        for k in ("ansible.builtin.shell", "ansible.builtin.command"):
            if isinstance(t.get(k), str) and not balanced(t[k]):
                unbalanced.append("%s: %s" % (name, t.get("name")))
check(not unbalanced, "J1: no shell task has an unbalanced quote", unbalanced)

print("[K] only a slot-shaped name reaches the shell, the KV path and the logins")
slot_assert = task("tasks/main.yml", "Assert the slot name")
check("always" in slot_assert.get("tags", []), "K1: the slot assert runs on every tag")
check(position("tasks/main.yml", "Assert the slot name") < position("tasks/main.yml", "Git commit identity")
      and position("tasks/main.yml", "Assert the slot name") < position("tasks/main.yml", "OpenBao credential plumbing"),
      "K2: ...before the identity and OpenBao task files")


def slot_ok(host, gate):
    v = dict(DEFAULTS, inventory_hostname=host, dev_worker_gitea_identity_cutover=gate)
    for _ in range(5):
        for k, val in list(v.items()):
            if k.startswith("dev_worker_git") and isinstance(val, str) and "{{" in val:
                v[k] = render(val, **v)
    return all(render("{{ %s }}" % c, **v) == "True" for c in slot_assert["ansible.builtin.assert"]["that"])


for host in ("dev-worker-1", "dev-worker-12"):
    check(slot_ok(host, False) and slot_ok(host, True), "K3: %s passes with the gate off and on" % host)
for host in ('dev-worker-3"; touch /tmp/x; "', "dev-worker-3 ", "dev-worker-x", "reviewer-1", "dev-worker-3\n"):
    check(not slot_ok(host, False) and not slot_ok(host, True), "K4: %r is refused" % host)

print()
print("ALL PASS" if fails == 0 else "%d CHECK(S) FAILED" % fails)
sys.exit(1 if fails else 0)
PY

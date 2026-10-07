"""scripts/gitea-owner-merge.sh: owner-gated merges with a one-shot owner token (auth-hardening plan B8, R13).

Runs the script against fakes on PATH: `git` (the workstation-bot credential that scripts/gitea-api.sh reads),
`curl` (a small Gitea API emulator) and `kubectl` (the in-pod token mint and the psql delete). Every fake logs its
argv, and the emulator and kubectl append to one shared event log, so the tests can prove:
  - the gate (head, both bots' approvals, required and reported status contexts, live protection drift) refuses
    BEFORE any kubectl call, so no owner token is ever minted on those paths;
  - on the happy path exactly one token is minted and deleted, used only between the two, and never in any argv;
  - force_merge is sent only after Gitea's "Changed protected files" refusal;
  - the delete runs even when the merge call fails, and an unconfirmed revocation is loud;
  - without --execute the script is a dry run: one credential-free gate pass and a plan, nothing else.

SAFETY (2026-10-07 incident: a hand-made "guard" PATH was split at a drive colon and a live run merged a PR):
every run first proves that bash resolves kubectl, curl and git to the fakes, and aborts with FakesNotFirst
otherwise, before the script starts. The environment also breaks the real tools (no kubeconfig, a dead HTTPS
proxy, no system or global git config), in case anything still reached one.
"""
import json
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "gitea-owner-merge.sh")
BASH = shutil.which("bash")
WS_TOKEN = "f00dfeed" * 5  # the routine workstation-bot credential
HEAD = "8a8daed88aa05197de65bfd2d97af00aa740f250"
OLD = "1111111111111111111111111111111111111111"
MERGE_SHA = "9999999999999999999999999999999999999999"
REQUIRED_PLATFORM = [
    "CI / ci-gate*",
    "E2E Preflight / preflight*",
    "E2E Tests / smoke*",
    "Contract Tests / contract-gate*",
    "S2S Authority Guard / guard*",
]
ACK = "CI / owner-ack (pull_request)"
GATE = "CI / ci-gate (pull_request)"
RUN_URL = "/cchifor/platform/actions/runs/71276/jobs/471671"

FAKE_GIT = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    printf '%s\\n' "$*" >> "$FAKE_DIR/argv.git"
    if [ "$1" = credential ] && [ "$2" = fill ]; then
      cat >/dev/null
      printf 'protocol=https\\nhost=git.chifor.me\\nusername=workstation-bot\\npassword={WS_TOKEN}\\n'
      exit 0
    fi
    exit 1
    """)

# Parses the curl options both scripts use (-o FILE, -w FMT, -X METHOD, --data-binary @FILE, -H @- with the
# header on stdin, the URL), then lets respond.py decide. The header travels to respond.py in the environment,
# never in an argv, so the argv log stays a faithful record of what the script put on curl's command line.
FAKE_CURL = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf '%s\\n' "$*" >> "$FAKE_DIR/argv.curl"
    out=""; wfmt=""; method=GET; data=""; url=""; hdr_stdin=0
    while [ $# -gt 0 ]; do
      case "$1" in
        -o) out=$2; shift 2;;
        -w) wfmt=$2; shift 2;;
        -X) method=$2; shift 2;;
        --data-binary) data=$2; shift 2;;
        -H) [ "$2" = "@-" ] && hdr_stdin=1; shift 2;;
        https://*|http://*) url=$1; shift;;
        *) shift;;
      esac
    done
    hdrs=""
    [ "$hdr_stdin" = 1 ] && hdrs=$(cat)
    body=""
    case "$data" in @*) body=$(cat "${data#@}");; esac
    resp=$(printf '%s' "$body" | FAKE_METHOD="$method" FAKE_URL="$url" FAKE_HEADERS="$hdrs" "$FAKE_PY" "$FAKE_DIR/respond.py" | tr -d '\\r')
    code=${resp%%$'\\n'*}
    case "$resp" in *$'\\n'*) rbody=${resp#*$'\\n'};; *) rbody="";; esac
    case "$code" in EXIT*) echo "curl: (7) Failed to connect" >&2; printf '000'; exit "${code#EXIT}";; esac
    if [ -n "$out" ]; then printf '%s' "$rbody" > "$out"; else printf '%s' "$rbody"; fi
    [ -n "$wfmt" ] && printf '%s' "${wfmt//'%{http_code}'/$code}"
    exit 0
    """)

FAKE_KUBECTL = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf '%s\\n' "$*" >> "$FAKE_DIR/argv.kubectl"
    stdin=""
    case " $* " in *" -i "*) stdin=$(cat);; esac
    printf '%s' "$stdin" | FAKE_ARGS="$*" "$FAKE_PY" "$FAKE_DIR/kube.py"
    """)

STATE_HELPERS = textwrap.dedent("""\
    import json, os, re, sys
    D = os.environ["FAKE_DIR"]
    SC = json.load(open(os.path.join(D, "scenario.json"), encoding="utf-8"))
    SP = os.path.join(D, "state.json")
    ST = json.load(open(SP, encoding="utf-8")) if os.path.exists(SP) else {}
    ST.setdefault("minted", {}); ST.setdefault("revoked", [])
    def save():
        with open(SP, "w", encoding="utf-8") as f:
            json.dump(ST, f)
    def event(s):
        with open(os.path.join(D, "events"), "a", encoding="utf-8") as f:
            f.write(s + "\\n")
    """)

# A minimal Gitea: identity by token, the PR, its reviews, the combined status (paged at limit), the branch,
# and the owner-only writes (comment, rerun-failed-jobs, merge). A revoked owner token answers 401.
RESPOND_PY = STATE_HELPERS + textwrap.dedent("""\
    method = os.environ["FAKE_METHOD"]; url = os.environ["FAKE_URL"]
    m = re.search(r"Authorization: token (\\S+)", os.environ.get("FAKE_HEADERS", ""))
    tok = m.group(1) if m else ""
    body = sys.stdin.read()
    if tok == SC["ws_token"]:
        auth = "ws"
    elif tok in ST["minted"].values():
        auth = "revoked" if tok in ST["revoked"] else "owner"
    else:
        auth = "none"
    full = url.split("/api/v1", 1)[1] if "/api/v1" in url else url
    path, _, query = full.partition("?")
    q = dict(kv.split("=", 1) for kv in query.split("&") if "=" in kv)
    event(f"CALL {auth} {method} {path}" + (f" {body}" if body else ""))
    def reply(code, obj=""):
        save()
        print(code)
        print(obj if isinstance(obj, str) else json.dumps(obj))
        sys.exit(0)
    if auth in ("none", "revoked"):
        reply(401, {"message": "invalid token"})
    if path == "/user":
        reply(200, {"login": "workstation-bot", "is_admin": False} if auth == "ws" else {"login": "chifor", "is_admin": True})
    base = "/repos/cchifor/" + SC["repo"]
    n = str(SC["pr"]["number"])
    def page(items):
        p, lim = int(q.get("page", 1)), int(q.get("limit", 50))
        return items[(p - 1) * lim:p * lim]
    if method == "GET" and path == f"{base}/pulls/{n}":
        pr = dict(SC["pr"])
        if ST.get("merged"):
            pr.update(merged=True, state="closed", merge_commit_sha=SC["merge_sha"])
        reply(200, pr)
    if method == "GET" and path == f"{base}/pulls/{n}/reviews":
        reply(200, page(SC["reviews"]))
    if method == "GET" and path == f"{base}/branches/main":
        reply(200, SC["branch"])
    if method == "GET" and path.startswith(f"{base}/commits/") and path.endswith("/status"):
        phases, key = (SC["after_rerun"], "reads_after") if ST.get("rerun") else (SC["statuses"], "reads")
        idx = min(ST.get(key, 0), len(phases) - 1)
        items = page(phases[idx])
        if q.get("page", "1") == "1":
            ST[key] = ST.get(key, 0) + 1
        reply(200, {"state": "x", "statuses": items, "total_count": len(items)})
    if method == "POST" and auth == "owner":
        if path == f"{base}/issues/{n}/comments":
            reply(201, {"id": 1})
        if re.fullmatch(base + r"/actions/runs/\\d+/rerun-failed-jobs", path):
            ST["rerun"] = True
            reply(201, {})
        if path == f"{base}/pulls/{n}/merge":
            i = ST.get("merges", 0)
            ST["merges"] = i + 1
            code, obj = SC["merge_responses"][min(i, len(SC["merge_responses"]) - 1)]
            if code == 200:
                ST["merged"] = True
            reply(code, obj)
    reply(404 if auth == "owner" else 403, {"message": "not here"})
    """)

KUBE_PY = STATE_HELPERS + textwrap.dedent("""\
    args = os.environ["FAKE_ARGS"]
    stdin = sys.stdin.read()
    if " get pod" in " " + args:
        if "cnpg.io/cluster=infra-pg" in args:
            print("pod/infra-pg-6")
        elif "app.kubernetes.io/name=gitea" in args:
            print("pod/gitea-86f5679868-586wg")
        sys.exit(0)
    if "generate-access-token" in args:
        name = re.search(r"--token-name (\\S+)", args).group(1)
        event(f"MINT {name}")
        if SC.get("mint_fail"):
            print("Command error: boom", file=sys.stderr)
            sys.exit(1)
        tok = ("c0ffee%02d" % (len(ST["minted"]) + 1)).ljust(40, "e")
        ST["minted"][name] = tok
        save()
        print(tok)
        sys.exit(0)
    if "psql" in args:
        if stdin.lower().startswith("delete"):
            name = re.search(r"and name='([^']+)'", stdin).group(1)
            event(f"DELETE {name}")
            if not SC.get("revocation_ineffective") and name in ST["minted"]:
                ST["revoked"].append(ST["minted"][name])
            save()
            print("DELETE 1")
        elif "string_agg" in stdin:
            event("LIST")
            live = [k for k, v in ST["minted"].items() if v not in ST["revoked"]]
            print(",".join(["cc-admin-20260913"] + live))
        sys.exit(0)
    sys.exit(1)
    """)


def status(context, state, sid, url=None):
    return {"id": sid, "context": context, "status": state,
            "target_url": url or "/cchifor/platform/actions/runs/71276/jobs/471600"}


def green(**overrides):
    """A fully green platform head: the five required contexts, owner-ack, and a skipped job."""
    ctx = {
        GATE: "success",
        "E2E Preflight / preflight (pull_request)": "success",
        "E2E Tests / smoke (pull_request)": "success",
        "Contract Tests / contract-gate (pull_request)": "success",
        "S2S Authority Guard / guard (pull_request)": "success",
        ACK: "success",
        "CI / frontend (pull_request)": "skipped",
    }
    ctx.update(overrides)
    out = []
    for i, (c, s) in enumerate(ctx.items(), start=1):
        if s is not None:
            out.append(status(c, s, 100 + i, RUN_URL if c == ACK else None))
    return out


class FakesNotFirst(RuntimeError):
    """Raised BEFORE the script runs when a real kubectl, curl or git would be used (2026-10-07 incident)."""


def approved(login, commit=HEAD, rid=1, state="APPROVED", dismissed=False):
    return {"id": rid, "user": {"login": login}, "state": state, "commit_id": commit, "dismissed": dismissed}


@unittest.skipIf(BASH is None, "bash not available")
class GiteaOwnerMergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        self.dir = os.path.join(self.tmp, "fake")
        os.makedirs(self.bin)
        os.makedirs(self.dir)
        for name, src in (("git", FAKE_GIT), ("curl", FAKE_CURL), ("kubectl", FAKE_KUBECTL)):
            p = os.path.join(self.bin, name)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(src)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        for name, src in (("respond.py", RESPOND_PY), ("kube.py", KUBE_PY)):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(src)
        for f in ("argv.git", "argv.curl", "argv.kubectl", "events"):
            open(os.path.join(self.dir, f), "w").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def fresh(self):
        """A clean fake world for the next case of a looping test."""
        self.tearDown()
        self.setUp()

    # -- scenario and run -------------------------------------------------------------------------------------
    def scenario(self, **kw):
        sc = {
            "ws_token": WS_TOKEN,
            "repo": "platform",
            "merge_sha": MERGE_SHA,
            "pr": {"number": 7, "state": "open", "merged": False, "draft": False, "title": "feat: x",
                   "head": {"sha": HEAD}, "base": {"ref": "main"}, "mergeable": True},
            "reviews": [approved("reviewer-claude", rid=1), approved("reviewer-codex", rid=2)],
            "statuses": [green()],
            "after_rerun": [green()],
            "branch": {"name": "main", "protected": True, "enable_status_check": True,
                       "status_check_contexts": list(REQUIRED_PLATFORM)},
            "merge_responses": [[200, ""]],
        }
        sc.update(kw)
        with open(os.path.join(self.dir, "scenario.json"), "w", encoding="utf-8") as f:
            json.dump(sc, f)

    def env(self, polls="2", path_first=True):
        """The script's environment. The fakes' bin dir goes FIRST on PATH, joined with os.pathsep in the native
        form (Git Bash converts a native Windows PATH; a drive-colon path joined with ':' would be split).
        Second line of defence, should a real tool ever be reached anyway: no kubeconfig, a dead HTTPS proxy,
        and no system or global git config (so no credential helper)."""
        e = dict(os.environ)
        e["PATH"] = (self.bin + os.pathsep + e["PATH"]) if path_first else e["PATH"]
        e["FAKE_DIR"] = self.dir.replace("\\", "/")
        e["FAKE_PY"] = sys.executable.replace("\\", "/")  # CI runners may have python3 only
        e["OWNER_MERGE_POLL_SECONDS"] = "0"
        e["OWNER_MERGE_MAX_POLLS"] = polls
        e["KUBECONFIG"] = os.path.join(self.tmp, "no-such-kubeconfig")
        for proxy in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            e[proxy] = "http://127.0.0.1:9"
        e.pop("NO_PROXY", None)
        e.pop("no_proxy", None)
        e["GIT_CONFIG_NOSYSTEM"] = "1"
        e["GIT_CONFIG_GLOBAL"] = os.path.join(self.tmp, "no-such-gitconfig")
        return e

    def resolved_tools(self, env, prelude=""):
        """Where bash resolves kubectl, curl and git under ENV (after the shell PRELUDE), as native paths."""
        probe = prelude + ('for t in kubectl curl git; do p=$(command -v "$t") || p="<none>"; '
                 'if command -v cygpath >/dev/null 2>&1 && [ "$p" != "<none>" ]; then p=$(cygpath -m "$p"); fi; '
                 'echo "$t=$p"; done')
        out = subprocess.run([BASH, "-c", probe], capture_output=True, text=True, env=env, timeout=30).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def assert_fakes_first(self, env, prelude=""):
        """Abort unless every tool the script can reach the forge or the cluster with is a fake."""
        want = os.path.normcase(os.path.normpath(self.bin))
        tools = self.resolved_tools(env, prelude)
        for t in ("kubectl", "curl", "git"):
            got = tools.get(t, "<none>")
            if got == "<none>" or os.path.normcase(os.path.normpath(os.path.dirname(got))) != want:
                raise FakesNotFirst(f"{t} resolves to {got}, not the fake in {self.bin}: refusing to run the script")

    def run_script(self, *args, polls="2", execute=True):
        e = self.env(polls)
        self.assert_fakes_first(e)
        argv = [*args, "--execute"] if execute else list(args)
        return subprocess.run([BASH, SCRIPT, *argv], capture_output=True, text=True, env=e, timeout=300)

    # -- log readers -----------------------------------------------------------------------------------------
    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return [line for line in f.read().splitlines() if line]

    def events(self):
        return self.read("events")

    def mints(self):
        return [e for e in self.events() if e.startswith("MINT ")]

    def deletes(self):
        return [e for e in self.events() if e.startswith("DELETE ")]

    def owner_calls(self):
        return [e for e in self.events() if e.startswith("CALL owner ")]

    def merge_bodies(self):
        return [json.loads(e.split(" ", 4)[4]) for e in self.owner_calls() if e.split(" ")[3].endswith("/merge")]

    def minted_tokens(self):
        p = os.path.join(self.dir, "state.json")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return list(json.load(f)["minted"].values())

    def assert_no_secret_in_argv_or_output(self, r):
        secrets = [WS_TOKEN] + self.minted_tokens()
        for log in ("argv.git", "argv.curl", "argv.kubectl"):
            for line in self.read(log):
                for s in secrets:
                    self.assertNotIn(s, line, log)
        for s in secrets:
            self.assertNotIn(s, r.stdout + r.stderr)

    def assert_refused_without_minting(self, r):
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(self.read("argv.kubectl"), [], "kubectl must not run before the gate passes")
        self.assertEqual(self.mints(), [])
        self.assertEqual(self.owner_calls(), [])

    # -- the harness guard (2026-10-07: a hand-built PATH let a "read-only" run reach the real tools) -----------
    def test_harness_resolves_every_tool_to_the_fakes(self):
        tools = self.resolved_tools(self.env())
        for t in ("kubectl", "curl", "git"):
            self.assertEqual(os.path.normcase(os.path.normpath(os.path.dirname(tools[t]))),
                             os.path.normcase(os.path.normpath(self.bin)), (t, tools))

    def test_harness_guard_aborts_when_the_fakes_are_not_first(self):
        with self.assertRaises(FakesNotFirst):
            self.assert_fakes_first(self.env(path_first=False))
        if os.name == "nt":
            # the incident's exact mistake, inside bash: a drive-colon dir prepended to the POSIX PATH with ':'
            # is split into "C" and "/Users/...", so the real tools win
            prelude = 'PATH="%s:$PATH"; ' % self.bin.replace("\\", "/")
            with self.assertRaises(FakesNotFirst):
                self.assert_fakes_first(self.env(path_first=False), prelude)

    # -- dry run is the default: no mint, comment, re-run or merge without --execute ---------------------------
    def assert_nothing_but_reads(self):
        self.assertEqual(self.read("argv.kubectl"), [], "a dry run never calls kubectl")
        self.assertEqual(self.mints(), [])
        self.assertEqual(self.owner_calls(), [])
        calls = [e for e in self.events() if e.startswith("CALL ")]
        self.assertTrue(calls and all(e.startswith("CALL ws GET ") for e in calls), calls)

    def test_without_execute_it_is_a_dry_run(self):
        self.scenario(merge_responses=[[405, {"message": "Changed protected files"}], [200, ""]])
        r = self.run_script("platform", "7", HEAD, execute=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn("would merge", r.stdout)
        self.assert_nothing_but_reads()

    def test_explicit_dry_run_flag_is_the_same(self):
        self.scenario()
        r = self.run_script("platform", "7", HEAD, "--dry-run", execute=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assert_nothing_but_reads()

    def test_dry_run_with_approve_pin_names_the_pin_and_the_run(self):
        self.scenario(statuses=[green(**{ACK: "failure", GATE: "failure"})])
        r = self.run_script("platform", "7", HEAD, "--approve-pin", execute=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn(f"approve-pin {HEAD}", r.stdout)
        self.assertIn("run 71276", r.stdout)
        self.assert_nothing_but_reads()

    def test_dry_run_refuses_like_the_real_run(self):
        self.scenario(statuses=[green(**{"E2E Tests / smoke (pull_request)": "failure"})])
        r = self.run_script("platform", "7", HEAD, execute=False)
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assert_nothing_but_reads()

    def test_dry_run_does_not_wait_for_pending_checks(self):
        self.scenario(statuses=[green(**{GATE: "pending"})])
        r = self.run_script("platform", "7", HEAD, polls="5", execute=False)
        self.assertEqual(r.returncode, 6, r.stdout + r.stderr)
        self.assertIn("would wait", r.stdout)
        pr_reads = [e for e in self.events() if e == "CALL ws GET /repos/cchifor/platform/pulls/7"]
        self.assertEqual(len(pr_reads), 1, "one gate pass, no polling")
        self.assert_nothing_but_reads()

    # -- argument and file checks ----------------------------------------------------------------------------
    def test_bad_arguments_exit_2_without_any_call(self):
        self.scenario()
        for args in (("platform", "7", HEAD[:12]), ("platform", "x7", HEAD), ("platform", "7", HEAD.upper()),
                     ("../platform", "7", HEAD), ("platform", "7", HEAD, "--force"), ("platform", "7"),
                     ("platform", "7", HEAD, "--dry-run"), ("platform", "7", HEAD, "extra")):
            r = self.run_script(*args)  # each with --execute appended: --dry-run plus --execute is refused
            self.assertEqual(r.returncode, 2, args)
        self.assertEqual(self.read("argv.curl"), [])
        self.assertEqual(self.read("argv.kubectl"), [])

    def test_repo_without_a_contexts_file_is_refused(self):
        self.scenario(repo="nosuchrepo")
        r = self.run_script("nosuchrepo", "7", HEAD)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("required-contexts-nosuchrepo.txt", r.stdout + r.stderr)
        self.assertEqual(self.read("argv.curl"), [])
        self.assertEqual(self.read("argv.kubectl"), [])

    def test_shipped_context_files_are_lf_and_match_the_live_lists(self):
        d = os.path.join(REPO, "scripts", "owner-ops")
        for repo in ("platform", "ailab"):
            with open(os.path.join(d, f"required-contexts-{repo}.txt"), "rb") as f:
                raw = f.read()
            self.assertNotIn(b"\r", raw, repo)
            patterns = [l for l in raw.decode("utf-8").splitlines() if l.strip() and not l.startswith("#")]
            self.assertEqual(patterns, REQUIRED_PLATFORM if repo == "platform" else [], repo)

    # -- the gate refuses before minting ---------------------------------------------------------------------
    def test_refuses_when_the_head_moved(self):
        self.scenario(pr={"number": 7, "state": "open", "merged": False, "draft": False, "title": "feat: x",
                          "head": {"sha": OLD}, "base": {"ref": "main"}})
        r = self.run_script("platform", "7", HEAD)
        self.assert_refused_without_minting(r)
        self.assertIn("head moved", r.stdout + r.stderr)

    def test_refuses_an_already_merged_or_closed_pr(self):
        for pr in ({"state": "closed", "merged": True}, {"state": "closed", "merged": False}):
            self.fresh()
            base = {"number": 7, "draft": False, "title": "feat: x", "head": {"sha": HEAD}, "base": {"ref": "main"}}
            base.update(pr)
            self.scenario(pr=base)
            r = self.run_script("platform", "7", HEAD)
            self.assert_refused_without_minting(r)

    def test_refuses_a_wip_or_draft_pr(self):
        for extra in ({"title": "WIP: feat: x"}, {"title": "[WIP] feat"}, {"draft": True}):
            self.fresh()
            pr = {"number": 7, "state": "open", "merged": False, "draft": False, "title": "feat: x",
                  "head": {"sha": HEAD}, "base": {"ref": "main"}}
            pr.update(extra)
            self.scenario(pr=pr)
            r = self.run_script("platform", "7", HEAD)
            self.assert_refused_without_minting(r)

    def test_refuses_when_a_bot_did_not_approve_the_head(self):
        cases = [
            [approved("reviewer-claude", rid=1)],  # codex never reviewed
            [approved("reviewer-claude", rid=1), approved("reviewer-codex", commit=OLD, rid=2)],  # stale head
            [approved("reviewer-claude", rid=1), approved("reviewer-codex", rid=2),
             approved("reviewer-codex", rid=3, state="REQUEST_CHANGES")],  # latest at head is not an approval
            [approved("reviewer-claude", rid=1), approved("reviewer-codex", rid=2, dismissed=True)],
            [approved("reviewer-claude", rid=1), approved("someone-else", rid=2)],
        ]
        for reviews in cases:
            self.fresh()
            self.scenario(reviews=reviews)
            r = self.run_script("platform", "7", HEAD)
            self.assert_refused_without_minting(r)

    def test_reads_every_page_of_reviews(self):
        # 60 unrelated reviews first: an approval on page 2 must be found (and the gate must pass).
        noise = [approved("someone-else", rid=i, state="COMMENT") for i in range(1, 61)]
        self.scenario(reviews=noise + [approved("reviewer-claude", rid=61), approved("reviewer-codex", rid=62)])
        r = self.run_script("platform", "7", HEAD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_refuses_when_a_required_context_failed(self):
        self.scenario(statuses=[green(**{"E2E Tests / smoke (pull_request)": "failure"})])
        r = self.run_script("platform", "7", HEAD)
        self.assert_refused_without_minting(r)
        self.assertIn("E2E Tests / smoke", r.stdout + r.stderr)

    def test_refuses_when_a_required_context_stays_pending(self):
        self.scenario(statuses=[green(**{"S2S Authority Guard / guard (pull_request)": "pending"})])
        r = self.run_script("platform", "7", HEAD, polls="3")
        self.assert_refused_without_minting(r)

    def test_refuses_when_a_required_context_is_missing(self):
        self.scenario(statuses=[green(**{"E2E Preflight / preflight (pull_request)": None})])
        r = self.run_script("platform", "7", HEAD)
        self.assert_refused_without_minting(r)
        self.assertIn("E2E Preflight / preflight*", r.stdout + r.stderr)

    def test_refuses_when_any_reported_context_failed(self):
        # force_merge bypasses every check, so a failed context that is not required still refuses.
        self.scenario(statuses=[green(**{"CI / frontend (pull_request)": "failure"})])
        r = self.run_script("platform", "7", HEAD)
        self.assert_refused_without_minting(r)

    def test_refuses_when_owner_ack_failed_without_approve_pin(self):
        self.scenario(statuses=[green(**{ACK: "failure", GATE: "failure"})])
        r = self.run_script("platform", "7", HEAD)
        self.assert_refused_without_minting(r)
        self.assertIn("--approve-pin", r.stdout + r.stderr)

    def test_refuses_when_live_protection_requires_a_context_the_file_lacks(self):
        self.scenario(branch={"name": "main", "enable_status_check": True,
                              "status_check_contexts": REQUIRED_PLATFORM + ["New Gate / new*"]})
        r = self.run_script("platform", "7", HEAD)
        self.assert_refused_without_minting(r)
        self.assertIn("New Gate / new*", r.stdout + r.stderr)

    def test_reads_every_page_of_the_combined_status(self):
        # 120 skipped contexts first: the required ones sit on page 3 and must still be found.
        filler = [status(f"CI / job{i} (pull_request)", "skipped", 1000 + i) for i in range(120)]
        self.scenario(statuses=[filler + green()])
        r = self.run_script("platform", "7", HEAD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)

    def test_waits_without_a_credential_then_mints_once_green(self):
        pending = green(**{GATE: "pending", "E2E Tests / smoke (pull_request)": "pending"})
        self.scenario(statuses=[pending, pending, green()])
        r = self.run_script("platform", "7", HEAD, polls="5")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ev = self.events()
        first_mint = next(i for i, e in enumerate(ev) if e.startswith("MINT "))
        status_reads = [i for i, e in enumerate(ev) if e.startswith("CALL ws GET") and e.endswith("/status")]
        self.assertGreaterEqual(len(status_reads), 3)
        self.assertTrue(all(i < first_mint for i in status_reads), "every status read happens before the mint")
        self.assertEqual(len(self.mints()), 1)

    # -- the owner operation --------------------------------------------------------------------------------
    def test_happy_path_mints_once_merges_and_deletes_once(self):
        self.scenario()
        r = self.run_script("platform", "7", HEAD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.mints()), 1)
        self.assertEqual(len(self.deletes()), 1)
        self.assertEqual(self.mints()[0][5:], self.deletes()[0][7:], "the minted token is the one deleted")
        self.assertEqual(self.merge_bodies(), [{"Do": "merge", "head_commit_id": HEAD}])
        self.assertIn(MERGE_SHA, r.stdout)
        self.assertIn("HTTP 401", r.stdout)  # the post-revocation proof
        self.assertIn("cc-admin-20260913", r.stdout)  # the remaining chifor tokens are listed
        self.assert_no_secret_in_argv_or_output(r)

    def test_token_is_used_only_between_mint_and_delete(self):
        self.scenario()
        r = self.run_script("platform", "7", HEAD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ev = self.events()
        i_mint = next(i for i, e in enumerate(ev) if e.startswith("MINT "))
        i_del = next(i for i, e in enumerate(ev) if e.startswith("DELETE "))
        self.assertLess(i_mint, i_del)
        between = ev[i_mint + 1:i_del]
        self.assertTrue(between and all(e.startswith("CALL owner ") for e in between), between)
        # after the delete, the token answers 401 (the revoked call) and is never used again for real
        self.assertTrue(all(not e.startswith("CALL owner ") for e in ev[i_del:]))
        self.assertTrue(any(e.startswith("CALL revoked GET /user") for e in ev[i_del:]))

    def test_curl_reads_no_config_and_follows_no_redirects(self):
        self.scenario()
        r = self.run_script("platform", "7", HEAD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        for argv in self.read("argv.curl"):
            self.assertTrue(argv.startswith("-q "), argv)
            self.assertIn("--proto =https", argv)
            self.assertIn("--max-redirs 0", argv)
            self.assertIn("https://git.chifor.me/api/v1/", argv)

    def test_force_merge_only_after_changed_protected_files(self):
        self.scenario(merge_responses=[[405, {"message": "Changed protected files", "url": "x"}], [200, ""]])
        r = self.run_script("platform", "7", HEAD)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(self.merge_bodies(), [{"Do": "merge", "head_commit_id": HEAD},
                                               {"Do": "merge", "head_commit_id": HEAD, "force_merge": True}])
        self.assertEqual(len(self.mints()), 1)
        self.assertEqual(len(self.deletes()), 1)
        self.assert_no_secret_in_argv_or_output(r)

    def test_no_force_merge_after_any_other_refusal_and_the_token_is_deleted(self):
        self.scenario(merge_responses=[[405, {"message": "Not all required status checks successful"}]])
        r = self.run_script("platform", "7", HEAD)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(self.merge_bodies(), [{"Do": "merge", "head_commit_id": HEAD}])
        self.assertEqual(len(self.deletes()), 1)
        self.assertIn("HTTP 401", r.stdout + r.stderr)

    def test_delete_runs_when_the_merge_call_fails_in_transport(self):
        self.scenario(merge_responses=[["EXIT7", ""]])
        r = self.run_script("platform", "7", HEAD)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(len(self.mints()), 1)
        self.assertEqual(len(self.deletes()), 1)
        self.assert_no_secret_in_argv_or_output(r)

    def test_delete_runs_when_the_mint_returns_garbage(self):
        self.scenario(mint_fail=True)
        r = self.run_script("platform", "7", HEAD)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(len(self.mints()), 1)
        self.assertEqual(len(self.deletes()), 1, "a half-made token is deleted by name")
        self.assertEqual(self.owner_calls(), [])

    def test_unconfirmed_revocation_is_loud(self):
        self.scenario(revocation_ineffective=True)
        r = self.run_script("platform", "7", HEAD)
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("NOT CONFIRMED", r.stdout + r.stderr)
        self.assert_no_secret_in_argv_or_output(r)

    # -- --approve-pin ---------------------------------------------------------------------------------------
    def pin_scenario(self, after=None, **kw):
        failed = green(**{ACK: "failure", GATE: "failure"})
        rerun_pending = [s for s in green(**{ACK: "pending", GATE: "pending"})]
        for s in rerun_pending:
            if s["context"] in (ACK, GATE):
                s["id"] += 500  # the rerun posts NEW statuses
        done = green()
        for s in done:
            if s["context"] in (ACK, GATE):
                s["id"] += 600
        self.scenario(statuses=[failed],
                      after_rerun=after if after is not None else [failed, rerun_pending, done], **kw)

    def test_approve_pin_posts_the_full_head_reruns_and_merges_with_two_short_tokens(self):
        self.pin_scenario()
        r = self.run_script("platform", "7", HEAD, "--approve-pin", polls="6")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        owner = self.owner_calls()
        self.assertIn(f'CALL owner POST /repos/cchifor/platform/issues/7/comments {{"body":"approve-pin {HEAD}"}}',
                      owner)
        self.assertIn("CALL owner POST /repos/cchifor/platform/actions/runs/71276/rerun-failed-jobs", owner)
        self.assertEqual(len(self.mints()), 2)
        self.assertEqual(len(self.deletes()), 2)
        # never hold a token across the wait: the first token is deleted before the post-pin status polls,
        # and the second is minted only after them
        ev = self.events()
        d1 = next(i for i, e in enumerate(ev) if e.startswith("DELETE "))
        m2 = [i for i, e in enumerate(ev) if e.startswith("MINT ")][1]
        polls = [i for i, e in enumerate(ev[d1:m2]) if e.startswith("CALL ws GET") and e.endswith("/status")]
        self.assertGreaterEqual(len(polls), 3)
        self.assertFalse([e for e in ev[d1:m2] if e.startswith("CALL owner ")])
        self.assertEqual(self.merge_bodies(), [{"Do": "merge", "head_commit_id": HEAD}])
        self.assert_no_secret_in_argv_or_output(r)

    def test_approve_pin_refuses_when_another_context_failed(self):
        self.scenario(statuses=[green(**{ACK: "failure", GATE: "failure",
                                         "Contract Tests / contract-gate (pull_request)": "failure"})])
        r = self.run_script("platform", "7", HEAD, "--approve-pin")
        self.assert_refused_without_minting(r)

    def test_approve_pin_stops_when_owner_ack_fails_again_after_the_rerun(self):
        again = green(**{ACK: "failure", GATE: "failure"})
        for s in again:
            if s["context"] in (ACK, GATE):
                s["id"] += 700  # a NEW failure, not the one the pin was posted for
        self.pin_scenario(after=[again])
        r = self.run_script("platform", "7", HEAD, "--approve-pin", polls="4")
        self.assertEqual(r.returncode, 3, r.stdout + r.stderr)
        self.assertEqual(len(self.mints()), 1, "no merge token after a failed acknowledgement")
        self.assertEqual(len(self.deletes()), 1)
        self.assertEqual(self.merge_bodies(), [])

    def test_approve_pin_skips_the_pin_when_owner_ack_is_already_green(self):
        self.scenario()
        r = self.run_script("platform", "7", HEAD, "--approve-pin")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual(len(self.mints()), 1)
        self.assertFalse([e for e in self.owner_calls() if "/comments" in e or "rerun" in e])

    def test_approve_pin_refuses_a_head_without_owner_ack(self):
        self.scenario(statuses=[green(**{ACK: None})])
        r = self.run_script("platform", "7", HEAD, "--approve-pin")
        self.assert_refused_without_minting(r)


if __name__ == "__main__":
    unittest.main()

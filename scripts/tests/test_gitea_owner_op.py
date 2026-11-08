"""scripts/gitea-owner-op.sh: owner-only Gitea API calls from an ops file, with one one-shot owner token (B8, R13).

Runs the script against fakes on PATH: `curl` (a small Gitea API emulator answering what each scenario says) and
`kubectl` (the pod lookups, the in-pod token mint and the psql delete). Every fake logs its argv, and the emulator
and kubectl append to one shared event log, so the tests can prove:
  - the dry run (the default) validates the ops file and prints the plan, and never calls kubectl or curl;
  - bad input (method, path, placeholders, --var values, the scopes line, body files) exits 2 before any kubectl
    or curl call, so no owner token is ever minted on those paths;
  - --execute mints exactly one token with the scopes the ops file declares, sends each call with the token only
    on curl's stdin, stops at the first unexpected status (exit 4), and deletes the token on every path;
  - an unconfirmed revocation exits 5 and prints the psql delete to run;
  - the token never appears in an argv, stdout or stderr, and no file is left in the cwd or the temp dir.

SAFETY (2026-10-07 incident: a hand-made "guard" PATH was split at a drive colon and a live run merged a PR):
every run first proves that bash resolves kubectl and curl to the fakes, and aborts with FakesNotFirst otherwise,
before the script starts. The environment also breaks the real tools (no kubeconfig, a dead HTTPS proxy), in case
anything still reached one. The script is NEVER run against the live forge.
"""
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "gitea-owner-op.sh")
PRESETS = os.path.join(REPO, "scripts", "owner-ops")
RENOVATE = os.path.join(PRESETS, "renovate-onboard.ops")
TEMPLATE = os.path.join(PRESETS, "repository-template-settings.ops")
BASH = shutil.which("bash")
ORIGIN = "https://git.chifor.me/api/v1"
TOKEN_NAME_TAIL = r"-\d{14}-[0-9a-f]{8}"
TEMPLATE_SETTINGS = {
    "template": True,
    "private": False,
    "default_merge_style": "squash",
    "allow_merge_commits": False,
    "allow_rebase": False,
    "allow_rebase_explicit": False,
    "allow_fast_forward_only_merge": False,
    "allow_squash_merge": True,
    "default_delete_branch_after_merge": True,
}

# Parses the curl options the script uses (-o FILE, -w FMT, -X METHOD, --data-binary @FILE, -H @- with the header
# on stdin, the URL), then lets respond.py decide. The header travels to respond.py in the environment, never in
# an argv, so the argv log stays a faithful record of what the script put on curl's command line.
FAKE_CURL = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf '%s\\n' "$*" >> "$FAKE_DIR/argv.curl"
    out=""; wfmt=""; method=GET; data=""; url=""; hdr_stdin=0; maxtime=""
    while [ $# -gt 0 ]; do
      case "$1" in
        --max-time) maxtime=$2; shift 2;;
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
    # STALL: a server that accepted the connection and never answers. Real curl gives up at --max-time (exit 28);
    # a call without that bound would hang for ever, so the fake records it and fails loudly instead of hanging.
    if [ "$code" = STALL ]; then
      if [ -z "$maxtime" ]; then echo "UNBOUNDED $method $url" >> "$FAKE_DIR/events"; exit 99; fi
      echo "curl: (28) Operation timed out after ${maxtime}000 milliseconds" >&2; printf '000'; exit 28
    fi
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

# A minimal Gitea: a minted, not revoked owner token is "owner"; anything else answers 401. Each owner call is
# answered from the scenario's responses ("METHOD PATH" -> [code, body]); "ECHO_TOKEN" puts the caller's token in
# the body (to prove the script never prints it), and a code "EXIT7" is a transport failure. The event records
# the request body as a JSON string, so a multi-line body stays on one line.
RESPOND_PY = STATE_HELPERS + textwrap.dedent("""\
    method = os.environ["FAKE_METHOD"]; url = os.environ["FAKE_URL"]
    m = re.search(r"Authorization: token (\\S+)", os.environ.get("FAKE_HEADERS", ""))
    tok = m.group(1) if m else ""
    body = sys.stdin.read()
    if tok in ST["minted"].values():
        auth = "revoked" if tok in ST["revoked"] else "owner"
    else:
        auth = "none"
    path = url.split("/api/v1", 1)[1] if "/api/v1" in url else url
    event(f"CALL {auth} {method} {path}" + (" " + json.dumps(body) if body else ""))
    def reply(code, obj=""):
        save()
        print(code)
        print(obj if isinstance(obj, str) else json.dumps(obj))
        sys.exit(0)
    if auth != "owner":
        reply(401, {"message": "invalid token"})
    code, obj = SC.get("responses", {}).get(f"{method} {path}", [404, {"message": "not here"}])
    if obj == "ECHO_TOKEN":
        obj = {"message": "rejected header: Authorization: token " + tok}
    reply(code, obj)
    """)

KUBE_PY = STATE_HELPERS + textwrap.dedent("""\
    args = os.environ["FAKE_ARGS"]
    stdin = sys.stdin.read()
    if " get pod" in " " + args:
        if SC.get("pods_missing"):
            sys.exit(0)
        if "cnpg.io/cluster=infra-pg" in args:
            # failover: the primary is infra-pg-6 at the first lookup and infra-pg-7 from the second on
            ST["pg_lookups"] = ST.get("pg_lookups", 0) + 1
            save()
            print("pod/infra-pg-7" if SC.get("failover") and ST["pg_lookups"] > 1 else "pod/infra-pg-6")
        elif "app.kubernetes.io/name=gitea" in args:
            print("pod/gitea-86f5679868-586wg")
        sys.exit(0)
    if "generate-access-token" in args:
        name = re.search(r"--token-name (\\S+)", args).group(1)
        scopes = re.search(r"--scopes (\\S+)", args).group(1)
        event(f"MINT {name} {scopes}")
        if SC.get("mint_fail"):
            print("Command error: boom", file=sys.stderr)
            sys.exit(1)
        tok = ("c0ffee%02d" % (len(ST["minted"]) + 1)).ljust(40, "e")
        ST["minted"][name] = tok
        save()
        print(tok)
        sys.exit(0)
    if "psql" in args:
        pod = re.search(r"exec -i (\\S+)", args).group(1)
        if SC.get("failover") and pod == "infra-pg-6":
            print("ERROR:  cannot execute DELETE in a read-only transaction", file=sys.stderr)
            sys.exit(1)
        if stdin.lower().startswith("delete"):
            name = re.search(r"and name='([^']+)'", stdin).group(1)
            event(f"DELETE {name}")
            event(f"DELETE-ON {pod}")
            event(f"SQL {stdin.strip()}")
            # revocation_ineffective: the row stays. api_still_accepts: the row goes, but the API still takes the
            # token (a cache), so only the 401 probe can tell.
            # stale_listing: the API refuses the token at once, but the listing still shows it.
            if not SC.get("revocation_ineffective") and name in ST["minted"]:
                if not SC.get("stale_listing"):
                    ST.setdefault("deleted", []).append(name)
                if not SC.get("api_still_accepts"):
                    ST["revoked"].append(ST["minted"][name])
            save()
            print("DELETE 1")
        elif "string_agg" in stdin:
            event("LIST")
            if SC.get("list_fails"):
                print("psql: error: connection lost", file=sys.stderr)
                sys.exit(2)
            live = [k for k in ST["minted"] if k not in ST.get("deleted", [])]
            print(",".join(["cc-admin-20260913"] + live))
        sys.exit(0)
    sys.exit(1)
    """)

RENOVATE_OK = {
    "PUT /repos/cchifor/relay/collaborators/renovate-bot": [204, ""],
    "PUT /repos/cchifor/relay/topics/renovate": [204, ""],
}


class FakesNotFirst(RuntimeError):
    """Raised BEFORE the script runs when a real kubectl or curl would be used (2026-10-07 incident)."""


@unittest.skipIf(BASH is None, "bash not available")
class GiteaOwnerOpTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        self.dir = os.path.join(self.tmp, "fake")
        self.cwd = os.path.join(self.tmp, "cwd")       # the script's working directory: must stay empty
        self.tmpdir = os.path.join(self.tmp, "tmpdir")  # its TMPDIR: the private temp dir must be gone after
        self.ops = os.path.join(self.tmp, "ops")        # ad-hoc ops files and their bodies
        for d in (self.bin, self.dir, self.cwd, self.tmpdir, self.ops):
            os.makedirs(d)
        for name, src in (("curl", FAKE_CURL), ("kubectl", FAKE_KUBECTL)):
            p = os.path.join(self.bin, name)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(src)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        for name, src in (("respond.py", RESPOND_PY), ("kube.py", KUBE_PY)):
            with open(os.path.join(self.dir, name), "w", encoding="utf-8", newline="\n") as f:
                f.write(src)
        for f in ("argv.curl", "argv.kubectl", "events"):
            open(os.path.join(self.dir, f), "w").close()
        self.scenario()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    # -- scenario, ops files and run -------------------------------------------------------------------------
    def scenario(self, **kw):
        sc = {"responses": dict(RENOVATE_OK)}
        sc.update(kw)
        with open(os.path.join(self.dir, "scenario.json"), "w", encoding="utf-8") as f:
            json.dump(sc, f)

    def ops_file(self, text, name="t.ops", bodies=None):
        """An ad-hoc ops file (and body files next to it); returns its path."""
        for rel, content in (bodies or {}).items():
            p = os.path.join(self.ops, *rel.split("/"))
            os.makedirs(os.path.dirname(p), exist_ok=True)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(content)
        p = os.path.join(self.ops, name)
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write(textwrap.dedent(text))
        return p

    def env(self, path_first=True):
        """The script's environment. The fakes' bin dir goes FIRST on PATH, joined with os.pathsep in the native
        form (Git Bash converts a native Windows PATH; a drive-colon path joined with ':' would be split).
        Second line of defence, should a real tool ever be reached anyway: no kubeconfig and a dead HTTPS proxy."""
        e = dict(os.environ)
        e["PATH"] = (self.bin + os.pathsep + e["PATH"]) if path_first else e["PATH"]
        e["FAKE_DIR"] = self.dir.replace("\\", "/")
        e["FAKE_PY"] = sys.executable.replace("\\", "/")  # CI runners may have python3 only
        e["TMPDIR"] = self.tmpdir
        e["KUBECONFIG"] = os.path.join(self.tmp, "no-such-kubeconfig")
        for proxy in ("HTTPS_PROXY", "https_proxy", "ALL_PROXY", "all_proxy"):
            e[proxy] = "http://127.0.0.1:9"
        e.pop("NO_PROXY", None)
        e.pop("no_proxy", None)
        return e

    def resolved_tools(self, env, prelude=""):
        """Where bash resolves kubectl and curl under ENV (after the shell PRELUDE), as native paths."""
        probe = prelude + ('for t in kubectl curl; do p=$(command -v "$t") || p="<none>"; '
                           'if command -v cygpath >/dev/null 2>&1 && [ "$p" != "<none>" ]; then p=$(cygpath -m "$p"); fi; '
                           'echo "$t=$p"; done')
        out = subprocess.run([BASH, "-c", probe], capture_output=True, text=True, env=env, timeout=30).stdout
        return dict(line.split("=", 1) for line in out.splitlines() if "=" in line)

    def assert_fakes_first(self, env, prelude=""):
        """Abort unless every tool the script can reach the forge or the cluster with is a fake."""
        want = os.path.normcase(os.path.normpath(self.bin))
        tools = self.resolved_tools(env, prelude)
        for t in ("kubectl", "curl"):
            got = tools.get(t, "<none>")
            if got == "<none>" or os.path.normcase(os.path.normpath(os.path.dirname(got))) != want:
                raise FakesNotFirst(f"{t} resolves to {got}, not the fake in {self.bin}: refusing to run the script")

    def run_script(self, ops, *args, execute=True):
        """Run the script on OPS; every run must leave the cwd and the temp dir empty."""
        e = self.env()
        self.assert_fakes_first(e)
        argv = [ops.replace("\\", "/"), *args] + (["--execute"] if execute else [])
        r = subprocess.run([BASH, SCRIPT, *argv], capture_output=True, text=True, env=e, cwd=self.cwd, timeout=300)
        self.assertEqual(os.listdir(self.cwd), [], "the script left files in the current directory")
        self.assertEqual(os.listdir(self.tmpdir), [], "the script left its temp dir behind")
        return r

    # -- log readers -----------------------------------------------------------------------------------------
    def read(self, name):
        with open(os.path.join(self.dir, name), encoding="utf-8") as f:
            return [line for line in f.read().splitlines() if line]

    def events(self):
        return self.read("events")

    def mints(self):
        """(name, scopes) per mint."""
        return [tuple(e.split(" ")[1:3]) for e in self.events() if e.startswith("MINT ")]

    def deletes(self):
        return [e[7:] for e in self.events() if e.startswith("DELETE ")]

    def owner_calls(self):
        """(method, path, body or None) per call the live owner token made."""
        out = []
        for e in self.events():
            if e.startswith("CALL owner "):
                parts = e.split(" ", 4)
                out.append((parts[2], parts[3], json.loads(parts[4]) if len(parts) > 4 else None))
        return out

    def minted_tokens(self):
        p = os.path.join(self.dir, "state.json")
        if not os.path.exists(p):
            return []
        with open(p, encoding="utf-8") as f:
            return list(json.load(f)["minted"].values())

    def assert_no_token_in_argv_or_output(self, r):
        tokens = self.minted_tokens()
        for log in ("argv.curl", "argv.kubectl"):
            for line in self.read(log):
                for t in tokens:
                    self.assertNotIn(t, line, log)
        for t in tokens:
            self.assertNotIn(t, r.stdout + r.stderr)

    def assert_nothing_called(self, r, code=2):
        self.assertEqual(r.returncode, code, r.stdout + r.stderr)
        self.assertEqual(self.read("argv.kubectl"), [], "no kubectl call before the input is valid")
        self.assertEqual(self.read("argv.curl"), [], "no curl call before the input is valid")
        self.assertEqual(self.events(), [])

    # -- the harness guard (2026-10-07: a hand-built PATH let a "read-only" run reach the real tools) -----------
    def test_harness_resolves_every_tool_to_the_fakes(self):
        tools = self.resolved_tools(self.env())
        for t in ("kubectl", "curl"):
            self.assertEqual(os.path.normcase(os.path.normpath(os.path.dirname(tools[t]))),
                             os.path.normcase(os.path.normpath(self.bin)), (t, tools))

    def test_harness_guard_aborts_when_the_fakes_are_not_first(self):
        with self.assertRaises(FakesNotFirst):
            self.assert_fakes_first(self.env(path_first=False))
        if os.name == "nt":
            # the incident's exact mistake, inside bash: a drive-colon dir prepended to the POSIX PATH with ':'
            prelude = 'PATH="%s:$PATH"; ' % self.bin.replace("\\", "/")
            with self.assertRaises(FakesNotFirst):
                self.assert_fakes_first(self.env(path_first=False), prelude)

    # -- dry run is the default: validate and print the plan, call nothing ---------------------------------
    def test_without_execute_it_is_a_dry_run(self):
        r = self.run_script(RENOVATE, "--var", "REPO=relay", execute=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assertIn("PUT /repos/cchifor/relay/collaborators/renovate-bot", r.stdout)
        self.assertIn("owner-op-renovate-onboard-relay-<utc>-<random>, scopes write:repository", r.stdout)
        self.assert_nothing_called(r, code=0)

    def test_explicit_dry_run_flag_is_the_same(self):
        r = self.run_script(RENOVATE, "--var", "REPO=relay", "--dry-run", execute=False)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DRY RUN", r.stdout)
        self.assert_nothing_called(r, code=0)

    def test_dry_run_refuses_bad_input_like_the_real_run(self):
        r = self.run_script(RENOVATE, execute=False)  # no --var REPO
        self.assert_nothing_called(r)
        self.assertIn("${REPO} has no --var REPO=VALUE", r.stderr)

    # -- the shipped presets --------------------------------------------------------------------------------
    def test_presets_plan_exactly_the_recorded_calls(self):
        cases = (
            (RENOVATE, ["--var", "REPO=relay"], "owner-op-renovate-onboard-relay",
             ["1. PUT /repos/cchifor/relay/collaborators/renovate-bot  body renovate-onboard-collaborator.json "
              "{permission}  expect 204",
              "2. PUT /repos/cchifor/relay/topics/renovate  no body  expect 204"]),
            (TEMPLATE, [], "owner-op-repository-template-settings",
             ["1. PATCH /repos/cchifor/repository-template  body repository-template-settings.json {"
              + ", ".join(TEMPLATE_SETTINGS) + "}  expect 200"]),
        )
        for ops, args, name, lines in cases:
            with self.subTest(ops=os.path.basename(ops)):
                r = self.run_script(ops, *args, execute=False)
                self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
                self.assertIn(f"{len(lines)} call(s)", r.stdout)
                self.assertIn(f"{name}-<utc>-<random>, scopes write:repository", r.stdout)
                plan = [m.group(1) for l in r.stdout.splitlines()
                        for m in [re.match(r"\[[^]]*\]\s+(\d+\. .*)$", l)] if m]
                self.assertEqual(plan, lines)
        self.assertEqual(self.read("argv.kubectl"), [])
        self.assertEqual(self.read("argv.curl"), [])

    def test_shipped_preset_files_are_lf_and_hold_the_recorded_settings(self):
        for name in ("renovate-onboard.ops", "renovate-onboard-collaborator.json",
                     "repository-template-settings.ops", "repository-template-settings.json"):
            with open(os.path.join(PRESETS, name), "rb") as f:
                self.assertNotIn(b"\r", f.read(), name)
        with open(os.path.join(PRESETS, "repository-template-settings.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), TEMPLATE_SETTINGS)
        with open(os.path.join(PRESETS, "renovate-onboard-collaborator.json"), encoding="utf-8") as f:
            self.assertEqual(json.load(f), {"permission": "write"})
        with open(TEMPLATE, encoding="utf-8") as f:
            self.assertIn(".gitea/branch-protection.json", f.read(), "point at where the protection is recorded")

    def test_path_rule_is_gitea_api_sh_s(self):
        with open(os.path.join(REPO, "scripts", "gitea-api.sh"), encoding="utf-8") as f:
            api = re.search(r"^PATH_RE='(.*)'$", f.read(), re.M).group(1)
        with open(SCRIPT, encoding="utf-8") as f:
            op = re.search(r"^PATH_RE = r'(.*)'$", f.read(), re.M).group(1)
        self.assertEqual(op, api)

    # -- the owner operation --------------------------------------------------------------------------------
    def test_execute_runs_the_calls_with_one_token_and_deletes_it(self):
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        mints = self.mints()
        self.assertEqual(len(mints), 1)
        name, scopes = mints[0]
        self.assertRegex(name, r"^owner-op-renovate-onboard-relay" + TOKEN_NAME_TAIL + "$")
        self.assertEqual(scopes, "write:repository")
        self.assertEqual(self.owner_calls(), [
            ("PUT", "/repos/cchifor/relay/collaborators/renovate-bot", '{"permission": "write"}'),
            ("PUT", "/repos/cchifor/relay/topics/renovate", None),
        ])
        self.assertEqual(self.deletes(), [name], "the minted token is the one deleted")
        self.assertIn("next use -> HTTP 401", r.stdout)  # the post-revocation proof
        self.assertIn("chifor tokens now: cc-admin-20260913", r.stdout)  # the remaining tokens, by name
        self.assertIn("DONE", r.stdout)
        self.assert_no_token_in_argv_or_output(r)

    def test_the_mint_runs_in_the_gitea_pod_for_the_owner(self):
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        argv = self.read("argv.kubectl")
        self.assertTrue(all(a.startswith("--context admin@ai ") for a in argv), argv)
        mint = [a for a in argv if "generate-access-token" in a]
        self.assertEqual(len(mint), 1)
        self.assertIn("-n gitea exec gitea-86f5679868-586wg -c gitea -- gitea admin user generate-access-token "
                      "--username chifor --token-name owner-op-renovate-onboard-relay-", mint[0])
        self.assertTrue(mint[0].endswith(" --scopes write:repository --raw"), mint[0])
        self.assertTrue(any("-n databases exec -i infra-pg-6 -c postgres -- psql" in a for a in argv), argv)
        lookups = [a for a in argv if " get pod " in a]
        self.assertTrue(lookups and all("--field-selector=status.phase=Running" in a for a in lookups), lookups)

    def test_execute_mints_with_exactly_the_declared_scopes(self):
        ops = self.ops_file("""\
            # scopes: write:repository, write:organization
            GET /orgs/cchifor - 200
            """)
        self.scenario(responses={"GET /orgs/cchifor": [200, {"username": "cchifor"}]})
        r = self.run_script(ops)
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertEqual([s for _, s in self.mints()], ["write:repository,write:organization"])

    def test_token_is_sent_only_on_curl_stdin(self):
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        argv = self.read("argv.curl")
        self.assertEqual(len(argv), 3)  # two calls and the post-revocation proof
        for a in argv:
            self.assertTrue(a.startswith("-q "), a)  # no ~/.curlrc
            self.assertIn("--proto =https", a)
            self.assertIn("--max-redirs 0", a)
            self.assertIn("-H @-", a)
            self.assertIn(ORIGIN + "/", a)
            self.assertNotIn("Authorization", a)
        # the emulator saw the owner token on every call: it can only have come from stdin
        self.assertEqual(len(self.owner_calls()), 2)
        self.assert_no_token_in_argv_or_output(r)

    def test_token_is_used_only_between_mint_and_delete(self):
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        ev = self.events()
        i_mint = next(i for i, e in enumerate(ev) if e.startswith("MINT "))
        i_del = next(i for i, e in enumerate(ev) if e.startswith("DELETE "))
        self.assertLess(i_mint, i_del)
        between = ev[i_mint + 1:i_del]
        self.assertTrue(between and all(e.startswith("CALL owner ") for e in between), between)
        self.assertFalse([e for e in ev[i_del:] if e.startswith("CALL owner ")])
        self.assertTrue(any(e.startswith("CALL revoked GET /user") for e in ev[i_del:]))

    def test_vars_fill_paths_and_bodies_and_name_the_token(self):
        ops = self.ops_file("""\
            # scopes: write:repository
            PATCH /repos/cchifor/${REPO} bodies/desc.json 200,201
            """, bodies={"bodies/desc.json": '{"description": "${DESC}", "website": "${REPO}.example"}\n'})
        self.scenario(responses={"PATCH /repos/cchifor/relay": [201, {}]})
        r = self.run_script(ops, "--var", "REPO=relay", "--var=DESC=a.b_c-1")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        calls = self.owner_calls()
        self.assertEqual([(m, p) for m, p, _ in calls], [("PATCH", "/repos/cchifor/relay")])
        self.assertEqual(json.loads(calls[0][2]), {"description": "a.b_c-1", "website": "relay.example"})
        # the token name carries the values, ordered by variable name (DESC before REPO)
        self.assertRegex(self.mints()[0][0], r"^owner-op-t-a\.b_c-1-relay" + TOKEN_NAME_TAIL + "$")

    def test_stops_at_the_first_unexpected_status_and_still_revokes(self):
        ops = self.ops_file("""\
            # scopes: write:repository
            PUT /repos/cchifor/a/topics/one - 204
            PUT /repos/cchifor/a/topics/two - 204
            PUT /repos/cchifor/a/topics/three - 204
            """)
        long_body = "X" * 300 + "TAIL-NEVER-PRINTED"
        self.scenario(responses={"PUT /repos/cchifor/a/topics/one": [204, ""],
                                 "PUT /repos/cchifor/a/topics/two": [403, long_body],
                                 "PUT /repos/cchifor/a/topics/three": [204, ""]})
        r = self.run_script(ops)
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual([p for _, p, _ in self.owner_calls()],
                         ["/repos/cchifor/a/topics/one", "/repos/cchifor/a/topics/two"], "the third is never made")
        out = r.stdout + r.stderr
        self.assertIn("2/3 PUT /repos/cchifor/a/topics/two -> HTTP 403, expected 204: " + "X" * 300, out)
        self.assertNotIn("TAIL-NEVER-PRINTED", out, "only the first 300 bytes of the body")
        self.assertIn("calls 3..3 not made", out)
        self.assertEqual(len(self.mints()), 1)
        self.assertEqual(self.deletes(), [self.mints()[0][0]])
        self.assertIn("next use -> HTTP 401", r.stdout)
        self.assert_no_token_in_argv_or_output(r)

    def test_a_response_that_echoes_the_token_is_redacted(self):
        self.scenario(responses={"PUT /repos/cchifor/relay/collaborators/renovate-bot": [400, "ECHO_TOKEN"]})
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("<owner token>", r.stderr)
        self.assertEqual(len(self.deletes()), 1)
        self.assert_no_token_in_argv_or_output(r)

    def test_a_transport_failure_stops_and_revokes(self):
        self.scenario(responses={"PUT /repos/cchifor/relay/collaborators/renovate-bot": ["EXIT7", ""]})
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("HTTP 000", r.stderr)
        self.assertEqual(len(self.mints()), 1)
        self.assertEqual(len(self.deletes()), 1)
        self.assertEqual([p for _, p, _ in self.owner_calls()], ["/repos/cchifor/relay/collaborators/renovate-bot"],
                         "the topic call is never made")
        self.assert_no_token_in_argv_or_output(r)

    def test_every_curl_call_is_bounded(self):
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        argv = self.read("argv.curl")
        self.assertEqual(len(argv), 3)  # two calls and the post-revocation proof
        for a in argv:
            self.assertIn("--connect-timeout 10", a)
            self.assertIn("--max-time 60", a)

    def test_a_stalled_call_ends_at_its_bound_and_the_token_is_deleted(self):
        # the server accepts the connection and never answers: the call must end at its deadline (curl exit 28)
        # instead of holding the live token, and the trap must then delete it
        self.scenario(responses={"PUT /repos/cchifor/relay/collaborators/renovate-bot": ["STALL", ""]})
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertFalse([e for e in self.events() if e.startswith("UNBOUNDED ")], "a call without --max-time")
        self.assertIn("HTTP 000", r.stderr)
        self.assertEqual([p for _, p, _ in self.owner_calls()], ["/repos/cchifor/relay/collaborators/renovate-bot"])
        self.assertEqual(self.deletes(), [self.mints()[0][0]])
        self.assertIn("next use -> HTTP 401", r.stdout)
        self.assert_no_token_in_argv_or_output(r)

    def test_two_runs_in_the_same_second_never_share_a_token_name(self):
        # A frozen clock: both runs read the same second, so only the random part keeps the names apart. With a
        # shared name, one run's delete-by-name would revoke the other run's live token.
        p = os.path.join(self.bin, "date")
        with open(p, "w", encoding="utf-8", newline="\n") as f:
            f.write('#!/usr/bin/env bash\ncase "$*" in *%Y%m%d%H%M%S*) echo 20261108120000;; *) echo 12:00:00Z;; esac\n')
        os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        for _ in range(2):
            r = self.run_script(RENOVATE, "--var", "REPO=relay")
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        names = [n for n, _ in self.mints()]
        self.assertEqual(len(names), 2)
        for n in names:
            self.assertRegex(n, r"^owner-op-renovate-onboard-relay-20261108120000-[0-9a-f]{8}$")
        self.assertNotEqual(names[0], names[1])
        self.assertEqual(self.deletes(), names, "each run deletes its own token, and only it")

    def test_revocation_follows_a_primary_failover(self):
        # the primary moves between the mint and the revocation (the old one now refuses writes): the delete and
        # the listing must go to the new primary, so the run still confirms the revocation
        self.scenario(failover=True)
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
        self.assertIn("DELETE-ON infra-pg-7", self.events())
        self.assertEqual(self.deletes(), [self.mints()[0][0]])
        self.assertIn("chifor tokens now: cc-admin-20260913", r.stdout)

    def test_a_failed_mint_is_deleted_by_name_and_nothing_is_called(self):
        self.scenario(mint_fail=True)
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertEqual(len(self.mints()), 1)
        self.assertEqual(self.deletes(), [self.mints()[0][0]], "a half-made token is deleted by name")
        self.assertEqual(self.owner_calls(), [])
        self.assertEqual(self.read("argv.curl"), [], "no value, so no call and no 401 probe")

    def test_missing_pods_mint_nothing(self):
        self.scenario(pods_missing=True)
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 4, r.stdout + r.stderr)
        self.assertIn("nothing minted", r.stderr)
        self.assertEqual(self.mints(), [])
        self.assertEqual(self.deletes(), [])
        self.assertEqual(self.read("argv.curl"), [])

    def test_unconfirmed_revocation_exits_5_and_prints_the_delete(self):
        for responses, label in ((RENOVATE_OK, "after success"),
                                 ({"PUT /repos/cchifor/relay/collaborators/renovate-bot": [500, "boom"]},
                                  "after a failed call")):
            with self.subTest(label):
                self.tearDown()
                self.setUp()
                self.scenario(revocation_ineffective=True, responses=responses)
                r = self.run_script(RENOVATE, "--var", "REPO=relay")
                self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
                self.assertIn("REVOCATION NOT CONFIRMED", r.stderr)
                name = self.mints()[0][0]
                sql = ("delete from access_token where uid=(select id from \"user\" where name='chifor') "
                       f"and name='{name}';")
                self.assertIn("\n  " + sql + "\n", r.stderr, "the exact psql delete to run")
                self.assertIn("SQL " + sql, self.events(), "it is the very statement the trap ran")
                self.assert_no_token_in_argv_or_output(r)

    def test_a_token_that_still_works_after_the_delete_is_not_confirmed(self):
        # the database no longer lists it, but its next use does not answer 401: the 401 probe alone catches it
        self.scenario(api_still_accepts=True,
                      responses=dict(RENOVATE_OK, **{"GET /user": [200, {"login": "chifor"}]}))
        r = self.run_script(RENOVATE, "--var", "REPO=relay")
        self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
        self.assertIn("its next use answered HTTP 200", r.stderr)
        self.assertNotIn("the database still lists it", r.stderr)
        self.assert_no_token_in_argv_or_output(r)

    def test_a_token_the_database_still_lists_or_cannot_list_is_not_confirmed(self):
        # its next use answers 401, but the listing shows it (or fails): the database check alone catches it
        for flag in ("stale_listing", "list_fails"):
            with self.subTest(flag):
                self.tearDown()
                self.setUp()
                self.scenario(**{flag: True})
                r = self.run_script(RENOVATE, "--var", "REPO=relay")
                self.assertEqual(r.returncode, 5, r.stdout + r.stderr)
                self.assertIn("next use -> HTTP 401", r.stdout)
                self.assertIn("the database still lists it (or could not be read)", r.stderr)
                self.assertIn("delete from access_token where", r.stderr)

    # -- bad input: exit 2 before any kubectl or curl call --------------------------------------------------
    def test_bad_input_exits_2_before_any_call(self):
        ok_scopes = "# scopes: write:repository\n"
        cases = [
            # (label, ops text, bodies, args, expected message)
            ("unknown method", ok_scopes + "FETCH /repos/cchifor/x - 200\n", {}, [], "method 'FETCH'"),
            ("lower-case method", ok_scopes + "put /repos/cchifor/x - 200\n", {}, [], "method 'put'"),
            ("path with //", ok_scopes + "GET /repos//cchifor/x - 200\n", {}, [], "is not an API path"),
            ("path with ..", ok_scopes + "GET /repos/cchifor/../admin - 200\n", {}, [], "is not an API path"),
            ("path with a host", ok_scopes + "GET https://evil.example/api/v1/user - 200\n", {}, [],
             "is not an API path"),
            ("protocol-relative path", ok_scopes + "GET //evil.example/user - 200\n", {}, [], "is not an API path"),
            ("relative path", ok_scopes + "GET repos/cchifor/x - 200\n", {}, [], "is not an API path"),
            ("shell in path", ok_scopes + "GET /repos/cchifor/x;id - 200\n", {}, [], "is not an API path"),
            (".. after substitution", ok_scopes + "GET /repos/cchifor/${X}/x - 200\n", {}, ["--var", "X=.."],
             "is not an API path"),
            ("unreplaced placeholder in path", ok_scopes + "GET /repos/cchifor/${X} - 200\n", {}, [],
             "${X} has no --var X=VALUE"),
            ("unreplaced placeholder in body", ok_scopes + "PATCH /repos/cchifor/x b.json 200\n",
             {"b.json": '{"description": "${X}"}'}, [], "${X} has no --var X=VALUE"),
            ("malformed placeholder", ok_scopes + "GET /repos/cchifor/${1} - 200\n", {}, [], "malformed placeholder"),
            ("var value with a slash", ok_scopes + "GET /repos/cchifor/${X} - 200\n", {}, ["--var", "X=a/b"],
             "--var X: the value must match"),
            ("var value with a space", ok_scopes + "GET /repos/cchifor/${X} - 200\n", {}, ["--var", "X=a b"],
             "--var X: the value must match"),
            ("var value with shell", ok_scopes + "GET /repos/cchifor/${X} - 200\n", {}, ["--var", "X=$(id)"],
             "--var X: the value must match"),
            ("var value with a quote", ok_scopes + "PATCH /repos/cchifor/x b.json 200\n",
             {"b.json": '{"description": "${X}"}'}, ["--var", 'X=a"b'], "--var X: the value must match"),
            ("empty var value", ok_scopes + "GET /repos/cchifor/${X} - 200\n", {}, ["--var", "X="],
             "--var X: the value must match"),
            ("var without =", ok_scopes + "GET /repos/cchifor/x - 200\n", {}, ["--var", "X"], "give NAME=VALUE"),
            ("bad var name", ok_scopes + "GET /repos/cchifor/x - 200\n", {}, ["--var", "1X=a"], "give NAME=VALUE"),
            ("var given twice", ok_scopes + "GET /repos/cchifor/${X} - 200\n", {}, ["--var", "X=a", "--var", "X=b"],
             "--var X is given twice"),
            ("unused var", ok_scopes + "GET /repos/cchifor/x - 200\n", {}, ["--var", "X=a"],
             "--var X is not used by t.ops"),
            ("no scopes line", "GET /repos/cchifor/x - 200\n", {}, [], "no '# scopes: ...' line"),
            ("scopes line after an operation", "GET /repos/cchifor/x - 200\n" + ok_scopes, {}, [],
             "the scopes line must come before the first operation"),
            ("two scopes lines", ok_scopes + ok_scopes + "GET /repos/cchifor/x - 200\n", {}, [],
             "a second scopes line"),
            ("empty scopes", "# scopes:\nGET /repos/cchifor/x - 200\n", {}, [], "unknown scope ''"),
            ("unknown scope", "# scopes: write:everything\nGET /repos/cchifor/x - 200\n", {}, [],
             "unknown scope 'write:everything'"),
            ("scope all", "# scopes: all\nGET /repos/cchifor/x - 200\n", {}, [], "scope 'all' is refused"),
            ("duplicate scope", "# scopes: write:repository,write:repository\nGET /repos/cchifor/x - 200\n", {}, [],
             "listed twice"),
            ("read next to write", "# scopes: read:repository,write:repository\nGET /repos/cchifor/x - 200\n", {},
             [], "read:repository is redundant next to write:repository"),
            ("invalid JSON body", ok_scopes + "PATCH /repos/cchifor/x b.json 200\n", {"b.json": '{"a": }'}, [],
             "is not valid JSON"),
            ("NaN in body", ok_scopes + "PATCH /repos/cchifor/x b.json 200\n", {"b.json": '{"a": NaN}'}, [],
             "is not valid JSON"),
            ("JSON broken by substitution", ok_scopes + "PATCH /repos/cchifor/x b.json 200\n",
             {"b.json": '{"a": ${X}}'}, ["--var", "X=abc"], "is not valid JSON after the substitution"),
            ("missing body file", ok_scopes + "PATCH /repos/cchifor/x nope.json 200\n", {}, [],
             "cannot read body file nope.json"),
            ("body outside the ops dir", ok_scopes + "PATCH /repos/cchifor/x ../b.json 200\n", {}, [],
             "must be a path relative to the ops file's directory"),
            ("absolute body path", ok_scopes + "PATCH /repos/cchifor/x /etc/passwd 200\n", {}, [],
             "must be a path relative to the ops file's directory"),
            ("bad expected code", ok_scopes + "GET /repos/cchifor/x - 20x\n", {}, [], "expected codes '20x'"),
            ("out-of-range expected code", ok_scopes + "GET /repos/cchifor/x - 600\n", {}, [],
             "expected codes '600'"),
            ("expected codes not comma separated", ok_scopes + "GET /repos/cchifor/x - 200;201\n", {}, [],
             "expected codes '200;201'"),
            ("too few fields", ok_scopes + "GET /repos/cchifor/x 200\n", {}, [], "got 3 field(s)"),
            ("trailing comment", ok_scopes + "GET /repos/cchifor/x - 200 # why\n", {}, [], "got 6 field(s)"),
            ("no operations", ok_scopes, {}, [], "no operations"),
        ]
        for label, text, bodies, args, message in cases:
            with self.subTest(label):
                for f in ("argv.curl", "argv.kubectl", "events"):  # one failing case must not fail the rest
                    open(os.path.join(self.dir, f), "w").close()
                ops = self.ops_file(text, bodies=bodies)
                r = self.run_script(ops, *args)  # with --execute: refused all the same
                self.assert_nothing_called(r)
                self.assertIn(message, r.stderr)
                self.assertIn("nothing called", r.stderr)
                for rel in bodies:
                    os.remove(os.path.join(self.ops, *rel.split("/")))

    def test_bad_arguments_exit_2_before_any_call(self):
        ops = self.ops_file("# scopes: write:repository\nGET /repos/cchifor/x - 200\n")
        for args in ((ops, "--dry-run"),            # --execute is appended: the two exclude each other
                     (ops, "--force"),
                     (ops, ops),
                     (os.path.join(self.ops, "missing.ops"),)):
            with self.subTest(args=args[1:] or args):
                r = self.run_script(*args)
                self.assert_nothing_called(r)
        # --var without a value: the PARSER refuses it, last on the line or followed by another option (which it
        # must not swallow as the value). Built by hand, without the appended --execute, so this is that branch.
        for args in ((ops, "--var"), (ops, "--var", "--execute"), (ops, "--var", "--dry-run")):
            with self.subTest(args=args[1:]):
                for f in ("argv.curl", "argv.kubectl", "events"):
                    open(os.path.join(self.dir, f), "w").close()
                r = self.run_script(*args, execute=False)
                self.assert_nothing_called(r)
                self.assertIn("--var needs NAME=VALUE", r.stderr)
        r = subprocess.run([BASH, SCRIPT, "--execute"], capture_output=True, text=True, env=self.env(),
                           cwd=self.cwd, timeout=60)
        self.assertEqual(r.returncode, 2, r.stdout + r.stderr)
        self.assertIn("usage:", r.stderr)
        self.assertEqual(self.read("argv.kubectl"), [])

    def test_an_ops_file_name_that_cannot_name_a_token_is_refused(self):
        ops = self.ops_file("# scopes: write:repository\nGET /repos/cchifor/x - 200\n", name="bad name.ops")
        r = self.run_script(ops)
        self.assert_nothing_called(r)
        self.assertIn("it names the token", r.stderr)


if __name__ == "__main__":
    unittest.main()

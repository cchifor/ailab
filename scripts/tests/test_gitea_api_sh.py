"""scripts/gitea-api.sh: the workstation's routine Gitea API helper (auth-hardening plan B3).

Runs the script against a fake `git` (credential helper) and a fake `curl` that log their argv and stdin, so the
tests can prove the token travels only on curl's stdin, the identity guard refuses anything but a non-admin
workstation-bot, and bad input never reaches the network.
"""
import os
import shutil
import stat
import subprocess
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "gitea-api.sh")
BASH = shutil.which("bash")
TOKEN = "fake0token0value0never0in0argv0000000000"

FAKE_GIT = textwrap.dedent(f"""\
    #!/usr/bin/env bash
    if [ "$1" = credential ] && [ "$2" = fill ]; then
      cat >/dev/null
      printf 'GIT_TERMINAL_PROMPT=%s GCM_INTERACTIVE=%s\\n' "$GIT_TERMINAL_PROMPT" "$GCM_INTERACTIVE" >> "$FAKE_LOG/gitenv"
      case "${{FAKE_GIT_MODE:-ok}}" in
        fail) echo "fatal: could not read Username" >&2; exit 128;;
        nopass) printf 'protocol=https\\nhost=git.chifor.me\\nusername=workstation-bot\\n'; exit 0;;
      esac
      printf 'protocol=https\\nhost=git.chifor.me\\nusername=workstation-bot\\npassword={TOKEN}\\n'
      exit 0
    fi
    exit 1
    """)

# Emulates the curl options the script uses: -o FILE, -w '%{{http_code}}', -X, -H @- (headers on stdin), the URL.
FAKE_CURL = textwrap.dedent("""\
    #!/usr/bin/env bash
    printf '%s\\n' "$*" >> "$FAKE_LOG/argv"
    if [ -n "${FAKE_CURL_EXIT:-}" ]; then echo "curl: (7) Failed to connect" >&2; exit "$FAKE_CURL_EXIT"; fi
    out=""; url=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -o) out=$2; shift 2;;
        https://*|http://*) url=$1; shift;;
        *) shift;;
      esac
    done
    cat >> "$FAKE_LOG/stdin"
    case "$url" in
      */api/v1/user) body=$FAKE_USER_JSON; code=${FAKE_USER_CODE:-200};;
      *) body=${FAKE_BODY:-'{"ok":true}'}; code=${FAKE_CODE:-200};;
    esac
    printf '%s' "$body" > "$out"
    printf '%s' "$code"
    """)


@unittest.skipIf(BASH is None, "bash not available")
class GiteaApiShTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        self.log = os.path.join(self.tmp, "log")
        os.makedirs(self.bin)
        os.makedirs(self.log)
        for name, src in (("git", FAKE_GIT), ("curl", FAKE_CURL)):
            p = os.path.join(self.bin, name)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(src)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        for f in ("argv", "stdin", "gitenv"):
            open(os.path.join(self.log, f), "w").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args, user_json='{"login":"workstation-bot","is_admin":false}', **env):
        e = dict(os.environ)
        e["PATH"] = self.bin + os.pathsep + e["PATH"]
        e["FAKE_LOG"] = self.log
        e["FAKE_USER_JSON"] = user_json
        e.update(env)
        return subprocess.run([BASH, SCRIPT, *args], capture_output=True, text=True, env=e, timeout=30)

    def calls(self):
        with open(os.path.join(self.log, "argv"), encoding="utf-8") as f:
            return [line for line in f.read().splitlines() if line]

    def stdin(self):
        with open(os.path.join(self.log, "stdin"), encoding="utf-8") as f:
            return f.read()

    def test_valid_call_prints_status_and_body(self):
        r = self.run_script("GET", "/repos/cchifor/ailab", FAKE_BODY='{"name":"ailab"}')
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("HTTP 200", r.stdout)
        self.assertIn('{"name":"ailab"}', r.stdout)
        self.assertTrue(self.calls()[-1].endswith("https://git.chifor.me/api/v1/repos/cchifor/ailab"))

    def test_token_only_on_stdin_never_in_argv(self):
        r = self.run_script("GET", "/repos/cchifor/ailab")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertGreaterEqual(len(self.calls()), 2)  # the identity check, then the call
        for argv in self.calls():
            self.assertNotIn(TOKEN, argv)
        self.assertIn(f"Authorization: token {TOKEN}", self.stdin())
        self.assertNotIn(TOKEN, r.stdout + r.stderr)

    def test_refuses_admin_identity(self):
        r = self.run_script("GET", "/repos/cchifor/ailab", user_json='{"login":"chifor","is_admin":true}')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(self.calls()), 1)  # only the identity check ran

    def test_refuses_admin_flag_even_for_the_bot(self):
        r = self.run_script("GET", "/repos/cchifor/ailab", user_json='{"login":"workstation-bot","is_admin":true}')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_refuses_other_non_admin_user(self):
        r = self.run_script("GET", "/repos/cchifor/ailab", user_json='{"login":"cchifor","is_admin":false}')
        self.assertNotEqual(r.returncode, 0)
        self.assertEqual(len(self.calls()), 1)

    def test_bad_path_never_reaches_curl(self):
        for bad in ("repos/x", "//evil.example/x", "https://evil.example/", "/repos/x;rm", "/repos/x y", "/a?b=$(id)"):
            r = self.run_script("GET", bad)
            self.assertEqual(r.returncode, 2, bad)
        self.assertEqual(self.calls(), [])

    def test_bad_method_never_reaches_curl(self):
        r = self.run_script("TRACE", "/user")
        self.assertEqual(r.returncode, 2)
        self.assertEqual(self.calls(), [])

    def test_body_file_is_sent_as_json(self):
        body = os.path.join(self.tmp, "body.json")
        with open(body, "w", encoding="utf-8") as f:
            f.write('{"title":"t"}')
        r = self.run_script("POST", "/repos/cchifor/ailab/issues", body, FAKE_CODE="201")
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("HTTP 201", r.stdout)
        last = self.calls()[-1]
        self.assertIn("-X POST", last)
        self.assertIn("--data-binary @" + body, last)
        self.assertIn("Content-Type: application/json", last)

    def test_http_error_exits_nonzero_and_prints_body(self):
        r = self.run_script("GET", "/repos/cchifor/nope", FAKE_CODE="404", FAKE_BODY='{"message":"not found"}')
        self.assertEqual(r.returncode, 1)
        self.assertIn("HTTP 404", r.stdout)
        self.assertIn("not found", r.stdout)

    def test_curl_ignores_implicit_config(self):
        # -q must be curl's FIRST argument, or a ~/.curlrc can add -v/--trace (leaking the header) or extra URLs.
        r = self.run_script("GET", "/user")
        self.assertEqual(r.returncode, 0, r.stderr)
        for argv in self.calls():
            self.assertTrue(argv.startswith("-q "), argv)

    def test_failed_credential_lookup_exits_3_without_calling_curl(self):
        r = self.run_script("GET", "/user", FAKE_GIT_MODE="fail")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertIn("no credential", r.stderr)
        self.assertEqual(self.calls(), [])

    def test_lookup_without_password_exits_3_without_calling_curl(self):
        r = self.run_script("GET", "/user", FAKE_GIT_MODE="nopass")
        self.assertEqual(r.returncode, 3, r.stderr)
        self.assertEqual(self.calls(), [])

    def test_credential_lookup_never_prompts(self):
        self.run_script("GET", "/user")
        with open(os.path.join(self.log, "gitenv"), encoding="utf-8") as f:
            self.assertIn("GIT_TERMINAL_PROMPT=0 GCM_INTERACTIVE=never", f.read())

    def test_guard_tolerates_whitespace_and_key_order(self):
        r = self.run_script("GET", "/repos/cchifor/ailab",
                            user_json='{"id": 90, "is_admin": false, "login": "workstation-bot"}')
        self.assertEqual(r.returncode, 0, r.stderr)

    def test_guard_does_not_match_a_longer_login(self):
        r = self.run_script("GET", "/repos/cchifor/ailab",
                            user_json='{"login": "workstation-bot-evil", "is_admin": false}')
        self.assertEqual(r.returncode, 4, r.stderr)

    def test_guard_reads_only_the_top_level_fields(self):
        # An admin's /user with the bot's fields nested deeper (or in a crafted text field) must be refused:
        # the guard parses the JSON and checks the top-level login and is_admin only.
        for user_json in (
            '{"login":"chifor","is_admin":true,"x":{"login":"workstation-bot","is_admin":false}}',
            '{"login":"chifor","is_admin":true,"description":"\\"login\\":\\"workstation-bot\\",\\"is_admin\\":false"}',
        ):
            r = self.run_script("GET", "/repos/cchifor/ailab", user_json=user_json)
            self.assertEqual(r.returncode, 4, user_json)
        # only identity checks were sent, never the requested call
        self.assertTrue(all(c.endswith("/api/v1/user") for c in self.calls()), self.calls())

    def test_guard_refuses_invalid_json_and_non_boolean_admin(self):
        for user_json in ('not json', '{"login":"workstation-bot","is_admin":"false"}', '["workstation-bot"]'):
            r = self.run_script("GET", "/repos/cchifor/ailab", user_json=user_json)
            self.assertEqual(r.returncode, 4, user_json)

    def test_transport_failure_has_its_own_exit_code(self):
        # curl's own codes (1-4 included) must not leak out as the script's documented exit codes.
        r = self.run_script("GET", "/user", FAKE_CURL_EXIT="7")
        self.assertEqual(r.returncode, 5, r.stderr)
        self.assertIn("gitea-api: curl transport failure", r.stderr)

    def test_fixed_origin_and_no_redirects(self):
        r = self.run_script("GET", "/user")
        self.assertEqual(r.returncode, 0, r.stderr)
        for argv in self.calls():
            self.assertIn("--proto =https", argv)
            self.assertIn("--max-redirs 0", argv)
            self.assertIn("https://git.chifor.me/api/v1/", argv)


if __name__ == "__main__":
    unittest.main()

"""scripts/gitea-api.sh: the workstation's routine Gitea API helper (auth-hardening plan B3).

Runs the script against a fake `git` (credential helper) and a fake `curl` that log their argv and stdin, so the
tests can prove the token travels only on curl's stdin, the identity guard refuses anything but a non-admin
workstation-bot, and bad input never reaches the network.
"""
import json
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
    out=""; url=""; data=""
    while [ $# -gt 0 ]; do
      case "$1" in
        -o) out=$2; shift 2;;
        --data-binary) data=${2#@}; shift 2;;
        https://*|http://*) url=$1; shift;;
        *) shift;;
      esac
    done
    cat >> "$FAKE_LOG/stdin"
    if [ -n "$data" ]; then cat "$data" > "$FAKE_LOG/data"; fi
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
        e.pop("AILAB_RUN_ID", None)  # a session's own id must not leak into the fixtures
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

    # ---- run marker (worker-identities plan, Phase 1 item 4) ----
    RUN = "0b6c8f3e-2f4d-4a51-9a7e-3f1d2c4b5a69"
    MARK = "\n<!-- worker: workstation run: 0b6c8f3e-2f4d-4a51-9a7e-3f1d2c4b5a69 -->"
    COMMENTS = "/repos/cchifor/ailab/issues/12/comments"

    def send(self, method, path, raw, **kw):
        """Run the script with `raw` as the body file; return (result, the bytes curl was given)."""
        body = os.path.join(self.tmp, "body.json")
        with open(body, "wb") as f:
            f.write(raw)
        open(os.path.join(self.log, "data"), "wb").close()
        r = self.run_script(method, path, body, **kw)
        with open(os.path.join(self.log, "data"), "rb") as f:
            return r, f.read()

    def test_marker_on_every_comment_and_review_write(self):
        raw = json.dumps({"body": "LGTM, one nit.", "event": "COMMENT", "commit_id": "a" * 40,
                          "comments": [{"path": "x", "body": "inline"}]}).encode()
        for method, path in (("POST", self.COMMENTS),
                             ("POST", self.COMMENTS + "?x=1"),
                             ("PATCH", "/repos/cchifor/ailab/issues/comments/345"),
                             ("POST", "/repos/cchifor/ailab/pulls/12/reviews"),
                             ("POST", "/repos/cchifor/ailab/pulls/12/reviews/678")):
            with self.subTest(method=method, path=path):
                r, sent = self.send(method, path, raw, AILAB_RUN_ID=self.RUN)
                self.assertEqual(r.returncode, 0, r.stderr)
                got, want = json.loads(sent), json.loads(raw)
                self.assertEqual(got.pop("body"), want.pop("body") + self.MARK)
                self.assertEqual(got, want, "only the top-level body changes")

    def test_no_run_id_means_no_marker_and_identical_bytes(self):
        raw = b'{ "body" : "LGTM \\u00e9",\n  "z": 1, "a": [1,2] }\n'
        for env in ({}, {"AILAB_RUN_ID": ""}):
            with self.subTest(env=env):
                r, sent = self.send("POST", self.COMMENTS, raw, **env)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(sent, raw)

    def test_every_other_endpoint_is_byte_identical(self):
        raw = b'{"body": "PR description",  "title": "t"}'
        for method, path in (("POST", "/repos/cchifor/ailab/issues"),
                             ("PATCH", "/repos/cchifor/ailab/issues/12"),
                             ("POST", "/repos/cchifor/ailab/pulls"),
                             ("PATCH", "/repos/cchifor/ailab/pulls/12"),
                             ("PUT", self.COMMENTS),
                             ("POST", "/repos/cchifor/ailab/issues/12/labels"),
                             ("POST", "/repos/cchifor/ailab/pulls/12/merge"),
                             ("POST", "/repos/cchifor/ailab/pulls/12/reviews/678/comments"),
                             ("POST", self.COMMENTS + "/extra"),
                             ("POST", "/repos/cchifor/ailab/x/issues/12/comments")):
            with self.subTest(method=method, path=path):
                r, sent = self.send(method, path, raw, AILAB_RUN_ID=self.RUN)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(sent, raw)

    def test_a_command_body_is_never_touched(self):
        # The owner-ack gate counts approve-pin only when it is the WHOLE body; a marker line would
        # silently void the acknowledgement.
        for text in ("approve-pin 0123abc", "approve-pin " + "f" * 40, "approve-pin 0123ABC\n"):
            with self.subTest(text=text):
                raw = json.dumps({"body": text}).encode()
                r, sent = self.send("POST", "/repos/cchifor/workbricks/issues/7/comments", raw,
                                    AILAB_RUN_ID=self.RUN)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(sent, raw)
        # Not a command: prose after the pin, a second line, or a too-short sha is a plain comment.
        for text in ("approve-pin 0123abc please", "approve-pin 0123abc\nthanks", "approve-pin 012"):
            with self.subTest(text=text):
                r, sent = self.send("POST", self.COMMENTS, json.dumps({"body": text}).encode(),
                                    AILAB_RUN_ID=self.RUN)
                self.assertEqual(json.loads(sent)["body"], text + self.MARK)

    def test_marking_is_idempotent(self):
        _, once = self.send("POST", self.COMMENTS, json.dumps({"body": "done"}).encode(),
                            AILAB_RUN_ID=self.RUN)
        _, twice = self.send("POST", self.COMMENTS, once, AILAB_RUN_ID="another-run")
        self.assertEqual(twice, once)
        self.assertEqual(1, json.loads(twice)["body"].count("<!-- worker:"))
        # A dev-worker agent's own final marker counts as one too.
        dw = json.dumps({"body": "x\n<!-- worker: dev-worker-1 agent: claude run: abc -->\n"}).encode()
        _, sent = self.send("POST", self.COMMENTS, dw, AILAB_RUN_ID=self.RUN)
        self.assertEqual(sent, dw)

    def test_a_marker_anywhere_in_the_body_is_never_doubled(self):
        # An edited comment keeps its original marker with new prose after it; a marker can also sit
        # inline. Either way the body already names a run, and a second marker could name another.
        for body in ("first\n<!-- worker: workstation run: old-run -->\nedited: one more thing",
                     "see <!-- worker: workstation run: old-run --> above, and more"):
            with self.subTest(body=body):
                raw = json.dumps({"body": body}).encode()
                r, sent = self.send("PATCH", "/repos/cchifor/ailab/issues/comments/345", raw,
                                    AILAB_RUN_ID=self.RUN)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(sent, raw)

    def test_a_payload_without_a_string_body_is_byte_identical(self):
        for raw in (b'{"event": "APPROVED", "commit_id": "abc"}', b'{"body": null}', b'{"body": 5}'):
            with self.subTest(raw=raw):
                r, sent = self.send("POST", "/repos/cchifor/ailab/pulls/12/reviews", raw,
                                    AILAB_RUN_ID=self.RUN)
                self.assertEqual(r.returncode, 0, r.stderr)
                self.assertEqual(sent, raw)

    def test_a_bad_run_id_or_payload_never_reaches_curl(self):
        good = json.dumps({"body": "x"}).encode()
        for run_id, raw in (("x --> <b>", good), ("a" * 129, good), ("run id", good),
                            (self.RUN, b"not json"), (self.RUN, b'["body"]'), (self.RUN, b'"body"')):
            with self.subTest(run_id=run_id[:20], raw=raw):
                r, _ = self.send("POST", self.COMMENTS, raw, AILAB_RUN_ID=run_id)
                self.assertEqual(r.returncode, 2, r.stderr)
        self.assertEqual(self.calls(), [])

    def test_the_identity_guard_and_the_token_rules_are_unchanged(self):
        raw = json.dumps({"body": "x"}).encode()
        r, sent = self.send("POST", self.COMMENTS, raw, AILAB_RUN_ID=self.RUN,
                            user_json='{"login":"chifor","is_admin":true}')
        self.assertEqual(r.returncode, 4, r.stderr)
        self.assertEqual(len(self.calls()), 1)  # only the identity check ran
        self.assertEqual(sent, b"")
        r, sent = self.send("POST", self.COMMENTS, raw, AILAB_RUN_ID=self.RUN)
        self.assertEqual(r.returncode, 0, r.stderr)
        for argv in self.calls():
            self.assertNotIn(TOKEN, argv)
            self.assertTrue(argv.startswith("-q "), argv)
        self.assertNotIn(TOKEN, sent.decode())

    def test_the_marked_copy_is_removed(self):
        r, _ = self.send("POST", self.COMMENTS, json.dumps({"body": "x"}).encode(),
                         AILAB_RUN_ID=self.RUN)
        self.assertEqual(r.returncode, 0, r.stderr)
        copy = self.calls()[-1].split("--data-binary @", 1)[1].split(" ", 1)[0]
        self.assertNotEqual(copy, os.path.join(self.tmp, "body.json"))
        gone = subprocess.run([BASH, "-c", 'test ! -e "$1"', "_", copy], timeout=30)
        self.assertEqual(gone.returncode, 0, copy)


if __name__ == "__main__":
    unittest.main()

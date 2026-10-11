"""scripts/openbao-revoke-slot-holder.sh: cut a re-slotted dev-worker slot's previous machine out of OpenBao.

Runs the script against a fake `bao` and `kubectl` on PATH. Proves the sweep spares exactly the replacement
(its SecretID accessor and the tokens tagged with its holder), revokes the previous holder's tokens, and fails
closed: a denied, sealed or unreachable vault, a failed destroy or revoke, or a missing replacement accessor
exits 1, and a failed enumeration never reads as "nothing to revoke".
"""
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import textwrap
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SCRIPT = os.path.join(REPO, "scripts", "openbao-revoke-slot-holder.sh")
BASH = shutil.which("bash")

FAKE_BAO = textwrap.dedent(r"""
    #!/usr/bin/env bash
    printf '%s\n' "$*" >> "$FAKE_LOG/calls"
    [ "$BAO_TOKEN" = roottok ] || { echo "missing client token" >&2; exit 2; }
    case "$1 $2 $3" in
      "list -format=json auth/approle/role/dev-worker-3/secret-id")
        case "${SID_MODE:-ok}" in
          ok) echo '["keepacc","oldacc1","oldacc2"]';;
          empty) echo '{}'; exit 2;;
          denied) echo '{"errors":["1 error occurred:\n\t* permission denied\n\n"]}'; exit 2;;
          garbage) echo 'WARNING! something odd'; exit 0;;
        esac;;
      "list -format=json auth/token/accessors")
        case "${ACC_MODE:-ok}" in
          ok) echo '["t-old","t-new","t-other","t-gone","t-nometa"]';;
          down) echo 'Get "https://127.0.0.1:8200/v1/auth/token/accessors": dial tcp: connection refused'; exit 2;;
          sealed) echo '{"errors":["Vault is sealed"]}'; exit 2;;
        esac;;
      "write auth/approle/role/dev-worker-3/secret-id-accessor/destroy "*)
        [ "${DESTROY_MODE:-ok}" = ok ] || { echo "Code: 500. internal error"; exit 2; };;
      "token lookup -format=json")
        case "$5" in
          t-old) echo '{"data":{"meta":{"role_name":"dev-worker-3"}}}';;
          t-new) echo '{"data":{"meta":{"role_name":"dev-worker-3","holder":"vm4207"}}}';;
          t-other) echo '{"data":{"meta":{"role_name":"dev-worker-1"}}}';;
          t-nometa) [ "${LOOKUP_MODE:-ok}" = denied ] && { echo "Code: 403. permission denied"; exit 2; }
                    echo '{"data":{"meta":null}}';;
          t-gone) echo 'Error looking up token: Code: 400. Errors: * invalid accessor'; exit 2;;
        esac;;
      "token revoke -accessor")
        [ "${REVOKE_MODE:-ok}" = ok ] || { echo "Code: 503. Vault is sealed"; exit 2; };;
      *) echo "unexpected bao call: $*" >&2; exit 9;;
    esac
    """).lstrip()
FAKE_KUBECTL = "#!/usr/bin/env bash\nprintf '%s' cm9vdHRvaw==\n"   # base64 of the fake root token


@unittest.skipIf(BASH is None, "bash not available")
class RevokeSlotHolderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp()
        self.bin = os.path.join(self.tmp, "bin")
        self.log = os.path.join(self.tmp, "log")
        os.makedirs(self.bin)
        os.makedirs(self.log)
        shims = {"bao": FAKE_BAO, "kubectl": FAKE_KUBECTL,
                 # the script calls python3; point it at this interpreter (Windows runners have no python3)
                 "python3": "#!/usr/bin/env bash\nexec '%s' \"$@\"\n" % sys.executable.replace("\\", "/")}
        for name, src in shims.items():
            p = os.path.join(self.bin, name)
            with open(p, "w", encoding="utf-8", newline="\n") as f:
                f.write(src)
            os.chmod(p, os.stat(p).st_mode | stat.S_IEXEC)
        open(os.path.join(self.log, "calls"), "w").close()

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def run_script(self, *args, **env):
        e = dict(os.environ)
        e["PATH"] = self.bin + os.pathsep + e["PATH"]
        e["FAKE_LOG"] = self.log
        e.update(env)
        args = args or ("dev-worker-3", "keepacc", "vm4207")
        return subprocess.run([BASH, SCRIPT, *args], capture_output=True, text=True, env=e, timeout=60)

    def calls(self):
        with open(os.path.join(self.log, "calls"), encoding="utf-8") as f:
            return f.read()

    def test_sweeps_the_previous_holder_and_spares_the_replacement(self):
        r = self.run_script()
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertIn("2 secret-id accessors destroyed, 1 tokens revoked", r.stdout)
        calls = self.calls()
        self.assertIn("secret_id_accessor=oldacc1", calls)
        self.assertIn("secret_id_accessor=oldacc2", calls)
        self.assertNotIn("secret_id_accessor=keepacc", calls)
        self.assertIn("revoke -accessor t-old", calls)
        for spared in ("t-new", "t-other", "t-nometa", "t-gone"):
            self.assertNotIn("revoke -accessor " + spared, calls)
        self.assertNotIn("roottok", r.stdout + r.stderr)

    def test_a_failed_enumeration_never_reads_as_nothing_to_revoke(self):
        for env, why in (({"SID_MODE": "denied"}, "permission denied"),
                         ({"SID_MODE": "garbage"}, "unparsable secret-id listing"),
                         ({"ACC_MODE": "down"}, "connection refused"),
                         ({"ACC_MODE": "sealed"}, "sealed")):
            with self.subTest(env=env):
                r = self.run_script(**env)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn("ABORT", r.stderr)
                self.assertIn(why, r.stderr)
                self.assertNotIn("revoke -accessor", self.calls())

    def test_an_empty_listing_without_the_replacement_aborts(self):
        r = self.run_script(SID_MODE="empty")
        self.assertEqual(r.returncode, 1)
        self.assertIn("not listed", r.stderr)
        self.assertNotIn("destroy", self.calls())

    def test_a_denied_lookup_or_a_failed_destroy_or_revoke_aborts(self):
        for env, why in (({"LOOKUP_MODE": "denied"}, "token lookup t-nometa"),
                         ({"DESTROY_MODE": "fail"}, "destroying secret-id accessor"),
                         ({"REVOKE_MODE": "sealed"}, "revoking token accessor t-old")):
            with self.subTest(env=env):
                open(os.path.join(self.log, "calls"), "w").close()
                r = self.run_script(**env)
                self.assertEqual(r.returncode, 1, r.stdout)
                self.assertIn(why, r.stderr)

    def test_bad_usage_never_reaches_the_vault(self):
        for args in (("dev-worker-3", "keepacc"), ("dev-worker-3", "", "vm4207"), ("dev|x", "a", "b")):
            with self.subTest(args=args):
                r = self.run_script(*args)
                self.assertEqual(r.returncode, 2)
        self.assertEqual(self.calls(), "")


if __name__ == "__main__":
    unittest.main()

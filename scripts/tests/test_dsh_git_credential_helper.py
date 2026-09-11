#!/usr/bin/env python3
"""Behaviour and wiring gates for the dsh git credential helper.

WHY THIS EXISTS. git-credential-openbao.sh is what lets the dsh agent's shell
authenticate to git.chifor.me: gitconfig.seed names it as the credential helper
for that one authority, and both are installed into /dsh-home by seed-settings on
every pod creation. Three failure shapes matter and none is visible to
`kustomize build` or `kubeconform`:

  * BEHAVIOUR. The helper must answer `get` with exactly the two protocol lines
    git expects, read the mount at USE time (so a rotation reaches the next git
    call), and on absence or a malformed value emit nothing on stdout, one
    value-free line on stderr, and exit 0 -- so git falls through to the failure
    it has today with a cause attached, and no fragment of a token ever lands in
    the agent's transcript.

  * PROTOCOL + SCOPING. git itself must parse what the helper emits, and the
    SHIPPED gitconfig must confine the helper to https://git.chifor.me. A test
    that installs the helper with `-c credential.helper=` proves only the first
    half, so this one fills through the seeded config, isolated from the
    worker's own git configuration.

  * WIRING. The ConfigMap key, the install line and the absolute path the
    gitconfig names must move together: a helper installed somewhere other than
    where the gitconfig points is silently no helper at all, and a ConfigMap
    without the key is a seed-settings `install: cannot stat` on a 1-replica
    Recreate Deployment.

Behaviour cases run the real script under /bin/sh against a fixture directory
shaped like kubelet's Secret projection. They SKIP if sh or git is unavailable;
the wiring half always runs.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import os
import pathlib
import shutil
import subprocess
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[2]
DSH = ROOT / "kubernetes" / "apps" / "apps" / "dsh"

HELPER = DSH / "git-credential-openbao.sh"
GITCONFIG = DSH / "gitconfig.seed"
AGENTS = DSH / "agents.seed.md"

#: Where seed-settings installs each file, and therefore what gitconfig.seed must
#: name. Pinned here so the two cannot drift apart unnoticed.
INSTALLED_HELPER = "/dsh-home/.local/bin/git-credential-openbao"
INSTALLED_GITCONFIG = "/dsh-home/.gitconfig"
#: The ESO mount (deployment.yaml `credentials` volume) and the two fields.
MOUNT = "/dsh-credentials"
USER_FIELD = "GITEA_USER"
PAT_FIELD = "GITEA_PAT"
#: The prefix of the helper's one diagnostic line. AGENTS.md ties the agent's
#: diagnosis to it, so it is pinned too.
HINT_PREFIX = "git-credential-openbao:"

PAT = "0123456789abcdef0123456789abcdef01234567"


def _sh():
    return shutil.which("sh")


def _git():
    return shutil.which("git")


def _project(root, fields, stamp="..2026_09_11_10_01_12.1"):
    """Lay out `root` the way kubelet projects a Secret volume.

    Real files live in a timestamped directory, `..data` is a symlink to it, and
    each key is a symlink through `..data`. A rotation replaces `..data`
    atomically; the per-key symlinks never change. Reproducing that is what makes
    the rotation case below meaningful.
    """
    root = pathlib.Path(root)
    data = root / stamp
    data.mkdir(parents=True)
    for name, value in fields.items():
        (data / name).write_bytes(value)
    link = root / "..data"
    if link.is_symlink():
        link.unlink()
    link.symlink_to(stamp)
    for name in fields:
        key = root / name
        if not key.is_symlink():
            key.symlink_to(pathlib.Path("..data") / name)
    return root


def _run(op, mount, stdin="protocol=https\nhost=git.chifor.me\n\n"):
    sh = _sh()
    if sh is None:  # pragma: no cover
        raise unittest.SkipTest("no /bin/sh")
    env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
    if mount is not None:
        env["GIT_CREDENTIAL_OPENBAO_DIR"] = str(mount)
    return subprocess.run(
        [sh, str(HELPER), op],
        input=stdin,
        capture_output=True,
        text=True,
        env=env,
        timeout=30,
    )


class HelperBehaviour(unittest.TestCase):
    """The real script, against a kubelet-shaped fixture."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.mount = _project(
            pathlib.Path(self.tmp.name) / "mount",
            {USER_FIELD: b"dsh", PAT_FIELD: PAT.encode()},
        )

    def assertRefused(self, proc, *, must_not_contain=()):
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, "", "a refusal must emit NOTHING on stdout")
        lines = proc.stderr.strip().splitlines()
        self.assertEqual(len(lines), 1, f"exactly one stderr line expected:\n{proc.stderr}")
        self.assertTrue(lines[0].startswith(HINT_PREFIX), lines[0])
        # The line has to tell the operator where to fix it.
        self.assertIn("af/dsh/credentials", lines[0])
        for secret in must_not_contain:
            self.assertNotIn(secret, proc.stderr)

    def test_get_emits_exactly_the_two_protocol_lines(self):
        proc = _run("get", self.mount)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(proc.stdout, f"username=dsh\npassword={PAT}\n")
        self.assertEqual(proc.stderr, "")

    def test_trailing_newline_is_tolerated(self):
        # `bao kv patch KEY=@file` stores the file's bytes verbatim, and a file
        # written with echo ends in a newline. That is accepted; nothing else is.
        mount = _project(
            pathlib.Path(self.tmp.name) / "nl",
            {USER_FIELD: b"dsh\n", PAT_FIELD: PAT.encode() + b"\n"},
        )
        proc = _run("get", mount)
        self.assertEqual(proc.stdout, f"username=dsh\npassword={PAT}\n")

    def test_embedded_newline_is_refused_without_leaking(self):
        wrapped = PAT[:20] + "\n" + PAT[20:]
        mount = _project(
            pathlib.Path(self.tmp.name) / "wrap",
            {USER_FIELD: b"dsh", PAT_FIELD: wrapped.encode()},
        )
        self.assertRefused(_run("get", mount), must_not_contain=(PAT[:20], PAT[20:]))

    def test_carriage_return_is_refused(self):
        mount = _project(
            pathlib.Path(self.tmp.name) / "cr",
            {USER_FIELD: b"dsh", PAT_FIELD: PAT.encode() + b"\r\n"},
        )
        self.assertRefused(_run("get", mount), must_not_contain=(PAT,))

    def test_embedded_nul_is_refused_before_the_shell_can_drop_it(self):
        # dash discards NUL bytes inside `$(...)`, so a check on the expanded
        # string would see `abcdef` for `abc<NUL>def` and accept it. The helper
        # judges the file's bytes first; this pins that.
        nul = PAT[:20].encode() + b"\x00" + PAT[20:].encode()
        mount = _project(pathlib.Path(self.tmp.name) / "nul", {USER_FIELD: b"dsh", PAT_FIELD: nul})
        proc = _run("get", mount)
        self.assertRefused(proc, must_not_contain=(PAT[:20], PAT[20:]))
        self.assertNotIn(f"password={PAT}", proc.stdout)

    def test_more_than_one_trailing_newline_is_refused(self):
        mount = _project(
            pathlib.Path(self.tmp.name) / "nlnl",
            {USER_FIELD: b"dsh", PAT_FIELD: PAT.encode() + b"\n\n"},
        )
        self.assertRefused(_run("get", mount), must_not_contain=(PAT,))

    def test_lone_newline_is_empty(self):
        mount = _project(pathlib.Path(self.tmp.name) / "lone", {USER_FIELD: b"dsh", PAT_FIELD: b"\n"})
        self.assertRefused(_run("get", mount))

    def test_non_ascii_is_refused(self):
        mount = _project(
            pathlib.Path(self.tmp.name) / "utf8",
            {USER_FIELD: "dsh\u00e9".encode("utf-8"), PAT_FIELD: PAT.encode()},
        )
        self.assertRefused(_run("get", mount), must_not_contain=(PAT,))

    def test_whitespace_in_a_value_is_refused(self):
        mount = _project(
            pathlib.Path(self.tmp.name) / "ws",
            {USER_FIELD: b"d sh", PAT_FIELD: PAT.encode()},
        )
        self.assertRefused(_run("get", mount), must_not_contain=(PAT,))

    def test_empty_value_is_refused(self):
        for name in (USER_FIELD, PAT_FIELD):
            with self.subTest(field=name):
                fields = {USER_FIELD: b"dsh", PAT_FIELD: PAT.encode()}
                fields[name] = b""
                mount = _project(pathlib.Path(self.tmp.name) / f"empty-{name}", fields)
                self.assertRefused(_run("get", mount), must_not_contain=(PAT,))

    def test_missing_field_is_refused(self):
        for name in (USER_FIELD, PAT_FIELD):
            with self.subTest(field=name):
                fields = {USER_FIELD: b"dsh", PAT_FIELD: PAT.encode()}
                del fields[name]
                mount = _project(pathlib.Path(self.tmp.name) / f"missing-{name}", fields)
                self.assertRefused(_run("get", mount), must_not_contain=(PAT,))

    def test_missing_directory_is_refused(self):
        # An absent optional Secret mounts as an EMPTY directory; a removed volume
        # leaves no directory. Both are "not provisioned", not failures.
        self.assertRefused(_run("get", pathlib.Path(self.tmp.name) / "nowhere"))
        empty = pathlib.Path(self.tmp.name) / "empty"
        empty.mkdir()
        self.assertRefused(_run("get", empty))

    def test_unreadable_file_is_refused(self):
        if os.geteuid() == 0:
            self.skipTest("root reads everything; the EACCES case needs a non-root uid")
        real = self.mount / "..data" / PAT_FIELD
        real.chmod(0o000)
        self.addCleanup(real.chmod, 0o600)
        self.assertRefused(_run("get", self.mount), must_not_contain=(PAT,))

    def test_other_authorities_are_refused_silently(self):
        # Defense in depth under the gitconfig scoping: even if the helper were
        # attached to a wider section, it answers for ONE authority. A wrong
        # authority is not a provisioning problem, so there is no hint either.
        for desc in (
            "protocol=https\nhost=example.com\n\n",
            "protocol=http\nhost=git.chifor.me\n\n",
            "protocol=https\nhost=git.chifor.me:8443\n\n",
            "protocol=https\nhost=evil.git.chifor.me\n\n",
            "host=git.chifor.me\n\n",  # no protocol at all
            "",  # no description at all
        ):
            with self.subTest(desc=desc):
                proc = _run("get", self.mount, stdin=desc)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(proc.stderr, "")

    def test_explicit_default_port_is_the_same_authority(self):
        # git copies an explicitly written port into host=; `:443` on https is
        # this authority spelled differently, not a different one.
        proc = _run("get", self.mount, stdin="protocol=https\nhost=git.chifor.me:443\n\n")
        self.assertEqual(proc.stdout, f"username=dsh\npassword={PAT}\n")

    def test_extra_request_keys_do_not_matter(self):
        # git also sends path=, username= (when the URL carries one), wwwauth[]=
        # and more. None of it changes the answer for the right authority.
        desc = "capability[]=authtype\nprotocol=https\nhost=git.chifor.me\npath=cchifor/platform.git\nusername=whoever\n\n"
        proc = _run("get", self.mount, stdin=desc)
        self.assertEqual(proc.stdout, f"username=dsh\npassword={PAT}\n")

    def test_dangling_key_symlink_is_refused(self):
        # kubelet removes the previous ..data directory after a swap; a key whose
        # resolved target is gone must be a value-free refusal, not a crash.
        mount = _project(pathlib.Path(self.tmp.name) / "dangle", {USER_FIELD: b"dsh", PAT_FIELD: PAT.encode()})
        (mount / "..2026_09_11_10_01_12.1" / PAT_FIELD).unlink()
        self.assertRefused(_run("get", mount), must_not_contain=(PAT,))

    def test_check_and_read_use_the_same_resolved_file(self):
        # The helper resolves each key symlink once and validates+reads that
        # target, so a rotation between the two steps cannot emit unvalidated
        # bytes. Pinned structurally: both the byte check and the read name the
        # resolved variables, never the symlink path.
        helper = HELPER.read_text(encoding="utf-8")
        self.assertIn('USER_REAL=$(readlink -f "$USER_FILE"', helper)
        self.assertIn('PAT_REAL=$(readlink -f "$PAT_FILE"', helper)
        self.assertIn('single_word_file "$PAT_REAL"', helper)
        self.assertIn('pat=$(cat "$PAT_REAL"', helper)
        self.assertNotIn('cat "$PAT_FILE"', helper)
        self.assertNotIn('single_word_file "$PAT_FILE"', helper)

    def test_store_erase_and_unknown_operations_are_ignored(self):
        # gitcredentials(7): a helper must ignore operations it does not handle,
        # so future extensions of the protocol do not turn into failures.
        for op in ("store", "erase", "frobnicate", ""):
            with self.subTest(op=op):
                proc = _run(op, self.mount)
                self.assertEqual(proc.returncode, 0, proc.stderr)
                self.assertEqual(proc.stdout, "")
                self.assertEqual(proc.stderr, "")

    def test_rotation_is_seen_by_the_next_get(self):
        # kubelet rotates by re-pointing `..data`; the per-key symlinks are the
        # same inodes before and after. The helper reopens by pathname every
        # time, so the SECOND call must see the new value with no restart.
        first = _run("get", self.mount)
        self.assertIn(f"password={PAT}\n", first.stdout)
        new_pat = "f" * 40
        stamp = "..2026_09_11_11_00_00.2"
        data = self.mount / stamp
        data.mkdir()
        (data / USER_FIELD).write_bytes(b"dsh")
        (data / PAT_FIELD).write_bytes(new_pat.encode())
        tmp_link = self.mount / "..data_tmp"
        tmp_link.symlink_to(stamp)
        os.replace(tmp_link, self.mount / "..data")  # atomic, like kubelet
        second = _run("get", self.mount)
        self.assertEqual(second.stdout, f"username=dsh\npassword={new_pat}\n")


class GitProtocolThroughSeededConfig(unittest.TestCase):
    """git itself, filling through gitconfig.seed, isolated from this host."""

    def setUp(self):
        git = _git()
        if git is None:
            self.skipTest("git is not on PATH")
        self.git = git
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        home = pathlib.Path(self.tmp.name)
        self.mount = _project(home / "mount", {USER_FIELD: b"dsh", PAT_FIELD: PAT.encode()})
        # The helper copy stands in for INSTALLED_HELPER; the seeded gitconfig is
        # used byte-for-byte apart from that one path, so the scoping under test
        # is the shipped one.
        helper = home / "git-credential-openbao"
        shutil.copy(HELPER, helper)
        helper.chmod(0o755)
        seeded = GITCONFIG.read_text(encoding="utf-8")
        self.assertIn(INSTALLED_HELPER, seeded)
        config = home / "gitconfig"
        config.write_text(seeded.replace(INSTALLED_HELPER, str(helper)), encoding="utf-8")
        self.env = {
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            "HOME": str(home),
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(config),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_CREDENTIAL_OPENBAO_DIR": str(self.mount),
        }

    def fill(self, description):
        return subprocess.run(
            [self.git, "credential", "fill"],
            input=description,
            capture_output=True,
            text=True,
            env=self.env,
            cwd=self.tmp.name,
            timeout=30,
        )

    def test_https_git_chifor_me_is_answered(self):
        proc = self.fill("protocol=https\nhost=git.chifor.me\n\n")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("username=dsh\n", proc.stdout)
        self.assertIn(f"password={PAT}\n", proc.stdout)

    def test_another_host_gets_nothing(self):
        proc = self.fill("protocol=https\nhost=example.com\n\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("password=", proc.stdout)
        self.assertNotIn(PAT, proc.stdout + proc.stderr)

    def test_plain_http_to_the_forge_gets_nothing(self):
        # The seeded section is `credential "https://git.chifor.me"`: git's URL
        # match includes the protocol, so a downgrade is not answered.
        proc = self.fill("protocol=http\nhost=git.chifor.me\n\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("password=", proc.stdout)
        self.assertNotIn(PAT, proc.stdout + proc.stderr)

    def test_unprovisioned_mount_falls_through_with_the_hint(self):
        self.env["GIT_CREDENTIAL_OPENBAO_DIR"] = str(pathlib.Path(self.tmp.name) / "nowhere")
        proc = self.fill("protocol=https\nhost=git.chifor.me\n\n")
        self.assertNotEqual(proc.returncode, 0)
        self.assertNotIn("password=", proc.stdout)
        self.assertIn(HINT_PREFIX, proc.stderr)


class Wiring(unittest.TestCase):
    """The ConfigMap key, the install line and the gitconfig path move together."""

    def setUp(self):
        self.kustomization = (DSH / "kustomization.yaml").read_text(encoding="utf-8")
        self.deployment = (DSH / "deployment.yaml").read_text(encoding="utf-8")

    def test_files_exist(self):
        self.assertTrue(HELPER.is_file(), f"{HELPER} is missing")
        self.assertTrue(GITCONFIG.is_file(), f"{GITCONFIG} is missing")

    def test_helper_parses_under_sh(self):
        sh = _sh()
        if sh is None:  # pragma: no cover
            self.skipTest("no /bin/sh")
        self.assertTrue(
            HELPER.read_text(encoding="utf-8").startswith("#!/bin/sh\n"),
            "the container runs it as `sh`; no bashisms",
        )
        proc = subprocess.run([sh, "-n", str(HELPER)], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_configmap_carries_both_files(self):
        for key in (HELPER.name, GITCONFIG.name):
            with self.subTest(key=key):
                self.assertIn(f"- {key}", self.kustomization)

    def test_seed_settings_installs_both_where_the_gitconfig_points(self):
        self.assertIn(
            f"install -m 0755 /seed/{HELPER.name} {INSTALLED_HELPER}",
            self.deployment,
            "the helper must be installed executable, at the path gitconfig.seed names",
        )
        self.assertIn(
            f"install -m 0644 /seed/{GITCONFIG.name} {INSTALLED_GITCONFIG}",
            self.deployment,
            "HOME is /dsh-home in the dsh container, so git reads /dsh-home/.gitconfig",
        )
        # The helper's directory is not guaranteed to exist on the PVC.
        self.assertIn(f"mkdir -p {os.path.dirname(INSTALLED_HELPER)}", self.deployment)

    def test_gitconfig_is_scoped_to_the_forge_and_names_the_installed_helper(self):
        seeded = GITCONFIG.read_text(encoding="utf-8")
        self.assertIn('[credential "https://git.chifor.me"]', seeded)
        self.assertIn(f"helper = {INSTALLED_HELPER}", seeded)
        # One scoped section, no unscoped `[credential]` that would widen it.
        self.assertNotIn("\n[credential]\n", seeded)

    def test_helper_authority_matches_the_gitconfig_scope(self):
        # Two scoping layers, one authority: the gitconfig section and the
        # helper's own guard must name the same host and protocol.
        helper = HELPER.read_text(encoding="utf-8")
        seeded = GITCONFIG.read_text(encoding="utf-8")
        self.assertIn("AUTHORITY_PROTOCOL=https\n", helper)
        self.assertIn("AUTHORITY_HOST=git.chifor.me\n", helper)
        self.assertIn('[credential "https://git.chifor.me"]', seeded)

    def test_helper_reads_the_mount_the_deployment_projects(self):
        # deployment.yaml mounts the ESO Secret at MOUNT; the helper's default
        # must be the same path or provisioning OpenBao silently changes nothing.
        self.assertIn(f"mountPath: {MOUNT}", self.deployment)
        self.assertIn(f':-{MOUNT}}}"', HELPER.read_text(encoding="utf-8"))

    def test_agents_md_ties_the_diagnosis_to_the_helper_hint(self):
        # "could not read Username" alone does not distinguish "not provisioned"
        # from a missing gitconfig or an unexecutable helper; the agent is told
        # to look for the helper's own line.
        text = AGENTS.read_text(encoding="utf-8")
        self.assertIn(HINT_PREFIX, text)
        self.assertIn("git.chifor.me", text)


if __name__ == "__main__":
    unittest.main()

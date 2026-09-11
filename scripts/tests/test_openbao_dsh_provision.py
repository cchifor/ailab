#!/usr/bin/env python3
"""Gates for the OpenBao dsh provisioning Job.

WHY THIS EXISTS. The Job replaces a live OpenBao policy with a breakglass
token. Two failure shapes matter, and neither is visible to `kustomize build`
or `kubeconform`:

  * IT CAN WEDGE ITSELF. The script refuses to run when the live policy grants
    a path its allow-list does not know, so that a grant added by hand is never
    silently dropped. If someone adds a path to the policy heredoc without
    adding it to that allow-list, the Job succeeds once, writes the new grant,
    and then refuses every subsequent run -- because the grant it wrote is now
    "unknown" drift. The allow-list must therefore be a SUPERSET of what the
    policy writes.

  * IT CAN CAUSE AN OUTAGE SEVERAL DAYS LATER. `bao policy write` replaces the
    document. Dropping the estate/litellm grant stops dsh-litellm syncing, and
    because a Secret change never reaches an already-running container's
    environment, dsh keeps working on a stale credential until it is revoked.
    The grant must be present in the heredoc, asserted here rather than assumed.

Run:

    python3 -m unittest discover -s scripts/tests -p "test_*.py" -v
"""
import os
import pathlib
import re
import shutil
import subprocess
import tempfile
import unittest

import yaml

ROOT = pathlib.Path(__file__).resolve().parents[2]
MANIFEST = (
    ROOT
    / "kubernetes"
    / "apps"
    / "infrastructure"
    / "security"
    / "openbao"
    / "dsh-provision-job.yaml"
)

#: Grants that must survive any rewrite. dsh-litellm is the live model
#: credential; losing it is the delayed outage described above.
REQUIRED_GRANTS = {"af/data/estate/litellm", "af/metadata/estate/litellm"}

#: The grant the feature needs. ESO's `dataFrom.extract` reads the KV-v2 DATA
#: endpoint only, so no metadata grant is expected here.
FEATURE_GRANT = "af/data/dsh/credentials"

#: The fake `bao` the behaviour tests run the script against.
SHIM_DIR = pathlib.Path(__file__).resolve().parent / "fixtures" / "openbao-dsh-provision"

#: The policy exactly as the 2026-09-07 breakglass ceremony left it, read from
#: the live vault. Whitespace here is deliberately irregular in some tests: the
#: guard must not care.
LIVE_BASELINE = (
    'path "af/data/estate/litellm" { capabilities = ["read"] }\n'
    'path "af/metadata/estate/litellm" { capabilities = ["read", "list"] }\n'
)


def _script():
    for doc in yaml.safe_load_all(MANIFEST.read_text()):
        if doc and doc.get("kind") == "ConfigMap":
            return doc["data"]["provision.sh"]
    raise AssertionError("no ConfigMap carrying provision.sh")


def _code(script):
    """The script with full-line comments removed.

    Assertions about what the script DOES must not be satisfied or broken by
    what it SAYS: this file's own comments name `kv patch` precisely to explain
    why it is absent, which a raw substring check reads as its presence.
    """
    return "\n".join(
        line for line in script.splitlines() if not line.lstrip().startswith("#")
    )


def _policy_body(script):
    """Raw lines of the `bao policy write` heredoc."""
    return script.split("<<'EOF'", 1)[1].split("\nEOF", 1)[0].strip().splitlines()


def _policy_paths(script):
    """Paths inside the `bao policy write` heredoc."""
    return set(re.findall(r'path "([^"]+)"', "\n".join(_policy_body(script))))


def _normalised(text):
    return "".join(text.split())


def _guard_constant(script, name):
    m = re.search(rf"^{name}='([^']*)'", script, re.M)
    assert m, f"{name} not found in the script"
    return m.group(1)


class ProvisionScript(unittest.TestCase):
    def setUp(self):
        self.script = _script()

    def test_shell_parses(self):
        sh = shutil.which("sh")
        if sh is None:
            self.skipTest("no /bin/sh")
        with tempfile.NamedTemporaryFile("w", suffix=".sh") as f:
            f.write(self.script)
            f.flush()
            proc = subprocess.run([sh, "-n", f.name], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_required_grants_survive(self):
        self.assertTrue(REQUIRED_GRANTS <= _policy_paths(self.script))

    def test_feature_grant_present(self):
        self.assertIn(FEATURE_GRANT, _policy_paths(self.script))

    def test_desired_constant_matches_what_the_policy_writes(self):
        # THE self-wedge invariant. The guard accepts only two documents: the
        # reviewed baseline and DESIRED. If DESIRED drifts from the heredoc, the
        # Job writes the heredoc once and then refuses every later run, because
        # it reads back its own output as unreviewed drift.
        self.assertEqual(
            _normalised("\n".join(_policy_body(self.script))),
            _guard_constant(self.script, "DESIRED"),
        )

    def test_baseline_constant_is_the_live_policy(self):
        # The guard's pre-ownership arm has to match what is actually in the
        # vault, or the very first run refuses.
        self.assertEqual(
            _guard_constant(self.script, "BASELINE"), _normalised(LIVE_BASELINE)
        )

    def test_guard_compares_whole_documents_not_path_names(self):
        # A capability ADDED to an already-allowed path is invisible to a
        # path-name check and would be silently removed by the rewrite. The
        # comparison must be over the whole normalised document.
        self.assertIn("tr -d '[:space:]'", self.script)
        self.assertNotIn("grep -oE 'path", self.script)

    def test_kv_contents_are_not_seeded_from_git(self):
        # The point of the feature is that a credential added in OpenBao reaches
        # dsh with no per-machine step. A `kv patch` here would make the Job
        # reassert git contents daily and defeat that.
        code = _code(self.script)
        self.assertNotIn("kv patch", code)
        self.assertIn("-cas=0", code)

    def test_auth_role_is_asserted_not_written(self):
        # A role bound to the right ServiceAccount but carrying the wrong
        # policies authenticates fine and then reads nothing.
        code = _code(self.script)
        self.assertNotIn("bao write auth/kubernetes/role", code)
        self.assertIn("token_policies", code)


class JobShape(unittest.TestCase):
    def setUp(self):
        self.job = [
            d for d in yaml.safe_load_all(MANIFEST.read_text()) if d and d.get("kind") == "Job"
        ][0]

    def test_breakglass_secret_is_not_optional(self):
        env = self.job["spec"]["template"]["spec"]["containers"][0]["env"]
        tok = [e for e in env if e["name"] == "BAO_TOKEN"][0]
        self.assertIs(tok["valueFrom"]["secretKeyRef"]["optional"], False)

    def test_force_annotation_present(self):
        # A Job's spec.template is immutable; without this a script edit fails
        # "field is immutable" and takes the whole openbao Kustomization False.
        self.assertEqual(
            self.job["metadata"]["annotations"].get("kustomize.toolkit.fluxcd.io/force"),
            "enabled",
        )


class GuardBehaviour(unittest.TestCase):
    """Run provision.sh against a fake `bao` and assert what it actually does.

    The structural tests above prove the script SAYS the right things. These
    prove it DOES them -- in particular the one property that matters most:
    a guard failure must never reach `bao policy write`, because that write
    replaces the live document.
    """

    def _run(self, policy=LIVE_BASELINE, **fake):
        sh = shutil.which("sh")
        if sh is None:
            self.skipTest("no /bin/sh")
        with tempfile.TemporaryDirectory() as tmp:
            work = pathlib.Path(tmp)
            state = work / "state"
            state.mkdir()
            if policy is not None:
                (state / "policy").write_text(policy)
            script = work / "provision.sh"
            script.write_text(_script())
            log = work / "log"
            log.touch()
            env = {
                "PATH": f"{SHIM_DIR}:{os.environ['PATH']}",
                "FAKE_LOG": str(log),
                "FAKE_STATE": str(state),
                "HOME": str(work),
            }
            env.update({k: str(v) for k, v in fake.items()})
            proc = subprocess.run(
                [sh, str(script)], capture_output=True, text=True, env=env, timeout=60
            )
            return proc, log.read_text()

    def assertNoPolicyWrite(self, log):
        self.assertNotIn(
            "policy write", log, "the guard failed but the policy was replaced anyway"
        )

    # --- the two states the guard must accept -------------------------------
    def test_pre_ownership_baseline_proceeds(self):
        proc, log = self._run(FAKE_KV_EXISTS=0)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("policy write", log)
        self.assertIn("kv put", log)

    def test_second_run_over_its_own_output_proceeds(self):
        desired = "\n".join(_policy_body(_script())) + "\n"
        proc, log = self._run(policy=desired, FAKE_KV_EXISTS=1)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("policy write", log)
        # The path already exists, so its operator-owned contents are untouched.
        self.assertNotIn("kv put", log)

    # --- the drift cases, which are the point -------------------------------
    def test_added_capability_on_an_allowed_path_aborts(self):
        # Invisible to a path-name check; this is the bug the first guard had.
        drifted = LIVE_BASELINE.replace(
            'path "af/data/estate/litellm" { capabilities = ["read"] }',
            'path "af/data/estate/litellm" { capabilities = ["read", "list"] }',
        )
        proc, log = self._run(policy=drifted)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("REFUSING", proc.stderr)
        self.assertNoPolicyWrite(log)

    def test_new_path_written_with_tabs_aborts(self):
        # Evades any fixed `path "..."` pattern; normalised comparison does not care.
        drifted = LIVE_BASELINE + 'path\t"af/data/estate/codex"\t{ capabilities = ["read"] }\n'
        proc, log = self._run(policy=drifted)
        self.assertEqual(proc.returncode, 1)
        self.assertNoPolicyWrite(log)

    def test_removed_grant_aborts(self):
        proc, log = self._run(
            policy='path "af/data/estate/litellm" { capabilities = ["read"] }\n'
        )
        self.assertEqual(proc.returncode, 1)
        self.assertNoPolicyWrite(log)

    def test_irregular_whitespace_on_the_baseline_still_proceeds(self):
        # The guard must reject DRIFT, not formatting.
        reformatted = (
            'path   "af/data/estate/litellm"{capabilities=["read"]}\n\n'
            '\tpath "af/metadata/estate/litellm" {\n'
            '\t  capabilities = [ "read" , "list" ]\n'
            "\t}\n"
        )
        proc, log = self._run(policy=reformatted, FAKE_KV_EXISTS=1)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("policy write", log)

    # --- read failures must not look like absence ---------------------------
    def test_unreadable_existing_policy_aborts_without_writing(self):
        proc, log = self._run(policy=None, FAKE_READ_FAIL=1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("could not be read", proc.stderr)
        self.assertNoPolicyWrite(log)

    def test_failed_policy_list_aborts_without_writing(self):
        proc, log = self._run(FAKE_LIST_FAIL=1)
        self.assertEqual(proc.returncode, 1)
        self.assertNoPolicyWrite(log)

    def test_absent_policy_is_created(self):
        # A genuinely fresh vault: absence established from the LIST, not from a
        # failed read.
        proc, log = self._run(
            policy=None, FAKE_POLICY_LIST='["default"]', FAKE_KV_EXISTS=0
        )
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)
        self.assertIn("policy write", log)

    # --- the remaining fail-closed paths ------------------------------------
    def test_rejected_token_aborts(self):
        proc, log = self._run(FAKE_TOKEN_FAIL=1)
        self.assertEqual(proc.returncode, 1)
        self.assertNoPolicyWrite(log)

    def test_role_missing_the_policy_aborts_before_touching_kv(self):
        proc, log = self._run(FAKE_ROLE_POLICIES="something-else")
        self.assertEqual(proc.returncode, 1)
        self.assertNotIn("kv put", log)

    def test_soft_deleted_kv_path_fails_loudly(self):
        # `bao kv get` returns exit 0 for a soft-deleted current version, with
        # "data": null -- verified against the live vault. Reading that as
        # healthy would report success while ESO can read nothing.
        proc, log = self._run(FAKE_KV_DELETED=1)
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertIn("operator action needed", proc.stderr)
        # Attempting `-cas=0` here is correct and harmless: version history
        # exists, so the real CLI refuses with exit 2 and nothing is written.
        # What must not happen is reporting success.
        self.assertIn("kv put", log)
        self.assertNotIn("created", proc.stdout)

    def test_role_carrying_only_a_similarly_named_policy_aborts(self):
        # `af-app-dsh-ro` authenticates fine and then reads nothing this Job
        # grants. A substring match -- and `grep -w`, since `-` is not a word
        # constituent -- both accept it.
        proc, log = self._run(FAKE_ROLE_POLICIES="af-app-dsh-ro")
        self.assertEqual(proc.returncode, 1, proc.stdout + proc.stderr)
        self.assertNotIn("kv put", log)

    def test_role_carrying_the_policy_among_others_proceeds(self):
        proc, log = self._run(FAKE_ROLE_POLICIES="default af-app-dsh", FAKE_KV_EXISTS=1)
        self.assertEqual(proc.returncode, 0, proc.stdout + proc.stderr)

    def test_undeletable_kv_path_fails_loudly(self):
        # Version history but no readable version: -cas=0 can never succeed
        # again, so this needs a human rather than a silent skip.
        proc, log = self._run(FAKE_KV_EXISTS=0, FAKE_KV_PUT_FAIL=1)
        self.assertEqual(proc.returncode, 1)
        self.assertIn("operator action needed", proc.stderr)
